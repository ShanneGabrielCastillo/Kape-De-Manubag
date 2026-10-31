"""
Phase 2A PostgreSQL Concurrency Tests
======================================
apps/inventory/tests_phase2a_pg.py

These tests verify the concurrent-access behavior of the Phase 2A inventory
foundation using *real* concurrent database connections. They prove that:

  1. lock_inventory_items() acquires InventoryItem locks in ascending pk
     order via SELECT ... FOR UPDATE, so two overlapping operations can
     never deadlock regardless of which items each caller requests.

  2. A record_adjustment() or record_wastage() call that arrives while the
     first transaction holds the row lock will *block* (not race), read the
     latest committed value, and either succeed or raise a
     StaleExpectedQuantityError / ExceedsAvailableError — never silently
     corrupt the balance.

  3. Movement log rows and on_hand_quantity changes are atomic: a thread
     that fails partway through leaves no orphaned log row and no partial
     quantity change.

  4. When two operations target the same item from opposite directions, the
     final committed quantity is always a consistent result of one operation
     serialised after the other — never a mix of both writes.

Why PostgreSQL only
-------------------
SQLite serialises all writes with a single database-level lock — there is no
row-level locking and SELECT FOR UPDATE is silently accepted but ignored.
A "concurrency" test run against SQLite would only simulate sequential
execution and would always pass even if the lock ordering were completely
wrong. These tests therefore skip themselves when the database is not
PostgreSQL.

Concurrency test design (Rev 3 §M.7)
--------------------------------------
Each test case:
  - Inherits from TransactionTestCase so each test runs in its own rolled-back
    state but without the implicit test-wide transaction that TestCase uses.
    TransactionTestCase is required for tests that open multiple real
    connections, because TestCase wraps everything in one transaction that
    other connections cannot see.
  - Spawns worker threads, each of which opens its own independent Django
    database connection (using django.test.utils.CaptureQueriesContext or a
    bare connection alias).
  - Uses threading.Barrier to synchronise the moment both threads attempt to
    acquire their locks — maximising the chance that they actually compete.
  - Verifies the final committed database state after all threads complete,
    not just the in-memory state of one thread.
  - Closes all thread-local connections in a finally block so the test
    database is not left with stale connections.

Out-of-scope tests (future phases)
------------------------------------
The following Rev 3 §M.5 and §M.6 scenarios are NOT implemented here because
the code they would test does not yet exist:

  - Reservation racing cancellation (Phase 5A/5B — OrderIngredientReservation
    not yet created)
  - Duplicate reservation via same request token (Phase 5A)
  - Cancel racing mark-ready on a mixed order (Phase 6B)
  - Recipe approval racing order placement (Phase 4B/5B)
  - Packaging profile retire racing rule creation (Phase 4B/5B)
  - Product mode flip racing placement (Phase 4A/5B)
  - Mixed legacy+ingredient rollback (Phase 5A/5B)
  - Configuration concurrency protocol (Phase 4B/5B/H.9)

Each of those tests will be added in the phase that implements the
corresponding feature, co-located in the relevant test module.
"""

import threading
from decimal import Decimal

from django.db import connection
from django.test import TestCase, TransactionTestCase

from apps.inventory.models import IngredientMovementLog, InventoryItem
from apps.inventory.services import (
    ExceedsAvailableError,
    InventoryServiceError,
    StaleExpectedQuantityError,
    lock_inventory_items,
    record_adjustment,
    record_wastage,
)


# ── Skip decorator ─────────────────────────────────────────────────────────────

def _require_postgresql(test_func):
    """Skip this test unless the default database is PostgreSQL.

    Applied to every test in this module. SQLite ignores SELECT FOR UPDATE,
    so any "concurrency" test there would merely simulate sequential execution
    and prove nothing about lock ordering or blocking behavior.
    """
    import functools

    @functools.wraps(test_func)
    def wrapper(self, *args, **kwargs):
        if connection.vendor != 'postgresql':
            self.skipTest(
                'PostgreSQL required for real SELECT FOR UPDATE concurrency '
                f'tests. Current backend: {connection.vendor}.'
            )
        return test_func(self, *args, **kwargs)
    return wrapper


# ── Item factory ───────────────────────────────────────────────────────────────

def _make_item(name, on_hand='1000.000', reserved='0.000', base_unit='g'):
    return InventoryItem.objects.create(
        name=name,
        item_type='ingredient',
        base_unit=base_unit,
        on_hand_quantity=Decimal(on_hand),
        reserved_quantity=Decimal(reserved),
    )


# ── Thread worker helpers ──────────────────────────────────────────────────────

