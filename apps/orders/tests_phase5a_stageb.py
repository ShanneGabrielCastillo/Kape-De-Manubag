"""
tests_phase5a_stageb.py
=======================
Phase 5A Stage B — Checkout and POS Integration tests.

Verifies that checkout_view and create_pos_order now route all inventory
operations through place_order_inventory() instead of calling
deduct_inventory_for_order() directly.

Coverage:

  Checkout — legacy-only
  ──────────────────────
  1.  Legacy-only checkout deducts stock correctly.
  2.  Insufficient legacy stock rolls back the entire checkout (no order,
      no items, no InventoryLog row, stock untouched).
  3.  Inventory mode is snapshotted at placement (legacy product → 'legacy').

  Checkout — ingredient-only
  ──────────────────────────
  4.  Ingredient-only checkout: stock_deducted=False, order created,
      ingredient configuration is validated.
  5.  Ingredient-only checkout: bad recipe config → ValueError, full rollback.

  Checkout — mixed order
  ──────────────────────
  6.  Mixed checkout: only legacy-tracked product stock is deducted.

  POS — legacy-only
  ─────────────────
  7.  Legacy-only POS order deducts stock; stock_deducted=True.
  8.  Insufficient legacy stock rolls back POS order.
  9.  POS response format (success, order_id, order_number) unchanged.

  POS — ingredient-only
  ─────────────────────
  10. Ingredient-only POS order: stock_deducted=False, order created.
  11. Ingredient-only POS bad config → success=False, error contains detail.

  POS — mixed order
  ─────────────────
  12. Mixed POS order: only legacy-tracked product stock deducted.

  POS — actor passed correctly
  ────────────────────────────
  13. InventoryLog.performed_by is set to the cashier for POS orders.
  14. InventoryLog.performed_by is None for customer checkout orders.

  POS — order_type validation
  ───────────────────────────
  15. Invalid order_type is rejected (success=False, no order created).
  16. Valid order_type='takeout' is stored correctly.

  Regression — existing checkout behavior unchanged
  ──────────────────────────────────────────────────
  17. Checkout redirects to payment_waiting on success.
  18. Checkout redirects to cart on ValueError (stock failure).
  19. Cart is cleared on successful checkout.
  20. Checkout stock deducted; InventoryLog carries source/reason fields.

  Regression — existing POS behavior unchanged
  ─────────────────────────────────────────────
  21. POS response includes success=True on success.
  22. POS order goes into awaiting_payment status.
  23. stock_deducted is True after a legacy POS order.
  24. Two POS orders cannot overdraw stock.

  Regression — cancellation / restore unchanged
  ─────────────────────────────────────────────
  25. Cancelling a legacy order restores stock; stock_deducted becomes False.
  26. Double-cancel does not restore twice.

All tests use SQLite.  No PostgreSQL-specific concurrency is tested here;
that is covered by Phase 4B-6 tests.
"""

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
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
from apps.menu.models import Category, Product
from apps.orders.models import Cart, CartItem, Order, OrderItem

User = get_user_model()
_PASSWORD = 'test-5b-pass'
_counter = 0


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _cashier(username=None):
    uid = username or f'cashier5b_{_uid()}'
    return User.objects.create_user(username=uid, password=_PASSWORD, role='cashier')


def _category(name=None, is_packaging_required=True):
    n = name or f'Cat5b_{_uid()}'
    return Category.objects.create(
        name=n, slug=n.lower().replace(' ', '_'),
        is_packaging_required=is_packaging_required,
    )


def _product(name=None, stock=100,
             mode=Product.INVENTORY_TRACKING_LEGACY, category=None):
    n = name or f'Prod5b_{_uid()}'
    cat = category or _category()
    return Product.objects.create(
        category=cat, name=n,
        price=Decimal('65.00'),
        stock_quantity=stock,
        inventory_tracking_mode=mode,
    )


def _ingredient_item(name=None, on_hand=Decimal('1000.000')):
    n = name or f'Ing5b_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type='ingredient', base_unit='g',
        on_hand_quantity=on_hand, is_active=True,
    )


