"""
Phase 5B-7 PostgreSQL Concurrency Tests
========================================
apps/orders/tests_phase5b7_pg.py

These tests verify the concurrent-access behaviour of the Phase 5B–6B
ingredient reservation and consumption services using *real* concurrent
database connections.

They prove that Rev 3 §M.5 items 5–6 are correctly implemented:

  5. Cancel racing mark-ready on a mixed order: one outcome only,
     consistent counters.
  6. Duplicate ready requests (double click) consume once.

Why PostgreSQL only
-------------------
SQLite serialises all writes with a single database-level lock and
silently accepts but ignores SELECT FOR UPDATE.  A "concurrency" test on
SQLite only verifies sequential execution and proves nothing about lock
ordering or blocking under real concurrent load.  All tests in this file
skip themselves when the database is not PostgreSQL.

Concurrency harness (Rev 3 §M.7)
----------------------------------
Each concurrent test:
  - Uses TransactionTestCase so each test has real, independently committed
    (or rolled-back) transactions visible to other connections.
  - Spawns one worker thread per simulated concurrent request.
  - For Scenario A (cancel-vs-ready): uses a holder/waiter pattern with
    _wait_for_blocker() / pg_blocking_pids() to confirm the waiter is
    genuinely blocked on the PostgreSQL row-level Order lock before the
    holder commits.  This is the only reliable proof of genuine blocking;
    a Barrier or Event alone only synchronises Python execution.
  - For Scenario B (duplicate ready): uses threading.Barrier to synchronise
    both workers at the moment they attempt to acquire the Order lock.
  - Verifies the final committed database state, not merely in-memory state.
  - Closes all thread-local Django connections in finally blocks.

Relationship to existing PostgreSQL concurrency tests
------------------------------------------------------
Phase 2A tests (tests_phase2a_pg.py) cover lock_inventory_items() ordering
and concurrent adjustment/wastage operations.
Phase 4B-6 tests (tests_phase4b6_pg.py) cover configuration-writing services:
  approve_recipe(), retire_recipe(), approve_packaging_profile(), etc.
This module focuses on Phase 5B-3/6B order status transitions:
  - preparing → cancelled racing preparing → ready
  - Two concurrent preparing → ready requests
"""

import functools
import threading
import time
from decimal import Decimal

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone


# ── Skip decorator ────────────────────────────────────────────────────────────

def _require_postgresql(test_func):
    """Decorator: skip unless the default DB is PostgreSQL."""
    @functools.wraps(test_func)
    def wrapper(self, *args, **kwargs):
        if connection.vendor != 'postgresql':
            self.skipTest(
                'PostgreSQL required for real SELECT FOR UPDATE concurrency '
                f'tests. Current backend: {connection.vendor}.'
            )
        return test_func(self, *args, **kwargs)
    return wrapper


# ── Timeout constants ─────────────────────────────────────────────────────────

_BARRIER_TIMEOUT        = 10   # seconds — wait for all threads to reach barrier
_HOLD_TIMEOUT           = 12   # seconds — wait for holder to signal lock held
_BLOCKER_POLL_TIMEOUT   = 30   # seconds — _wait_for_blocker() polling window
_HOLDER_RELEASE_TIMEOUT = 120  # seconds — holder's release_event.wait() safety net
_JOIN_TIMEOUT           = 35   # seconds — join threads after release_event.set()
_PG_WAIT_POLL           = 0.05 # seconds — polling interval


# ── Lock-wait confirmation via pg_blocking_pids() ─────────────────────────────

def _wait_for_blocker(holder_pid, waiter_pid,
                      timeout=_BLOCKER_POLL_TIMEOUT, poll=_PG_WAIT_POLL):
    """
    Poll until holder_pid appears in pg_blocking_pids(waiter_pid).

    Returns True when the holder is demonstrably blocking the waiter at the
    PostgreSQL row-lock level, False if the timeout expires.

    This is immune to Python-thread scheduling latency: even if the waiter
    thread is not scheduled immediately after the event, the query returns
    False until the waiter genuinely blocks at the DB level.

    The holder must remain inside its transaction for the full duration of
    this call.  _HOLDER_RELEASE_TIMEOUT (120 s) gives a ~90 s buffer beyond
    _BLOCKER_POLL_TIMEOUT (30 s).
    """
    sql = "SELECT %s = ANY(pg_blocking_pids(%s))"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cur:
            cur.execute(sql, [holder_pid, waiter_pid])
            row = cur.fetchone()
        if row and row[0]:
            return True
        time.sleep(poll)
    return False