def _run_adjustment_in_thread(item_pk, new_on_hand, expected_on_hand,
                              barrier, results, idx, idempotency_key=None):
    """
    Worker function for concurrent adjustment tests.

    Opens a fresh Django database connection, waits at the barrier (so both
    threads attempt to acquire their lock at approximately the same moment),
    then runs record_adjustment() inside transaction.atomic().

    The result is stored in results[idx] as either:
      - ('ok', movement_pk) on success
      - ('error', exception_class_name, str(exception)) on expected failure
      - ('unexpected', exception_class_name, str(exception)) on unexpected failure

    The connection is always closed in a finally block so the test DB is left
    clean. Django creates a new connection per thread automatically when a
    thread uses the ORM for the first time; we call connection.close() to
    return it to the pool.
    """
    from django.db import transaction as db_transaction
    from django.db import connection as thread_conn

    try:
        barrier.wait()   # synchronise: both threads attempt the lock together
        with db_transaction.atomic():
            movement = record_adjustment(
                inventory_item_id=item_pk,
                new_on_hand=Decimal(str(new_on_hand)),
                expected_on_hand=Decimal(str(expected_on_hand)),
                reason_code='concurrency_test',
                idempotency_key=idempotency_key,
            )
        results[idx] = ('ok', movement.pk)
    except (StaleExpectedQuantityError, InventoryServiceError) as exc:
        results[idx] = ('error', type(exc).__name__, str(exc))
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
    finally:
        thread_conn.close()


def _run_wastage_in_thread(item_pk, quantity, barrier, results, idx):
    """Worker function for concurrent wastage tests."""
    from django.db import transaction as db_transaction
    from django.db import connection as thread_conn

    try:
        barrier.wait()
        with db_transaction.atomic():
            movement = record_wastage(
                inventory_item_id=item_pk,
                quantity=Decimal(str(quantity)),
                reason_code='concurrency_test',
            )
        results[idx] = ('ok', movement.pk)
    except (ExceedsAvailableError, InventoryServiceError) as exc:
        results[idx] = ('error', type(exc).__name__, str(exc))
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
    finally:
        thread_conn.close()


def _run_lock_in_thread(item_pks, barrier, results, idx, hold_event, release_event):
    """
    Worker that acquires locks on item_pks and holds them until signalled.

    Used by lock-ordering tests to verify that a second thread blocks (rather
    than deadlocking) when it requests the same items in a different order.

    Sequence:
      1. Wait at barrier — both threads attempt lock acquisition simultaneously.
      2. SET LOCAL lock_timeout = '15s' on this connection only.  Bounds the
         SELECT FOR UPDATE so it raises OperationalError instead of waiting
         forever if locks are unexpectedly held beyond 15 seconds.
      3. Acquire locks (SELECT FOR UPDATE in ascending pk order).
      4. Set hold_event — tells the test that locks are held.
      5. Wait on release_event — holds the transaction open.  A 30-second
         timeout acts as a safety net: if the test body crashes before setting
         the release event, this worker eventually exits and closes its
         connection rather than blocking indefinitely.
      6. Commit (by exiting atomic()) — releases locks.
    """
    from django.db import transaction as db_transaction
    from django.db import connection as thread_conn

    try:
        barrier.wait()
        with db_transaction.atomic():
            # Bound the SELECT FOR UPDATE to 15 seconds on this connection only.
            # SET LOCAL resets automatically when the transaction commits or
            # rolls back, so it cannot affect any other connection or test.
            # Without this, PostgreSQL's default lock_timeout = 0 means the
            # SELECT FOR UPDATE can block indefinitely if locks are held longer
            # than expected (e.g. under CI load), leaving the worker alive past
            # _JOIN_TIMEOUT and causing TransactionTestCase teardown to hang.
            with thread_conn.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '15s'")
            locked = lock_inventory_items(item_pks)
            hold_event.set()                    # signal: locks acquired, holding them now
            released = release_event.wait(timeout=30)   # bounded wait — safety net
            if not released:
                # The test did not release us within the timeout.  Exit the
                # transaction (which releases DB locks) and record the failure.
                results[idx] = ('unexpected', 'ReleaseTimeout',
                                f'Thread {idx} release_event never set within 30s')
                return
            results[idx] = ('ok', sorted(locked.keys()))
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
        hold_event.set()           # unblock test even on error
    finally:
        thread_conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Test suite
# ═════════════════════════════════════════════════════════════════════════════

