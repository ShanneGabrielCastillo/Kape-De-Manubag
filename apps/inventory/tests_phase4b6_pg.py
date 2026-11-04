"""
Phase 4B-6 PostgreSQL Concurrency Tests
=========================================
apps/inventory/tests_phase4b6_pg.py

These tests verify the concurrent-access behaviour of the Phase 4B
recipe and packaging configuration services using *real* concurrent
database connections.

They prove that Rev 3 §H.9 (Configuration concurrency protocol) is
correctly implemented:

  - The Product row is the configuration lock for recipe operations.
  - The PackagingProfile row is the configuration lock for profile
    retirement and rule saves.
  - Locks are acquired in the documented hierarchy:
      Order -> Product (ascending pk) -> PackagingProfile (ascending pk)
               -> InventoryItem (ascending pk)
  - Recipe rows are NOT locked (Rev 3 §H.9: "Not locked: Recipe, ...")
  - Services recheck status after acquiring the required lock (TOCTOU fix).
  - Database constraints remain the final backstop against duplicate
    approved configurations.

Why PostgreSQL only
-------------------
SQLite serialises all writes with a single database-level write lock and
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
  - Uses threading.Barrier to synchronise workers at the moment they attempt
    to acquire their locks - maximising true lock contention.
  - For hold/block tests: polls pg_stat_activity to confirm the waiting
    worker has actually entered a PostgreSQL row-level Lock wait before
    releasing the holder.  This is the only reliable proof of genuine
    blocking; a Barrier or Event alone only synchronises Python execution.
  - Verifies the final committed database state, not merely in-memory state.
  - Closes all thread-local Django connections in finally blocks.
  - Captures worker exceptions and returns them to the main thread.

Relationship to Phase 2A concurrency tests
--------------------------------------------
Phase 2A tests (tests_phase2a_pg.py) cover lock_inventory_items() ordering
and concurrent adjustment / wastage operations.  This module focuses
exclusively on the Phase 4B configuration-writing services:
  approve_recipe(), retire_recipe(),
  approve_packaging_profile(), retire_packaging_profile(),
  save_packaging_rule().
"""

import functools
import time
import threading
from decimal import Decimal

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone


# ── Skip guard ────────────────────────────────────────────────────────────────

def _require_postgresql(test_func):
    """Decorator: skip unless the default DB is PostgreSQL."""
    @functools.wraps(test_func)
    def wrapper(self, *args, **kwargs):
        if connection.vendor != 'postgresql':
            self.skipTest(
                f'PostgreSQL required for real SELECT FOR UPDATE concurrency '
                f'tests. Current backend: {connection.vendor}.'
            )
        return test_func(self, *args, **kwargs)
    return wrapper


# ── Fixture helpers ───────────────────────────────────────────────────────────

_counter = 0


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _make_user(role='admin'):
    from apps.accounts.models import CustomUser
    uid = f'pg6u_{_uid()}'
    return CustomUser.objects.create_user(username=uid, password='x', role=role)


def _make_category():
    from apps.menu.models import Category
    n = f'Cat_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower())


def _make_product(name=None):
    from apps.menu.models import Product
    n = name or f'Prod_{_uid()}'
    return Product.objects.create(
        category=_make_category(), name=n,
        price=Decimal('65.00'), stock_quantity=100,
    )


def _make_ingredient(name=None, on_hand=Decimal('500.000')):
    from apps.inventory.models import InventoryItem
    n = name or f'Ing_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type='ingredient', base_unit='g',
        on_hand_quantity=on_hand, is_active=True,
    )


def _make_packaging_inv(name=None, on_hand=Decimal('200.000')):
    from apps.inventory.models import InventoryItem
    n = name or f'Pkg_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type='packaging', base_unit='pcs',
        on_hand_quantity=on_hand, is_active=True,
    )


def _make_draft_recipe(product, size='none', scope='all', revision=1):
    from apps.inventory.models import Recipe
    return Recipe.objects.create(
        product=product, size=size, order_type_scope=scope,
        status=Recipe.STATUS_DRAFT, revision=revision,
    )


def _make_approved_recipe(product, size='none', scope='all', revision=1):
    from apps.inventory.models import Recipe
    return Recipe.objects.create(
        product=product, size=size, order_type_scope=scope,
        status=Recipe.STATUS_APPROVED, revision=revision,
        approved_at=timezone.now(), approval_note='Test approval',
    )


def _make_recipe_item(recipe, inv_item, qty=Decimal('10.000')):
    from apps.inventory.models import RecipeItem
    return RecipeItem.objects.create(
        recipe=recipe, inventory_item=inv_item, quantity=qty,
    )


def _make_draft_profile(name=None, revision=1):
    from apps.inventory.models import PackagingProfile
    n = name or f'Prof_{_uid()}'
    return PackagingProfile.objects.create(
        name=n, status=PackagingProfile.STATUS_DRAFT, revision=revision,
    )


def _make_approved_profile(name=None, revision=1):
    from apps.inventory.models import PackagingProfile
    n = name or f'Prof_{_uid()}'
    return PackagingProfile.objects.create(
        name=n, status=PackagingProfile.STATUS_APPROVED,
        revision=revision, approved_at=timezone.now(),
        approval_note='Test approval',
    )


def _make_profile_item(profile, inv_item, qty=Decimal('1.000')):
    from apps.inventory.models import PackagingProfileItem
    return PackagingProfileItem.objects.create(
        profile=profile, inventory_item=inv_item, quantity=qty,
    )


# ── Worker timeout constants ──────────────────────────────────────────────────
_BARRIER_TIMEOUT        = 10   # seconds — wait for all threads to reach the barrier
_HOLD_TIMEOUT           = 12   # seconds — wait for the holder to signal lock held
_BLOCKER_POLL_TIMEOUT   = 30   # seconds — _wait_for_blocker() polling window
_HOLDER_RELEASE_TIMEOUT = 120  # seconds — holder's release_event.wait() safety net
                                #   Must exceed _BLOCKER_POLL_TIMEOUT plus the time
                                #   the test needs to call release_event.set() after
                                #   _wait_for_blocker() returns.  Using 120 s gives
                                #   a ~90 s buffer: the holder stays alive throughout
                                #   the entire blocker-wait phase and well beyond.
_JOIN_TIMEOUT           = 35   # seconds — join threads after release_event.set()
                                #   Once the holder's release_event fires it exits
                                #   its atomic block and closes its connection quickly.
                                #   35 s is sufficient for that cleanup.
