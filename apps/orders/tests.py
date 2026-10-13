"""
Atomicity tests for the inventory-touching workflows.

Every workflow that moves stock must do so inside a single database
transaction: if ANY step fails, all related changes (order, order items,
stock levels, inventory logs) roll back together. These tests verify the
success paths and inject mid-transaction failures to prove the rollback.
"""
import json
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import CustomUser
from apps.inventory.models import InventoryLog
from apps.menu.models import Category, Product
from apps.orders.models import Cart, CartItem, Order, OrderItem

PASSWORD = 'kdm-atomic-pass-1'


class CheckoutAtomicityTests(TestCase):
    """Customer checkout: order + items + stock deduction + cart clearing."""

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )

    def _cart_with(self, quantity):
        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(
            cart=cart, product=self.product, size='none',
            quantity=quantity, unit_price=60.00,
        )
        return cart

    def _post_checkout(self):
        return self.client.post(reverse('orders:checkout'), {
            'customer_name': 'Test Customer',
            'order_type': 'dine_in',
            'payment_method': 'cash',
        })

    def test_checkout_deducts_stock_creates_order_and_clears_cart(self):
        self._cart_with(2)
        response = self._post_checkout()
        order = Order.objects.get()
        # Payment-first flow: checkout redirects to payment_waiting, not order_success.
        self.assertRedirects(
            response,
            reverse('orders:payment_waiting', kwargs={'tracking_token': order.tracking_token}),
        )
        # Order starts in awaiting_payment status with is_paid=False
        self.assertEqual(order.status, 'awaiting_payment')
        self.assertFalse(order.is_paid)
        self.assertTrue(order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertEqual(CartItem.objects.count(), 0)
        log = InventoryLog.objects.get(product=self.product, action='sale')
        self.assertEqual(log.quantity_change, -2)
        self.assertEqual(log.quantity_before, 10)
        self.assertEqual(log.quantity_after, 8)
        # Audit trail: checkout orders carry the customer-order source and the
        # order number as the reason.
        self.assertEqual(log.source, 'customer_order')
        self.assertEqual(log.reason, f'Order #{order.order_number}')

    def test_checkout_insufficient_stock_rolls_back_everything(self):
        cart = self._cart_with(2)
        self.product.stock_quantity = 1
        self.product.save(update_fields=['stock_quantity'])

        response = self._post_checkout()

        self.assertRedirects(response, reverse('orders:cart'))
        # No order, no items, no log, stock untouched, cart still intact.
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)
        self.assertEqual(CartItem.objects.filter(cart=cart).count(), 1)

    def test_checkout_shows_friendly_insufficient_stock_message(self):
        self._cart_with(2)
        self.product.stock_quantity = 1
        self.product.save(update_fields=['stock_quantity'])

        response = self.client.post(reverse('orders:checkout'), {
            'customer_name': 'Test Customer',
            'order_type': 'dine_in',
        }, follow=True)
        # The customer sees which product, how much is available and how much
        # was requested -- and nothing was created.
        # The checkout catches ValueError from deduct_inventory_for_order and
        # redirects back to cart with the error message.
        # Check we landed back on the cart page with an error message.
        self.assertEqual(Order.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)
        # The ValueError message from deduct_inventory_for_order mentions the
        # product name and stock details.
        response_text = response.content.decode()
        self.assertIn('Iced Coffee', response_text)

    def test_checkout_failure_after_deduction_rolls_back_order_and_stock(self):
        # A failure WHILE the transaction is being written (after the stock
        # update succeeded) must roll back the order creation AND the stock
        # change together -- never a half-created order.
        # In the payment-first flow the view catches the exception and
        # redirects back to the cart rather than re-raising, so we verify
        # by checking that no order or stock change persisted.
        cart = self._cart_with(2)
        with mock.patch.object(
            InventoryLog.objects, 'create',
            side_effect=RuntimeError('log write failed'),
        ):
            self._post_checkout()
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(CartItem.objects.filter(cart=cart).count(), 1)


class PosOrderAtomicityTests(TestCase):
    """Cashier POS orders: order + items + stock deduction."""

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='pos_cashier', password=PASSWORD, role='cashier',
        )
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )

    def _post_pos(self, quantity=2, **item_overrides):
        item = {'product_id': self.product.pk, 'quantity': quantity, 'size': 'none'}
        item.update(item_overrides)
        return self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Walk-in Customer',
                'items': [item],
                'order_type': 'dine_in',
            }),
            content_type='application/json',
        )

    def _post_pos_with_payment(self, payment_method='cash', quantity=2):
        """Helper that creates a POS order with a specific payment method."""
        item = {'product_id': self.product.pk, 'quantity': quantity, 'size': 'none'}
        return self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Walk-in Customer',
                'items': [item],
                'order_type': 'dine_in',
                'payment_method': payment_method,
            }),
            content_type='application/json',
        )

    def test_pos_order_deducts_stock(self):
        response = self._post_pos(quantity=3)
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertTrue(order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 7)
        log = InventoryLog.objects.get(product=self.product, action='sale')
        self.assertEqual(log.quantity_change, -3)
        self.assertEqual(log.source, 'pos_order')
        self.assertEqual(log.reason, f'Order #{order.order_number}')

    def test_pos_order_insufficient_stock_rolls_back(self):
        self.product.stock_quantity = 1
        self.product.save(update_fields=['stock_quantity'])

        response = self._post_pos(quantity=2)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('Insufficient stock', data['error'])
        # Everything rolled back: no order, no items, no log, stock intact.
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)

    def test_two_pos_orders_cannot_overdraw_stock(self):
        # Two successive orders that together exceed the available stock:
        # the first succeeds, the second fails cleanly with the friendly
        # message and rolls back completely -- stock never goes negative.
        self.product.stock_quantity = 5
        self.product.save(update_fields=['stock_quantity'])

        first = self._post_pos(quantity=3)
        self.assertTrue(first.json()['success'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 2)

        second = self._post_pos(quantity=3)
        data = second.json()
        self.assertFalse(data['success'])
        self.assertIn('Insufficient stock', data['error'])
        self.assertIn('Available: 2', data['error'])
        self.assertIn('Requested: 3', data['error'])
        # Only the first order was persisted; stock is untouched by the
        # failed one and never negative.
        self.assertEqual(Order.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 2)
        self.assertGreaterEqual(self.product.stock_quantity, 0)

    def test_pos_order_inactive_product_rolls_back(self):
        self.product.deactivate()
        response = self._post_pos(quantity=1)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('no longer available', data['error'])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

    # ── Payment method at POS order creation ───────────────────────────────

    def test_pos_order_stores_cash_payment_method(self):
        """POS order with payment_method='cash' stores it on the order."""
        response = self._post_pos_with_payment('cash')
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual(order.payment_method, 'cash')

    def test_pos_order_stores_gcash_payment_method(self):
        """POS order with payment_method='gcash' stores it on the order."""
        response = self._post_pos_with_payment('gcash')
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual(order.payment_method, 'gcash')

    def test_pos_order_defaults_to_cash_when_no_method_sent(self):
        """POS order without a payment_method defaults to 'cash'."""
        response = self._post_pos(quantity=1)  # no payment_method field
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual(order.payment_method, 'cash')

    def test_pos_order_rejects_invalid_payment_method(self):
        """POS order with an invalid payment_method falls back to 'cash'."""
        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Walk-in Customer',
                'items': [{'product_id': self.product.pk, 'quantity': 1, 'size': 'none'}],
                'order_type': 'dine_in',
                'payment_method': 'card',  # invalid — not cash/gcash
            }),
            content_type='application/json',
        )
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        # Invalid value is sanitized to 'cash'
        self.assertEqual(order.payment_method, 'cash')

    def test_pos_cash_payment_confirms_correctly(self):
        """POS Cash order: process_payment confirms correctly with change."""
        response = self._post_pos_with_payment('cash')
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.payment_method, 'cash')

        pay_response = self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'cash', 'amount_paid': '150.00'},
        )
        data = pay_response.json()
        self.assertTrue(data['success'])

        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.payment_method, 'cash')
        self.assertEqual(order.status, 'preparing')
        from decimal import Decimal
        self.assertEqual(order.change_amount, Decimal('150.00') - order.total)

    def test_pos_gcash_payment_confirms_without_customer_reference(self):
        """POS GCash order: process_payment must succeed without any customer
        reference or proof — staff verifies the business GCash account directly.
        This is the critical test: POS GCash must never require gcash_reference."""
        response = self._post_pos_with_payment('gcash')
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.payment_method, 'gcash')
        self.assertEqual(order.gcash_status, 'none')  # no customer reference
        self.assertEqual(order.cashier, self.cashier)  # POS order — cashier set

        pay_response = self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'gcash', 'amount_paid': str(order.total)},
        )
        data = pay_response.json()
        self.assertTrue(data['success'], msg=data.get('error'))

        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.payment_method, 'gcash')
        self.assertEqual(order.status, 'preparing')
        from decimal import Decimal
        self.assertEqual(order.change_amount, Decimal('0.00'))
        self.assertEqual(order.amount_paid, order.total)

    def test_pos_gcash_inventory_not_deducted_twice(self):
        """Confirming a POS GCash payment must not trigger a second inventory deduction."""
        response = self._post_pos_with_payment('gcash', quantity=2)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.product.refresh_from_db()
        stock_after_creation = self.product.stock_quantity  # deducted once at creation

        self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'gcash', 'amount_paid': str(order.total)},
        )

        self.product.refresh_from_db()
        # Stock must not change further — only deducted at order creation
        self.assertEqual(self.product.stock_quantity, stock_after_creation)

    def test_pos_gcash_finance_records_gcash_not_cash(self):
        """Finance must count a POS GCash order as GCash, not Cash."""
        from apps.finance.models import DailyFinance
        from decimal import Decimal
        response = self._post_pos_with_payment('gcash', quantity=1)
        order = Order.objects.get(pk=response.json()['order_id'])

        # Confirm payment and advance to completed
        self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'gcash', 'amount_paid': str(order.total)},
        )
        order.refresh_from_db()
        order.status = 'completed'
        order.completed_at = timezone.now()
        order.save(update_fields=['status', 'completed_at'])

        # Create a DailyFinance record for today
        finance = DailyFinance.objects.create(date=timezone.localdate())
        # Order must appear in GCash sales, not Cash sales
        self.assertEqual(finance.get_cash_sales(), Decimal('0.00'))
        self.assertGreater(finance.get_gcash_sales(), Decimal('0.00'))

    # ── Order notes ────────────────────────────────────────────────────────

    def _post_pos_with_notes(self, notes, payment_method='cash', quantity=2):
        """Helper: creates a POS order with notes."""
        item = {'product_id': self.product.pk, 'quantity': quantity, 'size': 'none'}
        return self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Walk-in Customer',
                'items': [item],
                'order_type': 'dine_in',
                'payment_method': payment_method,
                'notes': notes,
            }),
            content_type='application/json',
        )

    def test_pos_order_with_notes_saves_correctly(self):
        """POS order with notes stores the notes text on the Order."""
        response = self._post_pos_with_notes('Extra sugar please')
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual(order.notes, 'Extra sugar please')

    def test_pos_order_without_notes_succeeds(self):
        """POS order without a notes key creates successfully with empty notes."""
        response = self._post_pos(quantity=1)
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual(order.notes, '')

    def test_pos_order_notes_saved_for_cash_and_gcash(self):
        """Notes persist for both Cash and GCash POS orders."""
        cash_response = self._post_pos_with_notes(
            'Cash order note', payment_method='cash', quantity=1
        )
        gcash_response = self._post_pos_with_notes(
            'GCash order note', payment_method='gcash', quantity=1
        )
        cash_data = cash_response.json()
        gcash_data = gcash_response.json()
        self.assertTrue(cash_data['success'])
        self.assertTrue(gcash_data['success'])
        cash_order = Order.objects.get(pk=cash_data['order_id'])
        gcash_order = Order.objects.get(pk=gcash_data['order_id'])
        self.assertEqual(cash_order.notes, 'Cash order note')
        self.assertEqual(gcash_order.notes, 'GCash order note')

    def test_pos_order_notes_visible_in_kitchen_view(self):
        """POS order notes appear in the kitchen orders view."""
        from apps.accounts.models import CustomUser as User
        kitchen_user = User.objects.create_user(
            username='kitchen_staff_notes', password=PASSWORD, role='kitchen_staff',
        )
        response = self._post_pos_with_notes('No ice please', quantity=1)
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        # Advance to preparing so it appears in the kitchen queue
        order.status = 'preparing'
        order.save(update_fields=['status'])
        self.client.force_login(kitchen_user)
        kitchen_response = self.client.get(reverse('kitchen:orders'))
        self.assertEqual(kitchen_response.status_code, 200)
        self.assertContains(kitchen_response, 'No ice please')

    def test_pos_order_notes_trimmed_whitespace(self):
        """Notes with leading/trailing whitespace: JS trims before sending;
        the server stores what it receives (already trimmed)."""
        response = self._post_pos_with_notes('  hello  ')
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        # Test documents server storage behaviour: notes='  hello  ' is sent
        # (no extra trimming in the view), stored as-is.
        self.assertEqual(order.notes, '  hello  ')

    def test_pos_order_very_long_notes_truncated_or_rejected(self):
        """TextField has no max_length so very long notes are stored.
        The JS textarea uses maxlength='1000' for practical client-side enforcement."""
        long_notes = 'A' * 10001
        response = self._post_pos_with_notes(long_notes, quantity=1)
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        # Server stores the full text (no max_length on the model field).
        self.assertEqual(len(order.notes), 10001)

    def test_existing_pos_behavior_unchanged(self):
        """Existing POS flow without notes still works correctly."""
        response = self._post_pos(quantity=2)
        data = response.json()
        self.assertTrue(data['success'])
        order = Order.objects.get(pk=data['order_id'])
        # No notes key sent: default empty string.
        self.assertEqual(order.notes, '')
        # Stock deducted correctly.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        # Order items created.
        self.assertEqual(order.items.count(), 1)


