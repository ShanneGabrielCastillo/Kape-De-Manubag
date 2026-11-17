"""
Phase 1 — readiness-gated inventory cutover tests.

These tests cover the dry-run assessment and the audited cutover service:

  * a complete, ready product
  * missing approved recipe
  * missing packaging rule
  * inactive ingredient
  * inactive packaging item
  * zero / insufficient opening stock (including one bad size)
  * open orders in awaiting_payment, preparing and ready
  * outstanding reservation lines
  * already ingredient-tracked product
  * inactive product
  * missing product id and multiple requested products
  * audit failure rollback
  * dry-run does not mutate state
  * exactly one audit row per successful product
  * Product → InventoryItem ordered locking call
  * rollback from ingredient to legacy

All fixtures are synthetic and live only in Django's test database.
"""

from decimal import Decimal
from unittest import mock

from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import CustomUser
from apps.audit.models import AuditLog
from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    PackagingProfile,
    PackagingProfileItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.inventory.services import lock_inventory_items
from apps.menu.models import Category, Product
from apps.menu.services import (
    CUTOVER_AUDIT_ACTION,
    CutoverReadiness,
    TrackingModeChangeError,
    assess_cutover_readiness,
    assess_products_for_cutover,
    change_inventory_tracking_mode,
    cutover_readiness_report,
    cutover_stock_report,
)
from apps.orders.models import (
    Order,
    OrderIngredientReservation,
    OrderIngredientReservationItem,
    OrderItem,
)

PASSWORD = 'phase1-cutover-pass'
_counter = 0


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _user(username=None, role='admin'):
    return CustomUser.objects.create_user(
        username=username or f'p1_user_{_uid()}',
        password=PASSWORD, role=role,
    )


def _category():
    name = f'P1Cat_{_uid()}'
    return Category.objects.create(name=name, slug=name.lower())


def _product(name=None, sizes=('none',), mode='legacy', is_active=True):
    sizes = tuple(sizes)
    return Product.objects.create(
        category=_category(),
        name=name or f'P1 Product {_uid()}',
        price=Decimal('50.00'),
        price_medium=Decimal('55.00') if 'medium' in sizes else None,
        price_large=Decimal('60.00') if 'large' in sizes else None,
        price_hot=Decimal('50.00') if 'hot' in sizes else None,
        has_sizes=len(sizes) > 1,
        stock_quantity=100,
        inventory_tracking_mode=mode,
        is_active=is_active,
    )


def _configure(product, quantities=None, on_hand='1000.000',
               use_profile=False, packaging_item_active=True,
               profile_status='approved'):
    """Create a complete recipe/rules configuration for every configured size.

    ``quantities`` maps size -> ingredient quantity per unit. By default each
    size needs 10 g and the shared ingredient has 1000 g on hand.
    """
    from apps.menu.views import configured_sizes_for_product

    sizes = list(configured_sizes_for_product(product))
    quantities = quantities or {size: '10.000' for size in sizes}

    ingredient = InventoryItem.objects.create(
        name=f'P1 Ingredient {_uid()}', item_type='ingredient', base_unit='g',
        on_hand_quantity=Decimal(on_hand),
    )
    recipes = {}
    for size in sizes:
        recipe = Recipe.objects.create(
            product=product, size=size, order_type_scope='all',
            status=Recipe.STATUS_APPROVED, revision=1,
            approved_at=timezone.now(), approval_note='Phase 1 test approval',
        )
        RecipeItem.objects.create(
            recipe=recipe, inventory_item=ingredient,
            quantity=Decimal(quantities[size]),
        )
        recipes[size] = recipe

    packaging_item = None
    profile = None
    if use_profile:
        packaging_item = InventoryItem.objects.create(
            name=f'P1 Packaging {_uid()}', item_type='packaging',
            base_unit='pcs', on_hand_quantity=Decimal('500.000'),
            is_active=packaging_item_active,
        )
        profile = PackagingProfile.objects.create(
            name=f'P1 Profile {_uid()}', status=profile_status,
            revision=1,
            approved_at=timezone.now() if profile_status == 'approved' else None,
            approval_note='Phase 1 test profile',
        )
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=packaging_item,
            quantity=Decimal('1.000'),
        )

    for size in sizes:
        ProductPackagingRule.objects.create(
            product=product, size=size, order_type='dine_in',
            profile=None, no_packaging=True,
        )
        if use_profile:
            ProductPackagingRule.objects.create(
                product=product, size=size, order_type='takeout',
                profile=profile, no_packaging=False,
            )
        else:
            ProductPackagingRule.objects.create(
                product=product, size=size, order_type='takeout',
                profile=None, no_packaging=True,
            )

    return {
        'ingredient': ingredient,
        'recipes': recipes,
        'profile': profile,
        'packaging_item': packaging_item,
    }