_PG_WAIT_TIMEOUT        = 10   # seconds — legacy; kept for _wait_for_pg_lock_wait
_PG_WAIT_POLL           = 0.05 # seconds — polling interval


# ── PostgreSQL lock-wait confirmation ─────────────────────────────────────────

def _wait_for_pg_lock_wait(backend_pid, timeout=_PG_WAIT_TIMEOUT,
                            poll=_PG_WAIT_POLL):
    """
    Poll pg_stat_activity until the given backend reports
    wait_event_type = 'Lock', or until timeout seconds elapse.

    DEPRECATED in favour of _wait_for_blocker(), which uses pg_blocking_pids()
    for a deterministic check of the actual blocking relationship.  Kept for
    reference; no longer called by production tests.

    Returns True if the backend entered a Lock wait within the timeout,
    False otherwise.
    """
    deadline = time.monotonic() + timeout
    sql = """
        SELECT 1
        FROM   pg_stat_activity
        WHERE  pid = %s
          AND  state = 'active'
          AND  wait_event_type = 'Lock'
    """
    while time.monotonic() < deadline:
        with connection.cursor() as cur:
            cur.execute(sql, [backend_pid])
            if cur.fetchone():
                return True
        time.sleep(poll)
    return False


def _wait_for_blocker(holder_pid, waiter_pid, timeout=_BLOCKER_POLL_TIMEOUT,
                      poll=_PG_WAIT_POLL):
    """
    Poll until holder_pid appears in pg_blocking_pids(waiter_pid).

    Returns True when the holder is demonstrably blocking the waiter at the
    PostgreSQL row-lock level, or False if the timeout expires.

    pg_blocking_pids(waiter_pid) returns False until the waiter's connection
    has actually issued its SELECT FOR UPDATE and been placed on the lock wait
    queue by PostgreSQL.  This is immune to Python-thread scheduling latency:
    even if the waiter thread is not scheduled immediately after the barrier,
    the query keeps returning False until the waiter genuinely blocks.

    The holder must remain inside its transaction for the full duration of
    this call.  Use _HOLDER_RELEASE_TIMEOUT (currently 120 s) for the
    holder's release_event.wait() to guarantee it stays alive throughout
    the _BLOCKER_POLL_TIMEOUT (30 s) polling window.
    """
    sql_check = "SELECT %s = ANY(pg_blocking_pids(%s))"
    deadline   = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cur:
            cur.execute(sql_check, [holder_pid, waiter_pid])
            row = cur.fetchone()
        if row and row[0]:
            return True
        time.sleep(poll)
    return False


def _blocker_diagnostic(holder_pid, waiter_pid, results=None,
                        threads=None):
    """
    Return a diagnostic string for use in assertion failure messages when
    _wait_for_blocker() times out.

    Reports:
      - pg_stat_activity rows for both PIDs (if still present).
      - Each worker's current result tuple, if results[] is provided.
      - Whether each worker thread is still alive, if threads[] is provided.
    """
    lines = [f'holder_pid={holder_pid}, waiter_pid={waiter_pid}']

    # --- pg_stat_activity ---
    sql = """
        SELECT pid,
               state,
               wait_event_type,
               wait_event,
               pg_blocking_pids(pid) AS blocking
        FROM   pg_stat_activity
        WHERE  pid IN (%s, %s)
        ORDER  BY pid
    """
    try:
        with connection.cursor() as cur:
            cur.execute(sql, [holder_pid, waiter_pid])
            rows = cur.fetchall()
        if rows:
            for r in rows:
                lines.append(
                    f'  pg_stat pid={r[0]}: state={r[1]}, '
                    f'wait_type={r[2]}, wait_event={r[3]}, blocking={r[4]}'
                )
        else:
            lines.append(
                '  pg_stat: no rows found — both connections may have closed '
                'before diagnosis ran (holder safety timeout too short?)'
            )
    except Exception as exc:
        lines.append(f'  pg_stat query failed: {exc}')

    # --- worker results ---
    if results is not None:
        for i, r in enumerate(results):
            lines.append(f'  results[{i}] = {r!r}')

    # --- thread liveness ---
    if threads is not None:
        for t in threads:
            lines.append(f'  thread {t.name!r}: alive={t.is_alive()}')

    return '\n'.join(lines)


# ── Worker functions ──────────────────────────────────────────────────────────

def _worker_approve_recipe(recipe_id, user_pk, note, barrier, results, idx,
                           pid_box=None):
    """
    Worker: call approve_recipe() inside its own transaction.

    If pid_box is a list, stores this worker's PostgreSQL backend PID in
    pid_box[0] before reaching the barrier so the main thread can poll
    pg_stat_activity once the holder has the lock.
    """
    from django.db import connection as c
    from apps.inventory.services import approve_recipe, RecipeConfigError
    from apps.accounts.models import CustomUser
    try:
        if pid_box is not None:
            with c.cursor() as cur:
                cur.execute('SELECT pg_backend_pid()')
                pid_box[0] = cur.fetchone()[0]
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        user = CustomUser.objects.get(pk=user_pk)
        recipe = approve_recipe(recipe_id, user, note)
        results[idx] = ('ok', recipe.pk, recipe.approved_by_id, recipe.approval_note)
    except RecipeConfigError as exc:
        results[idx] = ('config_error', str(exc))
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
    finally:
        c.close()


def _worker_retire_recipe(recipe_id, barrier, results, idx):
    """Worker: call retire_recipe()."""
    from django.db import connection as c
    from apps.inventory.services import retire_recipe, RecipeConfigError
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        recipe = retire_recipe(recipe_id)
        results[idx] = ('ok', recipe.pk)
    except RecipeConfigError as exc:
        results[idx] = ('config_error', str(exc))
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
    finally:
        c.close()


def _worker_approve_profile(profile_id, user_pk, note, barrier, results, idx):
    """Worker: call approve_packaging_profile()."""
    from django.db import connection as c
    from apps.inventory.services import approve_packaging_profile, PackagingConfigError
    from apps.accounts.models import CustomUser
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        user = CustomUser.objects.get(pk=user_pk)
        profile = approve_packaging_profile(profile_id, user, note)
        results[idx] = ('ok', profile.pk)
    except PackagingConfigError as exc:
        results[idx] = ('config_error', str(exc))
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
    finally:
        c.close()