def _blocker_diagnostic(holder_pid, waiter_pid, results=None, threads=None):
    """Return diagnostic text for assertion failure messages."""
    lines = [f'holder_pid={holder_pid}, waiter_pid={waiter_pid}']
    sql = """
        SELECT pid, state, wait_event_type, wait_event,
               pg_blocking_pids(pid) AS blocking
        FROM   pg_stat_activity
        WHERE  pid IN (%s, %s)
        ORDER  BY pid
    """
    try:
        with connection.cursor() as cur:
            cur.execute(sql, [holder_pid, waiter_pid])
            for r in cur.fetchall():
                lines.append(
                    f'  pg_stat pid={r[0]}: state={r[1]}, '
                    f'wait_type={r[2]}, wait_event={r[3]}, blocking={r[4]}'
                )
    except Exception as exc:
        lines.append(f'  pg_stat query failed: {exc}')
    if results is not None:
        for i, r in enumerate(results):
            lines.append(f'  results[{i}] = {r!r}')
    if threads is not None:
        for t in threads:
            lines.append(f'  thread {t.name!r}: alive={t.is_alive()}')
    return '\n'.join(lines)


# ── Thread runner helpers ──────────────────────────────────────────────────────

def _run_threads(threads, join_timeout=_JOIN_TIMEOUT):
    """Start all threads and return names of any that did not exit in time."""
    for t in threads:
        t.start()
    timed_out = []
    for t in threads:
        t.join(timeout=join_timeout)
        if t.is_alive():
            timed_out.append(t.name)
    return timed_out


def _assert_no_unexpected(test_case, results, context=''):
    """Assert that no worker produced an 'unexpected' or 'timeout' result."""
    for i, r in enumerate(results):
        test_case.assertIsNotNone(r, f'Thread {i} produced no result. {context}')
        test_case.assertNotEqual(
            r[0], 'unexpected',
            f'Thread {i} unexpected error: {r}. {context}',
        )
        test_case.assertNotEqual(
            r[0], 'timeout',
            f'Thread {i} timed out: {r}. {context}',
        )


# ── Fixture helpers ───────────────────────────────────────────────────────────

_counter = 0


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _make_ingredient_preparing_order(quantity=2, qty_per_unit=Decimal('10.000'),
                                     on_hand=Decimal('1000.000')):
    """
    Create a complete ingredient-only order in ``preparing`` status with an
    active ``OrderIngredientReservation``.

    Returns ``(order, reservation, ingr_item)``.

    The reservation is ``status='reserved'`` with
    ``reserved_quantity = quantity * qty_per_unit`` outstanding.
    The ``InventoryItem`` has ``reserved_quantity`` already incremented.

    All writes are committed (not inside an atomic block) so that the
    reservation is visible to other database connections used by worker
    threads.
    """
    from apps.accounts.models import CustomUser
    from apps.inventory.models import InventoryItem
    from apps.menu.models import Category, Product
    from apps.orders.models import (
        Order,
        OrderIngredientReservation,
        OrderIngredientReservationItem,
        OrderItem,
    )

    # ── Inventory item ────────────────────────────────────────────────────
    ingr = InventoryItem.objects.create(
        name=f'PG7Ingr_{_uid()}',
        item_type='ingredient',
        base_unit='g',
        on_hand_quantity=on_hand,
        reserved_quantity=Decimal('0.000'),
    )

    # ── Product ───────────────────────────────────────────────────────────
    cat = Category.objects.create(
        name=f'PG7Cat_{_uid()}',
        slug=f'pg7cat_{_uid()}',
    )
    prod = Product.objects.create(
        category=cat,
        name=f'PG7Prod_{_uid()}',
        price=Decimal('60.00'),
        stock_quantity=0,
        inventory_tracking_mode=Product.INVENTORY_TRACKING_INGREDIENT,
    )

    # ── Order ─────────────────────────────────────────────────────────────
    order = Order.objects.create(
        customer_name=f'PG7Customer_{_uid()}',
        order_type='dine_in',
        is_paid=True,
        status='preparing',
        stock_deducted=False,
    )

    # ── Order item ────────────────────────────────────────────────────────
    order_item = OrderItem.objects.create(
        order=order,
        product=prod,
        product_name=prod.name,
        category_name=cat.name,
        packaging_eligible=False,
        size='none',
        quantity=quantity,
        unit_price=Decimal('60.00'),
        subtotal=Decimal('60.00') * quantity,
        inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
    )

    # ── Reservation header ────────────────────────────────────────────────
    reservation = OrderIngredientReservation.objects.create(
        order=order,
        status=OrderIngredientReservation.STATUS_RESERVED,
        order_type_snapshot='dine_in',
    )

    # ── Reservation line ──────────────────────────────────────────────────
    reserved_qty = qty_per_unit * quantity
    OrderIngredientReservationItem.objects.create(
        reservation=reservation,
        order_item=order_item,
        inventory_item=ingr,
        item_name=ingr.name,
        base_unit=ingr.base_unit,
        component_source=OrderIngredientReservationItem.COMPONENT_SOURCE_RECIPE,
        source_revision=1,
        quantity_per_unit=qty_per_unit,
        order_item_quantity=quantity,
        reserved_quantity=reserved_qty,
    )

    # ── Update InventoryItem reserved_quantity ────────────────────────────
    # Mirrors what place_order_inventory() would have done.
    from django.db import models as _dj
    updated = InventoryItem.objects.filter(
        pk=ingr.pk,
        on_hand_quantity__gte=_dj.F('reserved_quantity') + reserved_qty,
    ).update(reserved_quantity=_dj.F('reserved_quantity') + reserved_qty)
    assert updated == 1, 'InventoryItem reserved_quantity update failed in fixture'

    # Also write the reservation movement log so the backward-compat guard
    # in _consume_on_ready / _settle_reservation recognises this order as
    # having gone through place_order_inventory.
    from apps.inventory.models import IngredientMovementLog
    ingr.refresh_from_db()
    IngredientMovementLog.objects.create(
        inventory_item=ingr,
        item_name=ingr.name,
        base_unit=ingr.base_unit,
        movement_type=IngredientMovementLog.MOVEMENT_RESERVATION,
        on_hand_delta=Decimal('0.000'),
        reserved_delta=reserved_qty,
        on_hand_before=on_hand,
        on_hand_after=on_hand,
        reserved_before=Decimal('0.000'),
        reserved_after=reserved_qty,
        order=order,
        order_number=order.order_number,
        idempotency_key=f'order:{order.pk}:reserve:{ingr.pk}',
    )

    return order, reservation, ingr