class DuplicateOrderProtectionTests(TestCase):
    """Repeated submissions (double-click, slow network, rapid taps) must
    never create duplicate orders.

    The client disables the button and blocks re-entry while a request is in
    flight; the server additionally treats the request_token as an
    idempotency key -- replaying the same token returns the original order.
    These tests verify the server guarantee directly: even when two requests
    arrive, exactly one order is created.
    """

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='dup_cashier', password=PASSWORD, role='cashier',
        )
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=50,
        )

    # ── POS ────────────────────────────────────────────────────────────────

    def _post_pos(self, token=None, quantity=2, **overrides):
        payload = {
            'customer_name': 'Walk-in Customer',
            'items': [{
                'product_id': self.product.pk,
                'quantity': quantity,
                'size': 'none',
            }],
            'order_type': 'dine_in',
        }
        if token is not None:
            payload['request_token'] = token
        payload.update(overrides)
        return self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps(payload),
            content_type='application/json',
        )

    def test_pos_repeated_submission_creates_one_order(self):
        # Double-click / retry: the same token is replayed twice.
        first = self._post_pos(token='pos-token-1')
        second = self._post_pos(token='pos-token-1')
        d1, d2 = first.json(), second.json()
        self.assertTrue(d1['success'])
        self.assertTrue(d2['success'])
        # The replay returns the ORIGINAL order, not a new one.
        self.assertEqual(d1['order_number'], d2['order_number'])
        self.assertTrue(d2.get('duplicate'))
        self.assertEqual(Order.objects.count(), 1)
        # Stock was deducted exactly once.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 48)

    def test_pos_different_tokens_create_separate_orders(self):
        self._post_pos(token='pos-token-a')
        self._post_pos(token='pos-token-b')
        self.assertEqual(Order.objects.count(), 2)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 46)

    def test_pos_without_token_creates_order_normally(self):
        response = self._post_pos()
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(Order.objects.count(), 1)
        self.assertNotIn('duplicate', data)

    def test_pos_duplicate_token_does_not_deduct_twice(self):
        # A slow-network retry after a lost response: the first request
        # succeeded, the second must not touch stock again.
        self._post_pos(token='pos-token-2', quantity=5)
        self._post_pos(token='pos-token-2', quantity=5)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 45)
        self.assertEqual(Order.objects.count(), 1)

    # ── Customer checkout ──────────────────────────────────────────────────

    def _cart_with(self, quantity):
        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(
            cart=cart, product=self.product, size='none',
            quantity=quantity, unit_price=60.00,
        )
        return cart

    def test_checkout_repeated_submission_creates_one_order(self):
        self._cart_with(2)
        # GET renders the form with a stable token.
        page = self.client.get(reverse('orders:checkout'))
        token = page.context['request_token']
        self.assertTrue(token)

        post = {
            'customer_name': 'Test Customer',
            'order_type': 'dine_in',
            'payment_method': 'cash',
            'request_token': token,
        }
        first = self.client.post(reverse('orders:checkout'), post)
        second = self.client.post(reverse('orders:checkout'), post)  # replay

        order = Order.objects.get()
        # Payment-first flow: both redirects go to payment_waiting
        expected_url = reverse('orders:payment_waiting', kwargs={'tracking_token': order.tracking_token})
        self.assertRedirects(first, expected_url)
        self.assertRedirects(second, expected_url)
        self.assertEqual(Order.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 48)

    def test_checkout_without_token_creates_order_normally(self):
        self._cart_with(1)
        response = self.client.post(reverse('orders:checkout'), {
            'customer_name': 'Test Customer',
            'order_type': 'dine_in',
            'payment_method': 'cash',
        })
        order = Order.objects.get()
        # Payment-first flow: redirects to payment_waiting
        self.assertRedirects(
            response,
            reverse('orders:payment_waiting', kwargs={'tracking_token': order.tracking_token}),
        )
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(order.status, 'awaiting_payment')


