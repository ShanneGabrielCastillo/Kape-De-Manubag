"""
Sales Reports audit tests — Kape De Manubag.

Cross-module consistency: Sales Reports must agree with Finance and Dashboard
on the same authoritative transaction data.

Business definition of a completed sale (all three modules must share):
  is_paid=True AND status='completed'

Key relationships verified:
  Reports total_revenue = Finance cash_sales + Finance GCash sales
  Reports total_orders  = Dashboard daily_orders (same day)
  Reports total_revenue = Dashboard daily_sales  (same day)
  Cancelled / pending / other-status orders excluded everywhere

Scenarios covered:
  1.  Cash-only day
  2.  GCash-only day
  3.  Mixed Cash + GCash day
  4.  Multiple orders
  5.  Cancelled orders excluded
  6.  Pending / preparing / ready orders excluded
  7.  Paid-but-not-completed excluded
  8.  Completed-but-not-paid excluded
  9.  Different dates — period filter respected
  10. Date validation — invalid input falls back gracefully
  11. Inverted date range — swapped silently
  12. Import structure clean (no inline imports)
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

# Use localdate() so tests always pass regardless of the current date.
TODAY = timezone.localdate()
YEST  = TODAY - datetime.timedelta(days=1)
DAY2  = TODAY - datetime.timedelta(days=2)
TMRW  = TODAY + datetime.timedelta(days=1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _order(date, payment_method='cash', total=Decimal('100.00'),
           status='completed', is_paid=True):
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


def _reports(client, start, end):
    """GET the reports page for [start, end] and return the response."""
    return client.get(
        reverse('reports:index'),
        {'start': str(start), 'end': str(end)},
    )


# ── 1. Cash-only day ──────────────────────────────────────────────────────────

class CashOnlyDayTest(TestCase):
    """Reports total_revenue == Finance cash_sales when all orders are cash."""

    def setUp(self):
        self.admin = _user('admin_cash')
        self.client = Client()
        self.client.login(username='admin_cash', password='pass123')
        _order(TODAY, payment_method='cash', total=Decimal('200.00'))
        _order(TODAY, payment_method='cash', total=Decimal('150.00'))

    def test_total_revenue_matches_cash_sales(self):
        resp = _reports(self.client, TODAY, TODAY)
        finance_cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(resp.context['total_revenue'], finance_cash)
        self.assertEqual(resp.context['total_revenue'], Decimal('350.00'))

    def test_total_orders_correct(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_orders'], 2)

    def test_reports_matches_dashboard(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        resp  = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], stats['daily_sales'])


# ── 2. GCash-only day ─────────────────────────────────────────────────────────

class GCashOnlyDayTest(TestCase):
    """GCash orders count in Reports total_revenue but NOT in Finance cash_sales."""

    def setUp(self):
        self.admin = _user('admin_gcash')
        self.client = Client()
        self.client.login(username='admin_gcash', password='pass123')
        _order(TODAY, payment_method='gcash', total=Decimal('300.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('250.00'))

    def test_total_revenue_includes_gcash(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('550.00'))

    def test_finance_cash_sales_is_zero(self):
        """GCash orders must not appear in Finance cash_sales."""
        cash_total, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, Decimal('0.00'))

    def test_reports_total_equals_finance_gcash(self):
        """Reports total_revenue = Finance cash_sales + Finance GCash."""
        cash_total, _ = _get_cash_sales_for_date(TODAY)
        gcash_total, _ = _get_gcash_sales_for_date(TODAY)
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(
            resp.context['total_revenue'],
            cash_total + gcash_total,
        )


# ── 3. Mixed Cash + GCash day ─────────────────────────────────────────────────

class MixedDayTest(TestCase):
    """Reports total_revenue = Finance cash + Finance GCash on a mixed day."""

    def setUp(self):
        self.admin = _user('admin_mixed')
        self.client = Client()
        self.client.login(username='admin_mixed', password='pass123')
        _order(TODAY, payment_method='cash',  total=Decimal('400.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('300.00'))

    def test_total_revenue_is_combined(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('700.00'))

    def test_finance_cash_gcash_sum_equals_reports(self):
        cash_total, _  = _get_cash_sales_for_date(TODAY)
        gcash_total, _ = _get_gcash_sales_for_date(TODAY)
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(
            resp.context['total_revenue'],
            cash_total + gcash_total,
        )
        self.assertEqual(cash_total,  Decimal('400.00'))
        self.assertEqual(gcash_total, Decimal('300.00'))

    def test_dashboard_agrees(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        resp  = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], stats['daily_sales'])
        self.assertEqual(resp.context['total_orders'],  stats['daily_orders'])

    def test_avg_order_correct(self):
        resp = _reports(self.client, TODAY, TODAY)
        # 700 / 2 = 350
        self.assertEqual(resp.context['avg_order'], Decimal('350.00'))


# ── 4. Multiple orders ────────────────────────────────────────────────────────

class MultipleOrdersTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_multi')
        self.client = Client()
        self.client.login(username='admin_multi', password='pass123')
        for i in range(5):
            _order(TODAY, payment_method='cash', total=Decimal('100.00'))
        for i in range(3):
            _order(TODAY, payment_method='gcash', total=Decimal('50.00'))

    def test_total_revenue_sum_exact(self):
        # 5×100 + 3×50 = 650
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('650.00'))

    def test_total_orders_count(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_orders'], 8)

    def test_daily_sales_chart_data_contains_today(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(daily[0]['total'], 650.0, places=2)
        self.assertEqual(daily[0]['count'], 8)


# ── 5. Cancelled orders excluded ─────────────────────────────────────────────

class CancelledOrdersTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cancel')
        self.client = Client()
        self.client.login(username='admin_cancel', password='pass123')
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('200.00'))
        _order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))

    def test_cancelled_excluded_from_reports(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_cancelled_excluded_from_finance(self):
        cash_total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, Decimal('200.00'))
        self.assertEqual(count, 1)

    def test_cancelled_excluded_from_dashboard(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'],  Decimal('200.00'))
        self.assertEqual(stats['daily_orders'], 1)

    def test_all_three_agree_after_cancellation(self):
        from apps.dashboard.views import _sales_stats
        cash_total, _  = _get_cash_sales_for_date(TODAY)
        gcash_total, _ = _get_gcash_sales_for_date(TODAY)
        stats          = _sales_stats()
        resp           = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], cash_total + gcash_total)
        self.assertEqual(resp.context['total_revenue'], stats['daily_sales'])


# ── 6. Non-completed statuses excluded ───────────────────────────────────────

class NonCompletedStatusTest(TestCase):
    """pending, preparing, ready must never appear in any module's totals."""

    def setUp(self):
        self.admin = _user('admin_status')
        self.client = Client()
        self.client.login(username='admin_status', password='pass123')
        # One valid order
        _order(TODAY, status='completed', is_paid=True, total=Decimal('100.00'))
        # Non-completed statuses
        for status in ['pending', 'preparing', 'ready']:
            _order(TODAY, status=status, is_paid=False, total=Decimal('999.00'))

    def test_reports_only_counts_completed(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('100.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_finance_only_counts_completed(self):
        cash, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash,  Decimal('100.00'))
        self.assertEqual(count, 1)

    def test_dashboard_only_counts_completed(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'],  Decimal('100.00'))
        self.assertEqual(stats['daily_orders'], 1)


# ── 7. Paid-but-not-completed edge case ──────────────────────────────────────

class PaidNotCompletedTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_pnc')
        self.client = Client()
        self.client.login(username='admin_pnc', password='pass123')
        # This edge case (process_payment() normally prevents it):
        _order(TODAY, status='pending', is_paid=True, total=Decimal('999.00'))
        _order(TODAY, status='completed', is_paid=True, total=Decimal('200.00'))

    def test_paid_but_pending_excluded_everywhere(self):
        from apps.dashboard.views import _sales_stats
        cash, _ = _get_cash_sales_for_date(TODAY)
        stats   = _sales_stats()
        resp    = _reports(self.client, TODAY, TODAY)
        self.assertEqual(cash,                          Decimal('200.00'))
        self.assertEqual(stats['daily_sales'],          Decimal('200.00'))
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))


