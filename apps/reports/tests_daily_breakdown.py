"""
Daily Breakdown verification tests — Kape De Manubag Sales Reports.

The Daily Breakdown section shows one row per day in the selected range
(only days with sales). Every daily total must reconcile with the
period's overall total_revenue.

Key invariants:
  sum(daily_sales[i]['total']) == total_revenue   (for the same period)
  daily_sales has exactly one entry per day in the range
  Zero-sale days have count=0 and total=0.0 (not shown in the table,
    but present in the list for the chart)
  Only is_paid=True AND status='completed' orders counted
  PHT business date used for grouping (TruncDate + TIME_ZONE='Asia/Manila')

Scenarios:
  1.  One-day report — single day with sales
  2.  Multiple-day report — totals per day + overall reconciliation
  3.  Days with sales vs days without sales in same range
  4.  Midnight PHT transaction — assigned to correct day
  5.  Cancelled orders excluded
  6.  Cash and GCash both counted in daily totals
  7.  Consecutive dates — no boundary bleed
  8.  Each daily total matches the Finance per-day total
  9.  Overall total equals sum of daily totals
  10. One-entry-per-day structural guarantee
"""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.views import _get_cash_sales_for_date, _get_gcash_sales_for_date
from apps.orders.models import Order

User = get_user_model()
PHT  = ZoneInfo('Asia/Manila')

TODAY = timezone.localdate()
YEST  = TODAY - datetime.timedelta(days=1)
DAY2  = TODAY - datetime.timedelta(days=2)
DAY3  = TODAY - datetime.timedelta(days=3)


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


def _order_at_pht(pht_date, hour, minute=0, total=Decimal('100.00'),
                  payment_method='cash', status='completed', is_paid=True):
    """Order at exact PHT time (for midnight boundary tests)."""
    naive_pht = datetime.datetime(
        pht_date.year, pht_date.month, pht_date.day, hour, minute
    )
    aware_pht = naive_pht.replace(tzinfo=PHT)
    utc_dt    = aware_pht.astimezone(datetime.timezone.utc)
    o = Order.objects.create(
        customer_name='Test',
        status=status, is_paid=is_paid,
        payment_method=payment_method,
        total=total, subtotal=total,
    )
    Order.objects.filter(pk=o.pk).update(created_at=utc_dt)
    o.refresh_from_db()
    return o


def _reports(client, start, end):
    return client.get(
        reverse('reports:index'),
        {'start': str(start), 'end': str(end)},
    )


def _daily(resp):
    return resp.context['daily_sales']


def _entry(daily, date):
    label = date.strftime('%b %d')
    return next((d for d in daily if d['date'] == label), None)


# ── 1. One-day report ─────────────────────────────────────────────────────────

class OneDayReportTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_oneday')
        self.client = Client()
        self.client.login(username='admin_oneday', password='pass123')
        _order(TODAY, total=Decimal('250.00'))
        _order(TODAY, total=Decimal('150.00'))

    def test_single_entry_in_list(self):
        daily = _daily(_reports(self.client, TODAY, TODAY))
        self.assertEqual(len(daily), 1)

    def test_entry_total_correct(self):
        daily = _daily(_reports(self.client, TODAY, TODAY))
        self.assertAlmostEqual(daily[0]['total'], 400.0, places=2)

    def test_entry_count_correct(self):
        daily = _daily(_reports(self.client, TODAY, TODAY))
        self.assertEqual(daily[0]['count'], 2)

    def test_daily_total_equals_period_total(self):
        resp  = _reports(self.client, TODAY, TODAY)
        daily = _daily(resp)
        self.assertAlmostEqual(
            daily[0]['total'],
            float(resp.context['total_revenue']),
            places=2,
        )


# ── 2. Multiple-day report ────────────────────────────────────────────────────

class MultipleDayReportTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_multiday')
        self.client = Client()
        self.client.login(username='admin_multiday', password='pass123')
        _order(DAY2,  total=Decimal('100.00'))
        _order(YEST,  total=Decimal('200.00'))
        _order(TODAY, total=Decimal('300.00'))

    def test_three_entries_in_list(self):
        daily = _daily(_reports(self.client, DAY2, TODAY))
        self.assertEqual(len(daily), 3)

    def test_each_day_total_correct(self):
        daily = _daily(_reports(self.client, DAY2, TODAY))
        self.assertAlmostEqual(_entry(daily, DAY2)['total'],  100.0, places=2)
        self.assertAlmostEqual(_entry(daily, YEST)['total'],  200.0, places=2)
        self.assertAlmostEqual(_entry(daily, TODAY)['total'], 300.0, places=2)

    def test_sum_of_daily_equals_period_total(self):
        """Core reconciliation invariant: sum(daily) == total_revenue."""
        resp  = _reports(self.client, DAY2, TODAY)
        daily = _daily(resp)
        daily_sum = sum(d['total'] for d in daily)
        self.assertAlmostEqual(
            daily_sum,
            float(resp.context['total_revenue']),
            places=2,
        )

    def test_period_total_correct(self):
        resp = _reports(self.client, DAY2, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('600.00'))


# ── 3. Days with and without sales ────────────────────────────────────────────

class MixedSalesDaysTest(TestCase):
    """Some days have sales, some do not — zero-sale days still in list."""

    def setUp(self):
        self.admin = _user('admin_mixed_days')
        self.client = Client()
        self.client.login(username='admin_mixed_days', password='pass123')
        # Sales on DAY2 and TODAY, nothing on YEST
        _order(DAY2,  total=Decimal('100.00'))
        _order(TODAY, total=Decimal('300.00'))

    def test_list_has_entry_for_every_day(self):
        daily = _daily(_reports(self.client, DAY2, TODAY))
        self.assertEqual(len(daily), 3)   # DAY2, YEST, TODAY

    def test_zero_sale_day_has_count_zero(self):
        daily = _daily(_reports(self.client, DAY2, TODAY))
        yest_entry = _entry(daily, YEST)
        self.assertIsNotNone(yest_entry)
        self.assertEqual(yest_entry['count'], 0)
        self.assertEqual(yest_entry['total'], 0.0)

    def test_sale_days_have_correct_totals(self):
        daily = _daily(_reports(self.client, DAY2, TODAY))
        self.assertAlmostEqual(_entry(daily, DAY2)['total'],  100.0, places=2)
        self.assertAlmostEqual(_entry(daily, TODAY)['total'], 300.0, places=2)

    def test_has_any_sales_flag_true(self):
        resp = _reports(self.client, DAY2, TODAY)
        self.assertTrue(resp.context['has_any_sales'])

    def test_sum_of_daily_equals_period_total_with_zero_days(self):
        resp  = _reports(self.client, DAY2, TODAY)
        daily = _daily(resp)
        daily_sum = sum(d['total'] for d in daily)
        self.assertAlmostEqual(
            daily_sum,
            float(resp.context['total_revenue']),
            places=2,
        )


# ── 4. No sales at all ────────────────────────────────────────────────────────

class NoSalesBreakdownTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_nosales_bd')
        self.client = Client()
        self.client.login(username='admin_nosales_bd', password='pass123')

    def test_all_entries_zero(self):
        daily = _daily(_reports(self.client, YEST, TODAY))
        for entry in daily:
            self.assertEqual(entry['count'], 0)
            self.assertEqual(entry['total'], 0.0)

    def test_has_any_sales_false(self):
        resp = _reports(self.client, YEST, TODAY)
        self.assertFalse(resp.context['has_any_sales'])

    def test_period_total_zero(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], 0)

    def test_sum_of_daily_is_zero(self):
        resp  = _reports(self.client, YEST, TODAY)
        daily = _daily(resp)
        self.assertEqual(sum(d['total'] for d in daily), 0.0)


# ── 5. Midnight PHT transaction assigned to correct day ──────────────────────