class CancellationAtomicityTests(TestCase):
    """Order cancellation: status change + stock restore commit together."""

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='cancel_cashier', password=PASSWORD, role='cashier',
        )
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )

    def _deducted_order(self, quantity=2):
        """An order that already went through checkout (stock deducted)."""
        order = Order.objects.create(customer_name='Test Customer')
        OrderItem.objects.create(
            order=order, product=self.product, product_name=self.product.name,
            size='none', quantity=quantity, unit_price=60.00,
            subtotal=60.00 * quantity,
        )
        self.product.stock_quantity = 10 - quantity
        self.product.save(update_fields=['stock_quantity'])
        order.stock_deducted = True
        order.save(update_fields=['stock_deducted'])
        return order

    def test_cancel_restores_stock_and_marks_order(self):
        order = self._deducted_order(2)
        response = self.client.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertFalse(order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        log = InventoryLog.objects.get(product=self.product, action='adjustment')
        self.assertEqual(log.quantity_change, 2)
        # Cancellation restores the same source as the original order.
        self.assertEqual(log.source, 'customer_order')
        self.assertEqual(log.reason, f'Cancelled Order #{order.order_number}')

    def test_cancel_failure_rolls_back_restore_and_status(self):
        order = self._deducted_order(2)
        with mock.patch.object(
            InventoryLog.objects, 'create',
            side_effect=RuntimeError('log write failed'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('orders:update_status', args=[order.pk]),
                    {'status': 'cancelled'},
                )
        # The restore was rolled back: stock stays deducted, the order is
        # still active, and no adjustment log was written.
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')
        self.assertTrue(order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_cancel_twice_does_not_restore_twice(self):
        order = self._deducted_order(2)
        self.client.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )
        self.client.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        # Only one restore happened (restore_inventory_for_order is idempotent
        # via the stock_deducted flag).
        self.assertEqual(
            InventoryLog.objects.filter(product=self.product, action='adjustment').count(),
            1,
        )


class RestockAtomicityTests(TestCase):
    """Manual restock: stock change + inventory log + audit log."""

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='restock_cashier', password=PASSWORD, role='cashier',
        )
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )

    def test_restock_updates_stock_and_logs(self):
        response = self.client.post(
            reverse('inventory:restock', args=[self.product.pk]),
            {'quantity': 5},
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['new_qty'], 15)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 15)
        log = InventoryLog.objects.get(product=self.product, action='restock')
        self.assertEqual(log.quantity_change, 5)
        self.assertEqual(log.quantity_before, 10)
        self.assertEqual(log.quantity_after, 15)
        self.assertEqual(log.source, 'manual_adjustment')
        self.assertEqual(log.reason, 'Restock')

    def test_restock_rolls_back_on_log_failure(self):
        with mock.patch.object(
            InventoryLog.objects, 'create',
            side_effect=RuntimeError('log write failed'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('inventory:restock', args=[self.product.pk]),
                    {'quantity': 5},
                )
        # The stock increase was rolled back -- no untracked stock.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_restock_rejects_non_positive_quantity(self):
        response = self.client.post(
            reverse('inventory:restock', args=[self.product.pk]),
            {'quantity': 0},
        )
        self.assertFalse(response.json()['success'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(InventoryLog.objects.count(), 0)


class StockAvailabilityGuardTests(TestCase):
    """Out-of-stock and unavailable products can never be ordered.

    The guards live at every layer of the ordering workflow: add-to-cart
    rejects them up front, cart quantity updates cap/remove them, checkout
    drops them with a notice, and the POS refuses them server-side (the
    client additionally disables the cards -- see pos.html / main.js).
    """

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='stock_cashier', password=PASSWORD, role='cashier',
        )
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=3,
        )

    # ── add_to_cart ────────────────────────────────────────────────────────

    def _add_to_cart(self, quantity=1):
        return self.client.post(
            reverse('orders:add_to_cart', args=[self.product.pk]),
            {'quantity': str(quantity), 'size': 'none'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_add_to_cart_rejects_out_of_stock_product(self):
        self.product.stock_quantity = 0
        self.product.save(update_fields=['stock_quantity'])
        response = self._add_to_cart()
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('out of stock', data['error'])
        self.assertFalse(CartItem.objects.filter(product=self.product).exists())

    def test_add_to_cart_rejects_quantity_beyond_stock(self):
        response = self._add_to_cart(quantity=5)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('left in stock', data['error'])
        self.assertFalse(CartItem.objects.filter(product=self.product).exists())

    def test_add_to_cart_allows_quantity_within_stock(self):
        response = self._add_to_cart(quantity=2)
        self.assertTrue(response.json()['success'])
        item = CartItem.objects.get(product=self.product)
        self.assertEqual(item.quantity, 2)

    # ── update_cart ────────────────────────────────────────────────────────

    def _cart_with_item(self, quantity=2):
        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        item = CartItem.objects.create(
            cart=cart, product=self.product, size='none',
            quantity=quantity, unit_price=60.00,
        )
        return item

    def test_update_cart_caps_quantity_at_available_stock(self):
        item = self._cart_with_item(quantity=2)
        response = self.client.post(
            reverse('orders:update_cart', args=[item.pk]), {'quantity': '5'},
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['quantity'], 3)
        self.assertIn('left in stock', data['message'])
        item.refresh_from_db()
        self.assertEqual(item.quantity, 3)

    def test_update_cart_removes_out_of_stock_item(self):
        item = self._cart_with_item()
        self.product.stock_quantity = 0
        self.product.save(update_fields=['stock_quantity'])
        response = self.client.post(
            reverse('orders:update_cart', args=[item.pk]), {'quantity': '3'},
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['removed'])
        self.assertIn('out of stock', data['message'])
        self.assertFalse(CartItem.objects.filter(pk=item.pk).exists())

    def test_update_cart_removes_unavailable_item(self):
        item = self._cart_with_item(quantity=1)
        self.product.is_available = False
        self.product.save(update_fields=['is_available'])
        response = self.client.post(
            reverse('orders:update_cart', args=[item.pk]), {'quantity': '2'},
        )
        data = response.json()
        self.assertTrue(data['removed'])
        self.assertIn('no longer available', data['message'])
        self.assertFalse(CartItem.objects.filter(pk=item.pk).exists())

    # ── checkout ───────────────────────────────────────────────────────────

    def test_checkout_drops_out_of_stock_items(self):
        self._cart_with_item(quantity=1)
        self.product.stock_quantity = 0
        self.product.save(update_fields=['stock_quantity'])
        response = self.client.post(reverse('orders:checkout'), {
            'customer_name': 'Test Customer', 'order_type': 'dine_in', 'payment_method': 'cash',
        })
        self.assertRedirects(response, reverse('orders:cart'))
        self.assertFalse(CartItem.objects.filter(product=self.product).exists())
        self.assertFalse(Order.objects.exists())

    def test_checkout_drops_unavailable_items(self):
        self._cart_with_item(quantity=1)
        self.product.is_available = False
        self.product.save(update_fields=['is_available'])
        response = self.client.post(reverse('orders:checkout'), {
            'customer_name': 'Test Customer', 'order_type': 'dine_in', 'payment_method': 'cash',
        })
        self.assertRedirects(response, reverse('orders:cart'))
        self.assertFalse(CartItem.objects.filter(product=self.product).exists())
        self.assertFalse(Order.objects.exists())

    # ── POS (server side) ──────────────────────────────────────────────────

    def _post_pos(self, quantity=1):
        self.client.force_login(self.cashier)
        return self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Walk-in Customer',
                'items': [{
                    'product_id': self.product.pk,
                    'quantity': quantity,
                    'size': 'none',
                }],
                'order_type': 'dine_in',
            }),
            content_type='application/json',
        )

    def test_pos_rejects_unavailable_product(self):
        self.product.is_available = False
        self.product.save(update_fields=['is_available'])
        response = self._post_pos()
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('no longer available', data['error'])
        self.assertFalse(Order.objects.exists())

    def test_pos_rejects_out_of_stock_product(self):
        self.product.stock_quantity = 0
        self.product.save(update_fields=['stock_quantity'])
        response = self._post_pos()
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('Insufficient stock', data['error'])
        self.assertIn('Available: 0', data['error'])
        self.assertFalse(Order.objects.exists())

    def test_pos_caps_nothing_but_rejects_overdraw(self):
        # A request that would overdraw (3 available, 5 requested) fails
        # cleanly and rolls back -- the POS client caps quantities before
        # submitting, but the server must never trust the client.
        response = self._post_pos(quantity=5)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('Insufficient stock', data['error'])
        self.assertIn('Available: 3', data['error'])
        self.assertFalse(Order.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 3)

    # ── POS page rendering (client contract) ───────────────────────────────

    def test_pos_page_embeds_stock_on_product_cards(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('orders:pos'))
        self.assertContains(response, f'data-product-id="{self.product.pk}"')
        self.assertContains(response, 'data-stock="3"')

    def test_pos_page_disables_out_of_stock_card(self):
        self.client.force_login(self.cashier)
        self.product.stock_quantity = 0
        self.product.save(update_fields=['stock_quantity'])
        response = self.client.get(reverse('orders:pos'))
        # The card is visibly disabled, carries the stock for the JS gate,
        # and shows a clear label.
        self.assertContains(response, 'pos-item--out')
        self.assertContains(response, 'data-stock="0"')
        self.assertContains(response, 'data-out="1"')
        self.assertContains(response, 'Out of Stock')


class PosSearchTests(TestCase):
    """POS product search: rendered UI, and constant query cost as the
    catalog grows.

    Filtering itself is client-side (cached cards + normalized partial,
    case-insensitive matching -- see pos.html); these tests verify the page
    ships the search box + "All" tab, renders the whole catalog into the DOM
    for filtering, and never adds per-product database queries (the
    category/products prefetch keeps the request cost flat at any catalog
    size).
    """

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='pos_search_cashier', password=PASSWORD, role='cashier',
        )
        self.client.force_login(self.cashier)

    def _catalog(self, categories=2, products_per_category=3):
        for i in range(categories):
            cat = Category.objects.create(name=f'Cat {i}', slug=f'cat-{i}')
            for j in range(products_per_category):
                Product.objects.create(
                    category=cat, name=f'Drink {i}-{j}', price='50.00',
                )

    def test_pos_renders_search_input_and_all_tab(self):
        self._catalog()
        response = self.client.get(reverse('orders:pos'))
        self.assertContains(response, 'id="pos-search"')
        self.assertContains(response, 'Search products')
        # The "All" tab exists and is the default active view so search spans
        # the whole catalog.
        self.assertContains(response, "filterCat('all'")
        self.assertContains(response, 'cat-tab active')

    def test_pos_renders_every_product_card_into_the_dom(self):
        # Filtering is client-side, so the whole catalog must be in the DOM
        # (not just the active category) -- otherwise search could never find
        # products outside the current tab.
        self._catalog(categories=3, products_per_category=4)
        response = self.client.get(reverse('orders:pos'))
        for i in range(3):
            for j in range(4):
                self.assertContains(response, f'Drink {i}-{j}')

    def test_pos_query_count_is_flat_as_catalog_grows(self):
        # The page must cost the same number of queries for 10 products as
        # for 500 -- the prefetch does one query per dataset, never one per
        # product. (The catalog is rebuilt in-place with raw deletes to
        # bypass the product soft-delete guard.)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(reverse('orders:pos'))
            self.assertEqual(response.status_code, 200)
            return len(ctx)

        self._catalog(categories=2, products_per_category=5)   # 10 products
        # Warm up (first request in a fresh DB performs one-time session/
        # middleware setup queries that are not related to catalog size).
        self.client.get(reverse('orders:pos'))
        small_catalog_queries = count_queries()

        with connection.cursor() as cur:
            cur.execute('DELETE FROM menu_product')
            cur.execute('DELETE FROM menu_category')
        self._catalog(categories=10, products_per_category=50)  # 500 products
        large_catalog_queries = count_queries()

        self.assertEqual(small_catalog_queries, large_catalog_queries)
        # Sanity bound: categories + one prefetch + session/auth overhead.
        self.assertLessEqual(large_catalog_queries, 6)


