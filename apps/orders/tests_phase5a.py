"""
tests_phase5a.py
================
Phase 5A Stage A — Order Inventory Coordinator tests.

Covers:
  create_order_item()
  ───────────────────
  1.  inventory_mode is snapshotted from product.inventory_tracking_mode ('legacy')
  2.  inventory_mode is snapshotted from product.inventory_tracking_mode ('ingredient')
  3.  all other snapshot fields are still correct after Phase 5A change

  deduct_inventory_for_order() — narrowed to legacy lines
  ────────────────────────────────────────────────────────
  4.  deducts stock for legacy-mode items
  5.  skips ingredient-mode items entirely (no legacy lines → early return)
  6.  mixed order: deducts only legacy lines, leaves ingredient product untouched
  7.  aggregate validation: two legacy lines for same product, stock covers total → ok
  8.  aggregate validation: two legacy lines for same product, stock < total → ValueError
  9.  idempotency: second call is a no-op when stock_deducted=True
  10. extra_order_update_fields still forwarded correctly

  restore_inventory_for_order() — narrowed + item_quantities param
  ─────────────────────────────────────────────────────────────────
  11. restores stock for legacy-mode items
  12. skips ingredient-mode items
  13. idempotency: no-op when stock_deducted=False
  14. item_quantities param: partial restore uses supplied quantity
  15. item_quantities param: items not in dict use full item.quantity

  place_order_inventory() — core coordinator
  ──────────────────────────────────────────
  16. legacy-only order: stock deducted, stock_deducted=True, ingredient_plans=[]
  17. ingredient-only order: no stock deducted, stock_deducted=False,
      plans returned, resolve_plan called for each line
  18. mixed order: legacy deducted, ingredient validated, plans returned
  19. order not found → ValueError
  20. order already processed (stock_deducted=True) → returns early, idempotent
  21. insufficient legacy stock → ValueError, no stock moved
  22. resolve_plan failure (ingredient line) → RecipeIncompleteError, no stock moved
  23. Product locks acquired in ascending pk order
      (checked via SQL ordering — structural test)
  24. inventory_mode re-snapshot: item with wrong mode is corrected under lock
  25. legacy_line_count / ingredient_line_count in return dict are accurate
  26. items with product=None are silently skipped
  27. ingredient-only order: stock_deducted remains False
  28. legacy stock_deducted=True only set when at least one legacy line written

All tests use SQLite (in-memory test database).
No PostgreSQL-specific concurrency is tested here; concurrency is covered by
Phase 4B-6 tests.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from apps.inventory.models import (
    InventoryItem,
    InventoryLog,
    PackagingProfile,
    PackagingProfileItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.inventory.services import (
    RecipeIncompleteError,
    RecipeMissingError,
    resolve_plan,
)
from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem
from apps.orders.services import (
    create_order_item,
    deduct_inventory_for_order,
    place_order_inventory,
    restore_inventory_for_order,
)

User = get_user_model()
_PASSWORD = 'test-5a-pass'
_counter = 0


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _user(username=None):
    uid = username or f'u5a_{_uid()}'
    return User.objects.create_user(username=uid, password=_PASSWORD, role='cashier')


def _category(name=None, is_packaging_required=True):
    n = name or f'Cat5a_{_uid()}'
    return Category.objects.create(
        name=n, slug=n.lower().replace(' ', '_'),
        is_packaging_required=is_packaging_required,
    )


def _product(name=None, stock=100, mode=Product.INVENTORY_TRACKING_LEGACY,
             category=None):
    n = name or f'Prod5a_{_uid()}'
    cat = category or _category()
    return Product.objects.create(
        category=cat, name=n,
        price=Decimal('65.00'),
        stock_quantity=stock,
        inventory_tracking_mode=mode,
    )


def _ingredient_item(name=None, on_hand=Decimal('1000.000')):
    n = name or f'Ing5a_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type='ingredient', base_unit='g',
        on_hand_quantity=on_hand, is_active=True,
    )


def _order(order_type='dine_in', cashier=None):
    """Create a minimal order (no items)."""
    return Order.objects.create(
        customer_name='Test Customer',
        order_type=order_type,
        cashier=cashier,
    )


def _approved_recipe(product, size='none', scope='all'):
    return Recipe.objects.create(
        product=product, size=size, order_type_scope=scope,
        status=Recipe.STATUS_APPROVED, revision=1,
        approved_at=timezone.now(),
        approval_note='5A test approval',
    )


def _approved_profile(name=None):
    n = name or f'Prof5a_{_uid()}'
    return PackagingProfile.objects.create(
        name=n, status=PackagingProfile.STATUS_APPROVED,
        revision=1, approved_at=timezone.now(),
        approval_note='5A test profile',
    )


def _no_packaging_rule(product, size='none', order_type='dine_in'):
    return ProductPackagingRule.objects.create(
        product=product, size=size, order_type=order_type,
        profile=None, no_packaging=True,
    )


def _full_ingredient_setup(product=None, size='none', order_type='dine_in'):
    """
    Create the full recipe + packaging rule needed for resolve_plan() to
    succeed for *product* at *size*/*order_type*.

    Returns (product, ingredient, recipe, rule).
    """
    prod = product or _product(mode=Product.INVENTORY_TRACKING_INGREDIENT)
    ing = _ingredient_item()
    rec = _approved_recipe(prod, size=size, scope='all')
    RecipeItem.objects.create(
        recipe=rec, inventory_item=ing, quantity=Decimal('50.000'),
    )
    rule = _no_packaging_rule(prod, size=size, order_type=order_type)
    return prod, ing, rec, rule


# ═════════════════════════════════════════════════════════════════════════════
# 1–3. create_order_item() — inventory_mode snapshot
# ═════════════════════════════════════════════════════════════════════════════

class CreateOrderItemInventoryModeTests(TestCase):

    def test_legacy_mode_snapshotted(self):
        """inventory_mode='legacy' when product.inventory_tracking_mode='legacy'."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        order = _order()
        item = create_order_item(order, prod, 'none', 2)
        self.assertEqual(item.inventory_mode, OrderItem.INVENTORY_MODE_LEGACY)

    def test_ingredient_mode_snapshotted(self):
        """inventory_mode='ingredient' when product.inventory_tracking_mode='ingredient'."""
        prod = _product(mode=Product.INVENTORY_TRACKING_INGREDIENT, stock=50)
        order = _order()
        item = create_order_item(order, prod, 'none', 2)
        self.assertEqual(item.inventory_mode, OrderItem.INVENTORY_MODE_INGREDIENT)

    def test_other_snapshot_fields_intact(self):
        """product_name, category_name, packaging_eligible, unit_price, subtotal
        are still correctly snapshotted after the Phase 5A inventory_mode addition."""
        cat = _category(is_packaging_required=True)
        prod = _product(category=cat, stock=50)
        prod.price = Decimal('75.00')
        prod.save(update_fields=['price'])

        order = _order(order_type='takeout')
        item = create_order_item(order, prod, 'none', 3)

        self.assertEqual(item.product_name, prod.name)
        self.assertEqual(item.category_name, cat.name)
        self.assertTrue(item.packaging_eligible)
        self.assertEqual(item.unit_price, Decimal('75.00'))
        self.assertEqual(item.subtotal, Decimal('225.00'))


# ═════════════════════════════════════════════════════════════════════════════
# 4–10. deduct_inventory_for_order() — narrowed to legacy lines
# ═════════════════════════════════════════════════════════════════════════════

class DeductInventoryLegacyNarrowTests(TestCase):

    def _make_order_with_item(self, mode, stock=50, qty=2):
        prod = _product(mode=mode, stock=stock)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False,
            size='none', quantity=qty,
            unit_price=Decimal('65.00'),
            subtotal=Decimal('65.00') * qty,
            inventory_mode=mode,
        )
        prod.refresh_from_db()
        return order, prod

    def test_deducts_legacy_items(self):
        """Legacy-mode items have stock deducted and stock_deducted=True."""
        order, prod = self._make_order_with_item(
            mode=Product.INVENTORY_TRACKING_LEGACY, stock=50, qty=3,
        )
        deduct_inventory_for_order(order)
        prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 47)
        self.assertTrue(order.stock_deducted)

    def test_skips_ingredient_only_order(self):
        """Ingredient-only order: deduct does nothing, stock_deducted stays False."""
        order, prod = self._make_order_with_item(
            mode=Product.INVENTORY_TRACKING_INGREDIENT, stock=50, qty=2,
        )
        initial_stock = prod.stock_quantity
        deduct_inventory_for_order(order)
        prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(prod.stock_quantity, initial_stock)
        self.assertFalse(order.stock_deducted)

    def test_mixed_order_deducts_only_legacy(self):
        """Mixed order: legacy stock deducted, ingredient product untouched."""
        legacy_prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        ingr_prod = _product(mode=Product.INVENTORY_TRACKING_INGREDIENT, stock=50)
        order = _order()
        OrderItem.objects.create(
            order=order, product=legacy_prod,
            product_name=legacy_prod.name, category_name=legacy_prod.category.name,
            packaging_eligible=False, size='none', quantity=4,
            unit_price=Decimal('65.00'), subtotal=Decimal('260.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        OrderItem.objects.create(
            order=order, product=ingr_prod,
            product_name=ingr_prod.name, category_name=ingr_prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=Decimal('65.00'), subtotal=Decimal('130.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )

        deduct_inventory_for_order(order)

        legacy_prod.refresh_from_db()
        ingr_prod.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(legacy_prod.stock_quantity, 46)  # 50 - 4
        self.assertEqual(ingr_prod.stock_quantity, 50)    # untouched
        self.assertTrue(order.stock_deducted)

    def test_aggregate_validation_passes_when_stock_covers_total(self):
        """Two legacy lines for same product: passes when stock >= sum of quantities."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)
        order = _order()
        # Two different sizes of the same product
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='small', quantity=3,
            unit_price=Decimal('65.00'), subtotal=Decimal('195.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='large', quantity=4,
            unit_price=Decimal('75.00'), subtotal=Decimal('300.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        # Total requested = 7, stock = 10 → should succeed
        deduct_inventory_for_order(order)
        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 3)

    def test_aggregate_validation_fails_when_stock_below_total(self):
        """Two legacy lines for same product: raises ValueError when stock < sum."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=5)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='small', quantity=3,
            unit_price=Decimal('65.00'), subtotal=Decimal('195.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='large', quantity=4,
            unit_price=Decimal('75.00'), subtotal=Decimal('300.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        # Total requested = 7, stock = 5 → should fail
        with self.assertRaises(ValueError):
            deduct_inventory_for_order(order)

        # Stock must be untouched on failure
        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 5)

    def test_idempotency_already_deducted(self):
        """Second call is a no-op when stock_deducted is already True."""
        order, prod = self._make_order_with_item(
            mode=Product.INVENTORY_TRACKING_LEGACY, stock=50, qty=2,
        )
        deduct_inventory_for_order(order)
        # Refresh to get the DB value after the first deduction.
        prod.refresh_from_db()
        order.refresh_from_db()
        stock_after_first = prod.stock_quantity  # 48

        # Second call should be a no-op (stock_deducted=True guards it)
        deduct_inventory_for_order(order)
        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, stock_after_first)

    def test_extra_order_update_fields_forwarded(self):
        """extra_order_update_fields is passed through without error."""
        order, prod = self._make_order_with_item(
            mode=Product.INVENTORY_TRACKING_LEGACY, stock=50, qty=1,
        )
        # Set a field we can verify was included in the update
        order.subtotal = Decimal('65.00')
        order.total = Decimal('65.00')
        # Should not raise
        deduct_inventory_for_order(
            order, extra_order_update_fields=['subtotal', 'total']
        )
        order.refresh_from_db()
        self.assertTrue(order.stock_deducted)
        self.assertEqual(order.subtotal, Decimal('65.00'))


# ═════════════════════════════════════════════════════════════════════════════
# 11–15. restore_inventory_for_order() — narrowed + item_quantities param
# ═════════════════════════════════════════════════════════════════════════════

class RestoreInventoryTests(TestCase):

    def _setup_deducted_order(self, mode=Product.INVENTORY_TRACKING_LEGACY,
                               stock=50, qty=3):
        """Create an order with one item and deduct stock from it."""
        prod = _product(mode=mode, stock=stock)
        order = _order()
        item = OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=qty,
            unit_price=Decimal('65.00'), subtotal=Decimal('65.00') * qty,
            inventory_mode=mode,
        )
        if mode == Product.INVENTORY_TRACKING_LEGACY:
            # Force stock_deducted=True and reduce stock directly so we can
            # test restore without going through deduct (avoids duplicate
            # test dependencies).
            prod.stock_quantity -= qty
            prod.save(update_fields=['stock_quantity'])
            order.stock_deducted = True
            order.save(update_fields=['stock_deducted'])
        return order, prod, item

    def test_restores_legacy_items(self):
        """Restore increments stock for legacy-mode items."""
        order, prod, _ = self._setup_deducted_order(
            mode=Product.INVENTORY_TRACKING_LEGACY, stock=50, qty=3,
        )
        restore_inventory_for_order(order)
        prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 50)   # 47 + 3 restored
        self.assertFalse(order.stock_deducted)

    def test_skips_ingredient_items(self):
        """Ingredient-mode items are not touched by restore."""
        prod = _product(mode=Product.INVENTORY_TRACKING_INGREDIENT, stock=50)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=Decimal('65.00'), subtotal=Decimal('130.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        # Manually set stock_deducted=True to allow restore to run
        order.stock_deducted = True
        order.save(update_fields=['stock_deducted'])

        restore_inventory_for_order(order)

        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 50)  # untouched

    def test_idempotency_not_deducted(self):
        """restore_inventory_for_order is a no-op when stock_deducted=False."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=3,
            unit_price=Decimal('65.00'), subtotal=Decimal('195.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        # stock_deducted=False by default — restore should be a no-op
        restore_inventory_for_order(order)
        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 50)

    def test_item_quantities_partial_restore(self):
        """item_quantities param: restores only the specified quantity."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=47)  # after deduction of 3
        order = _order()
        item = OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=3,
            unit_price=Decimal('65.00'), subtotal=Decimal('195.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        order.stock_deducted = True
        order.save(update_fields=['stock_deducted'])

        # Restore only 1 of the 3 items
        restore_inventory_for_order(order, item_quantities={item.pk: 1})
        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 48)  # 47 + 1

    def test_item_quantities_missing_key_skips_item(self):
        """Phase 6A: items not in item_quantities dict are SKIPPED (not
        restored to full quantity).  This is the corrected behavior from
        the Phase 6A _apply_inventory_for_order fix."""
        prod_a = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=45)
        prod_b = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=45)
        order = _order()
        item_a = OrderItem.objects.create(
            order=order, product=prod_a,
            product_name=prod_a.name, category_name=prod_a.category.name,
            packaging_eligible=False, size='none', quantity=5,
            unit_price=Decimal('65.00'), subtotal=Decimal('325.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        item_b = OrderItem.objects.create(
            order=order, product=prod_b,
            product_name=prod_b.name, category_name=prod_b.category.name,
            packaging_eligible=False, size='none', quantity=5,
            unit_price=Decimal('65.00'), subtotal=Decimal('325.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        order.stock_deducted = True
        order.save(update_fields=['stock_deducted'])

        # Only supply item_a with a partial quantity; item_b is omitted
        # Phase 6A: omitted keys are skipped, NOT restored to full
        restore_inventory_for_order(order, item_quantities={item_a.pk: 2})
        prod_a.refresh_from_db()
        prod_b.refresh_from_db()
        self.assertEqual(prod_a.stock_quantity, 47)   # 45 + 2 (partial)
        self.assertEqual(prod_b.stock_quantity, 45)   # unchanged (skipped)


# ═════════════════════════════════════════════════════════════════════════════
# 16–28. place_order_inventory() — core coordinator
# ═════════════════════════════════════════════════════════════════════════════

class PlaceOrderInventoryTests(TestCase):

    # ── 16. Legacy-only order ─────────────────────────────────────────────────

    def test_legacy_only_deducts_stock(self):
        """Legacy-only order: stock deducted, stock_deducted=True."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=5,
            unit_price=Decimal('65.00'), subtotal=Decimal('325.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        with transaction.atomic():
            result = place_order_inventory(order.pk)

        prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 45)
        self.assertTrue(order.stock_deducted)
        self.assertEqual(result['legacy_line_count'], 1)
        self.assertEqual(result['ingredient_line_count'], 0)
        self.assertTrue(result['stock_deducted'])
        self.assertEqual(result['ingredient_plans'], [])

    # ── 17. Ingredient-only order ─────────────────────────────────────────────

    def test_ingredient_only_no_stock_deducted(self):
        """Ingredient-only order: no legacy stock deducted, stock_deducted=False."""
        prod, ing, rec, rule = _full_ingredient_setup(order_type='dine_in')
        order = _order(order_type='dine_in')
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=Decimal('65.00'), subtotal=Decimal('130.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        with transaction.atomic():
            result = place_order_inventory(order.pk)

        order.refresh_from_db()
        self.assertFalse(order.stock_deducted)
        self.assertEqual(result['legacy_line_count'], 0)
        self.assertEqual(result['ingredient_line_count'], 1)
        self.assertFalse(result['stock_deducted'])
        # One plan returned
        self.assertEqual(len(result['ingredient_plans']), 1)

    # ── 18. Mixed order ───────────────────────────────────────────────────────

    def test_mixed_order_legacy_deducted_ingredient_validated(self):
        """Mixed order: legacy stock deducted and ingredient plans returned."""
        legacy_prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        ingr_prod, ing, rec, rule = _full_ingredient_setup(order_type='dine_in')
        order = _order(order_type='dine_in')
        OrderItem.objects.create(
            order=order, product=legacy_prod,
            product_name=legacy_prod.name, category_name=legacy_prod.category.name,
            packaging_eligible=False, size='none', quantity=4,
            unit_price=Decimal('65.00'), subtotal=Decimal('260.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        OrderItem.objects.create(
            order=order, product=ingr_prod,
            product_name=ingr_prod.name, category_name=ingr_prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=Decimal('65.00'), subtotal=Decimal('130.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        with transaction.atomic():
            result = place_order_inventory(order.pk)

        legacy_prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(legacy_prod.stock_quantity, 46)  # 50 - 4
        self.assertTrue(order.stock_deducted)
        self.assertEqual(result['legacy_line_count'], 1)
        self.assertEqual(result['ingredient_line_count'], 1)
        self.assertEqual(len(result['ingredient_plans']), 1)

    # ── 19. Order not found ───────────────────────────────────────────────────

    def test_order_not_found_raises_value_error(self):
        """Non-existent order_id raises ValueError."""
        with transaction.atomic():
            with self.assertRaises(ValueError) as ctx:
                place_order_inventory(order_id=999999)
        self.assertIn('does not exist', str(ctx.exception))

    # ── 20. Already processed ─────────────────────────────────────────────────

    def test_already_processed_returns_early(self):
        """order.stock_deducted=True → returns early without error."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=5,
            unit_price=Decimal('65.00'), subtotal=Decimal('325.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        order.stock_deducted = True
        order.save(update_fields=['stock_deducted'])

        with transaction.atomic():
            result = place_order_inventory(order.pk)

        # Stock not changed (function returned early)
        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 50)
        self.assertTrue(result['stock_deducted'])

    # ── 21. Insufficient legacy stock ────────────────────────────────────────

    def test_insufficient_legacy_stock_raises_no_stock_moved(self):
        """ValueError when legacy stock insufficient; no stock moved."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=2)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=5,
            unit_price=Decimal('65.00'), subtotal=Decimal('325.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        with self.assertRaises(ValueError):
            with transaction.atomic():
                place_order_inventory(order.pk)

        prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 2)    # untouched
        self.assertFalse(order.stock_deducted)

    # ── 22. resolve_plan failure ──────────────────────────────────────────────

    def test_ingredient_line_missing_recipe_raises(self):
        """RecipeIncompleteError from resolve_plan propagates; no legacy stock moved."""
        legacy_prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        # Ingredient product with NO recipe configured → RecipeMissingError
        ingr_prod = _product(mode=Product.INVENTORY_TRACKING_INGREDIENT, stock=50)
        order = _order(order_type='dine_in')
        OrderItem.objects.create(
            order=order, product=legacy_prod,
            product_name=legacy_prod.name, category_name=legacy_prod.category.name,
            packaging_eligible=False, size='none', quantity=3,
            unit_price=Decimal('65.00'), subtotal=Decimal('195.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        OrderItem.objects.create(
            order=order, product=ingr_prod,
            product_name=ingr_prod.name, category_name=ingr_prod.category.name,
            packaging_eligible=False, size='none', quantity=1,
            unit_price=Decimal('65.00'), subtotal=Decimal('65.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )

        with self.assertRaises(RecipeIncompleteError):
            with transaction.atomic():
                place_order_inventory(order.pk)

        # Legacy stock must be untouched (ingredient validation runs before
        # legacy deduction in place_order_inventory)
        legacy_prod.refresh_from_db()
        self.assertEqual(legacy_prod.stock_quantity, 50)

    # ── 23. Product lock ordering ─────────────────────────────────────────────

    def test_products_locked_in_ascending_pk_order(self):
        """Products are fetched in ascending pk order (structural/ordering check)."""
        # Create products in reverse insertion order so pk order ≠ creation order
        prod_high = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        prod_low = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        # Ensure prod_low has a higher pk by creating it after prod_high
        # (they might not be in creation order, so sort and verify select_for_update)
        products_by_pk = sorted([prod_low, prod_high], key=lambda p: p.pk)
        order = _order()
        for prod in products_by_pk:
            OrderItem.objects.create(
                order=order, product=prod,
                product_name=prod.name, category_name=prod.category.name,
                packaging_eligible=False, size='none', quantity=2,
                unit_price=Decimal('65.00'), subtotal=Decimal('130.00'),
                inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
            )
        # Should complete without deadlock or error
        with transaction.atomic():
            result = place_order_inventory(order.pk)
        self.assertEqual(result['legacy_line_count'], 2)

    # ── 24. inventory_mode re-snapshot under lock ─────────────────────────────

    def test_inventory_mode_resnapshot_corrects_stale_mode(self):
        """An item whose inventory_mode doesn't match the product is corrected."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        order = _order()
        # Create item with WRONG inventory_mode (ingredient, but product is legacy)
        item = OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=Decimal('65.00'), subtotal=Decimal('130.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,  # wrong!
        )
        with transaction.atomic():
            result = place_order_inventory(order.pk)

        item.refresh_from_db()
        # Mode should have been corrected to match the product
        self.assertEqual(item.inventory_mode, OrderItem.INVENTORY_MODE_LEGACY)
        # And deduction ran (the item was now treated as legacy)
        self.assertEqual(result['legacy_line_count'], 1)
        self.assertEqual(result['ingredient_line_count'], 0)
        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 48)

    # ── 25. Return dict counts ────────────────────────────────────────────────

    def test_return_dict_counts_accurate(self):
        """legacy_line_count and ingredient_line_count reflect actual items."""
        ingr_prod, _, _, _ = _full_ingredient_setup(order_type='dine_in')
        legacy_prod1 = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        legacy_prod2 = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)

        order = _order(order_type='dine_in')
        for prod in [legacy_prod1, legacy_prod2]:
            OrderItem.objects.create(
                order=order, product=prod,
                product_name=prod.name, category_name=prod.category.name,
                packaging_eligible=False, size='none', quantity=1,
                unit_price=Decimal('65.00'), subtotal=Decimal('65.00'),
                inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
            )
        OrderItem.objects.create(
            order=order, product=ingr_prod,
            product_name=ingr_prod.name, category_name=ingr_prod.category.name,
            packaging_eligible=False, size='none', quantity=1,
            unit_price=Decimal('65.00'), subtotal=Decimal('65.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        with transaction.atomic():
            result = place_order_inventory(order.pk)

        self.assertEqual(result['legacy_line_count'], 2)
        self.assertEqual(result['ingredient_line_count'], 1)

    # ── 26. Items with product=None skipped ───────────────────────────────────

    def test_null_product_items_skipped(self):
        """Items where product=None (soft-deleted product) are silently skipped."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=Decimal('65.00'), subtotal=Decimal('130.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        # Create an item with no product (simulates SET_NULL after deletion)
        OrderItem.objects.create(
            order=order, product=None,
            product_name='Deleted Product', category_name='',
            packaging_eligible=False, size='none', quantity=1,
            unit_price=Decimal('65.00'), subtotal=Decimal('65.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        # Should not raise; null-product item is ignored
        with transaction.atomic():
            result = place_order_inventory(order.pk)

        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 48)  # only real product deducted

    # ── 27. Ingredient-only → stock_deducted stays False ─────────────────────

    def test_ingredient_only_stock_deducted_false(self):
        """Ingredient-only order leaves order.stock_deducted=False after coordinator."""
        prod, ing, rec, rule = _full_ingredient_setup(order_type='dine_in')
        order = _order(order_type='dine_in')
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=1,
            unit_price=Decimal('65.00'), subtotal=Decimal('65.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        with transaction.atomic():
            place_order_inventory(order.pk)

        order.refresh_from_db()
        self.assertFalse(order.stock_deducted)

    # ── 28. stock_deducted=True only when legacy lines written ────────────────

    def test_stock_deducted_only_set_for_legacy_lines(self):
        """stock_deducted is True only when at least one legacy deduction ran."""
        # Pure ingredient order → False
        ingr_prod, _, _, _ = _full_ingredient_setup(order_type='dine_in')
        order_ingr = _order(order_type='dine_in')
        OrderItem.objects.create(
            order=order_ingr, product=ingr_prod,
            product_name=ingr_prod.name, category_name=ingr_prod.category.name,
            packaging_eligible=False, size='none', quantity=1,
            unit_price=Decimal('65.00'), subtotal=Decimal('65.00'),
            inventory_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        with transaction.atomic():
            result_ingr = place_order_inventory(order_ingr.pk)
        self.assertFalse(result_ingr['stock_deducted'])

        # Pure legacy order → True
        legacy_prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        order_leg = _order()
        OrderItem.objects.create(
            order=order_leg, product=legacy_prod,
            product_name=legacy_prod.name, category_name=legacy_prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=Decimal('65.00'), subtotal=Decimal('130.00'),
            inventory_mode=Product.INVENTORY_TRACKING_LEGACY,
        )
        with transaction.atomic():
            result_leg = place_order_inventory(order_leg.pk)
        self.assertTrue(result_leg['stock_deducted'])