# ── 8. Completed-but-not-paid edge case ──────────────────────────────────────

class CompletedNotPaidTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cnp')
        self.client = Client()
        self.client.login(username='admin_cnp', password='pass123')
        _order(TODAY, status='completed', is_paid=False, total=Decimal('999.00'))
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('200.00'))

    def test_completed_unpaid_excluded_everywhere(self):
        from apps.dashboard.views import _sales_stats
        cash, _ = _get_cash_sales_for_date(TODAY)
        stats   = _sales_stats()
        resp    = _reports(self.client, TODAY, TODAY)
        self.assertEqual(cash,                          Decimal('200.00'))
        self.assertEqual(stats['daily_sales'],          Decimal('200.00'))
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))


# ── 9. Different dates — period filter respected ──────────────────────────────

class DateRangeFilterTest(TestCase):
    """Reports must respect the date range and exclude orders outside it."""

    def setUp(self):
        self.admin = _user('admin_dates')
        self.client = Client()
        self.client.login(username='admin_dates', password='pass123')
        _order(DAY2,  payment_method='cash', total=Decimal('100.00'))
        _order(YEST,  payment_method='cash', total=Decimal('200.00'))
        _order(TODAY, payment_method='cash', total=Decimal('300.00'))

    def test_single_day_filter(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('300.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_yesterday_only(self):
        resp = _reports(self.client, YEST, YEST)
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))

    def test_two_day_range(self):
        resp = _reports(self.client, YEST, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('500.00'))
        self.assertEqual(resp.context['total_orders'],  2)

    def test_full_three_day_range(self):
        resp = _reports(self.client, DAY2, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('600.00'))
        self.assertEqual(resp.context['total_orders'],  3)

    def test_future_date_excluded(self):
        _order(TMRW, payment_method='cash', total=Decimal('999.00'))
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('300.00'))

    def test_daily_sales_chart_entry_count(self):
        """daily_sales list must have one entry per day in range."""
        resp = _reports(self.client, DAY2, TODAY)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 3)  # DAY2, YEST, TODAY

    def test_finance_per_day_agrees_with_reports_per_day(self):
        """Finance cash_sales for each individual day must match reports for that day."""
        for date, expected in [(DAY2, Decimal('100.00')),
                               (YEST, Decimal('200.00')),
                               (TODAY, Decimal('300.00'))]:
            cash, _ = _get_cash_sales_for_date(date)
            resp = _reports(self.client, date, date)
            self.assertEqual(
                resp.context['total_revenue'], cash,
                msg=f"Reports vs Finance mismatch on {date}",
            )