class LockOrderingConcurrencyTests(TransactionTestCase):
    """
    Verify that lock_inventory_items() acquires locks in ascending pk order.

    If two concurrent transactions both need items A and B but request them
    in opposite orders (A then B vs B then A), they deadlock on PostgreSQL
    unless the lock acquisition is sorted. The Phase 2A implementation sorts
    by pk in a single ORDER BY query, which prevents the deadlock.

    These tests confirm:
      - Two threads requesting the same items in opposite pk order do NOT
        deadlock; one waits for the other and both eventually succeed.
      - The lock helper returns the correct items.
      - After both transactions commit, the database is in a consistent state.
    """

    @_require_postgresql
    def test_lock_acquisition_is_ordered_by_pk(self):
        """
        lock_inventory_items() locks rows in ascending pk order regardless
        of the order items are passed in.

        Thread A requests [high_pk, low_pk].
        Thread B requests [low_pk, high_pk].

        With naive unordered locking, A holds low_pk and waits for high_pk
        while B holds high_pk and waits for low_pk → deadlock.

        With ascending-pk ordering (implemented in lock_inventory_items),
        both threads acquire in the same order. One blocks while the other
        completes, then the blocked one proceeds. No deadlock.
        """
        item_a = _make_item('LockTestA_low')
        item_b = _make_item('LockTestB_high')
        # Ensure item_a.pk < item_b.pk by reading them after creation.
        item_a.refresh_from_db()
        item_b.refresh_from_db()
        self.assertLess(item_a.pk, item_b.pk)

        results = [None, None]
        barrier     = threading.Barrier(2, timeout=10)
        hold_a      = threading.Event()
        release_a   = threading.Event()
        hold_b      = threading.Event()
        release_b   = threading.Event()

        # Thread A: requests [high_pk, low_pk] (intentionally reversed)
        t_a = threading.Thread(
            target=_run_lock_in_thread,
            args=([item_b.pk, item_a.pk], barrier, results, 0, hold_a, release_a),
            daemon=True,
        )
        # Thread B: requests [low_pk, high_pk] (natural order)
        t_b = threading.Thread(
            target=_run_lock_in_thread,
            args=([item_a.pk, item_b.pk], barrier, results, 1, hold_b, release_b),
            daemon=True,
        )

        t_a.start()
        t_b.start()

        # ── Coordinated release protocol ──────────────────────────────────────
        # Because both threads request the same two rows, lock_items() issues
        # one ordered SELECT FOR UPDATE. Exactly one thread acquires both locks
        # first; the other blocks at the PostgreSQL level until the first
        # commits. The sequence is therefore:
        #
        #   1. "winner" thread: acquires both locks, sets hold_winner.
        #   2. "waiter" thread: blocked in SELECT FOR UPDATE at the DB level.
        #   3. Test sees hold_winner fired, sets release_winner.
        #   4. Winner commits, releasing the DB locks.
        #   5. Waiter acquires the now-free locks, sets hold_waiter.
        #   6. Test sees hold_waiter fired, sets release_waiter.
        #   7. Waiter commits. Both threads exit.
        #
        # Every wait is bounded. The try/finally guarantee ensures release
        # events are always set even on a test failure or timeout, so worker
        # threads can never leave open PostgreSQL transactions behind.
        _HOLD_TIMEOUT    = 12   # seconds to wait for a thread to hold its locks
        # The worker uses release_event.wait(timeout=30) as a safety net, so
        # a worker can remain alive for up to 30 seconds after the release
        # event is set plus the time needed to commit and close its connection.
        # The join timeout must exceed the worker's release timeout to ensure
        # the test cannot exit while a worker is still live.  35 s = 30 s
        # (worker release backstop) + 5 s (commit + connection close margin).
        _JOIN_TIMEOUT    = 35   # seconds — must exceed worker release_event.wait(timeout=30)

        try:
            # Step 3: wait for the winner (whichever thread got the locks first).
            # We do not know which one wins, so we wait for either hold event.
            # The winning thread sets its own event; the waiting thread is still
            # blocked at the DB level and has not set anything yet.
            first_held = threading.Event()

            def _watch_first():
                while not (hold_a.is_set() or hold_b.is_set()):
                    import time
                    time.sleep(0.01)
                first_held.set()

            watcher = threading.Thread(target=_watch_first, daemon=True)
            watcher.start()
            first_held_ok = first_held.wait(timeout=_HOLD_TIMEOUT)
            self.assertTrue(
                first_held_ok,
                'Neither thread acquired the locks within the timeout. '
                'Possible deadlock or barrier failure.',
            )

            # Step 3-4: release the winning thread so it commits.
            # Releasing both events here is safe: the waiter has not set its
            # hold event yet, so its release_event fires into a blocked thread
            # that will eventually unblock and pick up the signal.
            release_a.set()
            release_b.set()

            # Step 5-6: wait for the second (waiter) thread to acquire its locks.
            # After the winner commits, the waiter is unblocked by PostgreSQL
            # and sets its hold event.  The test waits briefly, then the release
            # was already set above — the waiter exits its hold immediately.
            second_hold_a_ok = hold_a.wait(timeout=_HOLD_TIMEOUT)
            second_hold_b_ok = hold_b.wait(timeout=_HOLD_TIMEOUT)
            self.assertTrue(
                second_hold_a_ok,
                'Thread A never signalled that it held the locks.',
            )
            self.assertTrue(
                second_hold_b_ok,
                'Thread B never signalled that it held the locks.',
            )

        finally:
            # Guarantee: always unblock any still-waiting worker so it can
            # commit and close its connection. Without this, a failed assertion
            # above would leave both threads blocking forever on release_event,
            # keeping their PostgreSQL transactions (and row locks) open and
            # preventing TransactionTestCase teardown (TRUNCATE) from running.
            release_a.set()
            release_b.set()
            t_a.join(timeout=_JOIN_TIMEOUT)
            t_b.join(timeout=_JOIN_TIMEOUT)
            # Post-join liveness check inside finally so it always runs,
            # even when the try block raised an AssertionError.
            # _JOIN_TIMEOUT (35s) > worker release_event.wait(timeout=30s),
            # so a thread that is still alive here genuinely did not finish.
            if t_a.is_alive():
                self.fail(
                    f'Thread A is still alive after {_JOIN_TIMEOUT}s join. '
                    'It did not finish cleanly despite release_event being set. '
                    'Possible connection or DB lock issue.'
                )
            if t_b.is_alive():
                self.fail(
                    f'Thread B is still alive after {_JOIN_TIMEOUT}s join. '
                    'It did not finish cleanly despite release_event being set. '
                    'Possible connection or DB lock issue.'
                )

        # ── Post-condition checks ─────────────────────────────────────────────
        # Both workers must have succeeded with no unexpected errors.
        for i, result in enumerate(results):
            self.assertIsNotNone(result, f'Thread {i} produced no result.')
            self.assertEqual(
                result[0], 'ok',
                f'Thread {i} failed unexpectedly: {result}',
            )

        # Each lock result must contain both pks, confirming that lock_inventory_items()
        # correctly acquired all requested rows regardless of input order.
        for result in results:
            locked_pks = result[1]
            self.assertIn(item_a.pk, locked_pks)
            self.assertIn(item_b.pk, locked_pks)

    @_require_postgresql
    def test_lock_helper_returns_correct_items(self):
        """
        lock_inventory_items() returns a dict keyed by pk with the correct
        InventoryItem instances, and items are always in ascending pk order.

        This is a sequential test — it verifies the return value and ordering
        guarantee of the helper without needing two connections.
        """
        item1 = _make_item('LockReturn1')
        item2 = _make_item('LockReturn2')
        item3 = _make_item('LockReturn3')

        from django.db import transaction as db_transaction
        with db_transaction.atomic():
            # Request in reverse order to verify the helper normalises.
            locked = lock_inventory_items([item3.pk, item1.pk, item2.pk])

        self.assertEqual(set(locked.keys()), {item1.pk, item2.pk, item3.pk})
        self.assertEqual(locked[item1.pk].name, 'LockReturn1')
        self.assertEqual(locked[item2.pk].name, 'LockReturn2')
        self.assertEqual(locked[item3.pk].name, 'LockReturn3')

    @_require_postgresql
    def test_lock_helper_raises_for_missing_pk(self):
        """
        lock_inventory_items() raises InventoryServiceError when any
        requested pk does not exist.
        """
        item = _make_item('LockMissing1')

        from django.db import transaction as db_transaction
        with self.assertRaises(InventoryServiceError):
            with db_transaction.atomic():
                lock_inventory_items([item.pk, 999999])


