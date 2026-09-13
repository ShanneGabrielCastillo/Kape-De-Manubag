"""
Security tests for:
  1. Anonymous order rate limiting  (ORDER_RATE_LIMIT / ORDER_RATE_WINDOW)
  2. Secure public order tracking   (tracking_token)

These tests cover the requirements from the security audit task:
  - max 3 orders per 10-minute window per session
  - failed attempts do not consume quota
  - idempotency replays do not consume quota
  - independent sessions tracked separately
  - tracking_token generated on every new order
  - tracking_token is unique per order
  - tracker page uses token, not order_number
  - API tracker uses token, not order_number
  - invalid token returns 404
  - order_number alone cannot access tracker
"""
import time
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.menu.models import Category, Product
from apps.orders.models import Cart, CartItem, Order
from apps.orders.views import _ORDER_RATE_SESSION_KEY


PASSWORD = 'kdm-security-test-pass-1'

# ── helpers ────────────────────────────────────────────────────────────────

def _make_cart(client, product, quantity=1):
    """Seed the test client's session with a cart containing one item."""
    session = client.session
    session['_seed'] = 'x'          # force session save so session_key exists
    session.save()
    cart, _ = Cart.objects.get_or_create(session_key=session.session_key)
    CartItem.objects.get_or_create(
        cart=cart, product=product, size='none',
        defaults={'quantity': quantity, 'unit_price': product.price},
    )
    return cart


def _post_checkout(client, customer_name='Test Customer', order_type='dine_in', token=None):
    """Submit a checkout POST.  Does NOT follow redirects."""
    payload = {'customer_name': customer_name, 'order_type': order_type}
    if token:
        payload['request_token'] = token
    return client.post(reverse('orders:checkout'), payload)