def _check(report, key):
    for check in report.checks:
        if check.key == key:
            return check
    raise AssertionError(f'check {key!r} missing from report')


def _order(product, status='awaiting_payment', mode='legacy'):
    order = Order.objects.create(
        customer_name='Phase 1 test', order_type='dine_in', status=status,
    )
    item = OrderItem.objects.create(
        order=order, product=product, size='none', quantity=1,
        unit_price=Decimal('50.00'), inventory_mode=mode,
    )
    return order, item


def _reservation_line(product, recipe, ingredient, status='reserved'):
    order, item = _order(product, status='completed', mode='ingredient')
    reservation = OrderIngredientReservation.objects.create(
        order=order, status=status, order_type_snapshot='dine_in',
        settled_at=None if status == 'reserved' else timezone.now(),
    )
    OrderIngredientReservationItem.objects.create(
        reservation=reservation, order_item=item, inventory_item=ingredient,
        item_name=ingredient.name, base_unit=ingredient.base_unit,
        component_source='recipe', source_recipe=recipe,
        source_revision=recipe.revision,
        quantity_per_unit=Decimal('10.000'), order_item_quantity=1,
        reserved_quantity=Decimal('10.000'),
    )
    return reservation


# ═════════════════════════════════════════════════════════════════════════════
# Readiness assessment
# ═════════════════════════════════════════════════════════════════════════════