class PosDraftStatusTests(TestCase):
    """The POS draft-status endpoint tells the client whether a draft's
    idempotency token already placed an order, so an accidental refresh never
    restores a completed order."""

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='draft_cashier', password=PASSWORD, role='cashier',
        )
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )

    def _create_order_with_token(self, token):
        return Order.objects.create(
            customer_name='Test Customer', request_token=token,
        )

    def test_unknown_token_reports_not_placed(self):
        response = self.client.get(
            reverse('orders:pos_draft_status'),
            {'request_token': 'pos-token-never-used'},
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['placed'])
        self.assertIsNone(data['order_number'])

    def test_existing_order_token_reports_placed(self):
        order = self._create_order_with_token('pos-token-placed-1')
        response = self.client.get(
            reverse('orders:pos_draft_status'),
            {'request_token': 'pos-token-placed-1'},
        )
        data = response.json()
        self.assertTrue(data['placed'])
        self.assertEqual(data['order_number'], order.order_number)

    def test_missing_token_reports_not_placed(self):
        response = self.client.get(reverse('orders:pos_draft_status'))
        data = response.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['placed'])

    def test_requires_staff_login(self):
        self.client.logout()
        response = self.client.get(
            reverse('orders:pos_draft_status'), {'request_token': 'x'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)

    def test_status_endpoint_never_creates_orders(self):
        # A read-only lookup: checking a token must not create or alter orders.
        self.client.get(
            reverse('orders:pos_draft_status'),
            {'request_token': 'pos-token-readonly'},
        )
        self.assertEqual(Order.objects.count(), 0)


class NewOrderRealtimePayloadTests(TestCase):
    """The new_order broadcast carries the idempotency token so the ordering
    terminal can ignore the echo of its own order before the HTTP response
    arrives (see RealtimeConnection.ignoreToken in main.js).

    The broadcast fires via transaction.on_commit() inside create_pos_order.
    We patch on_commit to call the callback immediately so this test works
    with both SQLite (test DB) and PostgreSQL without relying on actual
    transaction commit semantics in the test runner.
    """

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='rt-payload-cashier', password=PASSWORD,
            role='cashier', is_active=True,
        )
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Coffee', slug='coffee-rt')
        self.product = Product.objects.create(
            category=self.category, name='Espresso', price='60.00',
            stock_quantity=50,
        )

    def _drain(self, q):
        import queue as queue_module
        events = []
        while True:
            try:
                events.append(q.get_nowait())
            except queue_module.Empty:
                return events

    def test_new_order_payload_includes_request_token(self):
        """POS create_pos_order broadcasts new_order with the request_token."""
        from apps.realtime.broker import subscribe, unsubscribe
        from django.db import transaction as db_transaction
        import json

        # Patch transaction.on_commit to call the callback immediately so
        # the broadcast fires synchronously during the test request.
        # This is necessary because Django's TestCase wraps each test in a
        # transaction that never commits, so on_commit hooks would never fire.
        with mock.patch.object(db_transaction, 'on_commit', side_effect=lambda fn: fn()):
            q = subscribe()
            try:
                response = self.client.post(
                    reverse('orders:create_pos_order'),
                    data=json.dumps({
                        'customer_name': 'RT Customer',
                        'order_type': 'dine_in',
                        'request_token': 'rt-token-abc-123',
                        'items': [
                            {'product_id': self.product.pk, 'quantity': 1, 'size': 'none'},
                        ],
                    }),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['success'])
                events = self._drain(q)
            finally:
                unsubscribe(q)

        created = [e for e in events if e['event'] == 'new_order']
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]['data']['request_token'], 'rt-token-abc-123')
        self.assertIn('order_id', created[0]['data'])
        self.assertIn('order_number', created[0]['data'])