def _worker_retire_profile(profile_id, barrier, results, idx, pid_box=None):
    """Worker: call retire_packaging_profile().

    If pid_box is a list, stores the PG backend PID before the barrier.
    """
    from django.db import connection as c
    from apps.inventory.services import retire_packaging_profile, PackagingConfigError
    try:
        if pid_box is not None:
            with c.cursor() as cur:
                cur.execute('SELECT pg_backend_pid()')
                pid_box[0] = cur.fetchone()[0]
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        profile = retire_packaging_profile(profile_id)
        results[idx] = ('ok', profile.pk)
    except PackagingConfigError as exc:
        results[idx] = ('config_error', str(exc))
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
    finally:
        c.close()


def _worker_save_rule(product_id, size, order_type, profile_id,
                      no_packaging, user_pk, barrier, results, idx,
                      existing_rule_id=None):
    """Worker: call save_packaging_rule()."""
    from django.db import connection as c
    from apps.inventory.services import save_packaging_rule, PackagingConfigError
    from apps.accounts.models import CustomUser
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        user = CustomUser.objects.get(pk=user_pk)
        rule = save_packaging_rule(
            product_id=product_id,
            size=size,
            order_type=order_type,
            profile_id=profile_id,
            no_packaging=no_packaging,
            confirmed_by=user,
            existing_rule_id=existing_rule_id,
        )
        results[idx] = ('ok', rule.pk)
    except PackagingConfigError as exc:
        results[idx] = ('config_error', str(exc))
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
    finally:
        c.close()


# ── Hold-then-release workers ─────────────────────────────────────────────────

def _worker_hold_product_lock(product_id, barrier, hold_event, release_event,
                               results, idx, holder_pid_box=None):
    """
    Acquire the Product lock and hold it until signalled.

    1. Reach barrier.
    2. SET LOCAL lock_timeout='15s' so a stalled lock times out cleanly.
    3. SELECT ... FOR UPDATE on the Product row.
    4. Record this thread's PG backend PID in holder_pid_box[0] if provided.
    5. Set hold_event — signal that the lock is held.
    6. Wait on release_event (bounded _HOLDER_RELEASE_TIMEOUT seconds).
       This must be longer than _BLOCKER_POLL_TIMEOUT so the holder stays
       alive throughout the entire blocker-detection polling window.
    7. Exit atomic() — commit, release the lock.
    """
    from django.db import connection as c, transaction as tx
    from apps.menu.models import Product
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        with tx.atomic():
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
            Product.objects.select_for_update().get(pk=product_id)
            # Record PID *after* the Product lock is held so the test
            # knows this PID is the blocker.
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
        hold_event.set()
    finally:
        c.close()


def _worker_hold_product_lock_then_retire(product_id, recipe_id,
                                          barrier, hold_event, release_event,
                                          results, idx, holder_pid_box=None):
    """
    Acquire the Product lock, signal hold_event, wait to be released, then
    call retire_recipe() - all inside one atomic transaction.

    Using the production service (not a raw ORM update) means this worker
    exercises the actual lock order and service behaviour, including the
    Product lock that retire_recipe() also acquires (which becomes a no-op
    re-acquire within the same transaction on PostgreSQL).
    """
    from django.db import connection as c, transaction as tx
    from apps.inventory.services import retire_recipe, RecipeConfigError
    from apps.menu.models import Product
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        with tx.atomic():
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
            # Hold the Product lock while the approver is blocking on it.
            Product.objects.select_for_update().get(pk=product_id)
            # Record PID *after* the Product lock is held.
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
            # Retire the recipe while still holding the transaction/lock.
            retire_recipe(recipe_id)
        results[idx] = ('ok',)
    except RecipeConfigError as exc:
        results[idx] = ('config_error', str(exc))
        hold_event.set()
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
        hold_event.set()
    finally:
        c.close()


def _worker_hold_profile_lock(profile_id, barrier, hold_event, release_event,
                               results, idx):
    """Acquire the PackagingProfile lock and hold it until signalled."""
    from django.db import connection as c, transaction as tx
    from apps.inventory.models import PackagingProfile
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        with tx.atomic():
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
            PackagingProfile.objects.select_for_update().get(pk=profile_id)
            hold_event.set()
            released = release_event.wait(timeout=_HOLDER_RELEASE_TIMEOUT)
            if not released:
                results[idx] = ('timeout',
                    f'release_event never set within {_HOLDER_RELEASE_TIMEOUT}s')
                return
        results[idx] = ('ok',)
    except Exception as exc:
        results[idx] = ('unexpected', type(exc).__name__, str(exc))
        hold_event.set()
    finally:
        c.close()


# ── Test runner helper ─────────────────────────────────────────────────────────

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


# =============================================================================
# Scenario A - Concurrent recipe approval (same product / size / scope)
# =============================================================================