class OrderRateLimitTests(TestCase):
    """Rate-limit: max ORDER_RATE_LIMIT orders per ORDER_RATE_WINDOW seconds."""

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=50,
        )

    # ── helpers ────────────────────────────────────────────────────────────

    def _place_order(self):
        """Add item to cart and submit checkout; return the response."""
        _make_cart(self.client, self.product)
        return _post_checkout(self.client)

    def _place_order_success(self):
        """Place an order and assert it succeeded (redirects to success page)."""
        resp = self._place_order()
        # On success the view redirects to order_success
        self.assertEqual(resp.status_code, 302, "Expected redirect after order")
        self.assertIn('/orders/success/', resp['Location'])
        return resp

    # ── core rate-limit tests ──────────────────────────────────────────────

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_first_three_orders_succeed(self):
        for i in range(3):
            self._place_order_success()
        self.assertEqual(Order.objects.count(), 3)

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_fourth_order_within_window_is_blocked(self):
        for _ in range(3):
            self._place_order_success()

        # 4th attempt — should be blocked
        _make_cart(self.client, self.product)
        resp = _post_checkout(self.client)
        # Rate-limited: stays on checkout page (200), does NOT redirect
        self.assertEqual(resp.status_code, 200)
        # Still only 3 orders exist
        self.assertEqual(Order.objects.count(), 3)

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_blocked_order_does_not_create_order(self):
        for _ in range(3):
            self._place_order_success()
        _make_cart(self.client, self.product)
        _post_checkout(self.client)
        self.assertEqual(Order.objects.count(), 3)

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_blocked_order_does_not_deduct_inventory(self):
        stock_before = self.product.stock_quantity
        for _ in range(3):
            self._place_order_success()
        self.product.refresh_from_db()
        stock_after_3 = self.product.stock_quantity

        # Add item again and attempt 4th order
        _make_cart(self.client, self.product)
        _post_checkout(self.client)
        self.product.refresh_from_db()

        # Stock must not have changed from after the 3rd order
        self.assertEqual(self.product.stock_quantity, stock_after_3)

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_blocked_order_does_not_clear_cart(self):
        for _ in range(3):
            self._place_order_success()

        _make_cart(self.client, self.product)
        session_key = self.client.session.session_key
        cart_items_before = CartItem.objects.filter(cart__session_key=session_key).count()
        self.assertGreater(cart_items_before, 0)

        _post_checkout(self.client)

        cart_items_after = CartItem.objects.filter(cart__session_key=session_key).count()
        self.assertEqual(cart_items_after, cart_items_before,
                         "Cart must be preserved when rate-limited")

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=1)
    def test_order_allowed_after_window_expires(self):
        """After the rate-window lapses, a new order should succeed."""
        for _ in range(3):
            self._place_order_success()

        # Simulate the window expiring by backdating the stored timestamps
        session = self.client.session
        session[_ORDER_RATE_SESSION_KEY] = [time.time() - 5]  # older than 1-second window
        session.save()

        # Now the 4th order should succeed
        self._place_order_success()
        self.assertEqual(Order.objects.count(), 4)

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_failed_checkout_does_not_consume_quota(self):
        """A checkout that bounces due to an empty cart must not burn quota."""
        # Hit checkout with empty cart three times — should not count
        for _ in range(3):
            resp = _post_checkout(self.client)
            # Empty cart → redirect to menu, not order created
            self.assertNotEqual(resp.status_code, 500)

        # Quota must still be intact — real order should succeed
        self._place_order_success()
        self.assertEqual(Order.objects.count(), 1)

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_idempotency_replay_does_not_consume_quota(self):
        """Replaying the same request_token (double-click) must not count twice."""
        _make_cart(self.client, self.product)
        page = self.client.get(reverse('orders:checkout'))
        token = page.context['request_token']

        payload = {'customer_name': 'Test', 'order_type': 'dine_in',
                   'request_token': token}
        # First real submission
        resp1 = self.client.post(reverse('orders:checkout'), payload)
        self.assertIn('/orders/success/', resp1['Location'])
        self.assertEqual(Order.objects.count(), 1)

        # Replay (same token) — server returns original order, rate counter
        # must not increment again for the replay.
        # Seed a new cart item so cart is not empty, then replay old token
        _make_cart(self.client, self.product)
        resp2 = self.client.post(reverse('orders:checkout'), payload)
        self.assertIn('/orders/success/', resp2['Location'])
        # Still only ONE order
        self.assertEqual(Order.objects.count(), 1)

        # Session timestamp list should have only 1 entry (not 2)
        session = self.client.session
        timestamps = session.get(_ORDER_RATE_SESSION_KEY, [])
        self.assertEqual(len(timestamps), 1,
                         "Idempotency replay must not add a second timestamp")

    # ── independent sessions ───────────────────────────────────────────────

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_two_sessions_are_tracked_independently(self):
        """Session A's orders must not affect Session B's quota."""
        from django.test import Client
        client_b = Client()

        # Session A places 3 orders — exhausts its limit
        for _ in range(3):
            self._place_order_success()

        # Session B should still be able to place 1 order
        _make_cart(client_b, self.product)
        resp_b = _post_checkout(client_b)
        self.assertEqual(resp_b.status_code, 302)
        self.assertIn('/orders/success/', resp_b['Location'])

    # ── error message content ──────────────────────────────────────────────

    @override_settings(ORDER_RATE_LIMIT=3, ORDER_RATE_WINDOW=600)
    def test_rate_limit_shows_friendly_message(self):
        """The customer should see a clear, friendly message, not a 500."""
        for _ in range(3):
            self._place_order_success()
        _make_cart(self.client, self.product)
        resp = _post_checkout(self.client)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        # Message should mention waiting, not expose internals
        self.assertIn("ordering limit", content.lower())
        # Must not be an unhandled server error page
        self.assertNotIn("Internal Server Error", content)
        self.assertNotIn("Traceback (most recent call last)", content)


# ── tracking token tests ───────────────────────────────────────────────────