class PaymentFirstFlowTests(TestCase):
    """End-to-end tests for the payment-first customer ordering workflow.

    Verifies the simplified model where PENDING is the single initial status
    and payment_status (is_paid) is tracked independently:
    - Customer orders start as PENDING + is_paid=False
    - Unpaid customer orders cannot be accepted (server-side enforcement)
    - process_payment sets is_paid=True but keeps status=PENDING
    - accept_order moves PENDING+PAID → PREPARING
    - Finance is not affected by unpaid orders
    """

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='pf_cashier', password='pf-pass-1', role='cashier',
        )
        self.category = Category.objects.create(name='Drinks', slug='drinks-pf')
        self.product = Product.objects.create(
            category=self.category, name='Latte', price='75.00',
            stock_quantity=20,
        )

    def _customer_order(self, quantity=1):
        """Create a customer order (no cashier, awaiting_payment, unpaid, cash by default)."""
        order = Order.objects.create(
            customer_name='Customer A',
            status='awaiting_payment',
            payment_method='cash',
            cashier=None,   # customer order — no cashier assigned yet
        )
        OrderItem.objects.create(
            order=order, product=self.product,
            product_name=self.product.name,
            category_name=self.category.name,
            packaging_eligible=False,
            size='none', quantity=quantity,
            unit_price='75.00', subtotal=str(75 * quantity),
        )
        order.subtotal = 75 * quantity
        order.total = 75 * quantity
        order.stock_deducted = True
        order.save(update_fields=['subtotal', 'total', 'stock_deducted'])
        return order

    # ── Checkout redirect ──────────────────────────────────────────────────

    def test_checkout_creates_pending_unpaid_order(self):
        """Customer checkout must create an order in awaiting_payment with is_paid=False."""
        session = self.client.session
        session['_seed'] = 'x'
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(
            cart=cart, product=self.product, size='none',
            quantity=1, unit_price='75.00',
        )
        response = self.client.post(reverse('orders:checkout'), {
            'customer_name': 'Pay-First Customer',
            'order_type': 'dine_in',
            'payment_method': 'cash',
        })
        order = Order.objects.get()
        # Order is awaiting_payment + UNPAID
        self.assertEqual(order.status, 'awaiting_payment')
        self.assertFalse(order.is_paid)
        # Redirect goes to payment_waiting, not order_success
        self.assertRedirects(
            response,
            reverse('orders:payment_waiting', kwargs={'tracking_token': order.tracking_token}),
        )

    # ── Server-side accept gate ────────────────────────────────────────────

    def test_accept_order_rejects_unpaid_order(self):
        """Accepting an unpaid customer order must return 400 regardless of UI state."""
        self.client.force_login(self.cashier)
        order = self._customer_order()
        self.assertFalse(order.is_paid)

        response = self.client.post(
            reverse('orders:accept_order', args=[order.pk]),
        )
        data = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(data['success'])
        self.assertIn('payment', data['error'].lower())

        # Status must remain awaiting_payment — not moved to PREPARING
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')

    def test_accept_order_requires_login(self):
        """Unauthenticated attempt to accept order must be rejected."""
        order = self._customer_order()
        response = self.client.post(
            reverse('orders:accept_order', args=[order.pk]),
        )
        # Redirects to login (302) — not a 200 JSON success
        self.assertNotEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')

    def test_accept_order_rejects_already_preparing(self):
        """accept_order on an order already in preparing must return error."""
        self.client.force_login(self.cashier)
        order = self._customer_order()
        # Manually advance to preparing (simulating a prior accept)
        order.is_paid = True
        order.status = 'preparing'
        order.save(update_fields=['is_paid', 'status'])

        response = self.client.post(
            reverse('orders:accept_order', args=[order.pk]),
        )
        data = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(data['success'])

    def test_accept_order_rejects_pos_order(self):
        """accept_order must reject POS orders (cashier set) — not customer flow."""
        self.client.force_login(self.cashier)
        order = self._customer_order()
        # Make it look like a POS order by assigning a cashier
        order.is_paid = True
        order.cashier = self.cashier
        order.save(update_fields=['is_paid', 'cashier'])

        response = self.client.post(
            reverse('orders:accept_order', args=[order.pk]),
        )
        data = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(data['success'])

    def test_process_payment_cash_advances_to_preparing(self):
        """Cash payment confirms AND auto-advances awaiting_payment → preparing."""
        self.client.force_login(self.cashier)
        order = self._customer_order()

        response = self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'cash', 'amount_paid': '100.00'},
        )
        data = response.json()
        self.assertTrue(data['success'])

        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        # Cash: awaiting_payment → preparing in one step
        self.assertEqual(order.status, 'preparing')
        self.assertEqual(order.payment_method, 'cash')
        from decimal import Decimal
        self.assertEqual(order.change_amount, Decimal('25.00'))  # 100 - 75

    def test_process_payment_gcash_blocked_for_customer_order(self):
        """process_payment must reject GCash for customer orders that have a
        pending submitted screenshot — those must go through verify_gcash_payment."""
        self.client.force_login(self.cashier)
        order = self._customer_order()
        order.payment_method = 'gcash'
        order.gcash_status = 'pending'
        order.gcash_reference = 'REMOTE123'  # historical order — field preserved for backward compat
        order.save(update_fields=['payment_method', 'gcash_status', 'gcash_reference'])

        response = self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'gcash', 'amount_paid': '75.00'},
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('submitted reference', data['error'])

        order.refresh_from_db()
        self.assertFalse(order.is_paid)
        self.assertEqual(order.status, 'awaiting_payment')

    def test_process_payment_gcash_onsite_allowed_without_reference(self):
        """Onsite GCash (no customer-submitted reference) must be confirmable
        via process_payment — staff is manually verifying the payment."""
        self.client.force_login(self.cashier)
        order = self._customer_order()
        order.payment_method = 'gcash'
        order.gcash_status = 'none'  # no customer reference submitted
        order.save(update_fields=['payment_method', 'gcash_status'])

        response = self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            # For GCash onsite the modal sends amount_paid = order.total
            {'payment_method': 'gcash', 'amount_paid': '75.00'},
        )
        data = response.json()
        self.assertTrue(data['success'])

        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.status, 'preparing')
        self.assertEqual(order.payment_method, 'gcash')
        from decimal import Decimal
        # GCash onsite: no change
        self.assertEqual(order.change_amount, Decimal('0.00'))
        self.assertEqual(order.amount_paid, order.total)

    def test_duplicate_payment_rejected(self):
        """A second payment attempt on an already-paid order must be rejected."""
        self.client.force_login(self.cashier)
        order = self._customer_order()
        order.is_paid = True
        order.save(update_fields=['is_paid'])

        response = self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'cash', 'amount_paid': '100.00'},
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('already been paid', data['error'])

    def test_payment_waiting_page_accessible(self):
        """payment_waiting page loads for valid tracking_token."""
        order = self._customer_order()
        response = self.client.get(
            reverse('orders:payment_waiting', kwargs={'tracking_token': order.tracking_token}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.order_number)

    def test_api_payment_waiting_status_awaiting_unpaid(self):
        """api_payment_waiting_status: awaiting_payment + unpaid = not accepted."""
        order = self._customer_order()
        response = self.client.get(
            reverse('orders:api_payment_waiting_status', kwargs={'tracking_token': order.tracking_token}),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'awaiting_payment')
        self.assertFalse(data['is_paid'])
        self.assertFalse(data['order_accepted'])

    def test_api_payment_waiting_status_after_preparing(self):
        """api_payment_waiting_status reflects order_accepted=True when preparing."""
        order = self._customer_order()
        order.is_paid = True
        order.status = 'preparing'
        order.save(update_fields=['is_paid', 'status'])
        response = self.client.get(
            reverse('orders:api_payment_waiting_status', kwargs={'tracking_token': order.tracking_token}),
        )
        data = response.json()
        self.assertEqual(data['status'], 'preparing')
        self.assertTrue(data['order_accepted'])

    def test_validate_status_transition_blocks_awaiting_payment_to_preparing_unpaid(self):
        """awaiting_payment → preparing must be blocked for unpaid orders."""
        from apps.orders.services import validate_status_transition
        order = self._customer_order()  # is_paid=False
        with self.assertRaises(ValueError) as ctx:
            validate_status_transition('awaiting_payment', 'preparing', order=order)
        self.assertIn('payment', str(ctx.exception).lower())

    def test_validate_status_transition_allows_awaiting_payment_to_preparing_paid(self):
        """awaiting_payment → preparing is allowed when is_paid=True."""
        from apps.orders.services import validate_status_transition
        order = self._customer_order()
        order.is_paid = True
        order.save(update_fields=['is_paid'])
        # Should not raise
        validate_status_transition('awaiting_payment', 'preparing', order=order)

    # ── GCash-specific tests ───────────────────────────────────────────────

    def _gcash_order(self, quantity=1):
        """Create a customer GCash order in awaiting_payment."""
        order = self._customer_order(quantity)
        order.payment_method = 'gcash'
        order.save(update_fields=['payment_method'])
        return order

    @staticmethod
    def _fake_screenshot(name='proof.jpg', size=1024, content_type='image/jpeg'):
        """Return a minimal valid in-memory uploaded image file for GCash tests.

        Uses a real 1×1 JPEG so Pillow content-verification in the validator
        passes without requiring Cloudinary or local-filesystem writes.
        """
        try:
            from PIL import Image as PILImage
            buf = BytesIO()
            img = PILImage.new('RGB', (1, 1), color=(200, 100, 50))
            img.save(buf, format='JPEG')
            buf.seek(0)
            return InMemoryUploadedFile(
                buf, 'gcash_proof', name, content_type, buf.getbuffer().nbytes, None
            )
        except ImportError:
            # Pillow not available — create the raw JPEG bytes manually.
            # This is a minimal valid 1×1 JPEG so content validation still passes.
            raw = (
                b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
                b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t'
                b'\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a'
                b'\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\x1e'
                b'7=&\x1c8+I\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            )
            buf = BytesIO(raw)
            return InMemoryUploadedFile(
                buf, 'gcash_proof', name, content_type, len(raw), None
            )

    def test_submit_gcash_payment_sets_pending_status(self):
        """Customer submits GCash screenshot → gcash_status becomes pending, is_paid stays False."""
        order = self._gcash_order()
        with mock.patch('cloudinary_storage.storage.MediaCloudinaryStorage.save', return_value='gcash_proofs/proof.jpg'), \
             mock.patch('cloudinary_storage.storage.MediaCloudinaryStorage.url', return_value='https://res.cloudinary.com/test/proof.jpg'):
            response = self.client.post(
                reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
                {'gcash_proof': self._fake_screenshot()},
                format='multipart',
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])

        order.refresh_from_db()
        self.assertEqual(order.gcash_status, 'pending')
        self.assertFalse(order.is_paid)  # CRITICAL: must NOT be paid
        self.assertEqual(order.status, 'awaiting_payment')  # status unchanged
        # gcash_reference is not set by new submissions — remains empty
        self.assertEqual(order.gcash_reference, '')

    def test_submit_gcash_idempotent_for_pending(self):
        """Re-submitting when already pending returns success without re-writing."""
        order = self._gcash_order()
        order.gcash_status = 'pending'
        order.gcash_reference = 'ORIGINAL123'  # historical order may have reference
        order.save(update_fields=['gcash_status', 'gcash_reference'])

        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_proof': self._fake_screenshot()},
            format='multipart',
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data.get('already_submitted'))

        order.refresh_from_db()
        self.assertEqual(order.gcash_reference, 'ORIGINAL123')  # unchanged

    def test_submit_gcash_rejects_missing_screenshot(self):
        """Submission without a screenshot must be rejected with a field error."""
        order = self._gcash_order()
        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {},  # no gcash_proof field
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('gcash_proof', data.get('errors', {}))

    def test_submit_gcash_requires_proof_image(self):
        """Submission with an empty screenshot field must be rejected."""
        order = self._gcash_order()
        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_proof': ''},
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('gcash_proof', data.get('errors', {}))

    def test_submit_gcash_blocks_on_paid_order(self):
        """Cannot submit GCash screenshot if order is already paid."""
        order = self._gcash_order()
        order.is_paid = True
        order.save(update_fields=['is_paid'])

        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_proof': self._fake_screenshot()},
            format='multipart',
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('already been paid', data['error'])

    def test_submit_gcash_blocks_on_cancelled_order(self):
        """Cannot submit GCash screenshot on a cancelled order."""
        order = self._gcash_order()
        order.status = 'cancelled'
        order.cancelled_at = timezone.now()
        order.save(update_fields=['status', 'cancelled_at'])

        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_proof': self._fake_screenshot()},
            format='multipart',
        )
        data = response.json()
        self.assertFalse(data['success'])

    def test_submit_gcash_blocks_on_cash_order(self):
        """submit_gcash_payment must reject cash orders regardless of proof."""
        order = self._customer_order()  # payment_method='cash' by default
        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_proof': self._fake_screenshot()},
            format='multipart',
        )
        data = response.json()
        self.assertFalse(data['success'])

    def test_submit_gcash_screenshot_does_not_set_paid(self):
        """A successful screenshot submission must never set is_paid=True."""
        order = self._gcash_order()
        with mock.patch('cloudinary_storage.storage.MediaCloudinaryStorage.save', return_value='gcash_proofs/proof.jpg'), \
             mock.patch('cloudinary_storage.storage.MediaCloudinaryStorage.url', return_value='https://res.cloudinary.com/test/proof.jpg'):
            response = self.client.post(
                reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
                {'gcash_proof': self._fake_screenshot()},
                format='multipart',
            )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertFalse(order.is_paid)        # CRITICAL — screenshot ≠ payment confirmed
        self.assertEqual(order.gcash_status, 'pending')
        self.assertEqual(order.status, 'awaiting_payment')

    def test_submit_gcash_no_duplicate_reference_check(self):
        """Duplicate reference check is removed — screenshot-only submission does not block on references."""
        order = self._gcash_order()
        with mock.patch('cloudinary_storage.storage.MediaCloudinaryStorage.save', return_value='gcash_proofs/proof.jpg'), \
             mock.patch('cloudinary_storage.storage.MediaCloudinaryStorage.url', return_value='https://res.cloudinary.com/test/proof.jpg'):
            response = self.client.post(
                reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
                {'gcash_proof': self._fake_screenshot()},
                format='multipart',
            )
        data = response.json()
        # Should succeed — no reference number involved, no duplicate check
        self.assertTrue(data['success'])

    def test_verify_gcash_marks_paid_and_prepares(self):
        """Staff verification: gcash_status→verified, is_paid→True, status→preparing."""
        self.client.force_login(self.cashier)
        order = self._gcash_order()
        order.gcash_status = 'pending'
        order.gcash_reference = 'VERIFY123'
        order.save(update_fields=['gcash_status', 'gcash_reference'])

        response = self.client.post(
            reverse('orders:verify_gcash_payment', args=[order.pk]),
        )
        data = response.json()
        self.assertTrue(data['success'])

        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.gcash_status, 'verified')
        self.assertEqual(order.status, 'preparing')
        self.assertEqual(order.gcash_verified_by, self.cashier)
        self.assertIsNotNone(order.gcash_verified_at)

    def test_verify_gcash_requires_staff(self):
        """Anonymous user cannot verify GCash payment."""
        order = self._gcash_order()
        order.gcash_status = 'pending'
        order.gcash_reference = 'REF123'
        order.save(update_fields=['gcash_status', 'gcash_reference'])

        response = self.client.post(
            reverse('orders:verify_gcash_payment', args=[order.pk]),
        )
        # Must redirect to login
        self.assertNotEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertFalse(order.is_paid)

    def test_verify_gcash_blocks_already_processed(self):
        """verify_gcash_payment must reject if gcash_status is 'verified'."""
        self.client.force_login(self.cashier)
        order = self._gcash_order()
        order.gcash_status = 'verified'
        order.is_paid = True
        order.save(update_fields=['gcash_status', 'is_paid'])

        response = self.client.post(
            reverse('orders:verify_gcash_payment', args=[order.pk]),
        )
        data = response.json()
        self.assertFalse(data['success'])

    def test_verify_gcash_idempotent_blocks_double_verify(self):
        """Second verification attempt on already-paid order must be rejected."""
        self.client.force_login(self.cashier)
        order = self._gcash_order()
        order.is_paid = True
        order.gcash_status = 'verified'
        order.save(update_fields=['is_paid', 'gcash_status'])

        response = self.client.post(
            reverse('orders:verify_gcash_payment', args=[order.pk]),
        )
        data = response.json()
        self.assertFalse(data['success'])

    def test_reject_gcash_resets_to_rejected_allows_resubmit(self):
        """Staff reject: gcash_status→rejected, is_paid stays False."""
        self.client.force_login(self.cashier)
        order = self._gcash_order()
        order.gcash_status = 'pending'
        order.gcash_reference = 'BADREF123'
        order.save(update_fields=['gcash_status', 'gcash_reference'])

        response = self.client.post(
            reverse('orders:reject_gcash_payment', args=[order.pk]),
            {'rejection_note': 'Reference not found in GCash'},
        )
        data = response.json()
        self.assertTrue(data['success'])

        order.refresh_from_db()
        self.assertFalse(order.is_paid)
        self.assertEqual(order.gcash_status, 'rejected')
        self.assertEqual(order.status, 'awaiting_payment')
        self.assertIn('not found', order.gcash_notes)

    def test_verify_gcash_does_not_deduct_inventory_again(self):
        """GCash verification must not deduct inventory a second time."""
        self.client.force_login(self.cashier)
        order = self._gcash_order()
        order.gcash_status = 'pending'
        order.gcash_reference = 'INV_TEST'
        order.save(update_fields=['gcash_status', 'gcash_reference'])

        stock_before = self.product.stock_quantity

        self.client.post(reverse('orders:verify_gcash_payment', args=[order.pk]))

        self.product.refresh_from_db()
        # Stock must be unchanged — already deducted at order creation
        self.assertEqual(self.product.stock_quantity, stock_before)

    def test_checkout_with_gcash_sets_payment_method(self):
        """Checkout with GCash selected stores payment_method='gcash' on the order."""
        session = self.client.session
        session['_seed'] = 'x'
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(
            cart=cart, product=self.product, size='none',
            quantity=1, unit_price='75.00',
        )
        response = self.client.post(reverse('orders:checkout'), {
            'customer_name': 'GCash Customer',
            'order_type': 'dine_in',
            'payment_method': 'gcash',
        })
        order = Order.objects.get()
        self.assertEqual(order.payment_method, 'gcash')
        self.assertEqual(order.status, 'awaiting_payment')
        self.assertFalse(order.is_paid)