class ConcurrentRecipeApprovalTests(TransactionTestCase):
    """
    Rev 3 §H.9: "Approve recipe revision -> Product -> referenced InventoryItems"

    Two transactions attempt to approve different draft recipes for the same
    (product, size, scope) simultaneously.  The Product lock serialises them.
    Only one revision can end up approved; the other must fail via the TOCTOU
    status recheck or the partial unique constraint backstop.
    """

    @_require_postgresql
    def test_concurrent_approval_same_scope_only_one_wins(self):
        """
        Two drafts for the same (product, size, scope) approved concurrently.

        Both service calls legitimately return 'ok'.  The Product lock
        serialises the two transactions:

          - The winner acquires the lock, finds no existing approved recipe
            for this scope, approves its own revision, and commits.
          - The loser acquires the lock next.  It refreshes its recipe's
            status (still draft — it is a different row).  It finds the
            winner's recipe already approved via the existing_approved query,
            retires it, and approves its own revision.  Also returns ok.

        Final database state:
          - Exactly one recipe is 'approved' (the loser's, since it ran second
            and retired the winner's).
          - The other recipe is 'retired'.
          - The partial unique index recipe_one_approved_per_scope is never
            violated because the loser retires the winner before approving.
        """
        prod = _make_product()
        ing  = _make_ingredient()
        user = _make_user()

        rec1 = _make_draft_recipe(prod, size='none', scope='all', revision=1)
        _make_recipe_item(rec1, ing)
        rec2 = _make_draft_recipe(prod, size='none', scope='all', revision=2)
        _make_recipe_item(rec2, ing)

        results = [None, None]
        barrier  = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        threads = [
            threading.Thread(
                name='approver-1',
                target=_worker_approve_recipe,
                args=(rec1.pk, user.pk, 'Concurrent approval A', barrier, results, 0),
                daemon=True,
            ),
            threading.Thread(
                name='approver-2',
                target=_worker_approve_recipe,
                args=(rec2.pk, user.pk, 'Concurrent approval B', barrier, results, 1),
                daemon=True,
            ),
        ]
        timed_out = _run_threads(threads)
        self.assertFalse(timed_out, f'Threads timed out: {timed_out}')
        _assert_no_unexpected(self, results)

        # Both service calls return ok: the second transaction retires the
        # first's approved recipe and approves its own.
        ok_count = sum(1 for r in results if r[0] == 'ok')
        self.assertEqual(ok_count, 2,
            f'Both approvals must return ok (second retires first then approves). '
            f'Results: {results}')

        from apps.inventory.models import Recipe
        # Exactly one recipe remains approved.
        approved = Recipe.objects.filter(
            product=prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_APPROVED,
        )
        self.assertEqual(approved.count(), 1,
            'Exactly one approved recipe must exist after concurrent approval.')
        # The other recipe is retired (not deleted).
        retired = Recipe.objects.filter(
            product=prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_RETIRED,
        )
        self.assertEqual(retired.count(), 1,
            'The previously approved recipe must be retired.')
        # Both rows still exist.
        self.assertEqual(Recipe.objects.filter(product=prod).count(), 2)

    @_require_postgresql
    def test_second_approval_blocked_until_first_commits(self):
        """
        The second approval attempt is genuinely blocked at the PostgreSQL
        row-level Product lock until the first transaction commits.

        Scenario:
          - Thread A (holder): acquires the Product lock, records its PG PID,
            signals hold_event, waits for the test to release it.
          - Thread B (approver): calls approve_recipe() for a draft recipe on
            the same product and blocks at the Product lock.
          - The test uses pg_blocking_pids(waiter_pid) to confirm A is
            actually blocking B at the PostgreSQL level before releasing A.
          - A commits and releases the lock.  B proceeds, acquires the lock,
            refreshes status, and approves the still-draft recipe successfully.

        Final assertions:
          - Holder returned ok.
          - Approver returned ok.
          - The draft recipe is now approved.
        """
        prod  = _make_product()
        ing   = _make_ingredient()
        user  = _make_user()

        rec1 = _make_draft_recipe(prod, size='none', scope='all', revision=1)
        _make_recipe_item(rec1, ing)

        results       = [None, None]
        barrier       = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
        hold_event    = threading.Event()
        release_event = threading.Event()
        holder_pid    = [None]
        approver_pid  = [None]

        t_holder = threading.Thread(
            name='lock-holder',
            target=_worker_hold_product_lock,
            args=(prod.pk, barrier, hold_event, release_event, results, 0),
            kwargs={'holder_pid_box': holder_pid},
            daemon=True,
        )
        t_approver = threading.Thread(
            name='approver',
            target=_worker_approve_recipe,
            args=(rec1.pk, user.pk, 'After lock released', barrier, results, 1),
            kwargs={'pid_box': approver_pid},
            daemon=True,
        )

        try:
            t_holder.start()
            t_approver.start()

            # Step 1: wait for the holder to acquire the Product lock and
            # record its PID.
            held = hold_event.wait(timeout=_HOLD_TIMEOUT)
            self.assertTrue(held, 'Holder never signalled hold_event.')

            # Step 2: wait for both PIDs to be populated.
            deadline = time.monotonic() + _HOLD_TIMEOUT
            while (holder_pid[0] is None or approver_pid[0] is None) \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(holder_pid[0],
                'Holder did not record its PG backend PID in time.')
            self.assertIsNotNone(approver_pid[0],
                'Approver did not record its PG backend PID in time.')

            # Step 3: confirm the holder is actually blocking the approver at
            # the PostgreSQL level using pg_blocking_pids().
            # This is immune to scheduler latency: it is True only when the
            # approver has reached its SELECT FOR UPDATE and is blocked by the
            # holder's transaction.  The holder stays alive for
            # _HOLDER_RELEASE_TIMEOUT (120 s), which far exceeds the
            # _BLOCKER_POLL_TIMEOUT (30 s) polling window.
            blocking = _wait_for_blocker(holder_pid[0], approver_pid[0])
            self.assertTrue(
                blocking,
                'pg_blocking_pids() did not confirm holder is blocking approver '
                f'within {_BLOCKER_POLL_TIMEOUT} s.\n'
                + _blocker_diagnostic(
                    holder_pid[0], approver_pid[0],
                    results=results,
                    threads=[t_holder, t_approver],
                ),
            )

            # Step 4: release the holder - it commits and releases the lock.
            release_event.set()

        finally:
            release_event.set()
            t_holder.join(timeout=_JOIN_TIMEOUT)
            t_approver.join(timeout=_JOIN_TIMEOUT)

        self.assertFalse(t_holder.is_alive(), 'Holder thread did not exit.')
        self.assertFalse(t_approver.is_alive(), 'Approver thread did not exit.')

        _assert_no_unexpected(self, results)
        self.assertEqual(results[0][0], 'ok',
            f'Holder had unexpected result: {results[0]}')
        self.assertEqual(results[1][0], 'ok',
            f'Approver should have succeeded but got: {results[1]}')

        from apps.inventory.models import Recipe
        rec1.refresh_from_db()
        self.assertEqual(rec1.status, Recipe.STATUS_APPROVED)


# =============================================================================
# Scenario B - Concurrent mixed-scope recipe approval
# =============================================================================