# ── Worker functions ───────────────────────────────────────────────────────────

def _worker_hold_order_lock(order_pk, barrier, hold_event, release_event,
                             results, idx, holder_pid_box=None):
    """
    Acquire the Order row lock and hold it until signalled.

    1. Wait at barrier (Barrier(1) releases immediately for a solo holder).
    2. SET LOCAL lock_timeout='15s' — bounds the lock wait.
    3. SELECT ... FOR UPDATE on the Order row.
    4. Record this connection's PG backend PID in holder_pid_box[0].
    5. Set hold_event — signals that the lock is held.
    6. Wait on release_event (bounded _HOLDER_RELEASE_TIMEOUT seconds).
    7. Exit atomic() — commit, release the lock.
    """
    from django.db import connection as c, transaction as tx
    from apps.orders.models import Order
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        with tx.atomic():
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
            Order.objects.select_for_update().get(pk=order_pk)
            if holder_pid_box is not None:
                with c.cursor() as cur:
                    cur.execute('SELECT pg_backend_pid()')
                    holder_pid_box[0] = cur.fetchone()[0]
            hold_event.set()
            released = release_event.wait(timeout=_HOLDER_RELEASE_TIMEOUT)
            if not released:
                results[idx] = ('timeout',
                    f'release_event never set within {_HOLDER_RELEASE_TIMEOUT}s')
                return
        results[idx] = ('ok',)
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
        hold_event.set()   # unblock test even on error
    finally:
        c.close()


def _worker_hold_order_lock_then_cancel(order_pk, disposition,
                                        barrier, hold_event, release_event,
                                        results, idx, holder_pid_box=None):
    """
    Hold the Order row lock, then complete a cancellation while holding it.

    The holder:
      1. Acquires the Order lock.
      2. Signals hold_event.
      3. Waits for release_event (the test waiter confirmed blocked).
      4. Calls on_status_changed('cancelled') — still inside the atomic block.
      5. Saves order.status = 'cancelled', commits.
    """
    from django.db import connection as c, transaction as tx
    from apps.orders.models import Order
    from apps.orders.services import on_status_changed
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        with tx.atomic():
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
            order = Order.objects.select_for_update().get(pk=order_pk)
            if holder_pid_box is not None:
                with c.cursor() as cur:
                    cur.execute('SELECT pg_backend_pid()')
                    holder_pid_box[0] = cur.fetchone()[0]
            hold_event.set()
            released = release_event.wait(timeout=_HOLDER_RELEASE_TIMEOUT)
            if not released:
                results[idx] = ('timeout',
                    f'release_event never set within {_HOLDER_RELEASE_TIMEOUT}s')
                return
            # Complete the cancellation while still holding the Order lock.
            on_status_changed(order_pk, 'cancelled', disposition=disposition)
            order.refresh_from_db()
            order.cancelled_at = timezone.now()
            order.status = 'cancelled'
            order.save()
        results[idx] = ('ok',)
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
        hold_event.set()
    finally:
        c.close()