def _approved_recipe(product, size='none', scope='all'):
    return Recipe.objects.create(
        product=product, size=size, order_type_scope=scope,
        status=Recipe.STATUS_APPROVED, revision=1,
        approved_at=timezone.now(),
        approval_note='5B test approval',
    )


def _no_packaging_rule(product, size='none', order_type='dine_in'):
    return ProductPackagingRule.objects.create(
        product=product, size=size, order_type=order_type,
        profile=None, no_packaging=True,
    )


def _full_ingredient_setup(product=None, size='none', order_type='dine_in'):
    """Create recipe + packaging rule so resolve_plan() succeeds."""
    prod = product or _product(mode=Product.INVENTORY_TRACKING_INGREDIENT)
    ing = _ingredient_item()
    rec = _approved_recipe(prod, size=size, scope='all')
    RecipeItem.objects.create(
        recipe=rec, inventory_item=ing, quantity=Decimal('50.000'),
    )
    rule = _no_packaging_rule(prod, size=size, order_type=order_type)
    return prod, ing, rec, rule


def _cart_with(client, product, quantity=2, size='none'):
    """Create a session cart with one item and return the cart."""
    session = client.session
    session['_seed'] = 'x'
    session.save()
    cart = Cart.objects.create(session_key=session.session_key)
    CartItem.objects.create(
        cart=cart, product=product, size=size,
        quantity=quantity, unit_price=product.price,
    )
    return cart


def _post_checkout(client, order_type='dine_in', payment_method='cash'):
    return client.post(reverse('orders:checkout'), {
        'customer_name': 'Stage B Customer',
        'order_type': order_type,
        'payment_method': payment_method,
    })


def _post_pos(client, product, quantity=2, size='none',
              order_type='dine_in', payment_method='cash',
              cashier=None, extra_items=None):
    items = [{'product_id': product.pk, 'quantity': quantity, 'size': size}]
    if extra_items:
        items.extend(extra_items)
    return client.post(
        reverse('orders:create_pos_order'),
        json.dumps({
            'customer_name': 'Walk-in',
            'items': items,
            'order_type': order_type,
            'payment_method': payment_method,
        }),
        content_type='application/json',
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1–3. Checkout — legacy-only
# ═════════════════════════════════════════════════════════════════════════════

class CheckoutLegacyTests(TestCase):

    def setUp(self):
        self.product = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)

    def test_legacy_checkout_deducts_stock(self):
        """Legacy-only checkout deducts stock and sets stock_deducted=True."""
        _cart_with(self.client, self.product, quantity=2)
        _post_checkout(self.client)
        order = Order.objects.get()
        self.assertTrue(order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)

    def test_insufficient_legacy_stock_rolls_back_checkout(self):
        """Insufficient stock: no order, no items, no log, stock untouched."""
        self.product.stock_quantity = 1
        self.product.save(update_fields=['stock_quantity'])
        _cart_with(self.client, self.product, quantity=2)
        _post_checkout(self.client)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)

    def test_inventory_mode_snapshotted_at_checkout(self):
        """inventory_mode='legacy' is snapshotted on the OrderItem."""
        _cart_with(self.client, self.product, quantity=1)
        _post_checkout(self.client)
        item = OrderItem.objects.get()
        self.assertEqual(item.inventory_mode, OrderItem.INVENTORY_MODE_LEGACY)


# ═════════════════════════════════════════════════════════════════════════════
# 4–5. Checkout — ingredient-only
# ═════════════════════════════════════════════════════════════════════════════