class ConcurrentMixedScopeRecipeTests(TransactionTestCase):
    """
    Rev 3 §D.4: "No mixed scopes ... Enforced at approval."

    Two transactions simultaneously approve an 'all' scope recipe and a
    'dine_in' scope recipe for the same (product, size).  The Product lock
    serialises them; the mixed-scope guard inside approve_recipe() rejects
    the losing transaction.
    """

    @_require_postgresql
    def test_all_and_dine_in_concurrent_approval_one_rejected(self):
        """
        Approving scope='all' and scope='dine_in' concurrently for the same
        (product, size): exactly one must be approved; the other rejected.

        The Product lock serialises the two transactions.  The winner commits;
        the loser detects the conflict via the mixed-scope check performed
        under the Product lock and raises RecipeConfigError.
        """
        prod  = _make_product()
        ing   = _make_ingredient()
        user  = _make_user()

        rec_all    = _make_draft_recipe(prod, size='none', scope='all',    revision=1)
        rec_dine_in = _make_draft_recipe(prod, size='none', scope='dine_in', revision=1)
        _make_recipe_item(rec_all,     ing)
        _make_recipe_item(rec_dine_in, ing)

        results = [None, None]
        barrier  = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        threads = [
            threading.Thread(
                name='approve-all',
                target=_worker_approve_recipe,
                args=(rec_all.pk, user.pk, 'All-scope approval', barrier, results, 0),
                daemon=True,
            ),
            threading.Thread(
                name='approve-dine-in',
                target=_worker_approve_recipe,
                args=(rec_dine_in.pk, user.pk, 'Dine-in approval', barrier, results, 1),
                daemon=True,
            ),
        ]
        timed_out = _run_threads(threads)
        self.assertFalse(timed_out, f'Threads timed out: {timed_out}')
        _assert_no_unexpected(self, results)

        ok_count    = sum(1 for r in results if r[0] == 'ok')
        error_count = sum(1 for r in results if r[0] == 'config_error')
        self.assertEqual(ok_count, 1,
            f'Expected exactly 1 approval to succeed; got {ok_count}. '
            f'Results: {results}')
        self.assertEqual(error_count, 1,
            f'Expected exactly 1 config_error; got {error_count}. '
            f'Results: {results}')

        from apps.inventory.models import Recipe
        approved_q = Recipe.objects.filter(
            product=prod, size='none', status=Recipe.STATUS_APPROVED,
        )
        self.assertEqual(approved_q.count(), 1,
            'Exactly one approved recipe must exist after concurrent '
            'mixed-scope attempt.')

        # The winning recipe must have no other approved recipe for same size.
        winning = approved_q.first()
        conflicting = Recipe.objects.filter(
            product=prod, size='none', status=Recipe.STATUS_APPROVED,
        ).exclude(pk=winning.pk)
        self.assertEqual(conflicting.count(), 0,
            'No conflicting approved recipe must exist.')

    @_require_postgresql
    def test_dine_in_and_takeout_coexist_concurrently(self):
        """
        Approving scope='dine_in' and scope='takeout' concurrently for the
        same (product, size) must allow both to succeed.

        Rev 3 §D.4: {dine_in, takeout} is a valid approved set.  Both share
        the same Product lock, so they serialise, but neither conflicts with
        the other's scope.
        """
        prod  = _make_product()
        ing   = _make_ingredient()
        user  = _make_user()

        rec_d = _make_draft_recipe(prod, size='none', scope='dine_in', revision=1)
        rec_t = _make_draft_recipe(prod, size='none', scope='takeout',  revision=1)
        _make_recipe_item(rec_d, ing)
        _make_recipe_item(rec_t, ing)

        results = [None, None]
        barrier  = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        threads = [
            threading.Thread(
                name='approve-dine-in',
                target=_worker_approve_recipe,
                args=(rec_d.pk, user.pk, 'Dine-in approved', barrier, results, 0),
                daemon=True,
            ),
            threading.Thread(
                name='approve-takeout',
                target=_worker_approve_recipe,
                args=(rec_t.pk, user.pk, 'Takeout approved', barrier, results, 1),
                daemon=True,
            ),
        ]
        timed_out = _run_threads(threads)
        self.assertFalse(timed_out, f'Threads timed out: {timed_out}')
        _assert_no_unexpected(self, results)

        ok_count = sum(1 for r in results if r[0] == 'ok')
        self.assertEqual(ok_count, 2,
            f'Both dine_in and takeout approvals must succeed. '
            f'Results: {results}')

        from apps.inventory.models import Recipe
        approved = Recipe.objects.filter(
            product=prod, size='none', status=Recipe.STATUS_APPROVED,
        )
        self.assertEqual(approved.count(), 2)
        scopes = set(approved.values_list('order_type_scope', flat=True))
        self.assertEqual(scopes, {'dine_in', 'takeout'})


# =============================================================================
# Scenario C - Recipe approval versus retirement (status staleness)
# =============================================================================