def _worker_hold_order_lock_then_mark_ready(order_pk,
                                            barrier, hold_event, release_event,
                                            results, idx, holder_pid_box=None):
    """
    Hold the Order row lock, then complete a mark-ready while holding it.

    The holder:
      1. Acquires the Order lock.
      2. Signals hold_event.
      3. Waits for release_event.
      4. Calls on_status_changed('ready') — still inside the atomic block.
      5. Saves order.status = 'ready', commits.
    """
    from django.db import connection as c, transaction as tx
    from apps.orders.models import Order
    from apps.orders.services import on_status_changed
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        with tx.atomic():
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
            order = Order.objects.select_for_update().get(pk=order_pk)
            if holder_pid_box is not None:
                with c.cursor() as cur:
                    cur.execute('SELECT pg_backend_pid()')
                    holder_pid_box[0] = cur.fetchone()[0]
            hold_event.set()
            released = release_event.wait(timeout=_HOLDER_RELEASE_TIMEOUT)
            if not released:
                results[idx] = ('timeout',
                    f'release_event never set within {_HOLDER_RELEASE_TIMEOUT}s')
                return
            # Complete the ready transition while still holding the Order lock.
            on_status_changed(order_pk, 'ready')
            order.refresh_from_db()
            order.ready_at = timezone.now()
            order.status = 'ready'
            order.save()
        results[idx] = ('ok',)
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
        hold_event.set()
    finally:
        c.close()


def _worker_cancel_order(order_pk, disposition,
                         barrier, results, idx,
                         waiter_pid_box=None, start_gate=None):
    """
    Worker: attempt to cancel an order.

    Records this connection's PG backend PID in waiter_pid_box[0] before
    the start point so the main thread can poll pg_blocking_pids().

    start_gate: if provided (a threading.Event), waits on this event instead
    of the barrier — allows holder-before-waiter ordering.
    """
    from django.db import connection as c, transaction as tx
    from apps.orders.models import Order
    from apps.orders.services import on_status_changed
    try:
        if waiter_pid_box is not None:
            with c.cursor() as cur:
                cur.execute('SELECT pg_backend_pid()')
                waiter_pid_box[0] = cur.fetchone()[0]
        if start_gate is not None:
            start_gate.wait()
        else:
            barrier.wait(timeout=_BARRIER_TIMEOUT)
        with tx.atomic():
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
            # This SELECT FOR UPDATE blocks if the holder has the Order lock.
            order = Order.objects.select_for_update().get(pk=order_pk)
            on_status_changed(order_pk, 'cancelled', disposition=disposition)
            order.refresh_from_db()
            order.cancelled_at = timezone.now()
            order.status = 'cancelled'
            order.save()
        results[idx] = ('ok',)
    except Exception as exc:
        results[idx] = ('error', type(exc).__name__, str(exc))
    finally:
        c.close()


def _worker_mark_ready(order_pk, barrier, results, idx,
                       waiter_pid_box=None, start_gate=None):
    """
    Worker: attempt to mark an order ready.

    Records this connection's PG backend PID in waiter_pid_box[0] before the
    start point so the main thread can poll pg_blocking_pids().
    """
    from django.db import connection as c, transaction as tx
    from apps.orders.models import Order
    from apps.orders.services import on_status_changed, validate_status_transition
    try:
        if waiter_pid_box is not None:
            with c.cursor() as cur:
                cur.execute('SELECT pg_backend_pid()')
                waiter_pid_box[0] = cur.fetchone()[0]
        if start_gate is not None:
            start_gate.wait()
        else:
            barrier.wait(timeout=_BARRIER_TIMEOUT)
        with tx.atomic():
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
            # This SELECT FOR UPDATE blocks if the holder has the Order lock.
            order = Order.objects.select_for_update().get(pk=order_pk)
            # validate_status_transition: if order is already 'ready' (second
            # request or won by cancel), same-status returns silently; if
            # cancelled, raises ValueError.
            try:
                validate_status_transition(order.status, 'ready', order=order)
            except Exception:
                # Transition is not valid — record but do not reraise.
                results[idx] = ('blocked', order.status)
                return
            # Only proceed if the transition is valid (order still 'preparing').
            on_status_changed(order_pk, 'ready')
            order.refresh_from_db()
            order.ready_at = timezone.now()
            order.status = 'ready'
            order.save()
        results[idx] = ('ok', order.status)
    except Exception as exc:
        results[idx] = ('error', type(exc).__name__, str(exc))
    finally:
        c.close()


# ═════════════════════════════════════════════════════════════════════════════
# Scenario A — Cancellation racing mark-ready (preparing)
# ═════════════════════════════════════════════════════════════════════════════