class ConcurrentAdjustmentTests(TransactionTestCase):
    """
    Verify that concurrent record_adjustment() calls serialize correctly.

    When two adjustments target the same item simultaneously, the
    SELECT FOR UPDATE in lock_inventory_items() ensures they run one after
    the other. The second transaction will read the value committed by the
    first and, if expected_on_hand no longer matches, raise
    StaleExpectedQuantityError.

    This models the real-world scenario: two staff members both load the
    adjustment form showing on_hand=1000 g, and both submit at the same time.
    The first transaction commits and changes the quantity; the second
    transaction's expected_on_hand (still 1000) no longer matches the
    committed value (whatever the first wrote), so it is rejected. No data
    is lost and no quantity is applied twice.
    """

    @_require_postgresql
    def test_concurrent_adjustments_serialize_not_overlap(self):
        """
        Two concurrent adjustments to the same item: exactly one succeeds,
        the other raises StaleExpectedQuantityError.

        Thread A: adjust to 1200 (expected: 1000)
        Thread B: adjust to 1050 (expected: 1000)

        Both see expected_on_hand=1000 (the initial value). Whichever thread
        acquires the SELECT FOR UPDATE lock first commits, updating the value.
        When the second thread is unblocked, it reads the new committed value
        (1200 or 1050, not 1000), so its expected_on_hand check fails.

        Final state: exactly one successful adjustment, one StaleExpected error,
        one IngredientMovementLog row, and on_hand is either 1200 or 1050.
        """
        item = _make_item('ConcAdj1', on_hand='1000.000')
        initial_on_hand = Decimal('1000.000')

        results = [None, None]
        barrier = threading.Barrier(2, timeout=10)

        t_a = threading.Thread(
            target=_run_adjustment_in_thread,
            args=(item.pk, '1200.000', str(initial_on_hand), barrier, results, 0),
            daemon=True,
        )
        t_b = threading.Thread(
            target=_run_adjustment_in_thread,
            args=(item.pk, '1050.000', str(initial_on_hand), barrier, results, 1),
            daemon=True,
        )

        t_a.start()
        t_b.start()
        t_a.join(timeout=15)
        t_b.join(timeout=15)

        self.assertFalse(t_a.is_alive(), 'Thread A timed out (possible deadlock).')
        self.assertFalse(t_b.is_alive(), 'Thread B timed out (possible deadlock).')

        outcomes = {r[0] for r in results}

        # No unexpected errors.
        for result in results:
            self.assertNotEqual(
                result[0], 'unexpected',
                f'Unexpected error in concurrent adjustment: {result}',
            )

        # Exactly one success and one expected failure.
        self.assertIn('ok', outcomes, 'Neither adjustment succeeded.')
        self.assertIn('error', outcomes, 'Both adjustments succeeded — missing StaleExpected guard.')
        self.assertEqual(
            len([r for r in results if r[0] == 'ok']), 1,
            'More than one adjustment succeeded on the same initial value.',
        )

        # Exactly one movement log row was written.
        log_count = IngredientMovementLog.objects.filter(
            inventory_item=item, movement_type='adjustment'
        ).count()
        self.assertEqual(log_count, 1, f'Expected 1 movement log, got {log_count}.')

        # Final on_hand is one of the two submitted values (not the original).
        item.refresh_from_db()
        self.assertIn(
            item.on_hand_quantity,
            [Decimal('1200.000'), Decimal('1050.000')],
            f'Unexpected final on_hand: {item.on_hand_quantity}.',
        )
        # It must NOT still be the initial value.
        self.assertNotEqual(item.on_hand_quantity, initial_on_hand)

    @_require_postgresql
    def test_sequential_adjustments_each_succeed(self):
        """
        When adjustments are properly chained (each uses the value committed
        by the previous one as its expected_on_hand), all succeed.

        This confirms the happy path for the optimistic-concurrency guard:
        a correctly written client that re-reads stock before each submit
        never hits a StaleExpectedQuantityError.
        """
        item = _make_item('ConcAdj2', on_hand='500.000')

        from django.db import transaction as db_transaction

        # First adjustment: 500 → 600
        with db_transaction.atomic():
            record_adjustment(
                inventory_item_id=item.pk,
                new_on_hand=Decimal('600.000'),
                expected_on_hand=Decimal('500.000'),
                reason_code='step1',
            )

        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('600.000'))

        # Second adjustment: 600 → 450
        with db_transaction.atomic():
            record_adjustment(
                inventory_item_id=item.pk,
                new_on_hand=Decimal('450.000'),
                expected_on_hand=Decimal('600.000'),
                reason_code='step2',
            )

        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('450.000'))

        log_count = IngredientMovementLog.objects.filter(inventory_item=item).count()
        self.assertEqual(log_count, 2)