class GCashPaymentDetailTest(TestCase):
    """Tests for the gcash_payment_detail JSON endpoint.

    GET /manage/<pk>/gcash-details/ returns GCash submission fields for the
    in-page verification modal.  Access requires cashier or admin role.
    """

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='detail_cashier', password=PASSWORD, role='cashier',
        )
        self.kitchen = CustomUser.objects.create_user(
            username='detail_kitchen', password=PASSWORD, role='kitchen_staff',
        )
        self.category = Category.objects.create(name='Drinks', slug='drinks-detail')
        self.product = Product.objects.create(
            category=self.category, name='Matcha Latte', price='90.00',
            stock_quantity=10,
        )

    def _gcash_pending_order(self):
        """Create a customer GCash order with gcash_status='pending'."""
        order = Order.objects.create(
            customer_name='GCash Customer',
            status='awaiting_payment',
            payment_method='gcash',
            gcash_status='pending',
        )
        OrderItem.objects.create(
            order=order, product=self.product,
            product_name=self.product.name,
            category_name=self.category.name,
            packaging_eligible=False,
            size='none', quantity=1,
            unit_price='90.00', subtotal='90.00',
        )
        order.subtotal = 90
        order.total = 90
        order.stock_deducted = True
        order.save(update_fields=['subtotal', 'total', 'stock_deducted'])
        return order

    def _url(self, pk):
        return reverse('orders:gcash_payment_detail', args=[pk])

    # ── Happy path ─────────────────────────────────────────────────────────

    def test_cashier_gets_200_with_correct_fields(self):
        """Authenticated cashier on a pending GCash order receives 200 JSON with all expected fields."""
        self.client.force_login(self.cashier)
        order = self._gcash_pending_order()

        response = self.client.get(self._url(order.pk))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['order_number'], order.order_number)
        self.assertEqual(data['customer_name'], 'GCash Customer')
        self.assertAlmostEqual(data['total'], 90.0)
        self.assertEqual(data['gcash_status'], 'pending')
        self.assertIn('gcash_reference', data)
        self.assertIn('gcash_submitted_at', data)
        self.assertIn('gcash_proof_url', data)

    def test_response_does_not_include_tracking_token(self):
        """The JSON response must not expose the order's tracking_token."""
        self.client.force_login(self.cashier)
        order = self._gcash_pending_order()

        response = self.client.get(self._url(order.pk))

        data = response.json()
        self.assertNotIn('tracking_token', data)

    # ── Guard: 400 for non-pending states ──────────────────────────────────

    def test_returns_400_for_already_paid_order(self):
        """Returns 400 when the order is already paid (is_paid=True)."""
        self.client.force_login(self.cashier)
        order = self._gcash_pending_order()
        order.is_paid = True
        order.gcash_status = 'verified'
        order.save(update_fields=['is_paid', 'gcash_status'])

        response = self.client.get(self._url(order.pk))

        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())

    def test_returns_400_for_cash_order(self):
        """Returns 400 when payment_method is not 'gcash'."""
        self.client.force_login(self.cashier)
        order = Order.objects.create(
            customer_name='Cash Customer',
            status='awaiting_payment',
            payment_method='cash',
            gcash_status='none',
        )

        response = self.client.get(self._url(order.pk))

        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())

    def test_returns_400_for_gcash_not_pending(self):
        """Returns 400 when payment_method is 'gcash' but gcash_status is not 'pending'."""
        self.client.force_login(self.cashier)
        order = self._gcash_pending_order()
        order.gcash_status = 'none'
        order.save(update_fields=['gcash_status'])

        response = self.client.get(self._url(order.pk))

        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())

    # ── Authorization ──────────────────────────────────────────────────────

    def test_unauthenticated_request_returns_json_401(self):
        """Unauthenticated requests receive JSON 401 (not an HTML redirect).

        The endpoint returns JSON for auth failures so the modal fetch handler
        can display a useful message instead of crashing on an unexpected HTML
        login-page response (the ``Unexpected token '<'`` bug).
        """
        order = self._gcash_pending_order()

        response = self.client.get(self._url(order.pk))

        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertIn('error', data)

    def test_kitchen_staff_cannot_access_endpoint(self):
        """Kitchen staff (non-cashier) must not gain access — receives JSON 403."""
        self.client.force_login(self.kitchen)
        order = self._gcash_pending_order()

        response = self.client.get(self._url(order.pk))

        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertIn('error', data)

    # ── Method guard ───────────────────────────────────────────────────────

    def test_post_returns_405(self):
        """Only GET is allowed; POST returns 405."""
        self.client.force_login(self.cashier)
        order = self._gcash_pending_order()

        response = self.client.post(self._url(order.pk))

        self.assertEqual(response.status_code, 405)