class TrackingTokenTests(TestCase):
    """Secure tracking token: generated automatically, unique, used for
    the public customer tracker endpoint."""

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks-tok')
        self.product = Product.objects.create(
            category=self.category, name='Matcha Latte', price='75.00',
            stock_quantity=20,
        )

    def _place_order(self):
        _make_cart(self.client, self.product)
        resp = _post_checkout(self.client)
        self.assertEqual(resp.status_code, 302)
        order = Order.objects.order_by('-created_at').first()
        return order

    # ── generation ────────────────────────────────────────────────────────

    def test_new_order_has_tracking_token(self):
        order = self._place_order()
        self.assertIsNotNone(order.tracking_token)
        self.assertTrue(len(order.tracking_token) > 0)

    def test_tracking_token_is_url_safe_length(self):
        """secrets.token_urlsafe(32) produces 43 URL-safe base64 chars."""
        order = self._place_order()
        self.assertEqual(len(order.tracking_token), 43)

    @override_settings(ORDER_RATE_LIMIT=10, ORDER_RATE_WINDOW=600)
    def test_tracking_tokens_are_unique(self):
        orders = []
        for _ in range(5):
            # Use a fresh session checkout_request_token each time so every
            # iteration genuinely creates a new order instead of replaying
            # the same idempotency key.
            session = self.client.session
            session.pop('checkout_request_token', None)
            session.save()
            _make_cart(self.client, self.product)
            resp = _post_checkout(self.client)
            self.assertEqual(resp.status_code, 302, "Expected order creation redirect")
            orders.append(Order.objects.order_by('-created_at').first())
        tokens = [o.tracking_token for o in orders]
        self.assertEqual(len(tokens), len(set(tokens)), "All tokens must be unique")

    def test_tracking_token_differs_from_order_number(self):
        order = self._place_order()
        self.assertNotEqual(order.tracking_token, order.order_number)

    def test_tracking_token_differs_from_request_token(self):
        order = self._place_order()
        if order.request_token:
            self.assertNotEqual(order.tracking_token, order.request_token)

    # ── tracker page access ────────────────────────────────────────────────

    def test_tracker_page_accessible_with_tracking_token(self):
        order = self._place_order()
        resp = self.client.get(
            reverse('orders:order_tracker', args=[order.tracking_token])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, order.order_number)

    def test_tracker_page_returns_404_for_invalid_token(self):
        resp = self.client.get(
            reverse('orders:order_tracker', args=['totally-invalid-token-xyz'])
        )
        self.assertEqual(resp.status_code, 404)

    def test_tracker_page_with_order_number_returns_404(self):
        """The predictable order_number alone must not grant tracker access."""
        order = self._place_order()
        resp = self.client.get(
            reverse('orders:order_tracker', args=[order.order_number])
        )
        # order_number is not a valid tracking_token — should 404
        self.assertEqual(resp.status_code, 404)

    # ── tracker API ────────────────────────────────────────────────────────

    def test_api_track_accessible_with_tracking_token(self):
        order = self._place_order()
        resp = self.client.get(
            reverse('orders:api_track_order', args=[order.tracking_token])
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['order_number'], order.order_number)
        self.assertIn('status', data)
        self.assertIn('queue_number', data)

    def test_api_track_returns_404_for_invalid_token(self):
        resp = self.client.get(
            reverse('orders:api_track_order', args=['bad-token-xyz'])
        )
        self.assertEqual(resp.status_code, 404)

    def test_api_track_does_not_expose_payment_info(self):
        """API response must not include payment amounts or cashier info."""
        order = self._place_order()
        resp = self.client.get(
            reverse('orders:api_track_order', args=[order.tracking_token])
        )
        data = resp.json()
        self.assertNotIn('amount_paid', data)
        self.assertNotIn('change_amount', data)
        self.assertNotIn('cashier', data)
        self.assertNotIn('is_paid', data)
        self.assertNotIn('tracking_token', data)

    # ── success page ──────────────────────────────────────────────────────

    def test_order_success_page_contains_tracking_link(self):
        """Success page must link to the secure tracker URL (token-based)."""
        order = self._place_order()
        session = self.client.session
        session['last_order_id'] = order.pk
        session.save()
        resp = self.client.get(reverse('orders:order_success', args=[order.pk]))
        self.assertEqual(resp.status_code, 200)
        tracker_url = reverse('orders:order_tracker', args=[order.tracking_token])
        self.assertContains(resp, tracker_url,
                            msg_prefix="Success page must link to secure tracker URL")

    def test_order_success_page_does_not_contain_bare_order_number_tracker_link(self):
        """Success page must NOT link to /orders/track/<order_number>/."""
        order = self._place_order()
        session = self.client.session
        session['last_order_id'] = order.pk
        session.save()
        resp = self.client.get(reverse('orders:order_success', args=[order.pk]))
        insecure_url = f'/orders/track/{order.order_number}/'
        content = resp.content.decode()
        self.assertNotIn(insecure_url, content,
                         "Success page must not expose predictable order_number as tracker URL")
