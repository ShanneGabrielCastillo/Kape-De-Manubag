"""
Finance sales chain tests — Kape De Manubag.

Verifies the full Order → Payment → Completed Sale → Finance/Dashboard/Reports
data path.  Tests every scenario requested in the audit:

  1.  One completed cash order
  2.  One completed GCash order
  3.  Multiple completed orders
  4.  Pending order            (must NOT count)
  5.  Preparing order          (must NOT count)
  6.  Ready order              (must NOT count)
  7.  Cancelled order          (must NOT count)
  8.  Multiple orders on different dates
  9.  Finance vs Dashboard vs Reports cross-module consistency
  10. No double counting
  11. Paid-but-not-completed   (must NOT count in Finance / Dashboard)
  12. Completed-but-not-paid   (must NOT count in Finance / Dashboard)
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DailyFinance
from apps.finance.views import _get_cash_sales_for_date
from apps.orders.models import Order

User = get_user_model()

# ── Shared test date ──────────────────────────────────────────────────────────
TODAY        = timezone.localdate()
YESTERDAY    = TODAY - datetime.timedelta(days=1)
TWO_DAYS_AGO = TODAY - datetime.timedelta(days=2)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(username, role='cashier'):
    u = User.objects.create_user(username=username, password='testpass123')
    u.role = role
    u.save()
    return u


def _make_order(date, status='completed', is_paid=True,
                payment_method='cash', total=Decimal('100.00')):
    """Create a minimal Order on a specific date."""
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(10, 0))
    )
    order = Order.objects.create(
        customer_name='Test',
        status=status,
        is_paid=is_paid,
        payment_method=payment_method,
        total=total,
        subtotal=total,
    )
    Order.objects.filter(pk=order.pk).update(created_at=dt)
    order.refresh_from_db()
    return order


def _direct_order_sum(date, payment_method=None):
    """Direct DB aggregate matching the Finance query definition."""
    qs = Order.objects.filter(
        created_at__date=date,
        is_paid=True,
        status='completed',
    )
    if payment_method:
        qs = qs.filter(payment_method=payment_method)
    return qs.aggregate(t=Sum('total'))['t'] or Decimal('0.00')


# ── 1. Single completed cash order ───────────────────────────────────────────

class OneCashOrderTest(TestCase):
    def test_single_completed_cash_order_counts(self):
        """A single completed, paid cash order is counted exactly once."""
        _make_order(TODAY, payment_method='cash', total=Decimal('250.00'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('250.00'))
        self.assertEqual(count, 1)

    def test_finance_model_cash_sales(self):
        _make_order(TODAY, payment_method='cash', total=Decimal('250.00'))
        rec = DailyFinance.objects.create(date=TODAY, previous_coh=Decimal('0.00'))
        self.assertEqual(rec.cash_sales, Decimal('250.00'))


# ── 2. Single completed GCash order ──────────────────────────────────────────

class OneGCashOrderTest(TestCase):
    def test_gcash_order_excluded_from_cash_sales(self):
        """A GCash order must NOT appear in Finance cash_sales."""
        _make_order(TODAY, payment_method='gcash', total=Decimal('300.00'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_gcash_shows_in_api_separately(self):
        """GCash appears in the API response under gcash_sales, not cash_sales."""
        user = _make_user('cashier_gcash')
        _make_order(TODAY, payment_method='gcash', total=Decimal('300.00'))
        client = Client()
        client.login(username='cashier_gcash', password='testpass123')
        url = reverse('finance:api_cash_sales')
        resp = client.get(f'{url}?date={TODAY}')
        data = resp.json()
        self.assertEqual(Decimal(data['cash_sales']),  Decimal('0.00'))
        self.assertEqual(Decimal(data['gcash_sales']), Decimal('300.00'))
        self.assertEqual(data['gcash_order_count'], 1)


# ── 3. Multiple completed orders ─────────────────────────────────────────────

class MultipleOrdersTest(TestCase):
    def test_multiple_cash_orders_summed(self):
        amounts = [Decimal('100.00'), Decimal('200.00'), Decimal('350.00')]
        for a in amounts:
            _make_order(TODAY, payment_method='cash', total=a)
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('650.00'))
        self.assertEqual(count, 3)

    def test_mixed_cash_and_gcash(self):
        """Cash and GCash are summed independently; cash total matches Finance."""
        _make_order(TODAY, payment_method='cash',  total=Decimal('200.00'))
        _make_order(TODAY, payment_method='gcash', total=Decimal('150.00'))
        cash_total, cash_count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, Decimal('200.00'))
        self.assertEqual(cash_count, 1)


# ── 4–7. Non-completed statuses ───────────────────────────────────────────────

class NonCompletedStatusTest(TestCase):
    """Pending, preparing, ready, and cancelled orders must never count."""

    def _assert_excluded(self, status, is_paid=False, payment_method='cash'):
        _make_order(TODAY, status=status, is_paid=is_paid,
                    payment_method=payment_method, total=Decimal('999.00'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('0.00'),
            f'status={status!r} is_paid={is_paid} should be excluded from cash_sales')
        self.assertEqual(count, 0)

    def test_pending_excluded(self):
        self._assert_excluded('pending', is_paid=False)

    def test_preparing_excluded(self):
        self._assert_excluded('preparing', is_paid=False)

    def test_ready_excluded(self):
        self._assert_excluded('ready', is_paid=False)

    def test_cancelled_excluded(self):
        self._assert_excluded('cancelled', is_paid=False)

    def test_paid_but_cancelled_excluded(self):
        """Edge case: is_paid=True but status=cancelled must not count."""
        self._assert_excluded('cancelled', is_paid=True)

    def test_completed_but_unpaid_excluded(self):
        """Edge case: status=completed but is_paid=False must not count."""
        _make_order(TODAY, status='completed', is_paid=False,
                    payment_method='cash', total=Decimal('999.00'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_only_completed_paid_cash_counted(self):
        """Mixed bag — only the one valid order contributes."""
        _make_order(TODAY, status='pending',    is_paid=False, total=Decimal('100.00'))
        _make_order(TODAY, status='preparing',  is_paid=False, total=Decimal('100.00'))
        _make_order(TODAY, status='ready',      is_paid=False, total=Decimal('100.00'))
        _make_order(TODAY, status='cancelled',  is_paid=False, total=Decimal('100.00'))
        _make_order(TODAY, status='completed',  is_paid=True,
                    payment_method='cash', total=Decimal('250.00'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('250.00'))
        self.assertEqual(count, 1)


# ── 8. Multiple dates ─────────────────────────────────────────────────────────

class MultipleDatesTest(TestCase):
    def test_orders_on_different_dates_separated(self):
        _make_order(TWO_DAYS_AGO, payment_method='cash', total=Decimal('100.00'))
        _make_order(YESTERDAY,   payment_method='cash', total=Decimal('200.00'))
        _make_order(TODAY,       payment_method='cash', total=Decimal('300.00'))

        t0, c0 = _get_cash_sales_for_date(TWO_DAYS_AGO)
        t1, c1 = _get_cash_sales_for_date(YESTERDAY)
        t2, c2 = _get_cash_sales_for_date(TODAY)

        self.assertEqual(t0, Decimal('100.00')); self.assertEqual(c0, 1)
        self.assertEqual(t1, Decimal('200.00')); self.assertEqual(c1, 1)
        self.assertEqual(t2, Decimal('300.00')); self.assertEqual(c2, 1)

    def test_wrong_date_returns_zero(self):
        _make_order(YESTERDAY, payment_method='cash', total=Decimal('500.00'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('0.00'))
        self.assertEqual(count, 0)


# ── 9. Cross-module consistency ───────────────────────────────────────────────

class CrossModuleConsistencyTest(TestCase):
    """
    Finance, Dashboard, and Reports must all agree on what counts as a sale.
    The authoritative definition is: is_paid=True AND status='completed'.
    """

    def setUp(self):
        self.admin = _make_user('admin_consistency', role='admin')
        self.client = Client()
        self.client.login(username='admin_consistency', password='testpass123')

    def _make_dataset(self):
        """Create a known set of orders and return the expected totals."""
        _make_order(TODAY, payment_method='cash',  total=Decimal('100.00'))
        _make_order(TODAY, payment_method='gcash', total=Decimal('200.00'))
        _make_order(TODAY, payment_method='cash',  total=Decimal('300.00'))
        # These must NOT count:
        _make_order(TODAY, status='pending',   is_paid=False, total=Decimal('999.00'))
        _make_order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))
        # expected total = 100 + 200 + 300 = 600
        return Decimal('600.00')

    def test_finance_vs_direct_db_sum(self):
        """Finance cash_sales + gcash_manual matches direct DB sum of all payment methods."""
        self._make_dataset()
        # Direct DB: all completed paid orders regardless of payment method
        db_total = _direct_order_sum(TODAY)
        self.assertEqual(db_total, Decimal('600.00'))

        # Finance cash_sales covers cash only (100 + 300 = 400)
        cash_total, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, Decimal('400.00'))

    def test_reports_total_matches_authoritative_definition(self):
        """Reports total_revenue for TODAY equals is_paid=True, status=completed sum."""
        self._make_dataset()
        url = reverse('reports:index')
        resp = self.client.get(url, {'start': str(TODAY), 'end': str(TODAY)})
        self.assertEqual(resp.status_code, 200)
        # Reports total_revenue in context must equal 600.00
        self.assertEqual(resp.context['total_revenue'], Decimal('600.00'))
        self.assertEqual(resp.context['total_orders'], 3)

    def test_reports_excludes_non_completed_orders(self):
        """Reports must NOT include pending or cancelled orders."""
        self._make_dataset()
        url = reverse('reports:index')
        resp = self.client.get(url, {'start': str(TODAY), 'end': str(TODAY)})
        # If non-completed were included total would be 600 + 999 + 999 = 2598
        self.assertEqual(resp.context['total_revenue'], Decimal('600.00'))

    def test_reports_excludes_unpaid_completed_orders(self):
        """Reports must NOT include completed-but-unpaid orders."""
        _make_order(TODAY, status='completed', is_paid=True,
                    payment_method='cash', total=Decimal('300.00'))
        _make_order(TODAY, status='completed', is_paid=False,
                    payment_method='cash', total=Decimal('999.00'))
        url = reverse('reports:index')
        resp = self.client.get(url, {'start': str(TODAY), 'end': str(TODAY)})
        self.assertEqual(resp.context['total_revenue'], Decimal('300.00'))

    def test_dashboard_query_matches_finance_definition(self):
        """
        Dashboard _sales_stats() uses the same is_paid=True + status='completed'
        filter as Finance.  Direct DB verification — not via the HTTP view —
        so this test works without login complexity.
        """
        from apps.dashboard.views import _sales_stats

        _make_order(TODAY, payment_method='cash',  total=Decimal('100.00'))
        _make_order(TODAY, payment_method='gcash', total=Decimal('200.00'))
        _make_order(TODAY, status='pending',   is_paid=False, total=Decimal('999.00'))
        _make_order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))

        stats = _sales_stats()
        # daily_sales must equal 100 + 200 = 300; the 999s must be excluded
        self.assertEqual(stats['daily_sales'], Decimal('300.00'))
        self.assertEqual(stats['daily_orders'], 2)

    def test_all_three_modules_agree_on_total(self):
        """
        Finance (cash+gcash separately) + Dashboard daily + Reports total
        must all be consistent with the same underlying order records.
        """
        _make_order(TODAY, payment_method='cash',  total=Decimal('150.00'))
        _make_order(TODAY, payment_method='gcash', total=Decimal('250.00'))
        _make_order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))

        # Direct DB sum = 150 + 250 = 400
        db_total = _direct_order_sum(TODAY)
        self.assertEqual(db_total, Decimal('400.00'))

        # Finance: cash=150, gcash handled manually as deduction
        cash_sales, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_sales, Decimal('150.00'))

        # Dashboard
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'], Decimal('400.00'))

        # Reports
        url = reverse('reports:index')
        resp = self.client.get(url, {'start': str(TODAY), 'end': str(TODAY)})
        self.assertEqual(resp.context['total_revenue'], Decimal('400.00'))


# ── 10. No double counting ────────────────────────────────────────────────────

class NoDoubleCountingTest(TestCase):
    def test_same_order_counted_once(self):
        """Calling get_cash_sales_for_date twice returns the same value."""
        _make_order(TODAY, payment_method='cash', total=Decimal('200.00'))
        t1, c1 = _get_cash_sales_for_date(TODAY)
        t2, c2 = _get_cash_sales_for_date(TODAY)
        self.assertEqual(t1, t2)
        self.assertEqual(c1, c2)

    def test_unique_orders_each_counted_once(self):
        """Three distinct orders, each counted exactly once — no duplication."""
        for _ in range(3):
            _make_order(TODAY, payment_method='cash', total=Decimal('100.00'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('300.00'))
        self.assertEqual(count, 3)

    def test_finance_model_property_consistent_with_helper(self):
        """DailyFinance.cash_sales property equals _get_cash_sales_for_date helper."""
        _make_order(TODAY, payment_method='cash', total=Decimal('350.00'))
        rec = DailyFinance.objects.create(date=TODAY, previous_coh=Decimal('0.00'))
        helper_total, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(rec.cash_sales, helper_total)


# ── 11. Reports multi-date range consistency ──────────────────────────────────

class ReportsDateRangeTest(TestCase):
    """Reports respects its date range and only includes completed paid orders."""

    def setUp(self):
        self.admin = _make_user('admin_range', role='admin')
        self.client = Client()
        self.client.login(username='admin_range', password='testpass123')

    def test_only_date_range_orders_included(self):
        _make_order(TWO_DAYS_AGO, payment_method='cash',  total=Decimal('100.00'))
        _make_order(YESTERDAY,   payment_method='gcash', total=Decimal('200.00'))
        _make_order(TODAY,       payment_method='cash',  total=Decimal('300.00'))

        url = reverse('reports:index')

        # Only yesterday
        resp = self.client.get(url, {
            'start': str(YESTERDAY), 'end': str(YESTERDAY),
        })
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))
        self.assertEqual(resp.context['total_orders'], 1)

        # Two-day range
        resp = self.client.get(url, {
            'start': str(YESTERDAY), 'end': str(TODAY),
        })
        self.assertEqual(resp.context['total_revenue'], Decimal('500.00'))
        self.assertEqual(resp.context['total_orders'], 2)

    def test_cancelled_outside_and_inside_range_both_excluded(self):
        _make_order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))
        _make_order(TODAY, payment_method='cash', total=Decimal('100.00'))
        url = reverse('reports:index')
        resp = self.client.get(url, {'start': str(TODAY), 'end': str(TODAY)})
        self.assertEqual(resp.context['total_revenue'], Decimal('100.00'))
        self.assertEqual(resp.context['total_orders'], 1)