class CutoverReadinessTests(TestCase):

    def test_complete_ready_product_is_eligible(self):
        product = _product()
        _configure(product)

        report = assess_cutover_readiness(product)

        self.assertIsInstance(report, CutoverReadiness)
        self.assertTrue(report.eligible)
        self.assertEqual(report.reasons, ())
        self.assertTrue(all(check.passed for check in report.checks))
        self.assertEqual({check.key for check in report.checks}, {
            'product_active',
            'not_already_ingredient',
            'approved_recipes',
            'recipe_lines',
            'packaging_rules',
            'packaging_profiles',
            'configuration_complete',
            'opening_stock',
            'no_open_orders',
            'no_outstanding_reservations',
        })
        self.assertEqual(len(report.size_availability), 2)  # none x 2 order types
        self.assertTrue(all(slot.available_units == 100
                            for slot in report.size_availability))
        self.assertEqual(report.missing_or_inactive_items, ())
        self.assertEqual(report.current_mode, 'legacy')

    def test_missing_approved_recipe_blocks(self):
        product = _product()
        _configure(product)
        Recipe.objects.filter(product=product).delete()

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'approved_recipes').passed)
        self.assertIn('no approved recipe',
                      _check(report, 'approved_recipes').detail)
        self.assertIn('Resolve-plan configuration', ' '.join(report.reasons))

    def test_draft_recipe_does_not_count_as_approved(self):
        product = _product()
        config = _configure(product)
        recipe = config['recipes']['none']
        recipe.status = Recipe.STATUS_DRAFT
        recipe.approved_at = None
        recipe.save(update_fields=['status', 'approved_at'])

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'approved_recipes').passed)

    def test_missing_packaging_rule_blocks(self):
        product = _product()
        _configure(product)
        ProductPackagingRule.objects.filter(
            product=product, order_type='takeout').delete()

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'packaging_rules').passed)
        self.assertIn('takeout: no packaging rule',
                      _check(report, 'packaging_rules').detail)

    def test_inactive_ingredient_blocks(self):
        product = _product()
        config = _configure(product)
        config['ingredient'].is_active = False
        config['ingredient'].save(update_fields=['is_active'])

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'recipe_lines').passed)
        self.assertFalse(_check(report, 'configuration_complete').passed)
        self.assertTrue(any(
            'Ingredient' in item and 'inactive' in item
            for item in report.missing_or_inactive_items
        ))

    def test_inactive_packaging_item_blocks(self):
        product = _product()
        _configure(product, use_profile=True, packaging_item_active=False)

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'packaging_profiles').passed)
        self.assertTrue(any(
            'Packaging item' in item and 'inactive' in item
            for item in report.missing_or_inactive_items
        ))

    def test_unapproved_packaging_profile_blocks(self):
        product = _product()
        _configure(product, use_profile=True, profile_status='draft')

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'packaging_profiles').passed)
        self.assertIn('not approved',
                      _check(report, 'packaging_profiles').detail)

    def test_zero_opening_stock_blocks(self):
        product = _product()
        _configure(product, on_hand='0.000')

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'opening_stock').passed)
        self.assertTrue(all(slot.available_units == 0
                            for slot in report.size_availability))

    def test_one_size_with_insufficient_stock_blocks(self):
        product = _product(sizes=('none', 'large'))
        _configure(product, quantities={'none': '10.000', 'large': '2000.000'},
                   on_hand='1000.000')

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'opening_stock').passed)
        by_slot = {(slot.size, slot.order_type): slot
                   for slot in report.size_availability}
        self.assertEqual(by_slot[('none', 'dine_in')].available_units, 100)
        self.assertEqual(by_slot[('large', 'dine_in')].available_units, 0)

    def test_already_ingredient_tracked_blocks(self):
        product = _product(mode='ingredient')
        _configure(product)

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'not_already_ingredient').passed)

    def test_inactive_product_blocks(self):
        product = _product(is_active=False)
        _configure(product)

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'product_active').passed)

    def test_open_orders_block_in_every_active_status(self):
        for status in ('awaiting_payment', 'preparing', 'ready'):
            with self.subTest(status=status):
                product = _product()
                _configure(product)
                order, _item = _order(product, status=status)

                report = assess_cutover_readiness(product)

                self.assertFalse(report.eligible)
                self.assertFalse(_check(report, 'no_open_orders').passed)
                self.assertIn(order.order_number, ' '.join(report.open_orders))

    def test_completed_and_cancelled_orders_do_not_block(self):
        for status in ('completed', 'cancelled'):
            with self.subTest(status=status):
                product = _product()
                _configure(product)
                _order(product, status=status)

                report = assess_cutover_readiness(product)

                self.assertTrue(_check(report, 'no_open_orders').passed)

    def test_outstanding_reservation_blocks(self):
        product = _product()
        config = _configure(product)
        _reservation_line(
            product, config['recipes']['none'], config['ingredient'],
            status='reserved',
        )

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(_check(report, 'no_outstanding_reservations').passed)
        self.assertTrue(report.outstanding_reservations)

    def test_settled_reservation_does_not_block(self):
        product = _product()
        config = _configure(product)
        _reservation_line(
            product, config['recipes']['none'], config['ingredient'],
            status='released',
        )

        report = assess_cutover_readiness(product)

        self.assertTrue(_check(report, 'no_outstanding_reservations').passed)

    def test_report_for_all_products_and_explicit_ids(self):
        ready = _product()
        _configure(ready)
        blocked = _product()
        _configure(blocked)
        Recipe.objects.filter(product=blocked).delete()

        all_reports = assess_products_for_cutover()
        self.assertIn(ready.pk, [r.product_id for r in all_reports])
        self.assertIn(blocked.pk, [r.product_id for r in all_reports])

        explicit = assess_products_for_cutover([ready.pk])
        self.assertEqual([r.product_id for r in explicit], [ready.pk])
        self.assertTrue(explicit[0].eligible)