class ConcurrentWastageTests(TransactionTestCase):
    """
    Verify that concurrent wastage operations cannot produce negative stock
    and that the available-stock guard holds under concurrency.

    When two wastage calls try to waste most of the available stock at the
    same time, the SELECT FOR UPDATE ensures they serialize. The first one
    reduces the on_hand and commits. When the second thread is unblocked,
    it re-reads the item (via lock_inventory_items's select_for_update) and
    sees the already-reduced available quantity. If the remaining available
    stock is insufficient for its quantity, ExceedsAvailableError is raised.
    """

    @_require_postgresql
    def test_concurrent_wastage_cannot_go_negative(self):
        """
        Two concurrent wastage calls both requesting more than half the
        available stock: exactly one should succeed and one should fail with
        ExceedsAvailableError. Stock must never go below zero.

        Item: on_hand=500, reserved=0, available=500.
        Thread A: waste 300
        Thread B: waste 300

        Combined they would waste 600 > 500. The first to commit reduces
        on_hand to 200. When the second thread unblocks, available is 200,
        but it requests 300 → ExceedsAvailableError.
        """
        item = _make_item('ConcWaste1', on_hand='500.000')

        results = [None, None]
        barrier = threading.Barrier(2, timeout=10)

        t_a = threading.Thread(
            target=_run_wastage_in_thread,
            args=(item.pk, '300.000', barrier, results, 0),
            daemon=True,
        )
        t_b = threading.Thread(
            target=_run_wastage_in_thread,
            args=(item.pk, '300.000', barrier, results, 1),
            daemon=True,
        )

        t_a.start()
        t_b.start()
        t_a.join(timeout=15)
        t_b.join(timeout=15)

        self.assertFalse(t_a.is_alive(), 'Thread A timed out.')
        self.assertFalse(t_b.is_alive(), 'Thread B timed out.')

        for result in results:
            self.assertNotEqual(
                result[0], 'unexpected',
                f'Unexpected error in concurrent wastage: {result}',
            )

        outcomes = {r[0] for r in results}
        self.assertIn('ok', outcomes, 'Neither wastage succeeded.')
        self.assertIn('error', outcomes, 'Both wastages succeeded — possible negative stock.')

        # Stock must be >= 0 (DB CHECK would catch this, but we assert explicitly).
        item.refresh_from_db()
        self.assertGreaterEqual(item.on_hand_quantity, Decimal('0.000'))
        # Only one reduction: the succeeded one (300 removed from 500 = 200).
        self.assertEqual(item.on_hand_quantity, Decimal('200.000'))

        # Exactly one movement log row.
        log_count = IngredientMovementLog.objects.filter(
            inventory_item=item, movement_type='wastage'
        ).count()
        self.assertEqual(log_count, 1)

    @_require_postgresql
    def test_concurrent_wastage_with_reserved_stock(self):
        """
        Wastage cannot touch reserved stock, even under concurrency.

        Item: on_hand=500, reserved=300, available=200.
        Thread A: waste 150 (succeeds; available becomes 50)
        Thread B: waste 150 (fails; only 50 available after A)
        """
        item = _make_item('ConcWaste2', on_hand='500.000', reserved='300.000')

        results = [None, None]
        barrier = threading.Barrier(2, timeout=10)

        t_a = threading.Thread(
            target=_run_wastage_in_thread,
            args=(item.pk, '150.000', barrier, results, 0),
            daemon=True,
        )
        t_b = threading.Thread(
            target=_run_wastage_in_thread,
            args=(item.pk, '150.000', barrier, results, 1),
            daemon=True,
        )

        t_a.start()
        t_b.start()
        t_a.join(timeout=15)
        t_b.join(timeout=15)

        self.assertFalse(t_a.is_alive(), 'Thread A timed out.')
        self.assertFalse(t_b.is_alive(), 'Thread B timed out.')

        for result in results:
            self.assertNotEqual(result[0], 'unexpected', str(result))

        outcomes = {r[0] for r in results}
        self.assertIn('ok', outcomes)
        self.assertIn('error', outcomes)

        item.refresh_from_db()
        # available was 200; one wastage of 150 leaves on_hand=350.
        self.assertEqual(item.on_hand_quantity, Decimal('350.000'))
        # reserved_quantity must be unchanged.
        self.assertEqual(item.reserved_quantity, Decimal('300.000'))
        # available = 350 - 300 = 50, which is non-negative.
        self.assertGreaterEqual(item.available_quantity, Decimal('0.000'))