class CancelRacingMarkReadyTests(TransactionTestCase):
    """
    Rev 3 §M.5 item 5: "Cancel racing mark-ready on a mixed order:
    one outcome only, consistent counters."

    Uses pure ingredient orders (no legacy items) to avoid the disposition
    requirement for legacy items — _validate_and_derive_ready generates
    system_ready dispositions automatically for ingredient-only lines.

    For each test the Order row lock is the serialisation point.  Both
    ``update_order_status`` (cancel path) and ``mark_order_ready`` acquire
    ``Order.objects.select_for_update()`` at the top of their atomic block.

    The holder/waiter pattern:
      - Holder acquires the lock in a bare transaction, holds it.
      - Test confirms via pg_blocking_pids() the waiter is genuinely blocked.
      - Holder performs its operation and commits.
      - Waiter unblocks, re-reads the committed state, and proceeds.
    """

    @_require_postgresql
    def test_a1_cancel_wins_ready_blocked(self):
        """
        Test A-1: Cancellation acquires the Order lock first.
        Mark-ready is blocked until cancel commits.
        After cancel commits, mark-ready re-reads status='cancelled'
        and raises a ValueError (invalid transition cancelled→ready).

        Expected final state:
          - Order.status = 'cancelled'
          - Reservation.status = 'released'
          - InventoryItem.reserved_quantity = 0
          - InventoryItem.on_hand_quantity = initial on_hand (unchanged)
          - Exactly 1 reservation_release movement log
          - 0 consumption movement logs
        """
        qty          = 2
        qty_per_unit = Decimal('10.000')
        on_hand      = Decimal('1000.000')
        order, reservation, ingr = _make_ingredient_preparing_order(
            quantity=qty, qty_per_unit=qty_per_unit, on_hand=on_hand,
        )
        reserved_amount = qty_per_unit * qty   # 20g
        order_pk        = order.pk
        # For a pure ingredient order, disposition for ready-cancel is empty:
        # _validate_and_derive_ready generates 'system_ready' dispositions
        # automatically for all items (all prepared, discarded=0).
        cancel_disposition = {
            item.pk: {'discarded_units': 0}
            for item in order.items.all()
        }

        results       = [None, None]
        # Barrier(1) releases the holder immediately — no other thread needed
        # at the barrier; the waiter uses start_gate instead.
        holder_barrier = threading.Barrier(1, timeout=_BARRIER_TIMEOUT)
        hold_event     = threading.Event()
        release_event  = threading.Event()
        start_gate     = threading.Event()

        holder_pid = [None]
        waiter_pid = [None]

        t_holder = threading.Thread(
            name='cancel-holder',
            target=_worker_hold_order_lock_then_cancel,
            args=(order_pk, cancel_disposition,
                  holder_barrier, hold_event, release_event,
                  results, 0),
            kwargs={'holder_pid_box': holder_pid},
            daemon=True,
        )
        t_waiter = threading.Thread(
            name='ready-waiter',
            target=_worker_mark_ready,
            args=(order_pk, holder_barrier, results, 1),
            kwargs={'waiter_pid_box': waiter_pid, 'start_gate': start_gate},
            daemon=True,
        )

        t_holder.start()
        t_waiter.start()

        # Wait for the holder to acquire the Order lock.
        self.assertTrue(
            hold_event.wait(timeout=_HOLD_TIMEOUT),
            'Cancel holder did not acquire the Order lock within timeout.',
        )

        # Give the waiter time to register its PID on its own connection
        # and issue the SELECT FOR UPDATE.  We poll until it appears in
        # pg_blocking_pids rather than sleeping an arbitrary amount.
        for _ in range(50):
            if waiter_pid[0] is not None:
                break
            time.sleep(0.1)

        self.assertIsNotNone(
            waiter_pid[0],
            'Waiter thread did not register its PG backend PID within timeout.',
        )

        # Release the waiter so it proceeds to SELECT FOR UPDATE and blocks
        # on the Order row lock held by the cancel holder.
        # Wrapped in try/finally so an assertion failure cannot leave the
        # waiter permanently blocked on start_gate.wait().
        try:
            start_gate.set()

            # Confirm the waiter is genuinely blocked on the Order row lock.
            blocked = _wait_for_blocker(
                holder_pid[0], waiter_pid[0],
                timeout=_BLOCKER_POLL_TIMEOUT, poll=0.05,
            )
            self.assertTrue(
                blocked,
                'Ready waiter was not blocked by cancel holder.\n'
                + _blocker_diagnostic(holder_pid[0], waiter_pid[0],
                                      results=results,
                                      threads=[t_holder, t_waiter]),
            )

            # Allow the cancellation to complete and commit.
            release_event.set()
        finally:
            # Guarantee the waiter is never left blocked if the test body
            # raises an assertion error before release_event.set().
            start_gate.set()
            release_event.set()

        t_holder.join(timeout=_JOIN_TIMEOUT)
        t_waiter.join(timeout=_JOIN_TIMEOUT)

        self.assertFalse(t_holder.is_alive(), 'Cancel holder thread did not exit.')
        self.assertFalse(t_waiter.is_alive(), 'Ready waiter thread did not exit.')

        _assert_no_unexpected(self, results, context='A-1')

        # ── Verify the holder (cancel) succeeded ─────────────────────────
        self.assertEqual(results[0], ('ok',),
            f'Cancel holder expected ok, got: {results[0]}')

        # ── Verify the waiter (ready) was blocked and saw cancelled status ─
        # 'blocked' means validate_status_transition raised (cancelled→ready)
        # 'ok' should not happen here
        waiter_result = results[1]
        self.assertIn(waiter_result[0], ('blocked', 'error'),
            f'Ready waiter should have been blocked or errored, got: {waiter_result}')
        if waiter_result[0] == 'blocked':
            self.assertEqual(waiter_result[1], 'cancelled',
                f'Waiter saw unexpected status after unblocking: {waiter_result}')

        # ── Final database state ──────────────────────────────────────────
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled',
            f'Order status should be cancelled, got: {order.status}')

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, 'released',
            f'Reservation should be released, got: {reservation.status}')
        self.assertIsNotNone(reservation.settled_at)

        ingr.refresh_from_db()
        self.assertEqual(
            ingr.reserved_quantity, Decimal('0.000'),
            f'reserved_quantity should be 0, got: {ingr.reserved_quantity}',
        )
        self.assertEqual(
            ingr.on_hand_quantity, on_hand,
            f'on_hand_quantity should be unchanged at {on_hand}, '
            f'got: {ingr.on_hand_quantity}',
        )

        from apps.inventory.models import IngredientMovementLog
        # Exactly one release movement — the reservation release.
        release_logs = IngredientMovementLog.objects.filter(
            order=order,
            movement_type=IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE,
        )
        self.assertEqual(release_logs.count(), 1,
            f'Expected exactly 1 release movement, found {release_logs.count()}')

        # Zero consumption movements.
        consume_logs = IngredientMovementLog.objects.filter(
            order=order,
            movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION,
        )
        self.assertEqual(consume_logs.count(), 0,
            f'Expected 0 consumption movements, found {consume_logs.count()}')

        # Reservation line fully released.
        line = reservation.items.first()
        line.refresh_from_db()
        self.assertEqual(line.released_quantity, reserved_amount)
        self.assertEqual(line.consumed_quantity, Decimal('0.000'))
        self.assertEqual(
            line.released_quantity + line.consumed_quantity,
            line.reserved_quantity,
        )

    @_require_postgresql
    def test_a2_ready_wins_cancel_follows_ready_workflow(self):
        """
        Test A-2: Mark-ready acquires the Order lock first.
        Cancellation is blocked until mark-ready commits.
        After mark-ready commits, cancel re-reads status='ready'
        and follows the existing ready-order cancellation workflow
        (all ingredients treated as prepared, no restoration, no
        additional consumption movements).

        Expected final state:
          - Order.status = 'cancelled'
          - Reservation.status = 'consumed' (consumed by mark-ready)
          - InventoryItem.on_hand_quantity = initial on_hand - reserved_amount
          - InventoryItem.reserved_quantity = 0
          - Exactly 1 consumption movement (from mark-ready, reason='order_ready')
          - 0 reservation-release movements
          - OrderCancellationDisposition rows exist for the order
          - No duplicate consumption movements after cancel
        """
        qty          = 2
        qty_per_unit = Decimal('10.000')
        on_hand      = Decimal('1000.000')
        order, reservation, ingr = _make_ingredient_preparing_order(
            quantity=qty, qty_per_unit=qty_per_unit, on_hand=on_hand,
        )
        reserved_amount = qty_per_unit * qty   # 20g
        order_pk        = order.pk
        # For a ready-cancel: _validate_and_derive_ready requires discarded_units
        # only for legacy items. Ingredient-only orders: empty dict is enough —
        # the service auto-generates system_ready dispositions.
        cancel_disposition = {
            item.pk: {'discarded_units': 0}
            for item in order.items.all()
        }

        results       = [None, None]
        holder_barrier = threading.Barrier(1, timeout=_BARRIER_TIMEOUT)
        hold_event     = threading.Event()
        release_event  = threading.Event()
        start_gate     = threading.Event()

        holder_pid = [None]
        waiter_pid = [None]

        t_holder = threading.Thread(
            name='ready-holder',
            target=_worker_hold_order_lock_then_mark_ready,
            args=(order_pk,
                  holder_barrier, hold_event, release_event,
                  results, 0),
            kwargs={'holder_pid_box': holder_pid},
            daemon=True,
        )
        t_waiter = threading.Thread(
            name='cancel-waiter',
            target=_worker_cancel_order,
            args=(order_pk, cancel_disposition,
                  holder_barrier, results, 1),
            kwargs={'waiter_pid_box': waiter_pid, 'start_gate': start_gate},
            daemon=True,
        )

        t_holder.start()
        t_waiter.start()

        self.assertTrue(
            hold_event.wait(timeout=_HOLD_TIMEOUT),
            'Ready holder did not acquire the Order lock within timeout.',
        )

        for _ in range(50):
            if waiter_pid[0] is not None:
                break
            time.sleep(0.1)

        self.assertIsNotNone(
            waiter_pid[0],
            'Waiter thread did not register its PG backend PID within timeout.',
        )

        # Release the waiter so it proceeds to SELECT FOR UPDATE and blocks
        # on the Order row lock held by the ready holder.
        # Wrapped in try/finally so an assertion failure cannot leave the
        # waiter permanently blocked on start_gate.wait().
        try:
            start_gate.set()

            blocked = _wait_for_blocker(
                holder_pid[0], waiter_pid[0],
                timeout=_BLOCKER_POLL_TIMEOUT, poll=0.05,
            )
            self.assertTrue(
                blocked,
                'Cancel waiter was not blocked by ready holder.\n'
                + _blocker_diagnostic(holder_pid[0], waiter_pid[0],
                                      results=results,
                                      threads=[t_holder, t_waiter]),
            )

            release_event.set()
        finally:
            # Guarantee the waiter is never left blocked if the test body
            # raises an assertion error before release_event.set().
            start_gate.set()
            release_event.set()

        t_holder.join(timeout=_JOIN_TIMEOUT)
        t_waiter.join(timeout=_JOIN_TIMEOUT)

        self.assertFalse(t_holder.is_alive(), 'Ready holder thread did not exit.')
        self.assertFalse(t_waiter.is_alive(), 'Cancel waiter thread did not exit.')

        _assert_no_unexpected(self, results, context='A-2')

        self.assertEqual(results[0], ('ok',),
            f'Ready holder expected ok, got: {results[0]}')
        self.assertEqual(results[1][0], 'ok',
            f'Cancel waiter expected ok, got: {results[1]}')

        # ── Final database state ──────────────────────────────────────────
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled',
            f'Order status should be cancelled, got: {order.status}')

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, 'consumed',
            f'Reservation should be consumed (by mark-ready), '
            f'got: {reservation.status}')
        self.assertIsNotNone(reservation.settled_at)

        ingr.refresh_from_db()
        expected_on_hand = on_hand - reserved_amount
        self.assertEqual(
            ingr.on_hand_quantity, expected_on_hand,
            f'on_hand_quantity should be {expected_on_hand}, '
            f'got: {ingr.on_hand_quantity}',
        )
        self.assertEqual(
            ingr.reserved_quantity, Decimal('0.000'),
            f'reserved_quantity should be 0, got: {ingr.reserved_quantity}',
        )

        from apps.inventory.models import IngredientMovementLog
        # Exactly one consumption movement (from mark-ready).
        consume_logs = IngredientMovementLog.objects.filter(
            order=order,
            movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION,
        )
        self.assertEqual(consume_logs.count(), 1,
            f'Expected exactly 1 consumption movement, found {consume_logs.count()}')
        log = consume_logs.first()
        self.assertEqual(log.reason_code, 'order_ready')
        self.assertEqual(log.on_hand_delta, -reserved_amount)

        # Zero release movements — cancel did not release (already consumed).
        release_logs = IngredientMovementLog.objects.filter(
            order=order,
            movement_type=IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE,
        )
        self.assertEqual(release_logs.count(), 0,
            f'Expected 0 release movements, found {release_logs.count()}')

        # Reservation line fully consumed.
        line = reservation.items.first()
        line.refresh_from_db()
        self.assertEqual(line.consumed_quantity, reserved_amount)
        self.assertEqual(line.released_quantity, Decimal('0.000'))

        # OrderCancellationDisposition rows must exist.
        from apps.orders.models import OrderCancellationDisposition
        disps = OrderCancellationDisposition.objects.filter(order=order)
        self.assertGreater(disps.count(), 0,
            'No OrderCancellationDisposition rows found after cancel.')