# ═════════════════════════════════════════════════════════════════════════════
# Dry-run immutability
# ═════════════════════════════════════════════════════════════════════════════

class CutoverDryRunTests(TestCase):

    def test_dry_run_leaves_database_state_unchanged(self):
        product = _product()
        config = _configure(product, on_hand='123.456')
        _order(product, status='preparing')

        before = {
            'mode': Product.objects.get(pk=product.pk).inventory_tracking_mode,
            'updated_at': Product.objects.get(pk=product.pk).updated_at,
            'on_hand': InventoryItem.objects.get(
                pk=config['ingredient'].pk).on_hand_quantity,
            'reserved': InventoryItem.objects.get(
                pk=config['ingredient'].pk).reserved_quantity,
            'audit': AuditLog.objects.count(),
            'movements': IngredientMovementLog.objects.count(),
            'reservations': OrderIngredientReservation.objects.count(),
            'orders': Order.objects.count(),
        }

        report = cutover_readiness_report([product.pk])

        after = {
            'mode': Product.objects.get(pk=product.pk).inventory_tracking_mode,
            'updated_at': Product.objects.get(pk=product.pk).updated_at,
            'on_hand': InventoryItem.objects.get(
                pk=config['ingredient'].pk).on_hand_quantity,
            'reserved': InventoryItem.objects.get(
                pk=config['ingredient'].pk).reserved_quantity,
            'audit': AuditLog.objects.count(),
            'movements': IngredientMovementLog.objects.count(),
            'reservations': OrderIngredientReservation.objects.count(),
            'orders': Order.objects.count(),
        }

        self.assertEqual(before, after)
        self.assertEqual(report['eligible_count'], 0)
        self.assertEqual(report['blocked_count'], 1)
        self.assertIn('checks', report['products'][0])
        self.assertIn('size_availability', report['products'][0])


# ═════════════════════════════════════════════════════════════════════════════
# Cutover operation
# ═════════════════════════════════════════════════════════════════════════════