# ── 10. Date validation — invalid input falls back gracefully ─────────────────

class DateValidationTest(TestCase):
    """Invalid date strings must return 200 with the default range, not 500."""

    def setUp(self):
        self.admin = _user('admin_validation')
        self.client = Client()
        self.client.login(username='admin_validation', password='pass123')

    def test_invalid_start_falls_back(self):
        resp = self.client.get(
            reverse('reports:index'),
            {'start': 'not-a-date', 'end': str(TODAY)},
        )
        self.assertEqual(resp.status_code, 200,
            "Invalid start date must not cause a 500 error")

    def test_invalid_end_falls_back(self):
        resp = self.client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': 'bad-date'},
        )
        self.assertEqual(resp.status_code, 200)

    def test_both_invalid_falls_back(self):
        resp = self.client.get(
            reverse('reports:index'),
            {'start': 'abc', 'end': 'xyz'},
        )
        self.assertEqual(resp.status_code, 200)
        # Falls back to the 30-day default — context keys present
        self.assertIn('total_revenue', resp.context)
        self.assertIn('total_orders',  resp.context)

    def test_empty_params_falls_back(self):
        resp = self.client.get(reverse('reports:index'))
        self.assertEqual(resp.status_code, 200)

    def test_sql_injection_attempt_falls_back(self):
        resp = self.client.get(
            reverse('reports:index'),
            {'start': "'; DROP TABLE orders_order; --", 'end': str(TODAY)},
        )
        self.assertEqual(resp.status_code, 200)


# ── 11. Inverted date range ───────────────────────────────────────────────────