class CheckoutIngredientTests(TestCase):

    def setUp(self):
        self.prod, self.ing, self.rec, self.rule = _full_ingredient_setup(
            order_type='dine_in',
        )

    def test_ingredient_checkout_order_created_stock_deducted_false(self):
        """Ingredient-only checkout: order created, stock_deducted=False."""
        _cart_with(self.client, self.prod, quantity=1)
        _post_checkout(self.client, order_type='dine_in')
        order = Order.objects.get()
        self.assertFalse(order.stock_deducted)
        self.assertEqual(order.items.count(), 1)

    def test_ingredient_checkout_bad_config_rolls_back(self):
        """Ingredient product with no recipe → full rollback, redirect to cart."""
        bad_prod = _product(mode=Product.INVENTORY_TRACKING_INGREDIENT, stock=10)
        # No recipe/packaging rule for bad_prod — resolve_plan() will raise
        _cart_with(self.client, bad_prod, quantity=1)
        response = _post_checkout(self.client, order_type='dine_in')
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertRedirects(response, reverse('orders:cart'))


# ═════════════════════════════════════════════════════════════════════════════
# 6. Checkout — mixed order
# ═════════════════════════════════════════════════════════════════════════════

class CheckoutMixedTests(TestCase):

    def test_mixed_checkout_deducts_only_legacy(self):
        """Mixed cart: only legacy-tracked product stock is deducted."""
        legacy_prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        ingr_prod, _, _, _ = _full_ingredient_setup(order_type='dine_in')
        ingr_stock_before = ingr_prod.stock_quantity

        session = self.client.session
        session['_seed'] = 'x'
        session.save()
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(cart=cart, product=legacy_prod, size='none',
                                quantity=3, unit_price=legacy_prod.price)
        CartItem.objects.create(cart=cart, product=ingr_prod, size='none',
                                quantity=1, unit_price=ingr_prod.price)

        _post_checkout(self.client, order_type='dine_in')

        order = Order.objects.get()
        self.assertTrue(order.stock_deducted)
        legacy_prod.refresh_from_db()
        ingr_prod.refresh_from_db()
        self.assertEqual(legacy_prod.stock_quantity, 47)  # 50 - 3
        self.assertEqual(ingr_prod.stock_quantity, ingr_stock_before)  # untouched


# ═════════════════════════════════════════════════════════════════════════════
# 7–9. POS — legacy-only
# ═════════════════════════════════════════════════════════════════════════════

class PosLegacyTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.product = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)

    def test_legacy_pos_deducts_stock(self):
        """Legacy POS order deducts stock and sets stock_deducted=True."""
        response = _post_pos(self.client, self.product, quantity=3)
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertTrue(order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 7)

    def test_legacy_pos_insufficient_stock_rolls_back(self):
        """Insufficient stock: success=False, no order, no items, stock intact."""
        self.product.stock_quantity = 1
        self.product.save(update_fields=['stock_quantity'])
        response = _post_pos(self.client, self.product, quantity=2)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('Insufficient stock', data['error'])
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)

    def test_pos_response_format_unchanged(self):
        """POS success response still contains success, order_id, order_number."""
        response = _post_pos(self.client, self.product, quantity=1)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertIn('order_id', data)
        self.assertIn('order_number', data)
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual(order.order_number, data['order_number'])


# ═════════════════════════════════════════════════════════════════════════════
# 10–11. POS — ingredient-only
# ═════════════════════════════════════════════════════════════════════════════

class PosIngredientTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod, self.ing, self.rec, self.rule = _full_ingredient_setup(
            order_type='dine_in',
        )

    def test_ingredient_pos_order_created_stock_deducted_false(self):
        """Ingredient-only POS order: order created, stock_deducted=False."""
        response = _post_pos(self.client, self.prod, quantity=1,
                             order_type='dine_in')
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertFalse(order.stock_deducted)

    def test_ingredient_pos_bad_config_returns_error(self):
        """Ingredient product with no recipe: success=False, order rolled back."""
        bad_prod = _product(mode=Product.INVENTORY_TRACKING_INGREDIENT, stock=10)
        response = _post_pos(self.client, bad_prod, quantity=1,
                             order_type='dine_in')
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('error', data)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)


# ═════════════════════════════════════════════════════════════════════════════
# 12. POS — mixed order
# ═════════════════════════════════════════════════════════════════════════════

class PosMixedTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)

    def test_mixed_pos_deducts_only_legacy(self):
        """Mixed POS order: only legacy-tracked product stock deducted."""
        legacy_prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=50)
        ingr_prod, _, _, _ = _full_ingredient_setup(order_type='dine_in')
        ingr_stock_before = ingr_prod.stock_quantity

        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Mixed POS',
                'items': [
                    {'product_id': legacy_prod.pk, 'quantity': 4, 'size': 'none'},
                    {'product_id': ingr_prod.pk, 'quantity': 2, 'size': 'none'},
                ],
                'order_type': 'dine_in',
            }),
            content_type='application/json',
        )
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertTrue(order.stock_deducted)
        legacy_prod.refresh_from_db()
        ingr_prod.refresh_from_db()
        self.assertEqual(legacy_prod.stock_quantity, 46)       # 50 - 4
        self.assertEqual(ingr_prod.stock_quantity, ingr_stock_before)  # untouched


# ═════════════════════════════════════════════════════════════════════════════
# 13–14. Actor passed correctly to inventory logs
# ═════════════════════════════════════════════════════════════════════════════

class ActorTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()

    def test_pos_inventory_log_has_cashier_as_actor(self):
        """InventoryLog.performed_by is the logged-in cashier for POS orders."""
        self.client.force_login(self.cashier)
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)
        _post_pos(self.client, prod, quantity=1)
        log = InventoryLog.objects.get(product=prod, action='sale')
        self.assertEqual(log.performed_by, self.cashier)

    def test_checkout_inventory_log_has_no_actor(self):
        """InventoryLog.performed_by is None for anonymous customer checkouts."""
        prod = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)
        _cart_with(self.client, prod, quantity=1)
        _post_checkout(self.client)
        log = InventoryLog.objects.get(product=prod, action='sale')
        self.assertIsNone(log.performed_by)


# ═════════════════════════════════════════════════════════════════════════════
# 15–16. POS order_type validation
# Updated in Phase 5A hardening: invalid values are now rejected, not coerced.
# ═════════════════════════════════════════════════════════════════════════════

class PosOrderTypeValidationTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.product = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)

    def test_invalid_order_type_rejected(self):
        """An unrecognised order_type is rejected: success=False, no order created."""
        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Test',
                'items': [{'product_id': self.product.pk, 'quantity': 1,
                           'size': 'none'}],
                'order_type': 'delivery',   # invalid
            }),
            content_type='application/json',
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('order_type', data['error'])
        self.assertIn('delivery', data['error'])
        # No order should have been created
        self.assertEqual(Order.objects.count(), 0)

    def test_valid_takeout_order_type_stored(self):
        """Valid order_type='takeout' is stored correctly."""
        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Test',
                'items': [{'product_id': self.product.pk, 'quantity': 1,
                           'size': 'none'}],
                'order_type': 'takeout',
            }),
            content_type='application/json',
        )
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual(order.order_type, 'takeout')


# ═════════════════════════════════════════════════════════════════════════════
# 17–20. Regression — existing checkout behavior unchanged
# ═════════════════════════════════════════════════════════════════════════════