class CutoverOperationTests(TestCase):

    def setUp(self):
        self.admin = _user()

    def test_successful_cutover_creates_exactly_one_audit_and_preserves_stock(self):
        product = _product()
        config = _configure(product, on_hand='1000.000')
        before = Product.objects.get(pk=product.pk)

        result = change_inventory_tracking_mode(
            [product.pk], 'ingredient', actor=self.admin,
            reason='phase 1 test',
        )

        product.refresh_from_db()
        config['ingredient'].refresh_from_db()
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')
        self.assertEqual(config['ingredient'].on_hand_quantity,
                         Decimal('1000.000'))
        self.assertEqual(result.product_ids, (product.pk,))
        self.assertEqual(result.old_modes(), {product.pk: 'legacy'})
        self.assertEqual(AuditLog.objects.filter(
            action=CUTOVER_AUDIT_ACTION).count(), 1)
        entry = AuditLog.objects.get(action=CUTOVER_AUDIT_ACTION)
        self.assertEqual(entry.user, self.admin)
        self.assertEqual(entry.object_id, str(product.pk))
        self.assertIn('phase 1 test', entry.detail)
        self.assertEqual(product.updated_at >= before.updated_at, True)

    def test_audit_failure_rolls_back_the_mode_change(self):
        product = _product()
        _configure(product)

        with mock.patch.object(
            AuditLog.objects, 'create', side_effect=DatabaseError('audit down'),
        ), self.assertLogs('apps.audit', level='ERROR'):
            with self.assertRaises(DatabaseError):
                change_inventory_tracking_mode(
                    [product.pk], 'ingredient', actor=self.admin)

        product.refresh_from_db()
        self.assertEqual(product.inventory_tracking_mode, 'legacy')
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_missing_product_id_is_rejected_before_writes(self):
        product = _product()
        _configure(product)

        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                [product.pk, 999999], 'ingredient', actor=self.admin)

        product.refresh_from_db()
        self.assertEqual(product.inventory_tracking_mode, 'legacy')
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_already_ingredient_product_is_rejected(self):
        product = _product(mode='ingredient')
        _configure(product)

        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                [product.pk], 'ingredient', actor=self.admin)

        self.assertEqual(AuditLog.objects.count(), 0)

    def test_one_blocked_product_prevents_all_changes(self):
        ready = _product()
        _configure(ready)
        blocked = _product()
        _configure(blocked)
        Recipe.objects.filter(product=blocked).delete()

        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                [ready.pk, blocked.pk], 'ingredient', actor=self.admin)

        self.assertEqual(Product.objects.get(pk=ready.pk).inventory_tracking_mode,
                         'legacy')
        self.assertEqual(Product.objects.get(pk=blocked.pk).inventory_tracking_mode,
                         'legacy')
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_multiple_ready_products_each_get_one_audit_row(self):
        first = _product()
        _configure(first)
        second = _product()
        _configure(second)

        result = change_inventory_tracking_mode(
            [second.pk, first.pk], 'ingredient', actor=self.admin)

        self.assertEqual(result.product_ids, (first.pk, second.pk))
        self.assertEqual(AuditLog.objects.filter(
            action=CUTOVER_AUDIT_ACTION).count(), 2)
        self.assertEqual(
            Product.objects.filter(
                pk__in=[first.pk, second.pk],
                inventory_tracking_mode='ingredient',
            ).count(),
            2,
        )

    def test_inventory_items_are_locked_through_the_shared_ordered_helper(self):
        product = _product()
        config = _configure(product)

        with mock.patch(
            'apps.inventory.services.lock_inventory_items',
            wraps=lock_inventory_items,
        ) as lock_mock:
            change_inventory_tracking_mode(
                [product.pk], 'ingredient', actor=self.admin)

        lock_mock.assert_called_once()
        locked_ids = lock_mock.call_args.args[0]
        self.assertEqual(list(locked_ids), sorted(locked_ids))
        self.assertIn(config['ingredient'].pk, locked_ids)

    def test_rollback_is_supported_and_audited(self):
        product = _product(mode='ingredient')
        _configure(product)

        result = change_inventory_tracking_mode(
            [product.pk], 'legacy', actor=self.admin, reason='rollback')

        product.refresh_from_db()
        self.assertEqual(product.inventory_tracking_mode, 'legacy')
        self.assertEqual(result.target_mode, 'legacy')
        self.assertEqual(AuditLog.objects.filter(
            action=CUTOVER_AUDIT_ACTION).count(), 1)

    def test_rollback_with_open_reservation_is_rejected(self):
        product = _product(mode='ingredient')
        config = _configure(product)
        _reservation_line(
            product, config['recipes']['none'], config['ingredient'],
            status='reserved',
        )

        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                [product.pk], 'legacy', actor=self.admin)

        product.refresh_from_db()
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')
        self.assertEqual(AuditLog.objects.count(), 0)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 1.1 — readiness-rule regression coverage
# ═════════════════════════════════════════════════════════════════════════════

