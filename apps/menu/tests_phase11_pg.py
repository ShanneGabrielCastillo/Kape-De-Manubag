"""Phase 1.1 — PostgreSQL-only concurrency tests for the cutover service.

Verifies, against real SELECT FOR UPDATE row locks, that:

  A. two concurrent cutover attempts on the same product serialise —
     exactly one wins, the loser revalidates under the lock and is
     rejected with "already ingredient-tracked", and exactly one audit
     row exists;
  B. an order placement racing a cutover cannot observe a torn state —
     the placement's mode re-snapshot runs under the same Product lock,
     so every order line is either fully legacy or fully ingredient and
     the reservation (if any) matches that mode;
  C. a reservation that commits before the cutover's revalidation blocks
     the cutover (gate 10), leaving every product mode unchanged;
  D. two concurrent multi-product cutovers whose plans share inventory
     items cannot deadlock — both lock Products (ascending) then
     InventoryItems (ascending, shared helper), so they serialise;
  E. a failure part-way through a multi-product cutover rolls back every
     product and writes no audit rows (atomicity under concurrency).

Why PostgreSQL only
-------------------
SQLite serialises all writes with a single database-level lock — SELECT
FOR UPDATE is silently accepted but ignored, so a "concurrency" test run
against SQLite would only prove sequential execution.  These tests skip
themselves when the default database is not PostgreSQL, following the
same convention as apps/inventory/tests_phase2a_pg.py and
apps/orders/tests_phase5b7_pg.py.

Running them
------------
Use the PostgreSQL-only settings module, which fails at import time unless the
five POSTGRES_* variables are set (and never falls back to SQLite):

    python manage.py test apps.menu.tests_phase11_pg \
        --settings=kape_de_manubag.test_postgres_settings --verbosity=2

No PostgreSQL server is required to collect the module — every test
skips with a clear message on any other backend.  A CI run that intends to
prove PostgreSQL concurrency must set ``KDM_REQUIRE_POSTGRESQL=1``: the tests
then FAIL instead of skipping when the backend is not PostgreSQL, so a
misdirected workflow can never turn five skips into a green run.
"""
import functools
import os
import threading
import time
from decimal import Decimal