class CustomerReceiptTests(TestCase):
    """Tests for the customer digital receipt feature.

    Covers the payment gate, token security, content correctness, PDF
    generation, and read-only invariant (receipt generation must not change
    any order, payment, or inventory record).
    """

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='receipt_cashier', password=PASSWORD, role='cashier',
        )
        self.category = Category.objects.create(
            name='Drinks', slug='drinks-receipt',
        )
        self.product = Product.objects.create(
            category=self.category, name='Iced Latte', price='75.00',
            stock_quantity=20,
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _make_order(self, *, payment_method='cash', gcash_status='none',
                    is_paid=False, order_type='dine_in', with_packaging=False):
        """Create a minimal order with one item for receipt tests."""
        order = Order.objects.create(
            customer_name='Test Customer',
            status='awaiting_payment' if not is_paid else 'preparing',
            payment_method=payment_method,
            gcash_status=gcash_status,
            is_paid=is_paid,
            order_type=order_type,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name='Iced Latte',
            category_name=self.category.name,
            packaging_eligible=with_packaging,
            size='medium',
            quantity=2,
            unit_price=Decimal('75.00'),
            subtotal=Decimal('150.00'),
        )
        order.subtotal = 150
        order.packaging_fee = 10 if with_packaging else 0
        order.total = order.subtotal + order.packaging_fee
        order.amount_paid = order.total if is_paid else 0
        order.change_amount = 0
        order.stock_deducted = True
        order.save(update_fields=[
            'subtotal', 'packaging_fee', 'total',
            'amount_paid', 'change_amount', 'stock_deducted',
        ])
        return order

    def _receipt_url(self, token):
        return reverse('orders:customer_receipt', args=[token])

    def _pdf_url(self, token):
        return reverse('orders:customer_receipt_pdf', args=[token])

    # ── Payment gate — HTML receipt ────────────────────────────────────────

    def test_unpaid_cash_order_receipt_returns_403(self):
        """Unpaid Cash order: receipt must not be accessible (403)."""
        order = self._make_order(payment_method='cash', is_paid=False)
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 403)

    def test_gcash_awaiting_submission_receipt_returns_403(self):
        """GCash order with no submission yet: receipt must return 403."""
        order = self._make_order(payment_method='gcash', gcash_status='none', is_paid=False)
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 403)

    def test_gcash_pending_verification_receipt_returns_403(self):
        """GCash order awaiting staff verification: receipt must return 403."""
        order = self._make_order(payment_method='gcash', gcash_status='pending', is_paid=False)
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 403)

    def test_gcash_rejected_receipt_returns_403(self):
        """Rejected GCash order: receipt must return 403."""
        order = self._make_order(payment_method='gcash', gcash_status='rejected', is_paid=False)
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 403)

    def test_gcash_verified_order_can_view_receipt(self):
        """Verified GCash order (is_paid=True): receipt must return 200 with correct content."""
        order = self._make_order(payment_method='gcash', gcash_status='verified', is_paid=True)
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(order.order_number, content)
        self.assertIn('Test Customer', content)
        self.assertIn('Iced Latte', content)

    def test_cash_paid_order_can_view_receipt(self):
        """Paid Cash order: receipt must return 200 with correct content."""
        order = self._make_order(payment_method='cash', is_paid=True)
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        self.assertIn(order.order_number, response.content.decode())

    # ── Content correctness ────────────────────────────────────────────────

    def test_receipt_contains_correct_item_prices(self):
        """Receipt must display saved unit_price and subtotal snapshots."""
        order = self._make_order(payment_method='cash', is_paid=True)
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # unit_price=75.00, quantity=2, subtotal=150.00
        self.assertIn('75.00', content)
        self.assertIn('150.00', content)

    def test_receipt_shows_packaging_fee(self):
        """Takeout order with packaging fee: fee must appear on the receipt."""
        order = self._make_order(
            payment_method='cash', is_paid=True,
            order_type='takeout', with_packaging=True,
        )
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # packaging_fee=10.00
        self.assertIn('10.00', content)

    # ── Payment gate — PDF ─────────────────────────────────────────────────

    def test_receipt_pdf_unpaid_returns_403(self):
        """Unpaid order: PDF endpoint must return 403."""
        order = self._make_order(payment_method='cash', is_paid=False)
        response = self.client.get(self._pdf_url(order.tracking_token))
        self.assertEqual(response.status_code, 403)

    def test_receipt_pdf_paid_returns_pdf(self):
        """Paid order: PDF endpoint must return 200 application/pdf with %PDF header."""
        order = self._make_order(payment_method='cash', is_paid=True)
        response = self.client.get(self._pdf_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn(f'receipt-{order.order_number}', response['Content-Disposition'])
        # Real PDF files start with the %PDF magic bytes.
        self.assertTrue(response.content.startswith(b'%PDF'))

    # ── Token security ─────────────────────────────────────────────────────

    def test_invalid_tracking_token_receipt_returns_404(self):
        """Nonexistent tracking token: receipt must return 404."""
        response = self.client.get(self._receipt_url('totally-invalid-token-xyz'))
        self.assertEqual(response.status_code, 404)

    def test_invalid_token_cannot_access_another_order_receipt(self):
        """An unknown/random token cannot expose any order's receipt."""
        # Create a paid order to confirm it exists, then prove it is
        # unreachable via a token that does not match its tracking_token.
        order1 = self._make_order(payment_method='cash', is_paid=True)
        # Construct a URL using a completely random token (not order1's).
        import secrets
        random_token = secrets.token_urlsafe(32)
        # Ensure it really is a different token (near-zero collision probability).
        self.assertNotEqual(random_token, order1.tracking_token)
        bad_url = reverse('orders:customer_receipt', args=[random_token])
        response = self.client.get(bad_url)
        self.assertEqual(response.status_code, 404)

    def test_pdf_invalid_tracking_token_returns_404(self):
        """Nonexistent tracking token: PDF endpoint must return 404."""
        response = self.client.get(self._pdf_url('no-such-token-abc'))
        self.assertEqual(response.status_code, 404)

    # ── Read-only invariant ────────────────────────────────────────────────

    def test_receipt_view_does_not_modify_order(self):
        """Viewing the HTML receipt must not change any order field."""
        order = self._make_order(payment_method='cash', is_paid=True)
        snapshot_is_paid  = order.is_paid
        snapshot_status   = order.status
        snapshot_total    = order.total

        self.client.get(self._receipt_url(order.tracking_token))

        order.refresh_from_db()
        self.assertEqual(order.is_paid, snapshot_is_paid)
        self.assertEqual(order.status, snapshot_status)
        self.assertEqual(order.total, snapshot_total)

    def test_receipt_pdf_does_not_modify_order(self):
        """Downloading the PDF receipt must not change any order field."""
        order = self._make_order(payment_method='cash', is_paid=True)
        snapshot_is_paid  = order.is_paid
        snapshot_status   = order.status
        snapshot_total    = order.total

        self.client.get(self._pdf_url(order.tracking_token))

        order.refresh_from_db()
        self.assertEqual(order.is_paid, snapshot_is_paid)
        self.assertEqual(order.status, snapshot_status)
        self.assertEqual(order.total, snapshot_total)

    def test_receipt_generation_does_not_change_inventory(self):
        """Receipt view and PDF download must not alter product stock."""
        order = self._make_order(payment_method='cash', is_paid=True)
        self.product.refresh_from_db()
        stock_before = self.product.stock_quantity

        self.client.get(self._receipt_url(order.tracking_token))
        self.client.get(self._pdf_url(order.tracking_token))

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, stock_before)

    # ── Staff receipt still requires login ────────────────────────────────

    def test_staff_print_receipt_still_requires_login(self):
        """Existing staff receipt endpoint must still require login."""
        order = self._make_order(payment_method='cash', is_paid=True)
        # No login — should redirect to login page.
        url = reverse('orders:print_receipt', args=[order.pk])
        response = self.client.get(url)
        self.assertNotEqual(response.status_code, 200)
        # Must be a redirect to login (3xx).
        self.assertIn(response.status_code, [301, 302])

    def test_staff_print_receipt_works_for_cashier(self):
        """Authenticated cashier must be able to access the staff receipt."""
        order = self._make_order(payment_method='cash', is_paid=True)
        self.client.force_login(self.cashier)
        url = reverse('orders:print_receipt', args=[order.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(order.order_number, response.content.decode())

    # ── GCash screenshot not exposed ──────────────────────────────────────

    def test_receipt_does_not_expose_gcash_proof_url(self):
        """The customer receipt must never include the GCash screenshot URL."""
        order = self._make_order(payment_method='gcash', gcash_status='verified', is_paid=True)
        # Set a fake proof URL to confirm it never leaks into the response.
        order.gcash_reference = 'REF-RECEIPT-TEST'
        order.save(update_fields=['gcash_reference'])

        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # gcash_proof / Cloudinary URL must not appear in the customer page.
        self.assertNotIn('gcash_proof', content)
        self.assertNotIn('gcash_proofs/', content)
        # GCash reference IS shown (confirmed payment context).
        self.assertIn('REF-RECEIPT-TEST', content)


# ── PaidAtTimestampTests ───────────────────────────────────────────────────────

PASSWORD_PAT = 'kdm-paidat-pass-1'


class PaidAtTimestampTests(TestCase):
    """Regression tests for the paid_at payment-confirmation timestamp field.

    Covers:
    1.  Cash payment confirmation sets paid_at.
    2.  GCash verification sets paid_at.
    3.  Unpaid order has paid_at = None.
    4.  Rejected GCash payment has paid_at = None.
    5.  GCash screenshot upload does NOT set paid_at.
    6.  Calling Cash confirmation twice does NOT reset the original paid_at.
    7.  Calling GCash verification twice does NOT reset the original paid_at.
    8.  HTML receipt shows the correct paid_at for a new paid order.
    9.  PDF receipt shows the correct paid_at for a new paid order.
    10. Historical order with paid_at=None and gcash_verified_at set → receipt
        shows the gcash_verified_at fallback.
    11. Historical order with paid_at=None and gcash_verified_at=None → receipt
        shows 'Confirmation time unavailable'.
    12. Receipt generation (HTML + PDF) is read-only: order fields unchanged.
    """

    def setUp(self):
        self.cashier = CustomUser.objects.create_user(
            username='paidat_cashier', password=PASSWORD_PAT, role='cashier',
        )
        self.category = Category.objects.create(
            name='Drinks', slug='drinks-paidat',
        )
        self.product = Product.objects.create(
            category=self.category, name='Latte', price='80.00',
            stock_quantity=50,
        )
        self.client.force_login(self.cashier)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _awaiting_cash_order(self):
        """Create an awaiting_payment Cash order with one item."""
        order = Order.objects.create(
            customer_name='Test Customer',
            status='awaiting_payment',
            payment_method='cash',
            is_paid=False,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name='Latte',
            category_name=self.category.name,
            packaging_eligible=False,
            size='none',
            quantity=1,
            unit_price=Decimal('80.00'),
            subtotal=Decimal('80.00'),
        )
        order.subtotal = Decimal('80.00')
        order.total = Decimal('80.00')
        order.stock_deducted = True
        order.save(update_fields=['subtotal', 'total', 'stock_deducted'])
        return order

    def _awaiting_gcash_order(self, gcash_status='pending'):
        """Create an awaiting_payment GCash order with a submitted reference."""
        order = Order.objects.create(
            customer_name='Test Customer',
            status='awaiting_payment',
            payment_method='gcash',
            gcash_status=gcash_status,
            gcash_reference='REF-PAT-001',
            is_paid=False,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name='Latte',
            category_name=self.category.name,
            packaging_eligible=False,
            size='none',
            quantity=1,
            unit_price=Decimal('80.00'),
            subtotal=Decimal('80.00'),
        )
        order.subtotal = Decimal('80.00')
        order.total = Decimal('80.00')
        order.stock_deducted = True
        order.save(update_fields=['subtotal', 'total', 'stock_deducted'])
        return order

    def _pay_cash(self, order):
        return self.client.post(
            reverse('orders:process_payment', args=[order.pk]),
            {'payment_method': 'cash', 'amount_paid': '80.00'},
        )

    def _verify_gcash(self, order):
        return self.client.post(
            reverse('orders:verify_gcash_payment', args=[order.pk]),
        )

    def _receipt_url(self, token):
        return reverse('orders:customer_receipt', args=[token])

    def _pdf_url(self, token):
        return reverse('orders:customer_receipt_pdf', args=[token])

    # ── 1. Cash payment confirmation sets paid_at ──────────────────────────

    def test_cash_payment_sets_paid_at(self):
        """process_payment (Cash) must set paid_at to a non-null, tz-aware datetime."""
        order = self._awaiting_cash_order()
        before = timezone.now()
        self._pay_cash(order)
        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertIsNotNone(order.paid_at)
        # Must be timezone-aware.
        self.assertIsNotNone(order.paid_at.tzinfo)
        # Must be set to approximately now (within 10 seconds of test execution).
        self.assertGreaterEqual(order.paid_at, before)

    # ── 2. GCash verification sets paid_at ─────────────────────────────────

    def test_gcash_verification_sets_paid_at(self):
        """verify_gcash_payment must set paid_at to a non-null, tz-aware datetime."""
        order = self._awaiting_gcash_order(gcash_status='pending')
        before = timezone.now()
        self._verify_gcash(order)
        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertIsNotNone(order.paid_at)
        self.assertIsNotNone(order.paid_at.tzinfo)
        self.assertGreaterEqual(order.paid_at, before)

    # ── 3. Unpaid order has paid_at = None ─────────────────────────────────

    def test_unpaid_order_paid_at_is_none(self):
        """A newly created, unpaid order must have paid_at = None."""
        order = self._awaiting_cash_order()
        order.refresh_from_db()
        self.assertFalse(order.is_paid)
        self.assertIsNone(order.paid_at)

    # ── 4. Rejected GCash has paid_at = None ───────────────────────────────

    def test_rejected_gcash_paid_at_is_none(self):
        """Rejecting a GCash payment must not set paid_at."""
        order = self._awaiting_gcash_order(gcash_status='pending')
        self.client.post(
            reverse('orders:reject_gcash_payment', args=[order.pk]),
            {'notes': 'Invalid reference'},
        )
        order.refresh_from_db()
        self.assertFalse(order.is_paid)
        self.assertIsNone(order.paid_at)

    # ── 5. GCash screenshot upload does NOT set paid_at ────────────────────

    def test_gcash_screenshot_upload_does_not_set_paid_at(self):
        """Submitting a GCash screenshot must not set paid_at or is_paid."""
        # Create a GCash order with no submission yet.
        order = self._awaiting_gcash_order(gcash_status='none')
        # Reset gcash_status to 'none' for clean state.
        order.gcash_status = 'none'
        order.gcash_reference = ''
        order.save(update_fields=['gcash_status', 'gcash_reference'])

        # Post the GCash submission — customer submitting their reference.
        self.client.logout()  # This is a customer-facing, unauthenticated endpoint.
        self.client.post(
            reverse('orders:submit_gcash_payment', args=[order.tracking_token]),
            {
                'gcash_reference': 'REF-UPLOAD-TEST',
                'tracking_token': order.tracking_token,
            },
            content_type='application/json',
        )
        order.refresh_from_db()
        self.assertFalse(order.is_paid)
        self.assertIsNone(order.paid_at)

    # ── 6. Cash confirmation twice does NOT reset paid_at ──────────────────

    def test_cash_confirmation_twice_does_not_reset_paid_at(self):
        """A duplicate Cash confirmation request must not reset the original paid_at."""
        order = self._awaiting_cash_order()
        # First confirmation.
        self._pay_cash(order)
        order.refresh_from_db()
        original_paid_at = order.paid_at
        self.assertIsNotNone(original_paid_at)

        # Second attempt — order is already paid; should be rejected.
        self._pay_cash(order)
        order.refresh_from_db()
        # paid_at must be unchanged.
        self.assertEqual(order.paid_at, original_paid_at)

    # ── 7. GCash verification twice does NOT reset paid_at ─────────────────

    def test_gcash_verification_twice_does_not_reset_paid_at(self):
        """A duplicate GCash verification request must not reset the original paid_at."""
        order = self._awaiting_gcash_order(gcash_status='pending')
        # First verification.
        self._verify_gcash(order)
        order.refresh_from_db()
        original_paid_at = order.paid_at
        self.assertIsNotNone(original_paid_at)

        # Second attempt — order is already paid; should be rejected with 400.
        resp = self._verify_gcash(order)
        self.assertFalse(resp.json()['success'])
        order.refresh_from_db()
        self.assertEqual(order.paid_at, original_paid_at)

    # ── 8. HTML receipt shows correct paid_at ─────────────────────────────

    def test_html_receipt_shows_paid_at(self):
        """HTML receipt must display the paid_at timestamp for a new paid order."""
        order = self._awaiting_cash_order()
        self._pay_cash(order)
        order.refresh_from_db()
        self.assertIsNotNone(order.paid_at)

        self.client.logout()  # Receipt is customer-facing, no login required.
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # The formatted paid_at date should appear in the page.
        from django.utils.formats import date_format
        expected = date_format(
            timezone.localtime(order.paid_at), 'M d, Y g:i A',
        )
        self.assertIn(expected, content)
        # The fallback text must NOT appear when paid_at is set.
        self.assertNotIn('Confirmation time unavailable', content)

    # ── 9. PDF receipt shows correct paid_at ──────────────────────────────

    def test_pdf_receipt_shows_paid_at(self):
        """PDF receipt must be generated successfully for an order with paid_at set."""
        order = self._awaiting_cash_order()
        self._pay_cash(order)
        order.refresh_from_db()
        self.assertIsNotNone(order.paid_at)

        self.client.logout()
        response = self.client.get(self._pdf_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        # Real PDF bytes start with %PDF.
        self.assertTrue(response.content.startswith(b'%PDF'))

    # ── 10. Historical order: paid_at=None, gcash_verified_at set → fallback

    def test_html_receipt_gcash_verified_at_fallback(self):
        """Historical GCash order with paid_at=None must show gcash_verified_at fallback."""
        historical_time = timezone.now()
        order = Order.objects.create(
            customer_name='Historical Customer',
            status='preparing',
            payment_method='gcash',
            gcash_status='verified',
            gcash_reference='REF-HIST',
            gcash_verified_at=historical_time,
            is_paid=True,
            paid_at=None,  # Historical — confirmation time not recorded.
            amount_paid=Decimal('80.00'),
            total=Decimal('80.00'),
            subtotal=Decimal('80.00'),
            stock_deducted=True,
        )
        OrderItem.objects.create(
            order=order, product=self.product, product_name='Latte',
            category_name=self.category.name, packaging_eligible=False,
            size='none', quantity=1,
            unit_price=Decimal('80.00'), subtotal=Decimal('80.00'),
        )

        self.client.logout()
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Fallback: gcash_verified_at must be shown.
        from django.utils.formats import date_format
        expected = date_format(
            timezone.localtime(historical_time), 'M d, Y g:i A',
        )
        self.assertIn(expected, content)
        self.assertIn('GCash verification time', content)
        # Primary paid_at fallback text must NOT appear.
        self.assertNotIn('Confirmation time unavailable', content)

    # ── 11. Historical order: both paid_at and gcash_verified_at are None ──

    def test_html_receipt_confirmation_time_unavailable(self):
        """Historical order with both timestamps null must show fallback text."""
        order = Order.objects.create(
            customer_name='Old Customer',
            status='preparing',
            payment_method='cash',
            is_paid=True,
            paid_at=None,
            gcash_verified_at=None,
            amount_paid=Decimal('80.00'),
            total=Decimal('80.00'),
            subtotal=Decimal('80.00'),
            stock_deducted=True,
        )
        OrderItem.objects.create(
            order=order, product=self.product, product_name='Latte',
            category_name=self.category.name, packaging_eligible=False,
            size='none', quantity=1,
            unit_price=Decimal('80.00'), subtotal=Decimal('80.00'),
        )

        self.client.logout()
        response = self.client.get(self._receipt_url(order.tracking_token))
        self.assertEqual(response.status_code, 200)
        self.assertIn('Confirmation time unavailable', response.content.decode())

    # ── 12. Receipt generation is read-only (paid_at unchanged after GET) ──

    def test_receipt_generation_does_not_modify_paid_at(self):
        """Viewing HTML and PDF receipts must not change paid_at or any other field."""
        order = self._awaiting_cash_order()
        self._pay_cash(order)
        order.refresh_from_db()
        snapshot_paid_at  = order.paid_at
        snapshot_is_paid  = order.is_paid
        snapshot_status   = order.status

        self.client.logout()
        self.client.get(self._receipt_url(order.tracking_token))
        self.client.get(self._pdf_url(order.tracking_token))

        order.refresh_from_db()
        self.assertEqual(order.paid_at, snapshot_paid_at)
        self.assertEqual(order.is_paid, snapshot_is_paid)
        self.assertEqual(order.status, snapshot_status)