class MidnightBreakdownTest(TestCase):
    """
    00:00 PHT = 16:00 UTC previous calendar day.
    TruncDate with TIME_ZONE='Asia/Manila' must group it under the PHT date.
    """

    def setUp(self):
        self.admin = _user('admin_midnight_bd')
        self.client = Client()
        self.client.login(username='admin_midnight_bd', password='pass123')
        # Midnight on TODAY (PHT) = 16:00 UTC yesterday
        _order_at_pht(TODAY, 0, 0, total=Decimal('200.00'))

    def test_midnight_order_in_today_entry(self):
        daily      = _daily(_reports(self.client, TODAY, TODAY))
        today_entry = _entry(daily, TODAY)
        self.assertIsNotNone(today_entry)
        self.assertAlmostEqual(today_entry['total'], 200.0, places=2)
        self.assertEqual(today_entry['count'], 1)

    def test_midnight_order_not_in_yesterday_entry(self):
        daily      = _daily(_reports(self.client, YEST, TODAY))
        yest_entry  = _entry(daily, YEST)
        today_entry = _entry(daily, TODAY)
        self.assertEqual(yest_entry['count'], 0)
        self.assertEqual(yest_entry['total'], 0.0)
        self.assertEqual(today_entry['count'], 1)

    def test_sum_of_daily_reconciles(self):
        resp  = _reports(self.client, TODAY, TODAY)
        daily = _daily(resp)
        self.assertAlmostEqual(
            sum(d['total'] for d in daily),
            float(resp.context['total_revenue']),
            places=2,
        )


# ── 6. Cancelled orders excluded ─────────────────────────────────────────────

class CancelledOrderBreakdownTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cancel_bd')
        self.client = Client()
        self.client.login(username='admin_cancel_bd', password='pass123')
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('200.00'))
        _order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))

    def test_cancelled_not_in_daily_total(self):
        daily = _daily(_reports(self.client, TODAY, TODAY))
        self.assertAlmostEqual(daily[0]['total'], 200.0, places=2)
        self.assertEqual(daily[0]['count'], 1)

    def test_period_total_excludes_cancelled(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))

    def test_daily_reconciles_without_cancelled(self):
        resp  = _reports(self.client, TODAY, TODAY)
        daily = _daily(resp)
        self.assertAlmostEqual(
            sum(d['total'] for d in daily),
            float(resp.context['total_revenue']),
            places=2,
        )


# ── 7. Cash and GCash both in daily totals ────────────────────────────────────

class CashAndGCashBreakdownTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cashgcash_bd')
        self.client = Client()
        self.client.login(username='admin_cashgcash_bd', password='pass123')
        _order(TODAY, payment_method='cash',  total=Decimal('300.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('200.00'))

    def test_both_in_daily_total(self):
        daily = _daily(_reports(self.client, TODAY, TODAY))
        self.assertAlmostEqual(daily[0]['total'], 500.0, places=2)
        self.assertEqual(daily[0]['count'], 2)

    def test_daily_agrees_with_finance_cash_plus_gcash(self):
        cash, _  = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        daily    = _daily(_reports(self.client, TODAY, TODAY))
        self.assertAlmostEqual(
            daily[0]['total'],
            float(cash + gcash),
            places=2,
        )

    def test_period_total_matches_daily(self):
        resp  = _reports(self.client, TODAY, TODAY)
        daily = _daily(resp)
        self.assertAlmostEqual(
            sum(d['total'] for d in daily),
            float(resp.context['total_revenue']),
            places=2,
        )


# ── 8. Consecutive dates — no boundary bleed ─────────────────────────────────

class ConsecutiveDatesBreakdownTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_consec_bd')
        self.client = Client()
        self.client.login(username='admin_consec_bd', password='pass123')
        _order(DAY2,  total=Decimal('100.00'))
        _order(YEST,  total=Decimal('200.00'))
        _order(TODAY, total=Decimal('300.00'))

    def test_each_day_correct(self):
        daily = _daily(_reports(self.client, DAY2, TODAY))
        self.assertAlmostEqual(_entry(daily, DAY2)['total'],  100.0, places=2)
        self.assertAlmostEqual(_entry(daily, YEST)['total'],  200.0, places=2)
        self.assertAlmostEqual(_entry(daily, TODAY)['total'], 300.0, places=2)

    def test_no_bleed_between_consecutive_days(self):
        """Total of individual day reports == combined range total."""
        r_day2  = _reports(self.client, DAY2,  DAY2)
        r_yest  = _reports(self.client, YEST,  YEST)
        r_today = _reports(self.client, TODAY, TODAY)
        r_all   = _reports(self.client, DAY2,  TODAY)
        sum_individual = (
            float(r_day2.context['total_revenue']) +
            float(r_yest.context['total_revenue']) +
            float(r_today.context['total_revenue'])
        )
        self.assertAlmostEqual(
            sum_individual,
            float(r_all.context['total_revenue']),
            places=2,
        )

    def test_daily_finance_agreement_each_day(self):
        for date, expected in [(DAY2, Decimal('100.00')),
                               (YEST, Decimal('200.00')),
                               (TODAY, Decimal('300.00'))]:
            cash, _  = _get_cash_sales_for_date(date)
            gcash, _ = _get_gcash_sales_for_date(date)
            resp     = _reports(self.client, date, date)
            daily    = _daily(resp)
            self.assertAlmostEqual(
                daily[0]['total'],
                float(cash + gcash),
                places=2,
                msg=f"Daily breakdown vs Finance mismatch on {date}",
            )


# ── 9. Overall reconciliation invariant ──────────────────────────────────────

class ReconciliationTest(TestCase):
    """
    sum(daily_sales[i]['total']) must always equal total_revenue
    for any date range — the core reconciliation guarantee.
    """

    def setUp(self):
        self.admin = _user('admin_reconcile')
        self.client = Client()
        self.client.login(username='admin_reconcile', password='pass123')

    def _assert_reconciles(self, start, end):
        resp      = _reports(self.client, start, end)
        daily     = _daily(resp)
        daily_sum = sum(d['total'] for d in daily)
        self.assertAlmostEqual(
            daily_sum,
            float(resp.context['total_revenue']),
            places=2,
            msg=f"daily_sum {daily_sum} != total_revenue "
                f"{resp.context['total_revenue']} for {start}–{end}",
        )

    def test_reconciles_no_sales(self):
        self._assert_reconciles(TODAY, TODAY)

    def test_reconciles_one_sale(self):
        _order(TODAY, total=Decimal('123.45'))
        self._assert_reconciles(TODAY, TODAY)

    def test_reconciles_multiple_days(self):
        _order(DAY3,  total=Decimal('100.00'))
        _order(DAY2,  total=Decimal('200.00'))
        _order(YEST,  total=Decimal('300.00'))
        _order(TODAY, total=Decimal('400.00'))
        self._assert_reconciles(DAY3, TODAY)

    def test_reconciles_with_zero_days_in_range(self):
        _order(DAY3,  total=Decimal('150.00'))
        _order(TODAY, total=Decimal('250.00'))
        # YEST and DAY2 have no sales
        self._assert_reconciles(DAY3, TODAY)

    def test_reconciles_mixed_payment_methods(self):
        _order(TODAY, payment_method='cash',  total=Decimal('300.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('200.00'))
        self._assert_reconciles(TODAY, TODAY)

    def test_reconciles_with_cancelled_orders_present(self):
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('200.00'))
        _order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))
        self._assert_reconciles(TODAY, TODAY)


# ── 10. Structural guarantees ─────────────────────────────────────────────────

class StructuralGuaranteesTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_struct_bd')
        self.client = Client()
        self.client.login(username='admin_struct_bd', password='pass123')
        _order(TODAY, total=Decimal('100.00'))

    def test_one_entry_per_day_in_range(self):
        daily = _daily(_reports(self.client, DAY2, TODAY))
        self.assertEqual(len(daily), 3)

    def test_each_entry_has_date_count_total(self):
        daily = _daily(_reports(self.client, TODAY, TODAY))
        for entry in daily:
            self.assertIn('date',  entry)
            self.assertIn('count', entry)
            self.assertIn('total', entry)

    def test_total_is_float(self):
        daily = _daily(_reports(self.client, TODAY, TODAY))
        for entry in daily:
            self.assertIsInstance(entry['total'], float)

    def test_count_is_int(self):
        daily = _daily(_reports(self.client, TODAY, TODAY))
        for entry in daily:
            self.assertIsInstance(entry['count'], int)

    def test_date_label_format(self):
        """Labels are in '%b %d' format (e.g. 'Aug 28')."""
        import datetime as dt
        daily = _daily(_reports(self.client, TODAY, TODAY))
        for entry in daily:
            try:
                dt.datetime.strptime(entry['date'], '%b %d')
                valid = True
            except ValueError:
                valid = False
            self.assertTrue(valid,
                f"Date label '{entry['date']}' not in '%b %d' format")
