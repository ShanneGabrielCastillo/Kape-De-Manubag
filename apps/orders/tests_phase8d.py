"""Phase 8D — placement-level regression tests for packaging separation,
packaging-rule enforcement, and cross-order reservation limits.

Test-ONLY milestone additions (no production changes). These complement —
they do not duplicate — the existing suites:

  • apps/inventory/tests_phase4b4.py: get_available_units()/resolve_plan()
    function-level math (min-over-components, packaging in minimum, missing
    rule, sizes/order-type independence, most-restrictive None semantics).
  • apps/orders/tests_phase5b.py: reservation creation/movement/idempotency
    battery (all dine_in fixtures — packaging separation there is implicit).
  • apps/orders/tests_phase6a.py: cancellation dispositions and rollbacks.
  • apps/orders/tests_phase5b7_pg.py: PostgreSQL cancel-vs-ready races and
    exactly-once consumption (pg_blocking_pids pattern).

The gaps closed here are the ones those suites leave implicit:
  1. an explicit assertion that a dine_in order with an explicit
     no_packaging rule reserves ZERO packaging items;
  2. takeout orders actually reserving the correct packaging quantity;
  3. takeout packaging shortage rolling back at placement;
  4. a MISSING packaging rule rejecting placement (no silent no-packaging);
  5. two sequential orders cannot reserve more than available stock.

All tests run on Django's isolated test database (TestCase — no
engine-specific commands), so they execute on both the SQLite suite and the
PostgreSQL CI job via normal apps.orders discovery.
"""
from decimal import Decimal

from django.db import transaction
from django.test import TestCase

from apps.inventory.models import IngredientMovementLog, InventoryItem
from apps.inventory.services import InsufficientStockError
from apps.orders.models import (
    OrderIngredientReservation,
    OrderIngredientReservationItem,
)
from apps.orders.services import place_order_inventory

# Reuse the established Phase 5B factories (§5: reuse existing helpers).
from apps.orders.tests_phase5b import (  # noqa: E402
    _approved_packaging_profile,
    _approved_recipe,
    _category,
    _ingredient_item,
    _ingredient_product,
    _ingredient_order,
    _packaging_item,
    _packaging_rule,
    _uid,
)


class TakeoutOrderFactoryMixin:
    """_ingredient_order in tests_phase5b is dine_in-only; takeout tests
    need an ingredient-mode line on a takeout order."""

    def _takeout_ingredient_order(self, prod, quantity=1):
        from apps.orders.models import Order, OrderItem
        order = Order.objects.create(
            customer_name=f'8D Takeout_{_uid()}', order_type='takeout',
        )
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name,
            category_name=prod.category.name,
            packaging_eligible=False,
            size='none', quantity=quantity,
            unit_price=Decimal('60.00'),
            subtotal=Decimal('60.00') * quantity,
            inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
        )
        return order


