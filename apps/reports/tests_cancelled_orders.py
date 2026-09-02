"""
Cancelled order handling tests — Kape De Manubag.

Formally proves that cancelled orders never inflate sales totals in any
module, and that every status transition is correctly handled.

The authoritative sales definition shared by all modules:
    is_paid=True AND status='completed'

Workflow simulated:
    Order created (pending)
    → cashier advances: preparing → ready
    → cashier pays: completed + is_paid=True   ← counts as sale
    → OR cashier cancels at any pre-completed stage ← never counts as sale

Scenarios:
    1.  Pending order excluded from all modules
    2.  Preparing order excluded from all modules
    3.  Ready order excluded from all modules
    4.  Completed+paid order included in all modules
    5.  Cancelled (from pending) excluded from all modules
    6.  Cancelled (from preparing) excluded
    7.  Cancelled (from ready) excluded
    8.  Edge case: is_paid=True, status='cancelled' (DB edit) excluded
    9.  Mixed: completed+cancelled on same day → only completed counted
    10. Multiple completed + multiple cancelled → correct totals
    11. All three modules agree for every transition state
    12. Cancellation does not double-count (order remains in DB)
    13. Historical traceability: cancelled orders still in DB
    14. process_payment() guard: cannot pay a cancelled order
    15. Cross-module totals identical for the same period
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.views import _get_cash_sales_for_date, _get_gcash_sales_for_date
from apps.orders.models import Order

User = get_user_model()

TODAY = timezone.localdate()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _cashier(username='cashier_test'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = 'cashier'
    u.save()
    return u


def _order(status='pending', is_paid=False, payment_method='cash',
           total=Decimal('100.00'), date=TODAY):
    """Create a minimal order on a given date."""
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(10, 0))
    )
    o = Order.objects.create(
        customer_name='Test',
        status=status, is_paid=is_paid,
        payment_method=payment_method,
        total=total, subtotal=total,
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


def _reports(client, start=None, end=None):
    start = start or TODAY
    end   = end   or TODAY
    return client.get(
        reverse('reports:index'),
        {'start': str(start), 'end': str(end)},
    )


def _assert_all_modules_zero(test_case, client, date=TODAY):
    """Assert Reports, Finance, and Dashboard all show 0 sales."""
    from apps.dashboard.views import _sales_stats
    resp      = _reports(client, date, date)
    cash, _   = _get_cash_sales_for_date(date)
    gcash, _  = _get_gcash_sales_for_date(date)
    stats     = _sales_stats()

    test_case.assertEqual(resp.context['total_revenue'], 0,
        "Reports: expected 0 revenue")
    test_case.assertEqual(resp.context['total_orders'],  0,
        "Reports: expected 0 orders")
    test_case.assertEqual(cash,  Decimal('0.00'),
        "Finance cash: expected 0")
    test_case.assertEqual(gcash, Decimal('0.00'),
        "Finance gcash: expected 0")
    test_case.assertEqual(stats['daily_sales'],  0,
        "Dashboard daily_sales: expected 0")
    test_case.assertEqual(stats['daily_orders'], 0,
        "Dashboard daily_orders: expected 0")


def _assert_all_modules_match(test_case, client, expected_total,
                               expected_count, date=TODAY):
    """Assert Reports, Finance, Dashboard all agree on the expected totals."""
    from apps.dashboard.views import _sales_stats
    resp     = _reports(client, date, date)
    cash, _  = _get_cash_sales_for_date(date)
    gcash, _ = _get_gcash_sales_for_date(date)
    stats    = _sales_stats()

    test_case.assertEqual(resp.context['total_revenue'], expected_total,
        f"Reports total_revenue: expected {expected_total}")
    test_case.assertEqual(resp.context['total_orders'],  expected_count,
        f"Reports total_orders: expected {expected_count}")
    test_case.assertEqual(cash + gcash, expected_total,
        f"Finance cash+gcash: expected {expected_total}")
    test_case.assertEqual(stats['daily_sales'],  expected_total,
        f"Dashboard daily_sales: expected {expected_total}")
    test_case.assertEqual(stats['daily_orders'], expected_count,
        f"Dashboard daily_orders: expected {expected_count}")


# ── 1. Pending order excluded ─────────────────────────────────────────────────

class PendingOrderTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_pending')
        self.client = Client()
        self.client.login(username='admin_pending', password='pass123')
        _order(status='pending', is_paid=False, total=Decimal('250.00'))

    def test_pending_excluded_from_all_modules(self):
        _assert_all_modules_zero(self, self.client)

    def test_pending_order_still_in_database(self):
        """Pending orders must remain queryable (historical traceability)."""
        self.assertEqual(Order.objects.filter(status='pending').count(), 1)


# ── 2. Preparing order excluded ───────────────────────────────────────────────

class PreparingOrderTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_preparing')
        self.client = Client()
        self.client.login(username='admin_preparing', password='pass123')
        _order(status='preparing', is_paid=False, total=Decimal('250.00'))

    def test_preparing_excluded_from_all_modules(self):
        _assert_all_modules_zero(self, self.client)


# ── 3. Ready order excluded ───────────────────────────────────────────────────

class ReadyOrderTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_ready')
        self.client = Client()
        self.client.login(username='admin_ready', password='pass123')
        _order(status='ready', is_paid=False, total=Decimal('250.00'))

    def test_ready_excluded_from_all_modules(self):
        _assert_all_modules_zero(self, self.client)


# ── 4. Completed+paid order included ─────────────────────────────────────────

class CompletedOrderTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_completed')
        self.client = Client()
        self.client.login(username='admin_completed', password='pass123')
        _order(status='completed', is_paid=True,
               payment_method='cash', total=Decimal('300.00'))

    def test_completed_included_in_all_modules(self):
        _assert_all_modules_match(self, self.client,
                                   expected_total=Decimal('300.00'),
                                   expected_count=1)

    def test_completed_gcash_in_finance_gcash(self):
        _order(status='completed', is_paid=True,
               payment_method='gcash', total=Decimal('200.00'))
        cash, _  = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(cash,  Decimal('300.00'))
        self.assertEqual(gcash, Decimal('200.00'))


# ── 5. Cancelled from pending — excluded ─────────────────────────────────────

class CancelledFromPendingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cancel_pending')
        self.client = Client()
        self.client.login(username='admin_cancel_pending', password='pass123')
        _order(status='cancelled', is_paid=False, total=Decimal('250.00'))

    def test_cancelled_pending_excluded_from_all_modules(self):
        _assert_all_modules_zero(self, self.client)

    def test_cancelled_order_still_in_database(self):
        """Cancelled orders must remain in the DB for historical traceability."""
        self.assertEqual(Order.objects.filter(status='cancelled').count(), 1)

    def test_cancellation_does_not_corrupt_future_queries(self):
        """After cancellation, a second completed order counts correctly."""
        _order(status='completed', is_paid=True, total=Decimal('150.00'))
        resp    = _reports(self.client)
        cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('150.00'))
        self.assertEqual(cash, Decimal('150.00'))


# ── 6. Cancelled from preparing ───────────────────────────────────────────────

class CancelledFromPreparingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cancel_prep')
        self.client = Client()
        self.client.login(username='admin_cancel_prep', password='pass123')
        _order(status='cancelled', is_paid=False, total=Decimal('350.00'))

    def test_cancelled_preparing_excluded(self):
        _assert_all_modules_zero(self, self.client)


# ── 7. Cancelled from ready ───────────────────────────────────────────────────

class CancelledFromReadyTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cancel_ready')
        self.client = Client()
        self.client.login(username='admin_cancel_ready', password='pass123')
        _order(status='cancelled', is_paid=False, total=Decimal('400.00'))

    def test_cancelled_ready_excluded(self):
        _assert_all_modules_zero(self, self.client)


# ── 8. Edge case: is_paid=True + status='cancelled' (DB edit) ────────────────

class PaidButCancelledEdgeCaseTest(TestCase):
    """
    The only way to reach is_paid=True + status='cancelled' is a direct DB
    edit or admin manipulation.  The sales filter still correctly excludes it
    because status != 'completed'.
    """

    def setUp(self):
        self.admin = _user('admin_paid_cancel')
        self.client = Client()
        self.client.login(username='admin_paid_cancel', password='pass123')
        # Simulate direct DB edit: paid but cancelled
        _order(status='cancelled', is_paid=True, total=Decimal('500.00'))

    def test_paid_cancelled_excluded_from_all_modules(self):
        _assert_all_modules_zero(self, self.client)

    def test_paid_cancelled_excluded_from_finance(self):
        cash, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash,  Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_paid_cancelled_excluded_from_reports(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], 0)
        self.assertEqual(resp.context['total_orders'],  0)


# ── 9. Mixed: completed + cancelled on same day ──────────────────────────────

class MixedCompletedAndCancelledTest(TestCase):
    """Only the completed+paid orders count; cancelled ones are silently ignored."""

    def setUp(self):
        self.admin = _user('admin_mixed')
        self.client = Client()
        self.client.login(username='admin_mixed', password='pass123')
        # 3 valid completed orders
        _order(status='completed', is_paid=True, total=Decimal('100.00'))
        _order(status='completed', is_paid=True, total=Decimal('200.00'))
        _order(status='completed', is_paid=True, total=Decimal('150.00'))
        # 2 cancelled orders — must NOT count
        _order(status='cancelled', is_paid=False, total=Decimal('999.00'))
        _order(status='cancelled', is_paid=False, total=Decimal('999.00'))

    def test_only_completed_orders_counted(self):
        _assert_all_modules_match(self, self.client,
                                   expected_total=Decimal('450.00'),
                                   expected_count=3)

    def test_cancelled_orders_do_not_inflate_total(self):
        resp    = _reports(self.client)
        cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertNotEqual(float(resp.context['total_revenue']), 999.0)
        self.assertNotEqual(float(cash), 999.0)

    def test_cancelled_orders_still_in_database(self):
        self.assertEqual(Order.objects.filter(status='cancelled').count(), 2)
        self.assertEqual(Order.objects.filter(status='completed').count(), 3)
        self.assertEqual(Order.objects.count(), 5)  # total DB rows unchanged


# ── 10. Multiple completed + multiple cancelled → correct totals ──────────────

class LargeScaleMixedTest(TestCase):
    """10 completed, 5 cancelled — totals must reflect only the completed ones."""

    def setUp(self):
        self.admin = _user('admin_large')
        self.client = Client()
        self.client.login(username='admin_large', password='pass123')
        for _ in range(10):
            _order(status='completed', is_paid=True, total=Decimal('50.00'))
        for _ in range(5):
            _order(status='cancelled', is_paid=False, total=Decimal('200.00'))

    def test_totals_reflect_completed_only(self):
        # 10 × 50 = 500; the 5 × 200 cancelled must not appear
        _assert_all_modules_match(self, self.client,
                                   expected_total=Decimal('500.00'),
                                   expected_count=10)

    def test_no_double_counting_of_completed_orders(self):
        """Each completed order counted exactly once."""
        cash, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash,  Decimal('500.00'))
        self.assertEqual(count, 10)


# ── 11. Full workflow: pending → preparing → ready → completed ────────────────

class FullWorkflowTest(TestCase):
    """
    Simulate the complete order lifecycle and assert that each intermediate
    status is excluded from sales, and only the final completed+paid state
    is included.
    """

    def setUp(self):
        self.admin = _user('admin_workflow')
        self.client = Client()
        self.client.login(username='admin_workflow', password='pass123')

    def test_workflow_step_by_step(self):
        # Step 1: Create order (pending)
        order = _order(status='pending', is_paid=False, total=Decimal('300.00'))
        _assert_all_modules_zero(self, self.client)

        # Step 2: Move to preparing
        Order.objects.filter(pk=order.pk).update(status='preparing')
        _assert_all_modules_zero(self, self.client)

        # Step 3: Move to ready
        Order.objects.filter(pk=order.pk).update(status='ready')
        _assert_all_modules_zero(self, self.client)

        # Step 4: Complete with payment (the authoritative transition)
        Order.objects.filter(pk=order.pk).update(
            status='completed', is_paid=True,
        )
        order.refresh_from_db()
        _assert_all_modules_match(self, self.client,
                                   expected_total=Decimal('300.00'),
                                   expected_count=1)

    def test_cancel_at_pending(self):
        order = _order(status='pending', is_paid=False, total=Decimal('300.00'))
        Order.objects.filter(pk=order.pk).update(status='cancelled')
        _assert_all_modules_zero(self, self.client)

    def test_cancel_at_preparing(self):
        order = _order(status='preparing', is_paid=False, total=Decimal('300.00'))
        Order.objects.filter(pk=order.pk).update(status='cancelled')
        _assert_all_modules_zero(self, self.client)

    def test_cancel_at_ready(self):
        order = _order(status='ready', is_paid=False, total=Decimal('300.00'))
        Order.objects.filter(pk=order.pk).update(status='cancelled')
        _assert_all_modules_zero(self, self.client)

    def test_completed_then_db_cancel_still_excluded(self):
        """
        Simulate a direct DB manipulation: order marked completed+paid,
        then its status is overwritten to cancelled.  The filter still
        correctly excludes it (status != 'completed').
        """
        order = _order(status='completed', is_paid=True, total=Decimal('300.00'))
        # Verify it's initially counted
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('300.00'))

        # Direct DB edit: cancel a completed order
        Order.objects.filter(pk=order.pk).update(status='cancelled')
        # Now it should be excluded even though is_paid=True
        _assert_all_modules_zero(self, self.client)


# ── 12. process_payment guard: cannot pay a cancelled order ──────────────────

class ProcessPaymentGuardTest(TestCase):
    """
    process_payment() has a terminal-state guard that prevents paying a
    cancelled (or already-completed) order.  This is the UI-level protection
    that prevents is_paid=True + status='cancelled' from being created.
    """

    def setUp(self):
        self.cashier = _cashier('cashier_guard')
        self.client  = Client()
        self.client.login(username='cashier_guard', password='pass123')

    def test_cannot_pay_cancelled_order(self):
        order = _order(status='cancelled', is_paid=False, total=Decimal('200.00'))
        url   = reverse('orders:process_payment', kwargs={'pk': order.pk})
        resp  = self.client.post(url, {
            'payment_method': 'cash',
            'amount_paid':    '200.00',
        })
        data = resp.json()
        self.assertFalse(data['success'],
            "process_payment must reject payment on a cancelled order")
        order.refresh_from_db()
        self.assertFalse(order.is_paid,
            "Cancelled order must not become paid")
        self.assertEqual(order.status, 'cancelled')

    def test_cancelled_order_not_in_sales_after_payment_attempt(self):
        """After a rejected payment attempt, sales totals must still be zero."""
        order = _order(status='cancelled', is_paid=False, total=Decimal('200.00'))
        url   = reverse('orders:process_payment', kwargs={'pk': order.pk})
        self.client.post(url, {
            'payment_method': 'cash',
            'amount_paid':    '200.00',
        })
        admin = _user('admin_guard_check')
        ac = Client()
        ac.login(username='admin_guard_check', password='pass123')
        _assert_all_modules_zero(self, ac)


# ── 13. Cross-module consistency — all statuses ───────────────────────────────

class CrossModuleStatusConsistencyTest(TestCase):
    """
    All five status values are present on the same day.
    Only the completed+paid order must appear in any module's totals.
    """

    def setUp(self):
        self.admin = _user('admin_cross')
        self.client = Client()
        self.client.login(username='admin_cross', password='pass123')
        _order(status='pending',   is_paid=False, total=Decimal('999.00'))
        _order(status='preparing', is_paid=False, total=Decimal('999.00'))
        _order(status='ready',     is_paid=False, total=Decimal('999.00'))
        _order(status='cancelled', is_paid=False, total=Decimal('999.00'))
        _order(status='completed', is_paid=True,  total=Decimal('250.00'))

    def test_only_completed_counts_in_all_modules(self):
        _assert_all_modules_match(self, self.client,
                                   expected_total=Decimal('250.00'),
                                   expected_count=1)

    def test_reports_total_not_inflated(self):
        resp = _reports(self.client)
        # If any non-completed status leaked through, total would exceed 250
        self.assertEqual(resp.context['total_revenue'], Decimal('250.00'))

    def test_finance_not_inflated(self):
        cash, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash,  Decimal('250.00'))
        self.assertEqual(count, 1)

    def test_dashboard_not_inflated(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'],  Decimal('250.00'))
        self.assertEqual(stats['daily_orders'], 1)

    def test_all_five_orders_still_in_database(self):
        """All orders must remain for historical traceability."""
        self.assertEqual(Order.objects.count(), 5)
        for status in ['pending', 'preparing', 'ready', 'cancelled', 'completed']:
            self.assertTrue(
                Order.objects.filter(status=status).exists(),
                f"Order with status='{status}' missing from database",
            )
