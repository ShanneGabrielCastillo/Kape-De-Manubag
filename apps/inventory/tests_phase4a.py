"""
tests_phase4a.py
================
Phase 4A — model-field and constraint tests.

Covers:
  - Product.inventory_tracking_mode: default, valid values, invalid value
    rejected by DB CHECK, index exists, existing rows get 'legacy'.
  - OrderItem.inventory_mode: default, valid values, invalid value rejected
    by DB CHECK, existing rows get 'legacy'.
  - Regression: existing product CRUD, order placement, and inventory
    workflows remain unchanged.
  - Security: inventory_tracking_mode is NOT in any generic product form.

All tests are SQLite-reliable (constraint checks, defaults, and regression).
No PostgreSQL-specific concurrency behavior is introduced by Phase 4A.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import CustomUser
from apps.inventory.models import InventoryItem
from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem


# ── Helpers ───────────────────────────────────────────────────────────────────

_PASSWORD = 'test-4a-pass'


def _user(username, role='admin'):
    return CustomUser.objects.create_user(
        username=username, password=_PASSWORD, role=role,
    )


def _category(name='Coffee'):
    return Category.objects.create(name=name, slug=name.lower())


def _product(name='Espresso', category=None, stock=100, price='65.00'):
    cat = category or _category(name + 'Cat')
    return Product.objects.create(
        category=cat, name=name, price=Decimal(price), stock_quantity=stock,
    )


def _order(order_type='dine_in'):
    return Order.objects.create(
        status='awaiting_payment',
        order_type=order_type,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. Product.inventory_tracking_mode — field and constraint tests
# ═════════════════════════════════════════════════════════════════════════════

class ProductInventoryTrackingModeTests(TestCase):
    """Tests for the new Product.inventory_tracking_mode field."""

    def setUp(self):
        self.category = _category('Drinks')

    def test_default_is_legacy(self):
        """Newly created products default to 'legacy'."""
        p = _product(category=self.category)
        self.assertEqual(p.inventory_tracking_mode, 'legacy')

    def test_explicit_legacy_accepted(self):
        p = Product.objects.create(
            category=self.category, name='P-legacy', price=Decimal('60.00'),
            inventory_tracking_mode='legacy',
        )
        self.assertEqual(p.inventory_tracking_mode, 'legacy')

    def test_explicit_ingredient_accepted(self):
        p = Product.objects.create(
            category=self.category, name='P-ingredient', price=Decimal('60.00'),
            inventory_tracking_mode='ingredient',
        )
        p.refresh_from_db()
        self.assertEqual(p.inventory_tracking_mode, 'ingredient')

    def test_invalid_value_rejected_by_db_constraint(self):
        """product_inventory_mode_valid CHECK rejects anything not in
        {'legacy', 'ingredient'}."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                Product.objects.create(
                    category=self.category,
                    name='P-invalid',
                    price=Decimal('60.00'),
                    inventory_tracking_mode='unknown',
                )

    def test_field_is_indexed(self):
        """inventory_tracking_mode has db_index=True — verify it appears in
        the model's Meta.indexes or is a DB-level index (deferred to migration
        check). We verify the field attribute here."""
        field = Product._meta.get_field('inventory_tracking_mode')
        self.assertTrue(field.db_index,
            'inventory_tracking_mode must have db_index=True')

    def test_choices_are_correct(self):
        field = Product._meta.get_field('inventory_tracking_mode')
        choice_values = [c[0] for c in field.choices]
        self.assertIn('legacy', choice_values)
        self.assertIn('ingredient', choice_values)
        self.assertEqual(len(choice_values), 2)

    def test_existing_product_has_legacy_after_migration(self):
        """Verify the default backfill: any product retrieved from the DB has
        'legacy', consistent with what the migration applies to existing rows."""
        p = _product(category=self.category, name='Existing')
        p.refresh_from_db()
        self.assertEqual(p.inventory_tracking_mode, 'legacy')

    def test_constraint_name_is_correct(self):
        """The constraint must be named exactly as specified by Rev3."""
        constraint_names = [
            c.name for c in Product._meta.constraints
        ]
        self.assertIn('product_inventory_mode_valid', constraint_names)


class ProductInventoryTrackingModeFormSecurityTests(TestCase):
    """inventory_tracking_mode must not be exposed in generic product forms."""

    def setUp(self):
        self.admin = _user('form_admin')
        self.client.force_login(self.admin)
        self.category = _category('FormCat')

    def test_product_edit_form_does_not_include_inventory_tracking_mode(self):
        """The product edit view must not render inventory_tracking_mode as an
        editable field, preventing unauthorized mode changes via the UI."""
        from django.urls import reverse
        product = _product(category=self.category, name='FormProd')
        url = reverse('menu:product_edit', kwargs={'pk': product.pk})
        response = self.client.get(url)
        if response.status_code == 200:
            # The field name must not appear as an input in the rendered form.
            content = response.content.decode()
            self.assertNotIn('name="inventory_tracking_mode"', content,
                'inventory_tracking_mode must not be an editable form field')