from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.inventory.models import (
    InventoryItem,
    PackagingProfile,
    PackagingProfileItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.menu.models import Category, Product
from apps.menu.services import (
    CUTOVER_AUDIT_ACTION,
    TrackingModeChangeError,
    change_inventory_tracking_mode,
)
from apps.orders.models import (
    Order,
    OrderIngredientReservation,
    OrderIngredientReservationItem,
    OrderItem,
)

_BARRIER_TIMEOUT = 10.0
_JOIN_TIMEOUT = 30.0

# When set to "1", a non-PostgreSQL backend is a test FAILURE rather than a
# skip.  The dedicated PostgreSQL CI workflow sets this so five silent skips
# can never masquerade as a successful concurrency verification.
_REQUIRE_POSTGRESQL = os.environ.get('KDM_REQUIRE_POSTGRESQL') == '1'


def _require_postgresql(test_func):
    """Decorator: skip unless the default database is PostgreSQL.

    With ``KDM_REQUIRE_POSTGRESQL=1`` the skip becomes an explicit assertion
    failure, proving that the test runner really is on PostgreSQL.
    """
    @functools.wraps(test_func)
    def wrapper(self, *args, **kwargs):
        if connection.vendor != 'postgresql':
            message = (
                'PostgreSQL required for real SELECT FOR UPDATE concurrency '
                f'tests. Current backend: {connection.vendor}.'
            )
            if _REQUIRE_POSTGRESQL:
                raise AssertionError(
                    message + ' KDM_REQUIRE_POSTGRESQL=1 forbids skipping; '
                    'the test runner is not using PostgreSQL.'
                )
            self.skipTest(message)
        return test_func(self, *args, **kwargs)
    return wrapper


def _uid():
    return str(time.monotonic_ns())


def _make_ready_product(name, on_hand='1000.000', mode='legacy'):
    """A fully cutover-ready product: approved recipes for every configured
    size plus both packaging rules (no_packaging)."""
    category = Category.objects.create(
        name=f'P11 Cat {name} {_uid()}', slug=f'p11-{name}-{_uid()}')
    product = Product.objects.create(
        category=category, name=name, price='50.00',
        stock_quantity=10, inventory_tracking_mode=mode,
    )
    ingredient = InventoryItem.objects.create(
        name=f'P11 Ingr {name} {_uid()}', item_type='ingredient',
        base_unit='g', on_hand_quantity=Decimal(on_hand),
    )
    for size in ('none',):   # unsized product: single configured size
        recipe = Recipe.objects.create(
            product=product, size=size, order_type_scope=Recipe.SCOPE_ALL,
            status=Recipe.STATUS_APPROVED, revision=1,
            approved_at=timezone.now(), approval_note='P11 PG fixture',
        )
        RecipeItem.objects.create(
            recipe=recipe, inventory_item=ingredient,
            quantity=Decimal('10.000'),
        )
        for order_type in ('dine_in', 'takeout'):
            ProductPackagingRule.objects.create(
                product=product, size=size, order_type=order_type,
                no_packaging=True,
            )
    return product, ingredient


def _run_in_thread(target, *args):
    """Start a daemon thread and return it, with result box conventions:
    the target receives a ``results`` list and writes (ok, value_or_exc)."""
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    return t


def _worker_cutover(product_ids, barrier, results, index, actor_name):
    """Open its own connection, meet the barrier, attempt the cutover."""
    from apps.accounts.models import CustomUser
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        with transaction.atomic():
            actor = CustomUser.objects.filter(
                username=actor_name).first()
            result = change_inventory_tracking_mode(
                product_ids, 'ingredient', actor=actor,
                reason=f'pg-worker-{index}')
        results[index] = (True, result)
    except Exception as exc:   # noqa: BLE001 — recorded for assertions
        results[index] = (False, exc)
    finally:
        connection.close()


class ConcurrentCutoverSameProductTests(TransactionTestCase):
    """Scenario A: two concurrent cutover attempts on the same product."""

    @_require_postgresql
    def test_exactly_one_wins_one_audit_row(self):
        from apps.accounts.models import CustomUser
        actor = CustomUser.objects.create_user(
            username=f'p11a_{_uid()}', password='x', role='admin')
        product, _ingredient = _make_ready_product('Alpha')

        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
        results = [None, None]
        t1 = _run_in_thread(_worker_cutover, [product.pk], barrier,
                            results, 0, actor.username)
        t2 = _run_in_thread(_worker_cutover, [product.pk], barrier,
                            results, 1, actor.username)
        t1.join(timeout=_JOIN_TIMEOUT)
        t2.join(timeout=_JOIN_TIMEOUT)

        outcomes = [ok for ok, _ in results]
        self.assertEqual(sum(outcomes), 1,
                         f'expected exactly one winner, got {results}')
        product.refresh_from_db()
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')
        self.assertEqual(
            AuditLog.objects.filter(action=CUTOVER_AUDIT_ACTION).count(), 1,
            'duplicate cutover audit rows')
        loser_exc = results[0][1] if not results[0][0] else results[1][1]
        self.assertIsInstance(loser_exc, TrackingModeChangeError)
        self.assertIn('already', str(loser_exc))


class CutoverRacingOrderPlacementTests(TransactionTestCase):
    """Scenario B: an order placement racing a cutover on the same product.

    The placement coordinator locks the Order row first, then Product rows
    (ascending), then InventoryItems (ascending) — the same hierarchy the
    cutover uses minus the Order lock, so the two can only serialise on the
    Product row, never deadlock.  Whichever wins, the order's lines carry a
    single consistent inventory_mode snapshot because placement re-reads
    the mode under the Product lock (apps/orders/services.py Step 4).
    """

    @_require_postgresql
    def test_order_mode_snapshot_is_consistent(self):
        from apps.accounts.models import CustomUser
        actor = CustomUser.objects.create_user(
            username=f'p11b_{_uid()}', password='x', role='admin')
        product, ingredient = _make_ready_product('Bravo')

        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
        results = [None, None]

        def _worker_place(barrier, results):
            from apps.orders.services import place_order_inventory
            try:
                barrier.wait(timeout=_BARRIER_TIMEOUT)
                with transaction.atomic():
                    order = Order.objects.create(
                        customer_name='P11 PG placement',
                        order_type='dine_in', status='awaiting_payment',
                    )
                    OrderItem.objects.create(
                        order=order, product=product,
                        product_name=product.name,
                        category_name=product.category.name,
                        packaging_eligible=False, size='none', quantity=1,
                        unit_price=Decimal('50.00'),
                        subtotal=Decimal('50.00'),
                        inventory_mode='legacy',
                    )
                    place_order_inventory(order.pk)
                results[0] = (True, order.pk)
            except Exception as exc:   # noqa: BLE001
                results[0] = (False, exc)
            finally:
                connection.close()

        def _worker_cut(barrier, results):
            try:
                barrier.wait(timeout=_BARRIER_TIMEOUT)
                with transaction.atomic():
                    change_inventory_tracking_mode(
                        [product.pk], 'ingredient', actor=actor,
                        reason='pg-race-b')
                results[1] = (True, None)
            except Exception as exc:   # noqa: BLE001
                results[1] = (False, exc)
            finally:
                connection.close()

        t_place = _run_in_thread(_worker_place, barrier, results)
        t_cut = _run_in_thread(_worker_cut, barrier, results)
        t_place.join(timeout=_JOIN_TIMEOUT)
        t_cut.join(timeout=_JOIN_TIMEOUT)

        # Both operations must terminate (no deadlock): a hung thread is a
        # failure even if the join did not raise.
        self.assertFalse(t_place.is_alive(), 'placement thread deadlocked')
        self.assertFalse(t_cut.is_alive(), 'cutover thread deadlocked')

        product.refresh_from_db()
        if results[0][0]:
            order = Order.objects.get(pk=results[0][1])
            modes = set(order.items.values_list('inventory_mode', flat=True))
            self.assertEqual(len(modes), 1,
                             f'torn snapshot: order lines carry {modes}')
            self.assertEqual(
                modes == {'ingredient'},
                product.inventory_tracking_mode == 'ingredient'
                and OrderIngredientReservation.objects.filter(
                    order=order).exists(),
                'reservation present iff the order committed as ingredient')

        # Whichever committed last determines the product mode; both
        # outcomes are legal, but the state must be internally consistent.
        self.assertIn(product.inventory_tracking_mode,
                      ('legacy', 'ingredient'))


class ReservationBlocksCutoverTests(TransactionTestCase):
    """Scenario C: an outstanding reservation commits before the cutover's
    locked revalidation — the cutover must be rejected and change nothing."""

    @_require_postgresql
    def test_open_reservation_rejects_cutover_atomically(self):
        from apps.accounts.models import CustomUser
        actor = CustomUser.objects.create_user(
            username=f'p11c_{_uid()}', password='x', role='admin')
        product, ingredient = _make_ready_product('Charlie')
        # Commit an order with an outstanding ingredient reservation first.
        with transaction.atomic():
            order = Order.objects.create(
                customer_name='P11 PG reservation', order_type='dine_in',
                status='preparing',
            )
            item = OrderItem.objects.create(
                order=order, product=product, product_name=product.name,
                category_name=product.category.name, packaging_eligible=False,
                size='none', quantity=1, unit_price=Decimal('50.00'),
                subtotal=Decimal('50.00'), inventory_mode='ingredient',
            )
            reservation = OrderIngredientReservation.objects.create(
                order=order, status=OrderIngredientReservation.STATUS_RESERVED,
                order_type_snapshot='dine_in',
            )
            OrderIngredientReservationItem.objects.create(
                reservation=reservation, order_item=item,
                inventory_item=ingredient, item_name=ingredient.name,
                base_unit=ingredient.base_unit, component_source='recipe',
                source_recipe=Recipe.objects.get(product=product),
                source_revision=1, quantity_per_unit=Decimal('10.000'),
                order_item_quantity=1, reserved_quantity=Decimal('10.000'),
            )

        with self.assertRaises(TrackingModeChangeError) as ctx:
            with transaction.atomic():
                change_inventory_tracking_mode(
                    [product.pk], 'ingredient', actor=actor)

        self.assertIn('reservation', str(ctx.exception).lower())
        product.refresh_from_db()
        self.assertEqual(product.inventory_tracking_mode, 'legacy')
        self.assertEqual(
            AuditLog.objects.filter(action=CUTOVER_AUDIT_ACTION).count(), 0)


class OverlappingInventoryCutoverTests(TransactionTestCase):
    """Scenario D: two multi-product cutovers whose plans share inventory
    items must serialise (Products ascending, then Items ascending) and
    must not deadlock.

    The batches overlap on the shared product (Echo), so the service's
    all-or-nothing semantics leave two legal interleavings:

      * Outcome A — worker 1 wins Echo's Product lock and converts both
        products; worker 2 revalidates under the lock and its batch is
        rejected whole;
      * Outcome B — worker 2 converts Echo first; worker 1's batch is
        rejected whole and Delta is never touched.

    The assertions below hold for both, so the test proves lock-ordering and
    batch atomicity without forcing a particular thread to win.
    """

    @_require_postgresql
    def test_overlapping_cutovers_serialise_without_deadlock(self):
        from apps.accounts.models import CustomUser
        actor = CustomUser.objects.create_user(
            username=f'p11d_{_uid()}', password='x', role='admin')
        # Shared ingredient: both products' plans reference it.
        shared = InventoryItem.objects.create(
            name=f'P11 Shared {_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal('5000.000'),
        )
        made = []
        for name in ('Delta', 'Echo'):
            category = Category.objects.create(
                name=f'P11 Cat {name} {_uid()}', slug=f'p11-{name}-{_uid()}')
            product = Product.objects.create(
                category=category, name=name, price='50.00',
                stock_quantity=10,
            )
            recipe = Recipe.objects.create(
                product=product, size='none',
                order_type_scope=Recipe.SCOPE_ALL,
                status=Recipe.STATUS_APPROVED, revision=1,
                approved_at=timezone.now(), approval_note='P11 PG fixture',
            )
            RecipeItem.objects.create(
                recipe=recipe, inventory_item=shared,
                quantity=Decimal('10.000'),
            )
            for order_type in ('dine_in', 'takeout'):
                ProductPackagingRule.objects.create(
                    product=product, size='none', order_type=order_type,
                    no_packaging=True,
                )
            made.append(product)

        # Overlapping batches: worker 1 = [p1, p2], worker 2 = [p2] —
        # both ultimately lock products ascending and the shared item.
        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
        results = [None, None]
        t1 = _run_in_thread(_worker_cutover, [made[0].pk, made[1].pk],
                            barrier, results, 0, actor.username)
        t2 = _run_in_thread(_worker_cutover, [made[1].pk], barrier,
                            results, 1, actor.username)
        t1.join(timeout=_JOIN_TIMEOUT)
        t2.join(timeout=_JOIN_TIMEOUT)

        self.assertFalse(t1.is_alive(), 'batch-1 thread deadlocked')
        self.assertFalse(t2.is_alive(), 'batch-2 thread deadlocked')
        self.assertNotIn(None, results, 'a worker recorded no outcome')

        delta, echo = made
        for product in made:
            product.refresh_from_db()

        # Whoever wins the race for Echo's Product row, Echo is converted by
        # exactly one batch; Delta's mode follows from the winning interleaving.
        self.assertEqual(echo.inventory_tracking_mode, 'ingredient',
                         f'Echo must be ingredient after both batches, '
                         f'got {results}')
        self.assertIn(delta.inventory_tracking_mode, ('legacy', 'ingredient'))

        successes = [r for r in results if r[0]]
        failures = [r for r in results if not r[0]]
        self.assertEqual(len(successes), 1,
                         f'expected exactly one winning batch, got {results}')
        self.assertEqual(len(failures), 1,
                         f'expected exactly one rejected batch, got {results}')

        # The rejected batch is always the one that lost Echo: it is rejected
        # wholesale by the locked revalidation, never partially applied.
        failed_exc = failures[0][1]
        self.assertIsInstance(failed_exc, TrackingModeChangeError)
        self.assertIn('already', str(failed_exc).lower())
        self.assertIn(str(echo.pk), str(failed_exc))

        audit_rows = list(
            AuditLog.objects.filter(action=CUTOVER_AUDIT_ACTION).order_by('pk'))
        audit_object_ids = [row.object_id for row in audit_rows]
        converted = [
            product for product in made
            if product.inventory_tracking_mode == 'ingredient'
        ]

        # Exactly one audit row per converted product — never a duplicate for
        # the shared product, and never a row for a product left legacy.
        self.assertEqual(
            len(audit_rows), len(converted),
            f'expected one audit row per converted product: converted='
            f'{[p.pk for p in converted]}, rows={audit_object_ids}')
        self.assertEqual(len(audit_object_ids), len(set(audit_object_ids)),
                         f'duplicate cutover audit row: {audit_object_ids}')
        self.assertEqual(
            sorted(audit_object_ids), sorted(str(p.pk) for p in converted),
            'audit rows must reference exactly the converted products')

        if delta.inventory_tracking_mode == 'ingredient':
            # Outcome A: worker 1 converted the whole batch; worker 2 lost.
            self.assertEqual(set(successes[0][1].product_ids),
                             {delta.pk, echo.pk})
            self.assertEqual(sorted(audit_object_ids),
                             sorted([str(delta.pk), str(echo.pk)]))
        else:
            # Outcome B: worker 2 converted Echo; worker 1's [Delta, Echo]
            # batch was rejected before writing Delta, leaving no partial write.
            self.assertEqual(set(successes[0][1].product_ids), {echo.pk})
            self.assertEqual(audit_object_ids, [str(echo.pk)])
            self.assertNotIn(str(delta.pk), audit_object_ids)


class MultiProductAtomicFailureTests(TransactionTestCase):
    """Scenario E: a mid-batch failure rolls back every product in the batch
    and writes no audit rows, even with concurrent readers active."""

    @_require_postgresql
    def test_failure_rolls_back_entire_batch(self):
        from apps.accounts.models import CustomUser
        from unittest import mock
        actor = CustomUser.objects.create_user(
            username=f'p11e_{_uid()}', password='x', role='admin')
        ready = _make_ready_product('Foxtrot')[0]
        blocked = Product.objects.create(  # second product has no config
            category=ready.category, name='P11 NoConfig', price='50.00',
            stock_quantity=10,
        )

        with self.assertRaises(TrackingModeChangeError):
            with transaction.atomic():
                change_inventory_tracking_mode(
                    [ready.pk, blocked.pk], 'ingredient', actor=actor)

        ready.refresh_from_db()
        blocked.refresh_from_db()
        self.assertEqual(ready.inventory_tracking_mode, 'legacy')
        self.assertEqual(blocked.inventory_tracking_mode, 'legacy')
        self.assertEqual(
            AuditLog.objects.filter(action=CUTOVER_AUDIT_ACTION).count(), 0)

        # The same atomicity holds when the AUDIT write fails mid-batch.
        # `log_action` is imported inside `change_inventory_tracking_mode()`, so
        # it is never an attribute of `apps.menu.services`; the failure has to
        # be injected at the real persistence call, `AuditLog.objects.create`,
        # and it must be a DatabaseError: that is the only failure `strict=True`
        # re-raises (audit/services.py:73-79), and therefore the only one that
        # proves the surrounding transaction is rolled back.
        ready2 = _make_ready_product('Golf')[0]
        ready3 = _make_ready_product('Hotel')[0]

        real_create = AuditLog.objects.create
        audit_writes = []

        def _fail_on_second_audit(*args, **kwargs):
            """Persist the first audit row for real, then fail.

            The first product is therefore saved AND audited before the second
            product's audit write fails, which is what makes the rollback
            assertion below meaningful: the surviving-state check has to undo
            an already-written audit row, not merely skip one.
            """
            audit_writes.append(kwargs.get('action'))
            if len(audit_writes) == 1:
                return real_create(*args, **kwargs)
            raise DatabaseError('audit store down')

        with mock.patch.object(
                AuditLog.objects, 'create',
                side_effect=_fail_on_second_audit), \
                self.assertLogs('apps.audit', level='ERROR'):
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    change_inventory_tracking_mode(
                        [ready2.pk, ready3.pk], 'ingredient', actor=actor)

        # Both audit writes were attempted: one succeeded, one raised.
        self.assertEqual(
            audit_writes, [CUTOVER_AUDIT_ACTION, CUTOVER_AUDIT_ACTION])
        ready2.refresh_from_db()
        ready3.refresh_from_db()
        self.assertEqual(ready2.inventory_tracking_mode, 'legacy')
        self.assertEqual(ready3.inventory_tracking_mode, 'legacy')
        self.assertEqual(
            AuditLog.objects.filter(action=CUTOVER_AUDIT_ACTION).count(), 0,
            'the first product\'s audit row must roll back with the batch')