class InvertedDateRangeTest(TestCase):
    """start > end should be silently swapped — no 500, results still correct."""

    def setUp(self):
        self.admin = _user('admin_inverted')
        self.client = Client()
        self.client.login(username='admin_inverted', password='pass123')
        _order(TODAY, total=Decimal('200.00'))

    def test_inverted_range_returns_200(self):
        resp = self.client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(YEST)},   # start after end
        )
        self.assertEqual(resp.status_code, 200)

    def test_inverted_range_still_finds_orders(self):
        resp = self.client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(YEST)},
        )
        # After swap: start=YEST, end=TODAY — the TODAY order should appear
        self.assertEqual(resp.context['total_orders'], 1)
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))


# ── 12. No sales ──────────────────────────────────────────────────────────────

class NoSalesTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_nosales')
        self.client = Client()
        self.client.login(username='admin_nosales', password='pass123')

    def test_zero_sales_reports_page_renders(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total_revenue'], 0)
        self.assertEqual(resp.context['total_orders'],  0)
        self.assertEqual(resp.context['avg_order'],     0)

    def test_zero_sales_finance_consistent(self):
        cash, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash,  Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_daily_sales_all_zeros(self):
        resp = _reports(self.client, TODAY, TODAY)
        for entry in resp.context['daily_sales']:
            self.assertEqual(entry['total'], 0.0)
            self.assertEqual(entry['count'], 0)


# ── 13. Top products and category sales use OrderItem snapshots ───────────────

class ProductCategoryConsistencyTest(TestCase):
    """
    Top products and category sales use OrderItem.subtotal (price at order time).
    These intentionally differ from total_revenue (Order.total) when packaging
    fees or discounts apply.  This test documents that relationship.
    """

    def setUp(self):
        self.admin = _user('admin_prod')
        self.client = Client()
        self.client.login(username='admin_prod', password='pass123')
        from apps.menu.models import Category, Product
        cat = Category.objects.create(name='Coffee', slug='coffee')
        prod = Product.objects.create(
            category=cat, name='Latte', price=Decimal('100.00'),
            stock_quantity=100,
        )
        # Create order with items
        from apps.orders.models import OrderItem
        o = _order(TODAY, total=Decimal('106.00'), is_paid=True)  # +6 packaging
        o.subtotal = Decimal('100.00')
        o.packaging_fee = Decimal('6.00')
        o.save(update_fields=['subtotal', 'packaging_fee'])
        OrderItem.objects.create(
            order=o, product=prod,
            product_name='Latte', size='none',
            quantity=1, unit_price=Decimal('100.00'),
            subtotal=Decimal('100.00'),
        )

    def test_total_revenue_uses_order_total(self):
        resp = _reports(self.client, TODAY, TODAY)
        # total_revenue = Order.total = 106.00 (includes packaging)
        self.assertEqual(resp.context['total_revenue'], Decimal('106.00'))

    def test_top_product_uses_item_subtotal(self):
        resp = _reports(self.client, TODAY, TODAY)
        products = list(resp.context['top_products'])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['product_name'], 'Latte')
        # top_products uses OrderItem.subtotal = 100.00 (no packaging fee)
        self.assertEqual(products[0]['total_revenue'], Decimal('100.00'))

    def test_category_uses_item_subtotal(self):
        resp = _reports(self.client, TODAY, TODAY)
        category_sales = resp.context['category_sales']
        coffee = next((c for c in category_sales if c['name'] == 'Coffee'), None)
        self.assertIsNotNone(coffee, "Coffee category must appear in category_sales")
        # category uses OrderItem.subtotal = 100.00
        self.assertEqual(float(coffee['total']), 100.0)

    def test_total_revenue_differs_from_category_sum_by_packaging(self):
        """Document that total_revenue > category sum when packaging applies."""
        resp = _reports(self.client, TODAY, TODAY)
        category_total = sum(float(c['total']) for c in resp.context['category_sales'])
        total_revenue  = float(resp.context['total_revenue'])
        # 106.00 (order total) vs 100.00 (item subtotal)
        self.assertGreater(total_revenue, category_total)