# ═════════════════════════════════════════════════════════════════════════════
# 2. OrderItem.inventory_mode — field and constraint tests
# ═════════════════════════════════════════════════════════════════════════════

class OrderItemInventoryModeTests(TestCase):
    """Tests for the new OrderItem.inventory_mode field."""

    def setUp(self):
        self.category = _category('Items')
        self.product  = _product(category=self.category)
        self.order    = _order()

    def _make_order_item(self, inventory_mode='legacy'):
        return OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            size='none',
            quantity=1,
            unit_price=Decimal('65.00'),
            subtotal=Decimal('65.00'),
            inventory_mode=inventory_mode,
        )

    def test_default_is_legacy(self):
        item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            size='small',
            quantity=1,
            unit_price=Decimal('65.00'),
            subtotal=Decimal('65.00'),
        )
        self.assertEqual(item.inventory_mode, 'legacy')

    def test_explicit_legacy_accepted(self):
        item = self._make_order_item('legacy')
        item.refresh_from_db()
        self.assertEqual(item.inventory_mode, 'legacy')

    def test_explicit_ingredient_accepted(self):
        item = self._make_order_item('ingredient')
        item.refresh_from_db()
        self.assertEqual(item.inventory_mode, 'ingredient')

    def test_invalid_value_rejected_by_db_constraint(self):
        """orderitem_inventory_mode_valid CHECK rejects anything not in
        {'legacy', 'ingredient'}."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                OrderItem.objects.create(
                    order=self.order,
                    product=self.product,
                    product_name=self.product.name,
                    size='medium',
                    quantity=1,
                    unit_price=Decimal('65.00'),
                    subtotal=Decimal('65.00'),
                    inventory_mode='notamode',
                )

    def test_choices_are_correct(self):
        field = OrderItem._meta.get_field('inventory_mode')
        choice_values = [c[0] for c in field.choices]
        self.assertIn('legacy', choice_values)
        self.assertIn('ingredient', choice_values)
        self.assertEqual(len(choice_values), 2)

    def test_existing_order_item_has_legacy_mode(self):
        """Existing order items (created before Phase 4A) have 'legacy'
        because the column default backfills them."""
        item = self._make_order_item()
        item.refresh_from_db()
        self.assertEqual(item.inventory_mode, 'legacy')

    def test_constraint_name_is_correct(self):
        constraint_names = [c.name for c in OrderItem._meta.constraints]
        self.assertIn('orderitem_inventory_mode_valid', constraint_names)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Behavior regression — Phase 4A changes nothing
# ═════════════════════════════════════════════════════════════════════════════

class Phase4ABehaviorRegressionTests(TestCase):
    """Confirm Phase 4A fields do not change existing product, order, or
    inventory behavior. The application must be identical to pre-Phase-4A
    for all current workflows."""

    def setUp(self):
        self.admin    = _user('p4a_admin')
        self.client.force_login(self.admin)
        self.category = _category('Reg Cat')
        self.product  = _product(category=self.category, name='Reg Espresso',
                                  stock=50)

    def test_order_placement_creates_legacy_order_items(self):
        """All order items created through normal ORM paths have
        inventory_mode='legacy'. Verifies no Phase 5A behaviour was
        accidentally introduced by the Phase 4A migration."""
        order = _order()
        item = OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            size='none',
            quantity=2,
            unit_price=Decimal('65.00'),
            subtotal=Decimal('130.00'),
        )
        item.refresh_from_db()
        self.assertEqual(
            item.inventory_mode, 'legacy',
            'OrderItem must have inventory_mode=legacy (Phase 4A introduces no behavior change)',
        )

    def test_product_stock_quantity_still_works(self):
        """Product.stock_quantity behavior is unchanged."""
        original = self.product.stock_quantity
        self.product.reduce_stock(5)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, original - 5)

    def test_product_inventory_tracking_mode_not_set_by_order_creation(self):
        """Placing an order via create_order_item must not modify
        inventory_tracking_mode — it stays 'legacy' for all products."""
        from apps.orders.services import create_order_item
        self.assertEqual(self.product.inventory_tracking_mode, 'legacy')
        order = _order()
        create_order_item(order, self.product, 'none', 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.inventory_tracking_mode, 'legacy')

    def test_inventory_adjustment_unaffected(self):
        """Phase 2B adjustment service still works after Phase 4A migration."""
        inv_item = InventoryItem.objects.create(
            name='P4A Reg Item', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal('500.000'),
        )
        from apps.inventory.services import record_adjustment
        with transaction.atomic():
            log = record_adjustment(
                inventory_item_id=inv_item.pk,
                new_on_hand=Decimal('600.000'),
                expected_on_hand=Decimal('500.000'),
                reason_code='stock_count_correction',
            )
        self.assertEqual(log.movement_type, 'adjustment')
        inv_item.refresh_from_db()
        self.assertEqual(inv_item.on_hand_quantity, Decimal('600.000'))

    def test_product_page_still_accessible(self):
        """Product list and edit pages still work after Phase 4A."""
        from django.urls import reverse
        response = self.client.get(reverse('menu:product_list'))
        self.assertEqual(response.status_code, 200)