class ReadinessRuleRegressionTests(TestCase):
    """Phase 1.1: verify gate behaviour against the real order/reservation
    workflow states (actual model constants — no invented statuses)."""

    def test_released_reservation_does_not_block(self):
        # A released reservation is closed history and must not block.
        product = _product()
        config = _configure(product)
        _reservation_line(
            product, config['recipes']['none'], config['ingredient'],
            status=OrderIngredientReservation.STATUS_RELEASED,
        )

        report = assess_cutover_readiness(product)

        self.assertTrue(report.eligible)
        self.assertTrue(
            _check(report, 'no_outstanding_reservations').passed)

    def test_consumed_reservation_does_not_block(self):
        # A consumed (settled) reservation is closed history.
        product = _product()
        config = _configure(product)
        _reservation_line(
            product, config['recipes']['none'], config['ingredient'],
            status=OrderIngredientReservation.STATUS_CONSUMED,
        )

        report = assess_cutover_readiness(product)

        self.assertTrue(report.eligible)

    def test_reserved_reservation_blocks_even_when_order_completed(self):
        # A reservation line stuck in STATUS_RESERVED whose order is already
        # completed is still an outstanding commitment — gate 10 keys on the
        # reservation status, not the order status.
        product = _product()
        config = _configure(product)
        reservation = _reservation_line(
            product, config['recipes']['none'], config['ingredient'],
            status='reserved',
        )
        # The helper creates the order as completed; force the reservation
        # back to reserved to simulate a ledger that was never settled.
        reservation.status = OrderIngredientReservation.STATUS_RESERVED
        reservation.settled_at = None
        reservation.save(update_fields=['status', 'settled_at'])

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        self.assertFalse(
            _check(report, 'no_outstanding_reservations').passed)

    def test_mixed_scope_ambiguity_blocks(self):
        # An approved scope='all' recipe AND an approved scope='dine_in'
        # recipe for the same size match the same (size, dine_in) slot —
        # resolve_plan() must never have to guess, so the readiness check
        # counts two applicable recipes and blocks.
        product = _product()
        _configure(product)
        Recipe.objects.create(
            product=product, size='none',
            order_type_scope=Recipe.SCOPE_DINE_IN,
            status=Recipe.STATUS_APPROVED, revision=1,
            approved_at=timezone.now(), approval_note='Phase 1.1 ambiguity',
        )

        report = assess_cutover_readiness(product)

        self.assertFalse(report.eligible)
        recipes_check = _check(report, 'approved_recipes')
        self.assertFalse(recipes_check.passed)
        self.assertIn('2 matching approved recipes', recipes_check.detail)

    def test_open_order_statuses_block_individually(self):
        # Each of the three live workflow statuses blocks on its own,
        # using the actual Order status constants.
        for status in ('awaiting_payment', 'preparing', 'ready'):
            with self.subTest(status=status):
                product = _product()
                _configure(product)
                _order(product, status=status, mode='legacy')

                report = assess_cutover_readiness(product)

                self.assertFalse(report.eligible)
                self.assertFalse(_check(report, 'no_open_orders').passed)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 1.1 — stock-shortage / classification report
# ═════════════════════════════════════════════════════════════════════════════