class ConcurrentApprovalAndRetirementTests(TransactionTestCase):
    """
    Rev 3 §H.9 TOCTOU fix: status is rechecked after the Product lock.

    Tests that services detect status changes that occurred while they were
    waiting for the Product lock, and either succeed with the correct
    post-lock state or raise RecipeConfigError.
    """

    @_require_postgresql
    def test_double_retire_only_one_succeeds(self):
        """
        Two concurrent retire_recipe() calls on the same approved recipe:
        exactly one succeeds; the second is rejected by the post-lock
        status recheck (which finds 'retired' after the winner committed).
        """
        prod = _make_product()
        ing  = _make_ingredient()
        rec  = _make_approved_recipe(prod, size='none', scope='all', revision=1)
        _make_recipe_item(rec, ing)

        results = [None, None]
        barrier  = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        threads = [
            threading.Thread(
                name='retire-1',
                target=_worker_retire_recipe,
                args=(rec.pk, barrier, results, 0),
                daemon=True,
            ),
            threading.Thread(
                name='retire-2',
                target=_worker_retire_recipe,
                args=(rec.pk, barrier, results, 1),
                daemon=True,
            ),
        ]
        timed_out = _run_threads(threads)
        self.assertFalse(timed_out, f'Threads timed out: {timed_out}')
        _assert_no_unexpected(self, results)

        ok_count = sum(1 for r in results if r[0] == 'ok')
        self.assertEqual(ok_count, 1,
            f'Exactly one retirement must succeed. Results: {results}')

        from apps.inventory.models import Recipe
        rec.refresh_from_db()
        self.assertEqual(rec.status, Recipe.STATUS_RETIRED)

    @_require_postgresql
    def test_approve_while_retire_holds_product_lock(self):
        """
        approve_recipe() for a draft revision is blocked on the Product lock
        while retire_recipe() for the previously approved revision holds it.
        Once the holder commits the retirement, the approver acquires the lock,
        refreshes status, and proceeds to approve the draft successfully.

        Scenario:
          - rec_approved: already approved (revision 1, scope='all').
          - rec_draft:    a separate draft (revision 2, scope='all').

        Thread A (holder): acquires the Product lock, signals hold_event,
          waits to be released, then calls retire_recipe(rec_approved) while
          still inside the same atomic block, and commits.

        Thread B (approver): calls approve_recipe(rec_draft) and blocks on
          the Product lock.

        The test polls pg_stat_activity to confirm B is genuinely waiting on
        the Product lock before releasing A.

        After A commits:
          - rec_approved is retired.
          - B acquires the lock, refreshes rec_draft's status (still draft),
            finds no conflicting approved recipe (rec_approved is now retired),
            and approves rec_draft.

        Final state:
          - rec_approved: retired.
          - rec_draft: approved.
          - Holder returned ok.
          - Approver returned ok.
        """
        prod  = _make_product()
        ing   = _make_ingredient()
        user  = _make_user()

        rec_approved = _make_approved_recipe(prod, size='none', scope='all', revision=1)
        _make_recipe_item(rec_approved, ing)
        rec_draft = _make_draft_recipe(prod, size='none', scope='all', revision=2)
        _make_recipe_item(rec_draft, ing)

        results       = [None, None]
        barrier       = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
        hold_event    = threading.Event()
        release_event = threading.Event()
        approver_pid  = [None]

        holder_pid = [None]

        t_holder = threading.Thread(
            name='retire-holder',
            target=_worker_hold_product_lock_then_retire,
            args=(prod.pk, rec_approved.pk,
                  barrier, hold_event, release_event, results, 0),
            kwargs={'holder_pid_box': holder_pid},
            daemon=True,
        )
        t_approver = threading.Thread(
            name='approver',
            target=_worker_approve_recipe,
            args=(rec_draft.pk, user.pk, 'After retire commits', barrier, results, 1),
            kwargs={'pid_box': approver_pid},
            daemon=True,
        )

        try:
            t_holder.start()
            t_approver.start()

            # Step 1: wait for the holder to acquire the Product lock and
            # record its PID.
            held = hold_event.wait(timeout=_HOLD_TIMEOUT)
            self.assertTrue(held,
                'Holder never signalled hold_event - did not acquire Product lock.')

            # Step 2: wait for both PIDs to be populated.
            deadline = time.monotonic() + _HOLD_TIMEOUT
            while (holder_pid[0] is None or approver_pid[0] is None) \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(holder_pid[0],
                'Holder did not record its PG backend PID in time.')
            self.assertIsNotNone(approver_pid[0],
                'Approver did not record its PG backend PID in time.')

            # Step 3: confirm the holder is actually blocking the approver.
            blocking = _wait_for_blocker(holder_pid[0], approver_pid[0])
            self.assertTrue(
                blocking,
                'pg_blocking_pids() did not confirm holder is blocking approver '
                f'within {_BLOCKER_POLL_TIMEOUT} s.\n'
                + _blocker_diagnostic(
                    holder_pid[0], approver_pid[0],
                    results=results,
                    threads=[t_holder, t_approver],
                ),
            )

            # Step 4: release the holder - it retires rec_approved and commits.
            release_event.set()

        finally:
            release_event.set()
            t_holder.join(timeout=_JOIN_TIMEOUT)
            t_approver.join(timeout=_JOIN_TIMEOUT)

        self.assertFalse(t_holder.is_alive(), 'Holder thread did not exit.')
        self.assertFalse(t_approver.is_alive(), 'Approver thread did not exit.')

        _assert_no_unexpected(self, results)

        self.assertEqual(results[0][0], 'ok',
            f'Holder (retire rec_approved) should have succeeded: {results[0]}')
        self.assertEqual(results[1][0], 'ok',
            f'Approver (approve rec_draft) should have succeeded: {results[1]}')

        from apps.inventory.models import Recipe
        rec_approved.refresh_from_db()
        rec_draft.refresh_from_db()
        self.assertEqual(rec_approved.status, Recipe.STATUS_RETIRED,
            'Previously approved recipe must be retired after the holder committed.')
        self.assertEqual(rec_draft.status, Recipe.STATUS_APPROVED,
            'Draft recipe must be approved after the approver acquired the lock.')


# =============================================================================
# Scenario D - Concurrent packaging profile approval (same name)
# =============================================================================

class ConcurrentProfileApprovalTests(TransactionTestCase):
    """
    Rev 3 §H.9: "Approve a new packaging profile revision -> PackagingProfile"

    Two transactions attempt to approve two revisions of the same profile
    name simultaneously.  The PackagingProfile lock (select_for_update on
    each profile row) serialises them; the partial unique index
    pp_one_approved_per_name is the DB-level backstop.
    """

    @_require_postgresql
    def test_concurrent_same_name_profile_approval_only_one_wins(self):
        """
        Two draft profiles with the same name (different revisions) approved
        concurrently: at most one becomes approved.

        The approve_packaging_profile() service locks each profile row
        individually.  Two drafts with the same name but different PKs have
        different row locks, so both transactions can proceed past their own
        profile lock.  The partial unique index pp_one_approved_per_name
        (case-insensitive) is the constraint backstop that prevents both from
        committing as approved.
        """
        profile_name = f'ConcProfile_{_uid()}'
        pkg  = _make_packaging_inv()
        user = _make_user()

        prof1 = _make_draft_profile(name=profile_name, revision=1)
        _make_profile_item(prof1, pkg)
        prof2 = _make_draft_profile(name=profile_name, revision=2)
        _make_profile_item(prof2, pkg)

        results = [None, None]
        barrier  = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        threads = [
            threading.Thread(
                name='approve-prof-1',
                target=_worker_approve_profile,
                args=(prof1.pk, user.pk, 'Revision 1 approved', barrier, results, 0),
                daemon=True,
            ),
            threading.Thread(
                name='approve-prof-2',
                target=_worker_approve_profile,
                args=(prof2.pk, user.pk, 'Revision 2 approved', barrier, results, 1),
                daemon=True,
            ),
        ]
        timed_out = _run_threads(threads)
        self.assertFalse(timed_out, f'Threads timed out: {timed_out}')
        _assert_no_unexpected(self, results)

        from apps.inventory.models import PackagingProfile
        approved = PackagingProfile.objects.filter(
            name=profile_name, status=PackagingProfile.STATUS_APPROVED,
        )
        self.assertLessEqual(approved.count(), 1,
            f'At most one approved profile for the same name must exist. '
            f'Found {approved.count()}. Results: {results}')

        ok_count = sum(1 for r in results if r[0] == 'ok')
        self.assertLessEqual(ok_count, 1,
            f'At most one approval should succeed. Results: {results}')


