"""
tests_phase5a_hardening.py
==========================
Phase 5A Hardening — focused tests for the two corrections applied after
the Stage A/B audit:

  A. POS order_type explicit rejection
  B. Order row lock inside place_order_inventory()
  C. Regression: idempotency, checkout and POS inventory integration

Coverage:

  A. POS order_type validation
  ─────────────────────────────
  1.  Invalid order_type value → success=False, descriptive error, no order.
  2.  order_type not sent at all → defaults to 'dine_in', order created.
  3.  Valid order_type='dine_in' → order created with dine_in.
  4.  Valid order_type='takeout' → order created with takeout.
  5.  Error response carries the invalid value in the message.
  6.  Error response uses the existing POS format {success, error}.
  7.  Validation happens before any DB write (stock untouched on rejection).
  8.  JSON array as order_type → rejected cleanly, no TypeError.
  9.  JSON object as order_type → rejected cleanly, no TypeError.

  B. Order row lock
  ──────────────────
  8.  Coordinator called outside a transaction raises an error (SQLite) or
      completes safely — validated structurally; on SQLite select_for_update
      is accepted without real locking but must not raise.
  9.  Coordinator acquires the Order lock within the caller's atomic block:
      idempotency re-check reads the post-lock value.

  C. Idempotency regression
  ──────────────────────────
  10. Calling coordinator twice on same legacy order: second call is a no-op,
      stock not deducted twice.
  11. Checkout deducts stock; subsequent payment confirmation does not deduct again.
  12. POS order created, payment confirmed, stock deducted exactly once.
"""

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import InventoryItem, InventoryLog, ProductPackagingRule, Recipe, RecipeItem
from apps.menu.models import Category, Product
from apps.orders.models import Cart, CartItem, Order, OrderItem
from apps.orders.services import place_order_inventory

User = get_user_model()
_PASSWORD = 'harden-5a-pass'
_counter = 0


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _cashier(username=None):
    uid = username or f'ch5h_{_uid()}'
    return User.objects.create_user(username=uid, password=_PASSWORD, role='cashier')


def _category(name=None):
    n = name or f'Cat5h_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower().replace(' ', '_'))


def _product(name=None, stock=50, mode=Product.INVENTORY_TRACKING_LEGACY):
    n = name or f'Prod5h_{_uid()}'
    return Product.objects.create(
        category=_category(), name=n,
        price=Decimal('65.00'),
        stock_quantity=stock,
        inventory_tracking_mode=mode,
    )


def _order(order_type='dine_in'):
    return Order.objects.create(customer_name='H Customer', order_type=order_type)


def _pos_payload(client, product, quantity=1, order_type='dine_in',
                 extra_fields=None):
    payload = {
        'customer_name': 'Walk-in',
        'items': [{'product_id': product.pk, 'quantity': quantity, 'size': 'none'}],
        'order_type': order_type,
    }
    if extra_fields:
        payload.update(extra_fields)
    return client.post(
        reverse('orders:create_pos_order'),
        json.dumps(payload),
        content_type='application/json',
    )


# ═════════════════════════════════════════════════════════════════════════════
# A.1–7. POS order_type explicit rejection
# ═════════════════════════════════════════════════════════════════════════════

class PosOrderTypeRejectionTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.product = _product(stock=10)

    def test_invalid_value_rejected(self):
        """Any unrecognised order_type returns success=False."""
        response = _pos_payload(self.client, self.product, order_type='delivery')
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('error', data)

    def test_missing_order_type_defaults_to_dine_in(self):
        """Omitting order_type defaults to 'dine_in' and creates the order."""
        payload = {
            'customer_name': 'Walk-in',
            'items': [{'product_id': self.product.pk, 'quantity': 1, 'size': 'none'}],
            # order_type intentionally absent
        }
        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps(payload),
            content_type='application/json',
        )
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual(order.order_type, 'dine_in')

    def test_valid_dine_in_accepted(self):
        """Valid order_type='dine_in' creates the order correctly."""
        response = _pos_payload(self.client, self.product, order_type='dine_in')
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(Order.objects.get(pk=data['order_id']).order_type, 'dine_in')

    def test_valid_takeout_accepted(self):
        """Valid order_type='takeout' creates the order correctly."""
        response = _pos_payload(self.client, self.product, order_type='takeout')
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(Order.objects.get(pk=data['order_id']).order_type, 'takeout')

    def test_error_message_contains_invalid_value(self):
        """The rejection error message includes the submitted invalid value."""
        response = _pos_payload(self.client, self.product, order_type='curbside')
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('curbside', data['error'])

    def test_error_response_format(self):
        """Rejection uses the existing POS error format {success: false, error: str}."""
        response = _pos_payload(self.client, self.product, order_type='drive_thru')
        data = response.json()
        self.assertIn('success', data)
        self.assertIn('error', data)
        self.assertFalse(data['success'])
        self.assertIsInstance(data['error'], str)
        self.assertGreater(len(data['error']), 0)

    def test_no_db_write_on_rejection(self):
        """Rejection happens before any DB write: no Order, no stock change."""
        stock_before = self.product.stock_quantity
        _pos_payload(self.client, self.product, order_type='invalid_type')
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, stock_before)

    def test_null_order_type_rejected(self):
        """Explicit null order_type is rejected (not treated as omitted)."""
        payload = {
            'customer_name': 'Walk-in',
            'items': [{'product_id': self.product.pk, 'quantity': 1, 'size': 'none'}],
            'order_type': None,
        }
        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps(payload),
            content_type='application/json',
        )
        data = response.json()
        # None is coerced to string 'None' or to None by data.get();
        # either way it is not in VALID_ORDER_TYPES and must be rejected.
        self.assertFalse(data['success'])

    def _assert_non_string_order_type_rejected(self, order_type_value):
        """
        Helper: send a POS request whose order_type is the given non-string
        JSON value and assert the response is a clean rejection (no
        TypeError, existing error format, no DB records created).
        """
        payload = {
            'customer_name': 'Walk-in',
            'items': [{'product_id': self.product.pk, 'quantity': 1,
                       'size': 'none'}],
            'order_type': order_type_value,
        }
        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps(payload),
            content_type='application/json',
        )
        data = response.json()
        # Must not raise TypeError — response must be valid JSON
        self.assertIsInstance(data, dict)
        # Existing POS error format
        self.assertIn('success', data)
        self.assertIn('error', data)
        self.assertFalse(data['success'])
        self.assertIsInstance(data['error'], str)
        # No DB records created
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_array_order_type_rejected(self):
        """JSON array as order_type is rejected cleanly without TypeError."""
        self._assert_non_string_order_type_rejected([])

    def test_object_order_type_rejected(self):
        """JSON object as order_type is rejected cleanly without TypeError."""
        self._assert_non_string_order_type_rejected({})


# ═════════════════════════════════════════════════════════════════════════════
# B.8–9. Order row lock
# ═════════════════════════════════════════════════════════════════════════════

