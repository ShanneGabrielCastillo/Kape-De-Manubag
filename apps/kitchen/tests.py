from unittest import mock
import queue as queue_module

from django.test import TestCase, Client
from django.urls import reverse
from django.db import transaction as db_transaction
from apps.accounts.models import CustomUser
from apps.orders.models import Order, OrderItem


class KitchenAccessTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.kitchen_user = CustomUser.objects.create_user(
            username='kitchen1', password='pass123', role='kitchen_staff'
        )
        self.cashier_user = CustomUser.objects.create_user(
            username='cashier1', password='pass123', role='cashier'
        )
        self.admin_user = CustomUser.objects.create_user(
            username='admin1', password='pass123', role='admin'
        )

    def test_kitchen_orders_accessible_by_kitchen_staff(self):
        self.client.login(username='kitchen1', password='pass123')
        response = self.client.get(reverse('kitchen:orders'))
        self.assertEqual(response.status_code, 200)

    def test_kitchen_orders_blocked_for_cashier(self):
        self.client.login(username='cashier1', password='pass123')
        response = self.client.get(reverse('kitchen:orders'))
        # Should redirect with error message
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(response.status_code, 302)

    def test_kitchen_orders_blocked_for_anonymous(self):
        response = self.client.get(reverse('kitchen:orders'))
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_kitchen_orders_accessible_by_admin(self):
        # Admin should NOT have access to kitchen:orders (kitchen_staff_required only)
        self.client.login(username='admin1', password='pass123')
        response = self.client.get(reverse('kitchen:orders'))
        self.assertNotEqual(response.status_code, 200)

    def test_is_kitchen_staff_property(self):
        self.assertTrue(self.kitchen_user.is_kitchen_staff)
        self.assertFalse(self.cashier_user.is_kitchen_staff)
        self.assertFalse(self.admin_user.is_kitchen_staff)

    def test_mark_ready_requires_post(self):
        # Create a minimal preparing order
        order = Order.objects.create(
            customer_name='Test Customer',
            status='preparing',
            is_paid=True
        )
        self.client.login(username='kitchen1', password='pass123')
        url = reverse('kitchen:mark_ready', args=[order.pk])
        # GET should be rejected (require_POST decorator)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_mark_ready_success(self):
        order = Order.objects.create(
            customer_name='Test Customer',
            status='preparing',
            is_paid=True
        )
        self.client.login(username='kitchen1', password='pass123')
        url = reverse('kitchen:mark_ready', args=[order.pk])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['new_status'], 'ready')
        # Refresh from DB and verify
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')
        self.assertIsNotNone(order.ready_at)

    def test_mark_ready_wrong_status(self):
        # Trying to mark an awaiting_payment order as ready should fail
        order = Order.objects.create(
            customer_name='Test Customer',
            status='awaiting_payment',
            is_paid=False
        )
        self.client.login(username='kitchen1', password='pass123')
        url = reverse('kitchen:mark_ready', args=[order.pk])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('error', data)

    def test_mark_ready_requires_auth(self):
        order = Order.objects.create(
            customer_name='Test Customer',
            status='preparing',
            is_paid=True
        )
        url = reverse('kitchen:mark_ready', args=[order.pk])
        response = self.client.post(url)
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_mark_ready_wrong_role(self):
        order = Order.objects.create(
            customer_name='Test Customer',
            status='preparing',
            is_paid=True
        )
        self.client.login(username='cashier1', password='pass123')
        url = reverse('kitchen:mark_ready', args=[order.pk])
        response = self.client.post(url)
        # Should redirect (kitchen_staff_required blocks cashier)
        self.assertNotEqual(response.status_code, 200)

    def test_kitchen_orders_shows_only_preparing_orders(self):
        # Create orders in different statuses
        Order.objects.create(
            customer_name='Customer A',
            status='awaiting_payment',
            is_paid=False
        )
        Order.objects.create(
            customer_name='Customer B',
            status='preparing',
            is_paid=True
        )
        Order.objects.create(
            customer_name='Customer C',
            status='ready',
            is_paid=True
        )
        Order.objects.create(
            customer_name='Customer D',
            status='completed',
            is_paid=True
        )

        self.client.login(username='kitchen1', password='pass123')
        response = self.client.get(reverse('kitchen:orders'))
        self.assertEqual(response.status_code, 200)
        # Only preparing orders should appear
        orders = response.context['orders']
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].customer_name, 'Customer B')

    def test_login_redirects_kitchen_staff_to_kitchen(self):
        # Test that kitchen_staff login redirects to kitchen:orders
        response = self.client.post(reverse('accounts:login'), {
            'username': 'kitchen1',
            'password': 'pass123',
        }, follow=False)
        self.assertEqual(response.status_code, 302)
        # Should redirect to kitchen:orders
        self.assertEqual(response.url, reverse('kitchen:orders'))


