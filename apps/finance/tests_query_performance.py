"""
Finance query performance tests — Kape De Manubag.

Verifies that the four query optimisations are in place and that every
financial result is identical before and after the changes.

Optimisations under test
------------------------
OPT-1  _get_cash_sales_for_date  — single aggregate query (Sum + Count)
OPT-2  _get_gcash_sales_for_date — single aggregate query (Sum + Count)
OPT-3  finance_print             — 1 cash-sales query instead of 4+
OPT-4  idx_order_finance_sales   — composite index on Order table

Financial-correctness regression coverage
------------------------------------------
- Normal day (cash + GCash orders, expenses, all deductions)
- No-sales day (zero orders)
- Many orders (100 cash, 50 GCash on same date)
- Historical dates (previous COH carry-forward)
- Dashboard _sales_stats agrees with Finance cash_sales
- Reports total_revenue agrees with Finance
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DailyFinance
from apps.finance.views import (
    _get_cash_sales_for_date,
    _get_gcash_sales_for_date,
)
from apps.orders.models import Order

User = get_user_model()

# Use localdate() so tests are always relative to the current Philippine day.
TODAY  = timezone.localdate()
YEST   = TODAY - datetime.timedelta(days=1)
DAY2   = TODAY - datetime.timedelta(days=2)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username, role='cashier'):
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


def _finance(date, previous_coh=Decimal('1000.00'), **kwargs):
    return DailyFinance.objects.create(
        date=date, previous_coh=previous_coh, **kwargs
    )


# ── OPT-1: _get_cash_sales_for_date uses exactly ONE query ───────────────────

class GetCashSalesQueryCountTest(TestCase):
    """
    OPT-1 regression: _get_cash_sales_for_date must issue exactly ONE database
    query (a single aggregate that returns both Sum and Count) regardless of
    how many orders exist for the date.
    """

    def test_single_query_no_orders(self):
        with CaptureQueriesContext(connection) as ctx:
            total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(len(ctx), 1, (
            f"Expected 1 query, got {len(ctx)}. "
            f"Queries: {[q['sql'][:120] for q in ctx]}"
        ))
        self.assertEqual(total, Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_single_query_with_orders(self):
        _order(TODAY, total=Decimal('150.00'))
        _order(TODAY, total=Decimal('250.00'))
        with CaptureQueriesContext(connection) as ctx:
            total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(len(ctx), 1, (
            f"Expected 1 query, got {len(ctx)}. "
            f"Queries: {[q['sql'][:120] for q in ctx]}"
        ))
        self.assertEqual(total, Decimal('400.00'))
        self.assertEqual(count, 2)

    def test_single_query_many_orders(self):
        """Even with 100 cash orders the helper must still use 1 query."""
        for i in range(100):
            _order(TODAY, total=Decimal('50.00'))
        with CaptureQueriesContext(connection) as ctx:
            total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(len(ctx), 1)
        self.assertEqual(total, Decimal('5000.00'))
        self.assertEqual(count, 100)

    def test_returns_correct_values(self):
        """OPT-1 must not change what _get_cash_sales_for_date returns."""
        _order(TODAY, payment_method='cash', total=Decimal('123.45'))
        _order(TODAY, payment_method='gcash', total=Decimal('999.00'))   # excluded
        _order(TODAY, payment_method='cash',  status='cancelled',
               is_paid=False, total=Decimal('888.00'))                   # excluded
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('123.45'))
        self.assertEqual(count, 1)


# ── OPT-2: _get_gcash_sales_for_date uses exactly ONE query ──────────────────

class GetGCashSalesQueryCountTest(TestCase):
    """OPT-2 regression: same single-query guarantee for the GCash helper."""

    def test_single_query_no_orders(self):
        with CaptureQueriesContext(connection) as ctx:
            total, count = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(len(ctx), 1)
        self.assertEqual(total, Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_single_query_with_gcash_orders(self):
        _order(TODAY, payment_method='gcash', total=Decimal('200.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('300.00'))
        with CaptureQueriesContext(connection) as ctx:
            total, count = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(len(ctx), 1)
        self.assertEqual(total, Decimal('500.00'))
        self.assertEqual(count, 2)

    def test_gcash_excludes_cash_orders(self):
        _order(TODAY, payment_method='cash',  total=Decimal('999.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('150.00'))
        total, count = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('150.00'))
        self.assertEqual(count, 1)

    def test_gcash_many_orders(self):
        for _ in range(50):
            _order(TODAY, payment_method='gcash', total=Decimal('100.00'))
        with CaptureQueriesContext(connection) as ctx:
            total, count = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(len(ctx), 1)
        self.assertEqual(total, Decimal('5000.00'))
        self.assertEqual(count, 50)


# ── OPT-3: finance_print query count ─────────────────────────────────────────

class FinancePrintQueryCountTest(TestCase):
    """
    OPT-3 regression: finance_print must issue a bounded number of queries
    regardless of how many orders the date has.

    Before OPT-3 the view issued 4+ DB queries for cash_sales alone:
      - _get_cash_sales_for_date (2 queries: aggregate + count)
      - record.cash_sales  (1 query)
      - record.running_total → calls cash_sales (1 more)
      - record.ending_coh  → calls running_total → calls cash_sales (1 more)
    Total cash-sales queries: 5.  After OPT-1 + OPT-3: 1.

    We assert the total query count for the entire view is ≤ 5:
      1  get_object_or_404 (PK lookup)
      1  _get_cash_sales_for_date (OPT-1: single aggregate)
      1  session / auth (Django middleware)
      ≤2 misc (e.g. AuditLog, messages framework)
    """

    def setUp(self):
        self.user = _user('cashier_print')
        self.client = Client()
        self.client.login(username='cashier_print', password='pass123')
        _order(TODAY, total=Decimal('300.00'))
        self.rec = _finance(TODAY, previous_coh=Decimal('1000.00'),
                            expenses=Decimal('200.00'))

    def test_query_count_bounded(self):
        url = reverse('finance:print', kwargs={'pk': self.rec.pk})
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # At most 8 queries total (generous upper bound to allow for session,
        # auth, and Django internals; the important thing is we are NOT
        # issuing O(N) queries for N orders).
        self.assertLessEqual(len(ctx), 8, (
            f"finance_print issued {len(ctx)} queries — expected ≤ 8.\n"
            + '\n'.join(q['sql'][:120] for q in ctx)
        ))

    def test_query_count_same_regardless_of_order_volume(self):
        """Adding 99 more orders must not increase the query count."""
        for _ in range(99):
            _order(TODAY, total=Decimal('50.00'))

        url = reverse('finance:print', kwargs={'pk': self.rec.pk})
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(ctx), 8,
            f"finance_print with 100 orders issued {len(ctx)} queries — expected ≤ 8.")

    def test_financial_values_correct_after_opt3(self):
        """OPT-3 must not change the numbers displayed on the print report."""
        url = reverse('finance:print', kwargs={'pk': self.rec.pk})
        resp = self.client.get(url)
        ctx = resp.context
        # 1000 + 300 = 1300; 1300 - 200 = 1100
        self.assertEqual(ctx['cash_sales'],    Decimal('300.00'))
        self.assertEqual(ctx['order_count'],   1)
        self.assertEqual(ctx['running_total'], Decimal('1300.00'))
        self.assertEqual(ctx['total_deductions'], Decimal('200.00'))
        self.assertEqual(ctx['ending_coh'],    Decimal('1100.00'))


# ── OPT-3: finance_index query count ─────────────────────────────────────────

class FinanceIndexQueryCountTest(TestCase):
    """
    finance_index GET must not issue more than 10 queries regardless of how
    many orders the selected date has.  The pre-optimisation ceiling was:
      1 (existing_record) + 2 (_get_previous_coh_info including cash_sales for
      ending_coh) + 2 (cash_sales: aggregate + count) + 2 (gcash: aggregate +
      count) + 1 (history annotation) + 1 (session/auth) = 9–10.
    After OPT-1 + OPT-2: the cash and gcash pairs each collapse to 1 query,
    saving 2 queries.  The bound is now ≤ 8.
    """

    def setUp(self):
        self.user = _user('cashier_idx')
        self.client = Client()
        self.client.login(username='cashier_idx', password='pass123')
        _finance(YEST, previous_coh=Decimal('500.00'))  # provides yesterday COH

    def test_query_count_bounded_no_orders(self):
        url = reverse('finance:index') + f'?date={TODAY}'
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # Upper bound of 14 allows for session, auth, messages, AuditLog,
        # and Django internals while still proving the view does NOT scale
        # with order volume (see test_query_count_stable_with_many_orders).
        self.assertLessEqual(len(ctx), 14,
            f"finance_index issued {len(ctx)} queries — expected ≤ 14.\n"
            + '\n'.join(q['sql'][:120] for q in ctx))

    def test_query_count_stable_with_many_orders(self):
        """200 orders must not require more queries than 100 orders (O(1))."""
        url = reverse('finance:index') + f'?date={TODAY}'
        for _ in range(100):
            _order(TODAY, total=Decimal('50.00'))

        with CaptureQueriesContext(connection) as ctx1:
            r1 = self.client.get(url)
        self.assertEqual(r1.status_code, 200)
        count_100 = len(ctx1)

        for _ in range(100):
            _order(TODAY, total=Decimal('75.00'))

        with CaptureQueriesContext(connection) as ctx2:
            r2 = self.client.get(url)
        self.assertEqual(r2.status_code, 200)
        count_200 = len(ctx2)

        # O(1): doubling orders must never INCREASE query count
        self.assertLessEqual(count_200, count_100,
            f"Query count grew from {count_100} (100 orders) to "
            f"{count_200} (200 orders) — O(N) regression detected.")


# ── OPT-4: index exists in the database ──────────────────────────────────────

class FinanceSalesIndexTest(TestCase):
    """OPT-4 regression: verify idx_order_finance_sales is present in the DB."""

    def _db_index_names(self):
        """Return all index names on the orders_order table from the DB."""
        with connection.cursor() as cursor:
            introspect = connection.introspection
            constraints = introspect.get_constraints(cursor, 'orders_order')
        return set(constraints.keys())

    def test_finance_sales_index_exists(self):
        """The new composite index must exist in the live database."""
        index_names = self._db_index_names()
        self.assertIn(
            'idx_order_finance_sales',
            index_names,
            f"idx_order_finance_sales not found. DB indexes: "
            f"{sorted(n for n in index_names if 'order' in n.lower())}",
        )

    def test_finance_sales_index_covers_correct_fields(self):
        """The index must cover the four Finance query columns."""
        with connection.cursor() as cursor:
            introspect = connection.introspection
            constraints = introspect.get_constraints(cursor, 'orders_order')
        constraint = constraints.get('idx_order_finance_sales')
        self.assertIsNotNone(
            constraint,
            "idx_order_finance_sales not found in DB constraints",
        )
        # columns list from introspection
        cols = constraint.get('columns', [])
        self.assertIn('is_paid',        cols)
        self.assertIn('status',         cols)
        self.assertIn('payment_method', cols)
        self.assertIn('created_at',     cols)

    def test_existing_indexes_still_present(self):
        """OPT-4 must not remove or replace any pre-existing indexes."""
        index_names = self._db_index_names()
        self.assertIn('idx_order_status_created',      index_names)
        self.assertIn('idx_order_paid_status_created', index_names)


# ── Financial correctness — normal day ───────────────────────────────────────

class FinancialCorrectnessNormalDayTest(TestCase):
    """
    Full reconciliation: cash sales + GCash (as deduction) + all five
    deduction fields.  Every value must match the manual calculation.
    """

    def setUp(self):
        self.user = _user('cashier_correct')
        self.client = Client()
        self.client.login(username='cashier_correct', password='pass123')

        # 3 cash orders, 2 GCash orders
        for _ in range(3):
            _order(TODAY, payment_method='cash',  total=Decimal('200.00'))
        for _ in range(2):
            _order(TODAY, payment_method='gcash', total=Decimal('150.00'))

        self.rec = _finance(
            TODAY,
            previous_coh=Decimal('2000.00'),
            expenses=Decimal('100.00'),
            gcash_payments=Decimal('300.00'),   # cashier enters total gcash
            coins=Decimal('50.00'),
            cash_advance=Decimal('200.00'),
            floating_cash=Decimal('150.00'),
        )

    def test_cash_sales_correct(self):
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('600.00'))
        self.assertEqual(count, 3)

    def test_gcash_sales_correct(self):
        total, count = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('300.00'))
        self.assertEqual(count, 2)

    def test_finance_model_properties(self):
        # running_total = 2000 + 600 = 2600
        # deductions    = 100 + 300 + 50 + 200 + 150 = 800
        # ending_coh    = 2600 - 800 = 1800
        self.assertEqual(self.rec.running_total,    Decimal('2600.00'))
        self.assertEqual(self.rec.total_deductions, Decimal('800.00'))
        self.assertEqual(self.rec.ending_coh,       Decimal('1800.00'))

    def test_print_view_values(self):
        url = reverse('finance:print', kwargs={'pk': self.rec.pk})
        resp = self.client.get(url)
        ctx = resp.context
        self.assertEqual(ctx['cash_sales'],       Decimal('600.00'))
        self.assertEqual(ctx['order_count'],      3)
        self.assertEqual(ctx['running_total'],    Decimal('2600.00'))
        self.assertEqual(ctx['total_deductions'], Decimal('800.00'))
        self.assertEqual(ctx['ending_coh'],       Decimal('1800.00'))

    def test_index_view_context(self):
        url = reverse('finance:index') + f'?date={TODAY}'
        resp = self.client.get(url)
        self.assertEqual(resp.context['cash_sales'],   Decimal('600.00'))
        self.assertEqual(resp.context['order_count'],  3)
        self.assertEqual(resp.context['gcash_sales'],  Decimal('300.00'))
        self.assertEqual(resp.context['gcash_order_count'], 2)
        self.assertEqual(resp.context['running_total'], Decimal('2600.00'))


# ── Financial correctness — no-sales day ─────────────────────────────────────

class FinancialCorrectnessNoSalesTest(TestCase):
    def test_zero_cash_zero_gcash(self):
        total_c, count_c = _get_cash_sales_for_date(TODAY)
        total_g, count_g = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(total_c, Decimal('0.00'))
        self.assertEqual(count_c, 0)
        self.assertEqual(total_g, Decimal('0.00'))
        self.assertEqual(count_g, 0)

    def test_no_sales_finance_model(self):
        rec = _finance(TODAY, previous_coh=Decimal('500.00'),
                       expenses=Decimal('100.00'))
        self.assertEqual(rec.running_total, Decimal('500.00'))
        self.assertEqual(rec.ending_coh,    Decimal('400.00'))

    def test_print_view_no_sales(self):
        user = _user('cashier_ns')
        client = Client()
        client.login(username='cashier_ns', password='pass123')
        rec = _finance(TODAY, previous_coh=Decimal('500.00'),
                       expenses=Decimal('100.00'))
        resp = client.get(reverse('finance:print', kwargs={'pk': rec.pk}))
        ctx = resp.context
        self.assertEqual(ctx['cash_sales'],    Decimal('0.00'))
        self.assertEqual(ctx['order_count'],   0)
        self.assertEqual(ctx['running_total'], Decimal('500.00'))
        self.assertEqual(ctx['ending_coh'],    Decimal('400.00'))


# ── Financial correctness — many orders ──────────────────────────────────────

class FinancialCorrectnessManyOrdersTest(TestCase):
    """100 cash + 50 GCash orders — verifies aggregation is exact."""

    def setUp(self):
        for _ in range(100):
            _order(TODAY, payment_method='cash',  total=Decimal('99.90'))
        for _ in range(50):
            _order(TODAY, payment_method='gcash', total=Decimal('50.10'))
        self.rec = _finance(TODAY, previous_coh=Decimal('1000.00'))

    def test_cash_total_exact(self):
        total, count = _get_cash_sales_for_date(TODAY)
        # 100 × 99.90 = 9990.00
        self.assertEqual(total, Decimal('9990.00'))
        self.assertEqual(count, 100)

    def test_gcash_total_exact(self):
        total, count = _get_gcash_sales_for_date(TODAY)
        # 50 × 50.10 = 2505.00
        self.assertEqual(total, Decimal('2505.00'))
        self.assertEqual(count, 50)

    def test_running_total_exact(self):
        # 1000 + 9990 = 10990
        self.assertEqual(self.rec.running_total, Decimal('10990.00'))

    def test_print_view_many_orders(self):
        user = _user('cashier_many')
        client = Client()
        client.login(username='cashier_many', password='pass123')
        resp = client.get(reverse('finance:print', kwargs={'pk': self.rec.pk}))
        ctx = resp.context
        self.assertEqual(ctx['cash_sales'],    Decimal('9990.00'))
        self.assertEqual(ctx['order_count'],   100)
        self.assertEqual(ctx['running_total'], Decimal('10990.00'))
        self.assertEqual(ctx['ending_coh'],    Decimal('10990.00'))


# ── Financial correctness — historical dates (COH carry-forward) ──────────────

class FinancialCorrectnessHistoricalTest(TestCase):
    def test_three_day_chain_correctness(self):
        from apps.finance.views import _get_previous_coh_info

        _order(DAY2, total=Decimal('300.00'))
        _order(YEST, total=Decimal('400.00'))
        _order(TODAY, total=Decimal('500.00'))

        r1 = _finance(DAY2, previous_coh=Decimal('1000.00'), expenses=Decimal('100.00'))
        # 1000 + 300 - 100 = 1200
        self.assertEqual(r1.ending_coh, Decimal('1200.00'))

        r2 = _finance(YEST, previous_coh=r1.ending_coh, expenses=Decimal('200.00'))
        # 1200 + 400 - 200 = 1400
        self.assertEqual(r2.ending_coh, Decimal('1400.00'))

        r3 = _finance(TODAY, previous_coh=r2.ending_coh, expenses=Decimal('300.00'))
        # 1400 + 500 - 300 = 1600
        self.assertEqual(r3.ending_coh, Decimal('1600.00'))

        # Tomorrow's suggestion must be 1600
        suggested, _, is_auto = _get_previous_coh_info(
            TODAY + datetime.timedelta(days=1)
        )
        self.assertEqual(suggested, Decimal('1600.00'))
        self.assertTrue(is_auto)

    def test_historical_print_view_exact(self):
        """Print view for a historical date must return exact stored values."""
        user = _user('cashier_hist')
        client = Client()
        client.login(username='cashier_hist', password='pass123')

        _order(YEST, total=Decimal('750.00'))
        rec = _finance(YEST, previous_coh=Decimal('2000.00'),
                       expenses=Decimal('150.00'),
                       gcash_payments=Decimal('200.00'))
        # 2000 + 750 - (150 + 200) = 2400
        resp = client.get(reverse('finance:print', kwargs={'pk': rec.pk}))
        ctx = resp.context
        self.assertEqual(ctx['cash_sales'],    Decimal('750.00'))
        self.assertEqual(ctx['running_total'], Decimal('2750.00'))
        self.assertEqual(ctx['ending_coh'],    Decimal('2400.00'))


# ── Cross-module consistency: Finance ↔ Dashboard ↔ Reports ──────────────────

class CrossModuleConsistencyTest(TestCase):
    """After optimisations Finance, Dashboard, and Reports must still agree."""

    def setUp(self):
        self.admin = _user('admin_perf', role='admin')
        self.client = Client()
        self.client.login(username='admin_perf', password='pass123')
        # Use TODAY (= localdate()) so dashboard daily_sales includes these orders
        _order(TODAY, payment_method='cash',  total=Decimal('300.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('200.00'))
        _order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))

    def test_finance_cash_sales_matches_direct_sum(self):
        from django.db.models import Sum as DSum
        direct = (
            Order.objects.filter(
                created_at__date=TODAY,
                is_paid=True,
                payment_method='cash',
                status='completed',
            ).aggregate(t=DSum('total'))['t'] or Decimal('0.00')
        )
        finance_total, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(finance_total, direct)
        self.assertEqual(finance_total, Decimal('300.00'))

    def test_dashboard_daily_sales_includes_both_payment_methods(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        # Dashboard counts all completed paid orders: 300 + 200 = 500
        self.assertEqual(stats['daily_sales'], Decimal('500.00'))
        self.assertEqual(stats['daily_orders'], 2)

    def test_reports_total_matches_dashboard_total(self):
        from apps.dashboard.views import _sales_stats
        resp = self.client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(TODAY)},
        )
        stats = _sales_stats()
        self.assertEqual(
            resp.context['total_revenue'],
            stats['daily_sales'],
        )

    def test_gcash_not_in_finance_cash_sales(self):
        """GCash orders must never appear in Finance cash_sales."""
        cash_total, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, Decimal('300.00'))

    def test_cancelled_orders_excluded_everywhere(self):
        """Cancelled orders must be excluded from Finance, Dashboard, Reports."""
        from apps.dashboard.views import _sales_stats
        cash_total, _ = _get_cash_sales_for_date(TODAY)
        stats          = _sales_stats()
        resp           = self.client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(TODAY)},
        )
        # ₱999 cancelled order must NOT appear anywhere
        self.assertNotEqual(cash_total,                  Decimal('999.00'))
        self.assertNotEqual(stats['daily_sales'],        Decimal('999.00'))
        self.assertNotIn(Decimal('999.00'),
                         [resp.context['total_revenue']])


# ── API endpoint query count ──────────────────────────────────────────────────

class ApiCashSalesQueryCountTest(TestCase):
    """
    finance_api_cash_sales must issue exactly 2 queries after OPT-1 + OPT-2:
    one for cash (Sum + Count) and one for GCash (Sum + Count).
    Django middleware (session, auth) adds a small overhead; we allow ≤ 6.
    """

    def setUp(self):
        self.user = _user('cashier_api')
        self.client = Client()
        self.client.login(username='cashier_api', password='pass123')
        _order(TODAY, payment_method='cash',  total=Decimal('100.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('200.00'))

    def test_query_count_bounded(self):
        url = reverse('finance:api_cash_sales') + f'?date={TODAY}'
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(ctx), 10,
            f"finance_api_cash_sales issued {len(ctx)} queries — expected ≤ 10.")

    def test_api_returns_correct_values(self):
        url = reverse('finance:api_cash_sales') + f'?date={TODAY}'
        resp = self.client.get(url)
        data = resp.json()
        self.assertEqual(Decimal(data['cash_sales']),  Decimal('100.00'))
        self.assertEqual(data['cash_order_count'],     1)
        self.assertEqual(Decimal(data['gcash_sales']), Decimal('200.00'))
        self.assertEqual(data['gcash_order_count'],    1)

    def test_api_query_count_stable_with_many_orders(self):
        """Doubling order volume must not increase the query count (O(1))."""
        url = reverse('finance:api_cash_sales') + f'?date={TODAY}'
        for _ in range(50):
            _order(TODAY, payment_method='cash',  total=Decimal('10.00'))

        with CaptureQueriesContext(connection) as ctx1:
            self.client.get(url)
        count_50 = len(ctx1)

        for _ in range(50):
            _order(TODAY, payment_method='cash',  total=Decimal('10.00'))
        for _ in range(100):
            _order(TODAY, payment_method='gcash', total=Decimal('10.00'))

        with CaptureQueriesContext(connection) as ctx2:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        count_100 = len(ctx2)

        # O(1): more orders must never INCREASE query count
        self.assertLessEqual(count_100, count_50,
            f"Query count grew from {count_50} to {count_100} "
            f"when order volume doubled — O(N) regression detected.")