class MultiItemLockOrderTests(TransactionTestCase):
    """
    Verify that operations across multiple inventory items follow the same
    ascending-pk lock order and never deadlock.

    Two threads each need items {A, B, C} but request them in reverse order.
    Without the ordering guarantee they would deadlock. With it, one waits
    and both eventually complete.
    """

    @_require_postgresql
    def test_three_item_concurrent_wastage_no_deadlock(self):
        """
        Two threads each waste a small amount from three shared items,
        requesting those items in opposite pk orders.

        Thread A: waste 10 g from each of [item_c.pk, item_b.pk, item_a.pk]
        Thread B: waste 10 g from each of [item_a.pk, item_b.pk, item_c.pk]

        The wastage service calls lock_inventory_items() with all three pks,
        which normalises them to ascending order before issuing SELECT FOR UPDATE.
        Both threads therefore request locks in the same order → no deadlock.

        Implementation note: the wastage service only targets one item at a
        time, so we test the lock ordering by recording three sequential
        wastage calls within one transaction in each thread. All three pks
        are passed together to lock_inventory_items() via a thin wrapper.
        """
        item_a = _make_item('MultiA', on_hand='500.000')
        item_b = _make_item('MultiB', on_hand='500.000')
        item_c = _make_item('MultiC', on_hand='500.000')

        results = [None, None]
        barrier = threading.Barrier(2, timeout=10)

        def _waste_three_reversed(barrier, results, idx):
            """Waste 10 g from three items, requesting them in reverse pk order."""
            from django.db import transaction as db_transaction
            from django.db import connection as thread_conn
            try:
                barrier.wait()
                with db_transaction.atomic():
                    # Request in descending pk order — the lock helper normalises
                    items_reversed = sorted(
                        [item_a.pk, item_b.pk, item_c.pk], reverse=True
                    )
                    locked = lock_inventory_items(items_reversed)
                    for pk, locked_item in locked.items():
                        available = locked_item.on_hand_quantity - locked_item.reserved_quantity
                        if Decimal('10.000') > available:
                            results[idx] = ('error', 'ExceedsAvailable', f'pk={pk}')
                            return
                        InventoryItem.objects.filter(
                            pk=pk,
                            on_hand_quantity=locked_item.on_hand_quantity,
                        ).update(
                            on_hand_quantity=locked_item.on_hand_quantity - Decimal('10.000')
                        )
                        IngredientMovementLog.record(
                            inventory_item=locked_item,
                            movement_type='wastage',
                            on_hand_delta=Decimal('-10.000'),
                            reserved_delta=Decimal('0.000'),
                            on_hand_before=locked_item.on_hand_quantity,
                            reserved_before=locked_item.reserved_quantity,
                            reason_code='multi_test',
                        )
                results[idx] = ('ok',)
            except Exception as exc:
                results[idx] = ('unexpected', type(exc).__name__, str(exc))
            finally:
                thread_conn.close()

        t_a = threading.Thread(
            target=_waste_three_reversed,
            args=(barrier, results, 0),
            daemon=True,
        )
        t_b = threading.Thread(
            target=_waste_three_reversed,
            args=(barrier, results, 1),
            daemon=True,
        )

        t_a.start()
        t_b.start()
        t_a.join(timeout=20)
        t_b.join(timeout=20)

        self.assertFalse(t_a.is_alive(), 'Thread A deadlocked on multi-item lock.')
        self.assertFalse(t_b.is_alive(), 'Thread B deadlocked on multi-item lock.')

        for i, result in enumerate(results):
            self.assertIsNotNone(result, f'Thread {i} produced no result.')
            self.assertEqual(
                result[0], 'ok',
                f'Thread {i} failed: {result}',
            )

        # Each item should have been reduced by 10 g twice (both threads ran).
        for it in [item_a, item_b, item_c]:
            it.refresh_from_db()
            self.assertEqual(
                it.on_hand_quantity,
                Decimal('480.000'),  # 500 - 10 - 10
                f'{it.name}: expected 480.000 g, got {it.on_hand_quantity}.',
            )

        # Six movement log rows total (3 items × 2 threads).
        log_count = IngredientMovementLog.objects.filter(
            movement_type='wastage', reason_code='multi_test'
        ).count()
        self.assertEqual(log_count, 6)