class CutoverStockReportTests(TestCase):
    """Phase 1.1 §5: the stock report distinguishes technical eligibility,
    configuration blocks, insufficient stock, low-stock warnings, and client
    confirmation (always False — never invented)."""

    def _report(self, pks=None):
        return cutover_stock_report(list(pks) if pks is not None else None)

    def test_eligible_product_classified_with_stock_rows(self):
        product = _product()
        config = _configure(product, on_hand='1000.000')

        report = self._report([product.pk])

        entry = report['products'][0]
        self.assertEqual(entry['classification'], 'technically_eligible')
        self.assertTrue(entry['stock_sufficient'])
        self.assertEqual(entry['max_producible_units'], 100)
        self.assertEqual(entry['limiting_inventory_item'],
                         config['ingredient'].name)
        self.assertFalse(entry['few_units'])
        self.assertEqual(entry['low_stock_warnings'], [])
        # Client confirmation is never invented.
        self.assertFalse(entry['client_confirmed'])
        self.assertIn('not client', entry['client_confirmation_note'])
        self.assertEqual(report['summary']['client_confirmed'], 0)

    def test_insufficient_stock_classified_separately_from_configuration(self):
        # Complete configuration but zero producible units: the ONLY failing
        # gate is opening_stock, so the classification is
        # blocked_insufficient_stock — not blocked_configuration.
        product = _product()
        _configure(product, on_hand='5.000')   # needs 10 g per unit

        report = self._report([product.pk])

        entry = report['products'][0]
        self.assertEqual(entry['classification'], 'blocked_insufficient_stock')
        self.assertEqual(entry['failed_checks'], ['opening_stock'])
        self.assertFalse(entry['stock_sufficient'])

    def test_configuration_block_classified(self):
        product = _product()   # no recipes, no rules at all

        report = self._report([product.pk])

        entry = report['products'][0]
        self.assertEqual(entry['classification'], 'blocked_configuration')
        self.assertIn('approved_recipes', entry['failed_checks'])
        self.assertIn('packaging_rules', entry['failed_checks'])

    def test_few_units_flag_and_limiting_item(self):
        # Eligible product that can produce only 2 units → few_units flag
        # plus a low-stock warning naming the limiting ingredient.
        product = _product()
        config = _configure(product, on_hand='25.000')   # 25 / 10 = 2 units

        report = self._report([product.pk])

        entry = report['products'][0]
        self.assertEqual(entry['classification'], 'technically_eligible')
        self.assertEqual(entry['max_producible_units'], 2)
        self.assertTrue(entry['few_units'])
        self.assertEqual(entry['limiting_inventory_item'],
                         config['ingredient'].name)
        self.assertTrue(any('limiting' in w
                            for w in entry['low_stock_warnings']))

    def test_low_stock_threshold_warning_for_eligible_product(self):
        # The limiting ingredient is at or below its own threshold even
        # though producible units are above the few-units cutoff.
        product = _product()
        config = _configure(product, on_hand='300.000')
        ingredient = config['ingredient']
        ingredient.low_stock_threshold = Decimal('500.000')
        ingredient.save(update_fields=['low_stock_threshold'])

        report = self._report([product.pk])

        entry = report['products'][0]
        self.assertEqual(entry['classification'], 'technically_eligible')
        self.assertEqual(entry['max_producible_units'], 30)
        self.assertTrue(any(ingredient.name in w
                            for w in entry['low_stock_warnings']))
        row = next(r for r in entry['inventory_items']
                   if r['inventory_item_id'] == ingredient.pk)
        self.assertTrue(row['is_low_stock'])

    def test_already_ingredient_and_inactive_classified(self):
        ingredient_product = _product(mode='ingredient')
        _configure(ingredient_product)
        inactive = _product()
        _configure(inactive)
        inactive.is_active = False
        inactive.save(update_fields=['is_active'])

        report = self._report([ingredient_product.pk, inactive.pk])

        by_id = {p['product_id']: p for p in report['products']}
        self.assertEqual(by_id[ingredient_product.pk]['classification'],
                         'already_ingredient')
        self.assertEqual(by_id[inactive.pk]['classification'], 'inactive')
        self.assertEqual(report['summary']['already_ingredient'], 1)
        self.assertEqual(report['summary']['inactive'], 1)

    def test_stock_report_is_read_only(self):
        product = _product()
        _configure(product)

        before = {
            'products': Product.objects.count(),
            'items': InventoryItem.objects.count(),
            'recipes': Recipe.objects.count(),
            'rules': ProductPackagingRule.objects.count(),
            'audit': AuditLog.objects.count(),
            'movements': IngredientMovementLog.objects.count(),
            'updated_at': product.updated_at,
        }
        cutover_stock_report([product.pk])
        product.refresh_from_db()

        after = {
            'products': Product.objects.count(),
            'items': InventoryItem.objects.count(),
            'recipes': Recipe.objects.count(),
            'rules': ProductPackagingRule.objects.count(),
            'audit': AuditLog.objects.count(),
            'movements': IngredientMovementLog.objects.count(),
            'updated_at': product.updated_at,
        }
        self.assertEqual(before, after)