class CheckoutRegressionTests(TestCase):

    def setUp(self):
        self.product = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)

    def test_checkout_redirects_to_payment_waiting_on_success(self):
        """Successful checkout redirects to the payment-waiting page."""
        _cart_with(self.client, self.product, quantity=1)
        response = _post_checkout(self.client)
        order = Order.objects.get()
        self.assertRedirects(
            response,
            reverse('orders:payment_waiting',
                    kwargs={'tracking_token': order.tracking_token}),
        )

    def test_checkout_redirects_to_cart_on_value_error(self):
        """ValueError (e.g. insufficient stock) redirects back to cart."""
        self.product.stock_quantity = 0
        self.product.save(update_fields=['stock_quantity'])
        _cart_with(self.client, self.product, quantity=1)
        response = _post_checkout(self.client)
        self.assertRedirects(response, reverse('orders:cart'))

    def test_cart_cleared_on_successful_checkout(self):
        """Cart items are deleted after a successful checkout."""
        cart = _cart_with(self.client, self.product, quantity=1)
        _post_checkout(self.client)
        self.assertEqual(CartItem.objects.filter(cart=cart).count(), 0)

    def test_checkout_inventory_log_source_and_reason(self):
        """InventoryLog carries source='customer_order' and reason='Order #...'."""
        _cart_with(self.client, self.product, quantity=2)
        _post_checkout(self.client)
        order = Order.objects.get()
        log = InventoryLog.objects.get(product=self.product, action='sale')
        self.assertEqual(log.source, 'customer_order')
        self.assertEqual(log.reason, f'Order #{order.order_number}')
        self.assertEqual(log.quantity_change, -2)


# ═════════════════════════════════════════════════════════════════════════════
# 21–24. Regression — existing POS behavior unchanged
# ═════════════════════════════════════════════════════════════════════════════

class PosRegressionTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.product = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)

    def test_pos_success_json(self):
        """POS response is {success: True, order_id: ..., order_number: ...}."""
        response = _post_pos(self.client, self.product, quantity=1)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertIsInstance(data['order_id'], int)
        self.assertIsInstance(data['order_number'], str)

    def test_pos_order_in_awaiting_payment(self):
        """New POS order starts in awaiting_payment status."""
        response = _post_pos(self.client, self.product, quantity=1)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.status, 'awaiting_payment')

    def test_pos_stock_deducted_true_for_legacy(self):
        """stock_deducted=True after a successful legacy POS order."""
        response = _post_pos(self.client, self.product, quantity=2)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertTrue(order.stock_deducted)

    def test_two_pos_orders_cannot_overdraw_stock(self):
        """Two sequential POS orders cannot together exceed available stock."""
        self.product.stock_quantity = 5
        self.product.save(update_fields=['stock_quantity'])

        first = _post_pos(self.client, self.product, quantity=3)
        self.assertTrue(first.json()['success'])

        second = _post_pos(self.client, self.product, quantity=3)
        data = second.json()
        self.assertFalse(data['success'])
        self.assertIn('Insufficient stock', data['error'])
        self.assertEqual(Order.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertGreaterEqual(self.product.stock_quantity, 0)


# ═════════════════════════════════════════════════════════════════════════════
# 25–26. Regression — cancellation / restore unchanged
# ═════════════════════════════════════════════════════════════════════════════

class CancellationRegressionTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.product = _product(mode=Product.INVENTORY_TRACKING_LEGACY, stock=10)

    def _deducted_order(self, quantity=2):
        order = Order.objects.create(customer_name='Test Customer')
        OrderItem.objects.create(
            order=order, product=self.product,
            product_name=self.product.name,
            category_name=self.product.category.name,
            packaging_eligible=False,
            size='none', quantity=quantity,
            unit_price=Decimal('65.00'),
            subtotal=Decimal('65.00') * quantity,
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        self.product.stock_quantity -= quantity
        self.product.save(update_fields=['stock_quantity'])
        order.stock_deducted = True
        order.save(update_fields=['stock_deducted'])
        return order

    def test_cancel_restores_stock(self):
        """Cancelling a deducted legacy order restores stock."""
        order = self._deducted_order(quantity=2)
        self.client.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )
        self.product.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertFalse(order.stock_deducted)
        log = InventoryLog.objects.get(product=self.product, action='adjustment')
        self.assertEqual(log.quantity_change, 2)

    def test_double_cancel_does_not_restore_twice(self):
        """Cancelling the same order twice does not double-restore stock."""
        order = self._deducted_order(quantity=2)
        for _ in range(2):
            self.client.post(
                reverse('orders:update_status', args=[order.pk]),
                {'status': 'cancelled'},
            )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(
            InventoryLog.objects.filter(
                product=self.product, action='adjustment'
            ).count(),
            1,
        )