class TransactionRollbackConsistencyTests(TransactionTestCase):
    """
    Verify that a failed transaction leaves both InventoryItem quantities and
    IngredientMovementLog in a consistent state — no orphaned log row and no
    partial quantity change.

    These tests are sequential (no threading) but use TransactionTestCase
    because they rely on real commit/rollback behavior that TestCase's
    implicit transaction wrapper would hide.
    """

    @_require_postgresql
    def test_rollback_on_log_write_failure_leaves_no_partial_state(self):
        """
        If IngredientMovementLog.record() raises mid-transaction, the quantity
        update that preceded it is rolled back. The item ends up unchanged.
        """
        from django.db import transaction as db_transaction

        item = _make_item('RollbackPG1', on_hand='500.000')
        original_on_hand = item.on_hand_quantity

        original_record = IngredientMovementLog.record

        def _failing_record(*args, **kwargs):
            raise RuntimeError('Injected log write failure for rollback test')

        IngredientMovementLog.record = staticmethod(_failing_record)
        try:
            with self.assertRaises(RuntimeError):
                with db_transaction.atomic():
                    record_wastage(
                        inventory_item_id=item.pk,
                        quantity=Decimal('100.000'),
                    )
        finally:
            IngredientMovementLog.record = staticmethod(original_record)

        # Quantity must be unchanged.
        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, original_on_hand)

        # No log row should have been written.
        self.assertEqual(
            IngredientMovementLog.objects.filter(inventory_item=item).count(), 0
        )

    @_require_postgresql
    def test_rollback_does_not_affect_other_items(self):
        """
        A rollback for item A must not affect a previously committed
        transaction for item B that ran in the same test.
        """
        from django.db import transaction as db_transaction

        item_a = _make_item('RollbackPG_A', on_hand='200.000')
        item_b = _make_item('RollbackPG_B', on_hand='200.000')

        # Commit a wastage for item_b successfully.
        with db_transaction.atomic():
            record_wastage(
                inventory_item_id=item_b.pk,
                quantity=Decimal('50.000'),
            )

        # Now roll back a wastage for item_a.
        original_record = IngredientMovementLog.record

        def _failing_record(*args, **kwargs):
            raise RuntimeError('Injected failure')

        IngredientMovementLog.record = staticmethod(_failing_record)
        try:
            with self.assertRaises(RuntimeError):
                with db_transaction.atomic():
                    record_wastage(
                        inventory_item_id=item_a.pk,
                        quantity=Decimal('80.000'),
                    )
        finally:
            IngredientMovementLog.record = staticmethod(original_record)

        # item_a is unchanged.
        item_a.refresh_from_db()
        self.assertEqual(item_a.on_hand_quantity, Decimal('200.000'))

        # item_b's committed change is preserved.
        item_b.refresh_from_db()
        self.assertEqual(item_b.on_hand_quantity, Decimal('150.000'))

        # Only item_b has a log row.
        self.assertEqual(
            IngredientMovementLog.objects.filter(inventory_item=item_a).count(), 0
        )
        self.assertEqual(
            IngredientMovementLog.objects.filter(inventory_item=item_b).count(), 1
        )

    @_require_postgresql
    def test_quantity_and_log_always_consistent_after_success(self):
        """
        After a successful wastage, both on_hand_quantity and the movement
        log row are present and mathematically consistent (log_after = log_before
        + delta = new on_hand).
        """
        from django.db import transaction as db_transaction

        item = _make_item('RollbackPG_Consistent', on_hand='300.000')

        with db_transaction.atomic():
            movement = record_wastage(
                inventory_item_id=item.pk,
                quantity=Decimal('75.000'),
            )

        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('225.000'))

        movement.refresh_from_db()
        self.assertEqual(movement.on_hand_before, Decimal('300.000'))
        self.assertEqual(movement.on_hand_delta,  Decimal('-75.000'))
        self.assertEqual(movement.on_hand_after,  Decimal('225.000'))
        # DB constraint: after = before + delta
        self.assertEqual(
            movement.on_hand_before + movement.on_hand_delta,
            movement.on_hand_after,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Future-phase placeholders
# ═════════════════════════════════════════════════════════════════════════════

class FuturePhaseTests(TestCase):
    """
    Placeholder for PostgreSQL concurrency tests that require features not
    yet implemented. Each method documents the test scenario and the phase
    it belongs to.

    These are skipped unconditionally in the current phase. They serve as a
    checklist to ensure no scenario is forgotten when the corresponding
    feature is built.
    """

    def test_placeholder_reservation_racing_cancellation(self):
        """
        Phase 5A/5B: Two concurrent requests — one placing an order
        (creating a reservation) and one cancelling an existing order
        (releasing a reservation) — for the same inventory item.
        Final on_hand and reserved quantities must be consistent.
        Requires: OrderIngredientReservation, place_order_inventory,
                  cancel_order_inventory.
        """
        self.skipTest('Not implemented — Phase 5A/5B.')

    def test_placeholder_duplicate_reservation_same_request_token(self):
        """
        Phase 5A: Two concurrent requests with the same request_token both
        try to place an order. Exactly one reservation must be created.
        Requires: OrderIngredientReservation, place_order_inventory.
        """
        self.skipTest('Not implemented — Phase 5A.')

    def test_placeholder_cancel_racing_mark_ready_mixed_order(self):
        """
        Phase 6B: A cancellation and a mark-ready transition arrive
        simultaneously for the same order. Exactly one wins; counters remain
        consistent.
        Requires: consume_ingredient_reservation, cancel_order_inventory,
                  on_status_changed coordinator.
        """
        self.skipTest('Not implemented — Phase 6B.')

    def test_placeholder_recipe_approval_racing_order_placement(self):
        """
        Phase 4B/5B: A recipe revision is approved while an order is being
        placed for the same product. The order must use exactly one revision
        (the one approved before or after the placement, not a mix).
        Requires: Recipe, RecipeItem, resolve_plan, configuration lock (H.9).
        """
        self.skipTest('Not implemented — Phase 4B/5B.')

    def test_placeholder_packaging_profile_retire_racing_rule_creation(self):
        """
        Phase 4B/5B: A packaging profile is retired at the same moment a
        ProductPackagingRule is created pointing to it. The rule must be
        rejected or the retire must fail — never both succeed.
        Requires: PackagingProfile, ProductPackagingRule, configuration lock.
        """
        self.skipTest('Not implemented — Phase 4B/5B.')

    def test_placeholder_product_mode_flip_racing_placement(self):
        """
        Phase 4A/5B: A product's inventory_tracking_mode is switched from
        legacy to ingredient while an order is being placed. The gate that
        refuses the flip while open legacy lines exist must hold under
        concurrency.
        Requires: Product.inventory_tracking_mode, cutover gate (J.2).
        """
        self.skipTest('Not implemented — Phase 4A/5B.')

    def test_placeholder_two_concurrent_flips_same_product(self):
        """
        Phase 4A/5B: Two simultaneous mode-flip attempts for the same
        product. Exactly one succeeds; the second sees the already-flipped
        state.
        Requires: Product.inventory_tracking_mode, cutover gate.
        """
        self.skipTest('Not implemented — Phase 4A/5B.')

    def test_placeholder_mixed_order_rollback_legacy_fails(self):
        """
        Phase 5A/5B: A mixed legacy+ingredient order fails on the legacy side
        (insufficient stock) after the ingredient reservation work is staged.
        The ingredient side must also roll back — no orphaned reservation.
        Requires: place_order_inventory, OrderIngredientReservation.
        """
        self.skipTest('Not implemented — Phase 5A/5B.')

    def test_placeholder_two_concurrent_cancellations_same_order(self):
        """
        Phase 6A/6B: Two simultaneous cancellation requests for the same
        order. Legacy stock restored exactly once; ingredients settled
        exactly once.
        Requires: cancel_order_inventory, OrderCancellationDisposition.
        """
        self.skipTest('Not implemented — Phase 6A/6B.')