# ═════════════════════════════════════════════════════════════════════════════
# Scenario B — Duplicate ready requests
# ═════════════════════════════════════════════════════════════════════════════

class DuplicateReadyRequestTests(TransactionTestCase):
    """
    Rev 3 §M.5 item 6: "Duplicate ready requests (double click) consume once."

    Two workers simultaneously attempt to mark the same order ready using
    separate database connections.  threading.Barrier synchronises their
    start so both issue their SELECT FOR UPDATE at approximately the same
    moment, maximising true lock contention.

    The Order row lock serialises them: one acquires the lock first (the
    winner), the other blocks.  The winner consumes the reservation.  When
    the loser acquires the lock, it reads the committed state:

      - order.status == 'ready': validate_status_transition('ready', 'ready')
        returns silently (same-status is a no-op per the validator comment:
        "Same-status 'change' is a no-op — treat it as valid").
      - on_status_changed: old_status == new_status == 'ready' → early return.
      - No consumption occurs on the second pass.
      - order.status = 'ready' is re-saved idempotently.

    Both workers return {'success': True, ...} but consumption happens once.
    """

    @_require_postgresql
    def test_b1_duplicate_ready_consumes_exactly_once(self):
        """
        Two simultaneous mark-ready requests on the same order.

        One request wins the Order lock and consumes the reservation.
        The second unblocks and finds status='ready' — same-status no-op.

        Expected final state:
          - Order.status = 'ready'
          - Reservation.status = 'consumed'
          - InventoryItem.on_hand_quantity = initial on_hand - reserved_amount
          - InventoryItem.reserved_quantity = 0
          - Exactly 1 consumption movement log
          - Idempotency key 'line:{line.pk}:consume' appears exactly once
          - Reservation line: consumed_quantity = reserved_quantity
          - Outstanding = 0
        """
        qty          = 3
        qty_per_unit = Decimal('10.000')
        on_hand      = Decimal('1000.000')
        order, reservation, ingr = _make_ingredient_preparing_order(
            quantity=qty, qty_per_unit=qty_per_unit, on_hand=on_hand,
        )
        reserved_amount = qty_per_unit * qty   # 30g
        order_pk        = order.pk

        results = [None, None]
        # Barrier(2) — both workers reach it and are released simultaneously.
        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        t0 = threading.Thread(
            name='ready-request-0',
            target=_worker_mark_ready,
            args=(order_pk, barrier, results, 0),
            daemon=True,
        )
        t1 = threading.Thread(
            name='ready-request-1',
            target=_worker_mark_ready,
            args=(order_pk, barrier, results, 1),
            daemon=True,
        )

        timed_out = _run_threads([t0, t1])
        self.assertFalse(timed_out,
            f'Threads timed out: {timed_out}  results={results}')
        _assert_no_unexpected(self, results, context='B-1')

        # Both workers must have produced a result.
        self.assertIsNotNone(results[0], 'Thread 0 produced no result.')
        self.assertIsNotNone(results[1], 'Thread 1 produced no result.')

        # At least one must have succeeded with status='ready'.
        ok_results = [r for r in results if r[0] == 'ok']
        self.assertGreaterEqual(len(ok_results), 1,
            f'At least one ready request must succeed. Results: {results}')

        # ── Final database state ──────────────────────────────────────────
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready',
            f'Order status should be ready, got: {order.status}')

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, 'consumed',
            f'Reservation should be consumed, got: {reservation.status}')
        self.assertIsNotNone(reservation.settled_at)

        ingr.refresh_from_db()
        expected_on_hand = on_hand - reserved_amount
        self.assertEqual(
            ingr.on_hand_quantity, expected_on_hand,
            f'on_hand_quantity should be {expected_on_hand}, '
            f'got: {ingr.on_hand_quantity}',
        )
        self.assertEqual(
            ingr.reserved_quantity, Decimal('0.000'),
            f'reserved_quantity should be 0, got: {ingr.reserved_quantity}',
        )

        # ── Movement log — exactly one consumption ────────────────────────
        from apps.inventory.models import IngredientMovementLog
        consume_logs = IngredientMovementLog.objects.filter(
            order=order,
            movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION,
        )
        self.assertEqual(
            consume_logs.count(), 1,
            f'Expected exactly 1 consumption movement, '
            f'found {consume_logs.count()}. '
            f'This would indicate double consumption.',
        )
        log = consume_logs.first()
        self.assertEqual(log.reason_code, 'order_ready')
        self.assertEqual(log.on_hand_delta, -reserved_amount)
        self.assertEqual(log.reserved_delta, -reserved_amount)

        # Idempotency key appears exactly once.
        line = reservation.items.first()
        idem_key = f'line:{line.pk}:consume'
        idem_logs = IngredientMovementLog.objects.filter(idempotency_key=idem_key)
        self.assertEqual(
            idem_logs.count(), 1,
            f'Idempotency key {idem_key!r} should appear exactly once, '
            f'found {idem_logs.count()}.',
        )

        # ── Reservation line ──────────────────────────────────────────────
        line.refresh_from_db()
        self.assertEqual(
            line.consumed_quantity, reserved_amount,
            f'Line consumed_quantity should be {reserved_amount}, '
            f'got: {line.consumed_quantity}',
        )
        self.assertEqual(line.released_quantity, Decimal('0.000'))
        outstanding = (
            line.reserved_quantity
            - line.consumed_quantity
            - line.released_quantity
        )
        self.assertEqual(outstanding, Decimal('0.000'),
            f'Outstanding should be 0, got: {outstanding}')

        # ── Invariant: reserved ≤ on_hand ─────────────────────────────────
        self.assertGreaterEqual(ingr.on_hand_quantity, Decimal('0.000'))
        self.assertGreaterEqual(ingr.reserved_quantity, Decimal('0.000'))
        self.assertLessEqual(ingr.reserved_quantity, ingr.on_hand_quantity)