class KitchenEventStreamTest(TestCase):
    """Tests for the kitchen SSE endpoint access control.

    Access-control tests (redirect cases) don't touch the stream at all.
    For authorized-user cases, we verify the response headers by patching
    subscribe() to return a queue that immediately stops iteration.
    """

    def setUp(self):
        self.client = Client()
        self.kitchen_user = CustomUser.objects.create_user(
            username='kitchen1', password='pass123', role='kitchen_staff'
        )
        self.admin_user = CustomUser.objects.create_user(
            username='admin1', password='pass123', role='admin'
        )
        self.cashier_user = CustomUser.objects.create_user(
            username='cashier1', password='pass123', role='cashier'
        )

    def test_kitchen_event_stream_accessible_by_kitchen_staff(self):
        """kitchen_staff gets a 200 SSE response."""
        from unittest.mock import patch, MagicMock
        import queue as q

        stub_queue = MagicMock()
        # Raise StopIteration on first get to exit the while loop via GeneratorExit
        stub_queue.get.side_effect = GeneratorExit()

        with patch('apps.realtime.views.subscribe', return_value=stub_queue), \
             patch('apps.realtime.views.unsubscribe'):
            self.client.login(username='kitchen1', password='pass123')
            response = self.client.get(reverse('realtime:kitchen_event_stream'))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'text/event-stream')

    def test_kitchen_event_stream_accessible_by_admin(self):
        """Admin gets a 200 SSE response (kitchen_or_admin_required allows admin)."""
        from unittest.mock import patch, MagicMock

        stub_queue = MagicMock()
        stub_queue.get.side_effect = GeneratorExit()

        with patch('apps.realtime.views.subscribe', return_value=stub_queue), \
             patch('apps.realtime.views.unsubscribe'):
            self.client.login(username='admin1', password='pass123')
            response = self.client.get(reverse('realtime:kitchen_event_stream'))
            self.assertEqual(response.status_code, 200)

    def test_kitchen_event_stream_blocked_for_cashier(self):
        """Cashier is redirected away (kitchen_or_admin_required blocks them)."""
        self.client.login(username='cashier1', password='pass123')
        # The decorator should redirect without ever running the stream generator
        try:
            response = self.client.get(
                reverse('realtime:kitchen_event_stream'),
                follow=False  # Don't follow redirects
            )
            self.assertEqual(response.status_code, 302)
        except Exception as e:
            self.fail(f"Cashier test raised exception: {e}")

    def test_kitchen_event_stream_blocked_for_anonymous(self):
        """Anonymous user is sent to login."""
        response = self.client.get(reverse('realtime:kitchen_event_stream'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)


class KitchenRealtimeTests(TestCase):
    """Tests for the kitchen-to-cashier realtime status-change path.

    These tests verify that mark_order_ready:
      1. Persists status='ready' in the database.
      2. Publishes exactly one 'status_changed' event via the broker.
      3. Publishes AFTER the transaction commits (on_commit).
      4. Does NOT publish on a rollback.
      5. Publishes a complete payload.
      6. Rejects unauthorized users and invalid transitions.

    Because Django's TestCase wraps every test in a transaction that never
    commits, transaction.on_commit() callbacks would never fire normally.
    We patch on_commit to call the callback immediately — the same technique
    used in NewOrderRealtimePayloadTests (apps/orders/tests.py).
    """

    def setUp(self):
        self.client = Client()
        self.kitchen_user = CustomUser.objects.create_user(
            username='rt-kitchen1', password='pass123', role='kitchen_staff'
        )
        self.cashier_user = CustomUser.objects.create_user(
            username='rt-cashier1', password='pass123', role='cashier'
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _make_preparing_order(self):
        return Order.objects.create(
            customer_name='RT Customer',
            status='preparing',
            is_paid=True,
        )

    def _drain(self, q):
        """Drain all items from queue q without blocking; return as a list."""
        events = []
        while True:
            try:
                events.append(q.get_nowait())
            except queue_module.Empty:
                return events

    # ── tests ─────────────────────────────────────────────────────────────

    def test_mark_ready_publishes_status_changed_on_commit(self):
        """mark_order_ready publishes exactly one status_changed event after commit."""
        from apps.realtime.broker import subscribe, unsubscribe

        order = self._make_preparing_order()

        with mock.patch.object(db_transaction, 'on_commit', side_effect=lambda fn: fn()):
            q = subscribe()
            try:
                self.client.login(username='rt-kitchen1', password='pass123')
                response = self.client.post(
                    reverse('kitchen:mark_ready', args=[order.pk])
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['success'])
                events = self._drain(q)
            finally:
                unsubscribe(q)

        status_events = [e for e in events if e['event'] == 'status_changed']
        self.assertEqual(len(status_events), 1,
                         f"Expected 1 status_changed event, got {len(status_events)}")
        self.assertEqual(status_events[0]['data']['new_status'], 'ready')
        self.assertEqual(status_events[0]['data']['order_id'], order.pk)

    def test_mark_ready_no_duplicate_events(self):
        """_skip_realtime flag prevents the post_save signal from also publishing,
        so exactly one event is emitted — not two."""
        from apps.realtime.broker import subscribe, unsubscribe

        order = self._make_preparing_order()

        with mock.patch.object(db_transaction, 'on_commit', side_effect=lambda fn: fn()):
            q = subscribe()
            try:
                self.client.login(username='rt-kitchen1', password='pass123')
                self.client.post(reverse('kitchen:mark_ready', args=[order.pk]))
                events = self._drain(q)
            finally:
                unsubscribe(q)

        status_events = [e for e in events if e['event'] == 'status_changed']
        self.assertEqual(len(status_events), 1,
                         "Duplicate event detected — _skip_realtime flag may not be working")

    def test_mark_ready_event_not_published_on_rollback(self):
        """If the atomic block rolls back, no event reaches the broker.

        We simulate a rollback by patching log_action to raise after the save,
        and patching on_commit to be a no-op (so even if the callback were
        registered it would not fire).
        """
        from apps.realtime.broker import subscribe, unsubscribe

        order = self._make_preparing_order()

        # no-op on_commit — callbacks will never fire
        with mock.patch.object(db_transaction, 'on_commit', side_effect=lambda fn: None), \
             mock.patch('apps.kitchen.views.log_action', side_effect=RuntimeError("boom")):
            q = subscribe()
            try:
                self.client.login(username='rt-kitchen1', password='pass123')
                # The view will raise inside the atomic block → rollback
                try:
                    self.client.post(reverse('kitchen:mark_ready', args=[order.pk]))
                except RuntimeError:
                    pass
                events = self._drain(q)
            finally:
                unsubscribe(q)

        status_events = [e for e in events if e['event'] == 'status_changed']
        self.assertEqual(len(status_events), 0,
                         "Event should not be published when the transaction rolls back")

    def test_mark_ready_event_payload_is_complete(self):
        """The published payload contains all required fields for the cashier JS handler."""
        from apps.realtime.broker import subscribe, unsubscribe

        order = self._make_preparing_order()

        with mock.patch.object(db_transaction, 'on_commit', side_effect=lambda fn: fn()):
            q = subscribe()
            try:
                self.client.login(username='rt-kitchen1', password='pass123')
                self.client.post(reverse('kitchen:mark_ready', args=[order.pk]))
                events = self._drain(q)
            finally:
                unsubscribe(q)

        status_events = [e for e in events if e['event'] == 'status_changed']
        self.assertEqual(len(status_events), 1)
        payload = status_events[0]['data']
        for field in ('order_id', 'order_number', 'queue_number',
                      'new_status', 'new_status_display', 'is_paid'):
            self.assertIn(field, payload,
                          f"Required field '{field}' missing from status_changed payload")
        self.assertEqual(payload['new_status'], 'ready')
        self.assertEqual(payload['new_status_display'], 'Ready')

    def test_mark_ready_db_status_is_ready(self):
        """After a successful POST, the DB row status is 'ready'."""
        order = self._make_preparing_order()
        self.client.login(username='rt-kitchen1', password='pass123')

        with mock.patch.object(db_transaction, 'on_commit', side_effect=lambda fn: fn()):
            response = self.client.post(reverse('kitchen:mark_ready', args=[order.pk]))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')

    def test_mark_ready_unauthorized_cashier_cannot_mark_ready(self):
        """A cashier (wrong role) is rejected and the order status is unchanged."""
        order = self._make_preparing_order()
        self.client.login(username='rt-cashier1', password='pass123')
        response = self.client.post(reverse('kitchen:mark_ready', args=[order.pk]))
        self.assertNotEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'preparing')

    def test_mark_ready_invalid_status_transition_rejected(self):
        """Marking an awaiting_payment order as ready returns HTTP 400."""
        order = Order.objects.create(
            customer_name='Bad Transition',
            status='awaiting_payment',
            is_paid=False,
        )
        self.client.login(username='rt-kitchen1', password='pass123')
        response = self.client.post(reverse('kitchen:mark_ready', args=[order.pk]))
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        # DB must be unchanged
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')

    def test_mark_ready_anonymous_cannot_mark_ready(self):
        """Unauthenticated request is redirected to login; order unchanged."""
        order = self._make_preparing_order()
        response = self.client.post(reverse('kitchen:mark_ready', args=[order.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)
        order.refresh_from_db()
        self.assertEqual(order.status, 'preparing')