# =============================================================================
# Scenario E - Profile retirement vs rule save (profile lock)
# =============================================================================

class ProfileRetireVsRuleSaveTests(TransactionTestCase):
    """
    Rev 3 §H.9:
      "Retire a packaging profile -> PackagingProfile (lock)"
      "Create/change/delete ProductPackagingRule -> Product -> PackagingProfile"

    The PackagingProfile lock serialises a retirement attempt against a
    concurrent rule-save that points to the same profile.  The outcome
    depends on which transaction wins the profile lock first.
    """

    @_require_postgresql
    def test_retire_while_rule_save_holds_profile_lock(self):
        """
        retire_packaging_profile() must block on the PackagingProfile lock
        while save_packaging_rule() holds it (via the Product -> Profile lock
        order), see the profile is referenced after the lock is released, and
        raise PackagingConfigError.

        Scenario (rule-save wins the lock):
          1. save_packaging_rule() holds Product then Profile lock.
          2. retire_packaging_profile() blocks on the Profile lock.
          3. save_packaging_rule() creates the rule and commits.
          4. retire_packaging_profile() acquires the lock, counts the new rule,
             and raises PackagingConfigError.

        If retirement wins the profile lock first (the other valid outcome),
        rule-save detects the retired profile and raises PackagingConfigError.
        Both outcomes are asserted correctly.
        """
        prod  = _make_product()
        prof  = _make_approved_profile()
        user  = _make_user()

        results = [None, None]
        barrier  = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        threads = [
            threading.Thread(
                name='save-rule',
                target=_worker_save_rule,
                args=(prod.pk, 'none', 'takeout', prof.pk, False, user.pk,
                      barrier, results, 0),
                daemon=True,
            ),
            threading.Thread(
                name='retire-profile',
                target=_worker_retire_profile,
                args=(prof.pk, barrier, results, 1),
                daemon=True,
            ),
        ]
        timed_out = _run_threads(threads)
        self.assertFalse(timed_out, f'Threads timed out: {timed_out}')
        _assert_no_unexpected(self, results)

        from apps.inventory.models import PackagingProfile, ProductPackagingRule
        prof.refresh_from_db()
        rule_exists = ProductPackagingRule.objects.filter(
            product=prod, profile=prof, size='none', order_type='takeout',
        ).exists()

        if rule_exists:
            # Rule-save won: profile remains approved, retirement rejected.
            self.assertEqual(prof.status, PackagingProfile.STATUS_APPROVED,
                'Profile must remain approved when a rule references it.')
            self.assertEqual(results[1][0], 'config_error',
                'Retirement must be rejected when a rule references the profile.')
        else:
            # Retirement won: profile retired, rule-save rejected.
            self.assertEqual(prof.status, PackagingProfile.STATUS_RETIRED,
                'Profile must be retired when no rule references it.')
            self.assertEqual(results[0][0], 'config_error',
                'Rule save must be rejected when the profile is retired.')

    @_require_postgresql
    def test_concurrent_rule_saves_same_combination_only_one_wins(self):
        """
        Two concurrent save_packaging_rule() calls for the same
        (product, size, order_type): exactly one rule must be created.

        Rev 3 §H.9: Product lock serialises writers.
        DB constraint ppr_unique_combination is the final backstop.
        """
        prod  = _make_product()
        prof1 = _make_approved_profile()
        prof2 = _make_approved_profile()
        user  = _make_user()

        results = [None, None]
        barrier  = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        threads = [
            threading.Thread(
                name='rule-save-1',
                target=_worker_save_rule,
                args=(prod.pk, 'none', 'takeout', prof1.pk, False, user.pk,
                      barrier, results, 0),
                daemon=True,
            ),
            threading.Thread(
                name='rule-save-2',
                target=_worker_save_rule,
                args=(prod.pk, 'none', 'takeout', prof2.pk, False, user.pk,
                      barrier, results, 1),
                daemon=True,
            ),
        ]
        timed_out = _run_threads(threads)
        self.assertFalse(timed_out, f'Threads timed out: {timed_out}')
        _assert_no_unexpected(self, results)

        from apps.inventory.models import ProductPackagingRule
        rules = ProductPackagingRule.objects.filter(
            product=prod, size='none', order_type='takeout',
        )
        self.assertEqual(rules.count(), 1,
            f'Exactly one rule must exist. Found {rules.count()}. '
            f'Results: {results}')

        ok_count = sum(1 for r in results if r[0] == 'ok')
        self.assertEqual(ok_count, 1,
            f'Exactly one rule save must succeed. Results: {results}')


# =============================================================================
# Scenario F - Profile lock: retirement blocked while lock is held
# =============================================================================

