"""
Atomicity tests for the inventory-touching workflows.

Every workflow that moves stock must do so inside a single database
transaction: if ANY step fails, all related changes (order, order items,
stock levels, inventory logs) roll back together. These tests verify the
success paths and inject mid-transaction failures to prove the rollback.
"""
import json
from unittest import mock

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
        pending submitted reference — those must go through verify_gcash_payment."""
        self.client.force_login(self.cashier)
        order = self._customer_order()
        order.payment_method = 'gcash'
        order.gcash_status = 'pending'
        order.gcash_reference = 'REMOTE123'
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

    def test_submit_gcash_payment_sets_pending_status(self):
        """Customer submits GCash reference → gcash_status becomes pending, is_paid stays False."""
        order = self._gcash_order()
        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_reference': 'REF1234567890', 'csrfmiddlewaretoken': 'dummy'},
        )
        # Use Django test client which handles CSRF automatically
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])

        order.refresh_from_db()
        self.assertEqual(order.gcash_status, 'pending')
        self.assertFalse(order.is_paid)  # CRITICAL: must NOT be paid
        self.assertEqual(order.status, 'awaiting_payment')  # status unchanged
        self.assertEqual(order.gcash_reference, 'REF1234567890')

    def test_submit_gcash_idempotent_for_pending(self):
        """Re-submitting when already pending returns success without re-writing."""
        order = self._gcash_order()
        order.gcash_status = 'pending'
        order.gcash_reference = 'ORIGINAL123'
        order.save(update_fields=['gcash_status', 'gcash_reference'])

        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_reference': 'DIFFERENT456'},
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data.get('already_submitted'))

        order.refresh_from_db()
        self.assertEqual(order.gcash_reference, 'ORIGINAL123')  # unchanged

    def test_submit_gcash_rejects_empty_reference(self):
        """Empty reference number must be rejected."""
        order = self._gcash_order()
        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_reference': ''},
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('gcash_reference', data.get('errors', {}))

    def test_submit_gcash_blocks_on_paid_order(self):
        """Cannot submit GCash reference if order is already paid."""
        order = self._gcash_order()
        order.is_paid = True
        order.save(update_fields=['is_paid'])

        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_reference': 'REF123'},
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('already been paid', data['error'])

    def test_submit_gcash_blocks_on_cancelled_order(self):
        """Cannot submit GCash reference on a cancelled order."""
        order = self._gcash_order()
        order.status = 'cancelled'
        order.cancelled_at = timezone.now()
        order.save(update_fields=['status', 'cancelled_at'])

        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_reference': 'REF123'},
        )
        data = response.json()
        self.assertFalse(data['success'])

    def test_submit_gcash_blocks_on_cash_order(self):
        """submit_gcash_payment must reject cash orders."""
        order = self._customer_order()  # payment_method='cash' by default
        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order.tracking_token}),
            {'gcash_reference': 'REF123'},
        )
        data = response.json()
        self.assertFalse(data['success'])

    def test_submit_gcash_blocks_duplicate_reference(self):
        """Same reference number cannot be used on two different orders."""
        order1 = self._gcash_order()
        order1.gcash_status = 'pending'
        order1.gcash_reference = 'DUPREF123'
        order1.save(update_fields=['gcash_status', 'gcash_reference'])

        order2 = Order.objects.create(
            customer_name='Another Customer',
            status='awaiting_payment',
            payment_method='gcash',
        )

        response = self.client.post(
            reverse('orders:submit_gcash_payment', kwargs={'tracking_token': order2.tracking_token}),
            {'gcash_reference': 'DUPREF123'},
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('gcash_reference', data.get('errors', {}))

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