class OrderRowLockTests(TestCase):
    """
    Structural tests for the Order row lock inside place_order_inventory().

    On SQLite, select_for_update() is accepted without raising but does not
    produce real row locks.  These tests verify the lock call does not break
    any existing behavior and that the coordinator correctly reads the locked
    order's state (used for the idempotency guard).
    """

    def _order_with_legacy_item(self, stock=50, qty=2):
        prod = _product(stock=stock)
        order = _order()
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=qty,
            unit_price=Decimal('65.00'), subtotal=Decimal('65.00') * qty,
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        return order, prod

    def test_coordinator_runs_inside_transaction(self):
        """Coordinator completes without error when called inside transaction.atomic()."""
        order, prod = self._order_with_legacy_item(stock=50, qty=2)
        with transaction.atomic():
            result = place_order_inventory(order.pk)
        self.assertEqual(result['legacy_line_count'], 1)
        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 48)

    def test_idempotency_guard_reads_locked_row(self):
        """
        After the first coordinator call sets stock_deducted=True, a second
        call within a new transaction reads the locked row and returns early
        without deducting again.
        """
        order, prod = self._order_with_legacy_item(stock=50, qty=2)

        # First call — deducts stock
        with transaction.atomic():
            place_order_inventory(order.pk)

        prod.refresh_from_db()
        order.refresh_from_db()
        stock_after_first = prod.stock_quantity   # 48
        self.assertTrue(order.stock_deducted)

        # Second call — must be idempotent
        with transaction.atomic():
            result = place_order_inventory(order.pk)

        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, stock_after_first)  # unchanged
        self.assertTrue(result['stock_deducted'])


# ═════════════════════════════════════════════════════════════════════════════
# C.10–12. Idempotency and payment regression
# ═════════════════════════════════════════════════════════════════════════════

class IdempotencyRegressionTests(TestCase):

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.product = _product(stock=20)

    def test_coordinator_double_call_does_not_deduct_twice(self):
        """place_order_inventory called twice on the same order deducts once."""
        order, prod = (_order(), self.product)
        OrderItem.objects.create(
            order=order, product=prod,
            product_name=prod.name, category_name=prod.category.name,
            packaging_eligible=False, size='none', quantity=3,
            unit_price=Decimal('65.00'), subtotal=Decimal('195.00'),
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        with transaction.atomic():
            place_order_inventory(order.pk)
        with transaction.atomic():
            place_order_inventory(order.pk)   # idempotent second call

        prod.refresh_from_db()
        self.assertEqual(prod.stock_quantity, 17)   # 20 - 3, not 20 - 6
        self.assertEqual(
            InventoryLog.objects.filter(product=prod, action='sale').count(), 1
        )

    def test_checkout_payment_confirmation_does_not_deduct_again(self):
        """
        Stock is deducted at checkout, not at payment confirmation.
        Confirming payment must leave the stock count unchanged.
        """
        # Set up cart
        session = self.client.session
        session['_seed'] = 'x'
        session.save()
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(
            cart=cart, product=self.product, size='none',
            quantity=2, unit_price=self.product.price,
        )

        # Submit checkout (anonymous client — log out first)
        anon_client = self.client_class()
        anon_session = anon_client.session
        anon_session['_seed'] = 'x'
        anon_session.save()
        anon_cart = Cart.objects.create(session_key=anon_session.session_key)
        CartItem.objects.create(
            cart=anon_cart, product=self.product, size='none',
            quantity=2, unit_price=self.product.price,
        )
        response = anon_client.post(reverse('orders:checkout'), {
            'customer_name': 'Idempotency Tester',
            'order_type': 'dine_in',
            'payment_method': 'cash',
        })
        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.get()
        self.product.refresh_from_db()
        stock_after_checkout = self.product.stock_quantity  # deducted once

        # Now process payment (cashier logs in to confirm)
        self.client.force_login(self.cashier)
        self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'cash', 'amount_paid': str(order.total)},
        )

        self.product.refresh_from_db()
        # Stock must not change further after payment confirmation
        self.assertEqual(self.product.stock_quantity, stock_after_checkout)

    def test_pos_order_payment_confirmation_does_not_deduct_again(self):
        """
        POS: stock deducted at order creation, not at payment confirmation.
        """
        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Walk-in',
                'items': [{'product_id': self.product.pk, 'quantity': 2,
                           'size': 'none'}],
                'order_type': 'dine_in',
                'payment_method': 'cash',
            }),
            content_type='application/json',
        )
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])

        self.product.refresh_from_db()
        stock_after_creation = self.product.stock_quantity   # 18

        self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'cash', 'amount_paid': str(order.total)},
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, stock_after_creation)