class PackagingSeparationPlacementTests(TakeoutOrderFactoryMixin, TestCase):
    """§C — packaging is reserved only for the order types whose rule says
    so, independently of ingredient stock."""

    def _ingredient_only_setup(self):
        ingr = _ingredient_item(on_hand=Decimal('1000.000'))
        prod, ingr, _, _, _ = _ingredient_product(
            ingr_item=ingr, qty_per_unit=Decimal('10.000')
        )
        return prod, ingr

    def test_dine_in_no_packaging_reserves_only_ingredients(self):
        # Explicit no_packaging rule for dine_in: placement reserves the
        # ingredient but must create ZERO packaging-source reservation items
        # (silence/absence of packaging rows is asserted explicitly here,
        # where the Phase 5B fixtures only imply it).
        prod, ingr = self._ingredient_only_setup()
        order = _ingredient_order(prod, quantity=2)
        with transaction.atomic():
            place_order_inventory(order.pk)
        res = OrderIngredientReservation.objects.get(order=order)
        sources = list(OrderIngredientReservationItem.objects
                       .filter(reservation=res)
                       .values_list('component_source', flat=True))
        self.assertEqual(sources, ['recipe'])
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('20.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('1000.000'))

    def test_takeout_reserves_packaging_quantity(self):
        # Takeout with a profile rule: placement reserves BOTH the
        # ingredient and the packaging item at qty_per_unit x line quantity,
        # each on its own ledger — packaging reservation never touches the
        # ingredient balance and vice versa.
        ingr = _ingredient_item(on_hand=Decimal('1000.000'))
        pkg = _packaging_item(on_hand=Decimal('50.000'))
        pp = _approved_packaging_profile(items=[(pkg, Decimal('1.000'))])
        prod, ingr, pkg, _, _ = _ingredient_product(
            ingr_item=ingr, pkg_item=pkg, qty_per_unit=Decimal('10.000'),
        )
        # The helper wires the dine_in rule only; add the takeout rule that
        # points at the same approved profile (shared across order types).
        _packaging_rule(prod, size='none', order_type='takeout', profile=pp)
        order = self._takeout_ingredient_order(prod, quantity=3)
        with transaction.atomic():
            place_order_inventory(order.pk)
        res = OrderIngredientReservation.objects.get(order=order)
        self.assertEqual(res.order_type_snapshot, 'takeout')
        items = {
            item.inventory_item_id: item
            for item in OrderIngredientReservationItem.objects
            .filter(reservation=res)
        }
        self.assertEqual(items[ingr.pk].component_source, 'recipe')
        self.assertEqual(items[ingr.pk].reserved_quantity, Decimal('30.000'))
        self.assertEqual(items[pkg.pk].component_source, 'packaging')
        self.assertEqual(items[pkg.pk].reserved_quantity, Decimal('3.000'))
        self.assertEqual(items[pkg.pk].consumed_quantity, Decimal('0.000'))
        ingr.refresh_from_db()
        pkg.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('30.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('1000.000'))
        self.assertEqual(pkg.reserved_quantity, Decimal('3.000'))
        self.assertEqual(pkg.on_hand_quantity, Decimal('50.000'))

    def test_takeout_insufficient_packaging_rejected_and_rolled_back(self):
        # The takeout channel enforces its own packaging stock: a shortage
        # there rejects the whole placement — nothing reserved, no movement.
        ingr = _ingredient_item(on_hand=Decimal('1000.000'))
        pkg = _packaging_item(on_hand=Decimal('2.000'))
        pp = _approved_packaging_profile(items=[(pkg, Decimal('1.000'))])
        prod, ingr, pkg, _, _ = _ingredient_product(
            ingr_item=ingr, pkg_item=pkg, qty_per_unit=Decimal('10.000'),
        )
        _packaging_rule(prod, size='none', order_type='takeout', profile=pp)
        order = self._takeout_ingredient_order(prod, quantity=3)  # needs 3 cups
        with self.assertRaises(InsufficientStockError):
            with transaction.atomic():
                place_order_inventory(order.pk)
        self.assertFalse(
            OrderIngredientReservation.objects.filter(order=order).exists())
        self.assertEqual(
            IngredientMovementLog.objects.filter(order=order).count(), 0)
        ingr.refresh_from_db()
        pkg.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('0.000'))
        self.assertEqual(pkg.reserved_quantity, Decimal('0.000'))


class PackagingRuleMissingPlacementTests(TakeoutOrderFactoryMixin, TestCase):
    """§B7 — a missing packaging rule is NEVER silently treated as
    'no packaging' (Rev 3 §I.2): placement must fail loudly and cleanly."""

    def test_missing_packaging_rule_rejects_placement(self):
        # Recipe exists (ingredients fine) but the dine_in packaging rule is
        # absent entirely: resolve_plan raises PackagingRuleMissingError (a
        # RecipeIncompleteError/ValueError) — no reservation, no movement,
        # nothing reserved.
        from apps.inventory.services import PackagingRuleMissingError
        ingr = _ingredient_item(on_hand=Decimal('1000.000'))
        prod, ingr, _, _, _ = _ingredient_product(
            ingr_item=ingr, qty_per_unit=Decimal('10.000')
        )
        # Remove the rules the factory created, leaving ONLY the recipe.
        from apps.inventory.models import ProductPackagingRule
        ProductPackagingRule.objects.filter(product=prod).delete()
        order = _ingredient_order(prod, quantity=1)
        with self.assertRaises(PackagingRuleMissingError):
            with transaction.atomic():
                place_order_inventory(order.pk)
        self.assertFalse(
            OrderIngredientReservation.objects.filter(order=order).exists())
        self.assertEqual(
            IngredientMovementLog.objects.filter(order=order).count(), 0)
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('0.000'))


class CrossOrderReservationLimitTests(TakeoutOrderFactoryMixin, TestCase):
    """§D6 — two sequential orders cannot together reserve more than the
    available stock: the first succeeds, the second is rejected because the
    first reservation reduced availability."""

    def test_second_order_cannot_over_reserve(self):
        ingr = _ingredient_item(on_hand=Decimal('50.000'))
        prod, ingr, _, _, _ = _ingredient_product(
            ingr_item=ingr, qty_per_unit=Decimal('10.000')
        )
        order1 = _ingredient_order(prod, quantity=2)   # needs 20g of 50g
        with transaction.atomic():
            place_order_inventory(order1.pk)
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('20.000'))
        self.assertEqual(ingr.available_quantity, Decimal('30.000'))

        order2 = _ingredient_order(prod, quantity=4)   # needs 40g > 30g left
        with self.assertRaises(InsufficientStockError):
            with transaction.atomic():
                place_order_inventory(order2.pk)
        # Order 1's reservation is untouched; order 2 left nothing behind.
        self.assertTrue(
            OrderIngredientReservation.objects.filter(order=order1).exists())
        self.assertFalse(
            OrderIngredientReservation.objects.filter(order=order2).exists())
        self.assertEqual(
            IngredientMovementLog.objects.filter(order=order2).count(), 0)
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('20.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('50.000'))