class ProfileLockTests(TransactionTestCase):
    """
    Rev 3 §H.9: the PackagingProfile lock prevents retirement from racing
    against a rule-save that holds the profile lock.

    Verifies actual PostgreSQL lock blocking using pg_stat_activity.
    """

    @_require_postgresql
    def test_retire_blocks_on_profile_lock_held_by_save_rule(self):
        """
        retire_packaging_profile() is genuinely blocked at the PostgreSQL
        PackagingProfile row lock while a rule-save holds it.  After the
        rule-save commits (creating the rule), retirement sees the reference
        and raises PackagingConfigError.

        The pg_stat_activity check proves the retirer is in a real PostgreSQL
        Lock wait before the holder is released.
        """
        prod  = _make_product()
        prof  = _make_approved_profile()
        user  = _make_user()

        results       = [None, None]
        hold_event    = threading.Event()
        release_event = threading.Event()
        barrier       = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
        retirer_pid   = [None]
        saver_pid     = [None]

        def _save_rule_then_hold(prod_id, prof_id, user_pk,
                                 barrier_, hold_ev, release_ev,
                                 results_, idx, saver_pid_box):
            """
            Inside one atomic transaction: lock Product + Profile, create the
            rule, record the saver's PG PID, signal hold_event, hold the
            locks until released.  Exercises H.9 lock order (Product -> Profile).
            """
            from django.db import connection as c, transaction as tx
            from apps.inventory.models import PackagingProfile, ProductPackagingRule
            from apps.menu.models import Product
            from apps.accounts.models import CustomUser
            try:
                barrier_.wait(timeout=_BARRIER_TIMEOUT)
                with tx.atomic():
                    with c.cursor() as cur:
                        cur.execute("SET LOCAL lock_timeout = '15s'")
                    # Lock in H.9 order: Product first, then PackagingProfile.
                    Product.objects.select_for_update().get(pk=prod_id)
                    PackagingProfile.objects.select_for_update().get(pk=prof_id)
                    ProductPackagingRule.objects.create(
                        product_id=prod_id,
                        size='none',
                        order_type='dine_in',
                        profile_id=prof_id,
                        no_packaging=False,
                        confirmed_by=CustomUser.objects.get(pk=user_pk),
                    )
                    # Record PID *after* both locks are held.
                    with c.cursor() as cur:
                        cur.execute('SELECT pg_backend_pid()')
                        saver_pid_box[0] = cur.fetchone()[0]
                    hold_ev.set()
                    released = release_ev.wait(timeout=_HOLDER_RELEASE_TIMEOUT)
                    if not released:
                        results_[idx] = ('timeout',
                            f'release_event not set within {_HOLDER_RELEASE_TIMEOUT}s')
                        return
                results_[idx] = ('ok',)
            except Exception as exc:
                results_[idx] = ('unexpected', type(exc).__name__, str(exc))
                hold_ev.set()
            finally:
                c.close()

        t_saver   = threading.Thread(
            name='saver',
            target=_save_rule_then_hold,
            args=(prod.pk, prof.pk, user.pk,
                  barrier, hold_event, release_event, results, 0, saver_pid),
            daemon=True,
        )
        t_retirer = threading.Thread(
            name='retirer',
            target=_worker_retire_profile,
            args=(prof.pk, barrier, results, 1),
            kwargs={'pid_box': retirer_pid},
            daemon=True,
        )

        try:
            t_saver.start()
            t_retirer.start()

            # Step 1: wait for the saver to hold both locks and record its PID.
            held = hold_event.wait(timeout=_HOLD_TIMEOUT)
            self.assertTrue(held, 'Saver never signalled hold_event.')

            # Step 2: wait for both PIDs to be populated.
            deadline = time.monotonic() + _HOLD_TIMEOUT
            while (saver_pid[0] is None or retirer_pid[0] is None) \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(saver_pid[0],
                'Saver did not record its PG backend PID in time.')
            self.assertIsNotNone(retirer_pid[0],
                'Retirer did not record its PG backend PID in time.')

            # Step 3: confirm the saver is actually blocking the retirer at
            # the PostgreSQL level using pg_blocking_pids().
            blocking = _wait_for_blocker(saver_pid[0], retirer_pid[0])
            self.assertTrue(
                blocking,
                'pg_blocking_pids() did not confirm saver is blocking retirer '
                f'within {_BLOCKER_POLL_TIMEOUT} s.\n'
                + _blocker_diagnostic(
                    saver_pid[0], retirer_pid[0],
                    results=results,
                    threads=[t_saver, t_retirer],
                ),
            )

            # Step 4: release the saver - it commits (creates the rule).
            release_event.set()

        finally:
            release_event.set()
            t_saver.join(timeout=_JOIN_TIMEOUT)
            t_retirer.join(timeout=_JOIN_TIMEOUT)

        self.assertFalse(t_saver.is_alive(), 'Saver thread did not exit.')
        self.assertFalse(t_retirer.is_alive(), 'Retirer thread did not exit.')
        _assert_no_unexpected(self, results)

        self.assertEqual(results[0][0], 'ok',
            f'Rule-saver should succeed: {results[0]}')
        self.assertEqual(results[1][0], 'config_error',
            f'Retirement must be rejected when a rule references the profile: '
            f'{results[1]}')

        from apps.inventory.models import PackagingProfile
        prof.refresh_from_db()
        self.assertEqual(prof.status, PackagingProfile.STATUS_APPROVED,
            'Profile must remain approved because a rule references it.')


# =============================================================================
# Scenario G - Approved profile immutability under concurrency
# =============================================================================

class ApprovedProfileImmutabilityTests(TransactionTestCase):
    """
    Rev 3 §D.6: "Immutable once approved, same lifecycle as recipes."

    Concurrent re-approval attempts on an already-approved profile must both
    fail because the profile is not in draft status.
    """

    @_require_postgresql
    def test_re_approving_already_approved_profile_rejected(self):
        """
        Two concurrent approve_packaging_profile() calls on the same already-
        approved profile: both must be rejected with a config_error.

        The PackagingProfile select_for_update serialises them.  Both find
        status != 'draft' after acquiring the lock and raise PackagingConfigError.

        Final state: profile remains approved and unchanged.
        """
        pkg  = _make_packaging_inv()
        user = _make_user()

        prof = _make_approved_profile()
        _make_profile_item(prof, pkg)

        results = [None, None]
        barrier  = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

        threads = [
            threading.Thread(
                name='re-approve-1',
                target=_worker_approve_profile,
                args=(prof.pk, user.pk, 'Re-approve attempt 1', barrier, results, 0),
                daemon=True,
            ),
            threading.Thread(
                name='re-approve-2',
                target=_worker_approve_profile,
                args=(prof.pk, user.pk, 'Re-approve attempt 2', barrier, results, 1),
                daemon=True,
            ),
        ]
        timed_out = _run_threads(threads)
        self.assertFalse(timed_out, f'Threads timed out: {timed_out}')
        _assert_no_unexpected(self, results)

        for i, r in enumerate(results):
            self.assertEqual(r[0], 'config_error',
                f'Thread {i} should be rejected (profile not draft): {r}')

        from apps.inventory.models import PackagingProfile
        prof.refresh_from_db()
        self.assertEqual(prof.status, PackagingProfile.STATUS_APPROVED)


# =============================================================================
# Deferred scenarios
# =============================================================================
#
# DEFERRED-1: Recipe approval racing place_order_inventory
#   Phase 5B: place_order_inventory not yet implemented.
#
# DEFERRED-2: InventoryItem deactivation racing approval
#   Phase 5B: deactivation guard service not yet implemented.
#
# DEFERRED-3: Product mode flip racing order placement
#   Phase 8: cutover service not yet implemented.
#
# DEFERRED-4: Concurrent reservation + cancellation
#   Phase 5B/6B: OrderIngredientReservation not yet implemented.
#
# DEFERRED-5: Mixed legacy+ingredient rollback under concurrency
#   Phase 5A/5B: coordinator not yet implemented.
