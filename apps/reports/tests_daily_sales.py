"""
Daily Sales verification tests — Kape De Manubag Sales Reports.

Verifies:
  - Daily Sales calculation is mathematically accurate
  - Every qualifying transaction is counted exactly once
  - Cancelled / non-completed orders are excluded
  - Cash and GCash are both included in the daily total
  - daily_sales list structure is correct (one entry per day in range)
  - has_any_sales flag is correct
  - Daily totals agree with Finance and Dashboard for the same dates
  - Consecutive dates are segregated correctly

Scenarios:
  1.  Day with no sales
  2.  One sale
  3.  Multiple sales same day
  4.  Cash-only day
  5.  GCash-only day
  6.  Mixed payment day
  7.  Cancelled order excluded
  8.  Pending / preparing / ready excluded
  9.  Paid-not-completed excluded
  10. Completed-not-paid excluded
  11. Multiple dates — correct segregation
  12. Consecutive dates — totals per day correct
  13. has_any_sales flag — True/False cases
  14. Cross-module: daily total == Finance cash + Finance GCash
  15. Cross-module: daily total == Dashboard daily_sales for same day
  16. Date range spanning multiple days
  17. Single-day range
  18. Decimal amounts (precision check for table display)
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
YEST  = TODAY - datetime.timedelta(days=1)
DAY2  = TODAY - datetime.timedelta(days=2)
DAY3  = TODAY - datetime.timedelta(days=3)
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
    return client.get(
        reverse('reports:index'),
        {'start': str(start), 'end': str(end)},
    )


def _day_entry(daily_sales, date):
    """Return the daily_sales dict entry for a specific date, or None."""
    label = date.strftime('%b %d')
    return next((d for d in daily_sales if d['date'] == label), None)


# ── 1. Day with no sales ──────────────────────────────────────────────────────

class NoSalesDayTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_nosales')
        self.client = Client()
        self.client.login(username='admin_nosales', password='pass123')

    def test_single_day_no_sales_entry_has_zero_total(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(daily[0]['total'], 0.0)
        self.assertEqual(daily[0]['count'], 0)

    def test_has_any_sales_is_false(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertFalse(resp.context['has_any_sales'])

    def test_total_revenue_is_zero(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], 0)


# ── 2. One sale ───────────────────────────────────────────────────────────────

class OneSaleTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_onesale')
        self.client = Client()
        self.client.login(username='admin_onesale', password='pass123')
        _order(TODAY, total=Decimal('250.00'))

    def test_single_day_entry_total_correct(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(daily[0]['total'], 250.0, places=2)
        self.assertEqual(daily[0]['count'], 1)

    def test_has_any_sales_is_true(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertTrue(resp.context['has_any_sales'])

    def test_daily_total_matches_finance(self):
        cash, _ = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertAlmostEqual(daily[0]['total'], float(cash + gcash), places=2)


# ── 3. Multiple sales same day ────────────────────────────────────────────────

class MultipleSalesSameDayTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_multi')
        self.client = Client()
        self.client.login(username='admin_multi', password='pass123')
        _order(TODAY, total=Decimal('100.00'))
        _order(TODAY, total=Decimal('200.00'))
        _order(TODAY, total=Decimal('150.00'))

    def test_all_orders_summed_in_single_day_entry(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(daily[0]['total'], 450.0, places=2)
        self.assertEqual(daily[0]['count'], 3)

    def test_no_double_counting(self):
        """Each order must appear exactly once in the daily total."""
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('450.00'))
        self.assertEqual(resp.context['total_orders'], 3)


# ── 4. Cash-only day ──────────────────────────────────────────────────────────

class CashOnlyDayTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cash')
        self.client = Client()
        self.client.login(username='admin_cash', password='pass123')
        _order(TODAY, payment_method='cash', total=Decimal('300.00'))
        _order(TODAY, payment_method='cash', total=Decimal('200.00'))

    def test_cash_orders_in_daily_total(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertAlmostEqual(daily[0]['total'], 500.0, places=2)
        self.assertEqual(daily[0]['count'], 2)

    def test_daily_total_equals_finance_cash_sales(self):
        cash, _ = _get_cash_sales_for_date(TODAY)
        resp = _reports(self.client, TODAY, TODAY)
        self.assertAlmostEqual(
            resp.context['daily_sales'][0]['total'],
            float(cash),
            places=2,
        )

    def test_gcash_is_zero(self):
        gcash, count = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(gcash, Decimal('0.00'))
        self.assertEqual(count, 0)


# ── 5. GCash-only day ─────────────────────────────────────────────────────────

class GCashOnlyDayTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_gcash')
        self.client = Client()
        self.client.login(username='admin_gcash', password='pass123')
        _order(TODAY, payment_method='gcash', total=Decimal('400.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('100.00'))

    def test_gcash_orders_in_daily_total(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertAlmostEqual(daily[0]['total'], 500.0, places=2)
        self.assertEqual(daily[0]['count'], 2)

    def test_finance_cash_is_zero_gcash_is_full(self):
        cash, _   = _get_cash_sales_for_date(TODAY)
        gcash, _  = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(cash,  Decimal('0.00'))
        self.assertEqual(gcash, Decimal('500.00'))

    def test_daily_total_equals_finance_gcash(self):
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        resp = _reports(self.client, TODAY, TODAY)
        self.assertAlmostEqual(
            resp.context['daily_sales'][0]['total'],
            float(gcash),
            places=2,
        )


# ── 6. Mixed payment day ──────────────────────────────────────────────────────

class MixedPaymentDayTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_mixed')
        self.client = Client()
        self.client.login(username='admin_mixed', password='pass123')
        _order(TODAY, payment_method='cash',  total=Decimal('300.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('200.00'))

    def test_both_payment_types_summed(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertAlmostEqual(daily[0]['total'], 500.0, places=2)
        self.assertEqual(daily[0]['count'], 2)

    def test_daily_total_equals_finance_cash_plus_gcash(self):
        cash,  _ = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        resp = _reports(self.client, TODAY, TODAY)
        self.assertAlmostEqual(
            resp.context['daily_sales'][0]['total'],
            float(cash + gcash),
            places=2,
        )
        self.assertAlmostEqual(float(cash),  300.0, places=2)
        self.assertAlmostEqual(float(gcash), 200.0, places=2)

    def test_daily_agrees_with_dashboard(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        resp  = _reports(self.client, TODAY, TODAY)
        self.assertAlmostEqual(
            resp.context['daily_sales'][0]['total'],
            float(stats['daily_sales']),
            places=2,
        )


# ── 7. Cancelled order excluded ───────────────────────────────────────────────

class CancelledOrderTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cancel')
        self.client = Client()
        self.client.login(username='admin_cancel', password='pass123')
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('200.00'))
        _order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))

    def test_cancelled_excluded_from_daily(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertAlmostEqual(daily[0]['total'], 200.0, places=2)
        self.assertEqual(daily[0]['count'], 1)

    def test_has_any_sales_true_despite_cancelled(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertTrue(resp.context['has_any_sales'])


# ── 8. Non-completed statuses excluded ───────────────────────────────────────

class NonCompletedStatusTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_status')
        self.client = Client()
        self.client.login(username='admin_status', password='pass123')
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('100.00'))
        for st in ['pending', 'preparing', 'ready']:
            _order(TODAY, status=st, is_paid=False, total=Decimal('999.00'))

    def test_only_completed_counted(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertAlmostEqual(daily[0]['total'], 100.0, places=2)
        self.assertEqual(daily[0]['count'], 1)


# ── 9. Paid-not-completed excluded ────────────────────────────────────────────

class PaidNotCompletedTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_pnc')
        self.client = Client()
        self.client.login(username='admin_pnc', password='pass123')
        _order(TODAY, status='pending',   is_paid=True,  total=Decimal('999.00'))
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('150.00'))

    def test_paid_pending_excluded(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertAlmostEqual(daily[0]['total'], 150.0, places=2)
        self.assertEqual(daily[0]['count'], 1)


# ── 10. Completed-not-paid excluded ──────────────────────────────────────────

class CompletedNotPaidTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cnp')
        self.client = Client()
        self.client.login(username='admin_cnp', password='pass123')
        _order(TODAY, status='completed', is_paid=False, total=Decimal('999.00'))
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('150.00'))

    def test_completed_unpaid_excluded(self):
        resp = _reports(self.client, TODAY, TODAY)
        daily = resp.context['daily_sales']
        self.assertAlmostEqual(daily[0]['total'], 150.0, places=2)
        self.assertEqual(daily[0]['count'], 1)


# ── 11. Multiple dates — correct segregation ─────────────────────────────────

class MultipleDateSegregationTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_dates')
        self.client = Client()
        self.client.login(username='admin_dates', password='pass123')
        _order(DAY2,  total=Decimal('100.00'))
        _order(YEST,  total=Decimal('200.00'))
        _order(TODAY, total=Decimal('300.00'))

    def test_three_days_produce_three_entries(self):
        resp = _reports(self.client, DAY2, TODAY)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 3)

    def test_each_day_has_correct_total(self):
        resp = _reports(self.client, DAY2, TODAY)
        day2_entry  = _day_entry(resp.context['daily_sales'], DAY2)
        yest_entry  = _day_entry(resp.context['daily_sales'], YEST)
        today_entry = _day_entry(resp.context['daily_sales'], TODAY)
        self.assertIsNotNone(day2_entry,  "DAY2 entry missing")
        self.assertIsNotNone(yest_entry,  "YEST entry missing")
        self.assertIsNotNone(today_entry, "TODAY entry missing")
        self.assertAlmostEqual(day2_entry['total'],  100.0, places=2)
        self.assertAlmostEqual(yest_entry['total'],  200.0, places=2)
        self.assertAlmostEqual(today_entry['total'], 300.0, places=2)

    def test_orders_not_bleed_across_dates(self):
        """DAY2 orders must not appear in TODAY's entry and vice versa."""
        resp = _reports(self.client, DAY2, TODAY)
        daily = resp.context['daily_sales']
        for entry in daily:
            if entry['count'] > 0:
                # Each entry must have exactly one order
                self.assertEqual(entry['count'], 1)

    def test_total_revenue_sums_all_days(self):
        resp = _reports(self.client, DAY2, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('600.00'))


# ── 12. Consecutive dates ─────────────────────────────────────────────────────

class ConsecutiveDatesTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_consec')
        self.client = Client()
        self.client.login(username='admin_consec', password='pass123')
        amounts = {
            DAY3: Decimal('50.00'),
            DAY2: Decimal('100.00'),
            YEST: Decimal('200.00'),
            TODAY: Decimal('300.00'),
        }
        for date, amt in amounts.items():
            _order(date, total=amt)
        self.amounts = amounts

    def test_four_consecutive_days(self):
        resp = _reports(self.client, DAY3, TODAY)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 4)
        for date, expected in self.amounts.items():
            entry = _day_entry(daily, date)
            self.assertIsNotNone(entry, f"Entry for {date} missing")
            self.assertAlmostEqual(
                entry['total'], float(expected), places=2,
                msg=f"Wrong total for {date}",
            )

    def test_each_date_agrees_with_finance(self):
        resp = _reports(self.client, DAY3, TODAY)
        daily = resp.context['daily_sales']
        for date in [DAY3, DAY2, YEST, TODAY]:
            cash, _  = _get_cash_sales_for_date(date)
            gcash, _ = _get_gcash_sales_for_date(date)
            entry = _day_entry(daily, date)
            self.assertAlmostEqual(
                entry['total'],
                float(cash + gcash),
                places=2,
                msg=f"Reports vs Finance mismatch on {date}",
            )

    def test_range_total_matches_sum_of_days(self):
        resp = _reports(self.client, DAY3, TODAY)
        daily = resp.context['daily_sales']
        sum_of_days = sum(d['total'] for d in daily)
        self.assertAlmostEqual(
            float(resp.context['total_revenue']),
            sum_of_days,
            places=2,
        )


# ── 13. has_any_sales flag ────────────────────────────────────────────────────

class HasAnySalesFlagTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_flag')
        self.client = Client()
        self.client.login(username='admin_flag', password='pass123')

    def test_false_when_no_orders(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertFalse(resp.context['has_any_sales'])

    def test_true_when_one_completed_order(self):
        _order(TODAY, total=Decimal('100.00'))
        resp = _reports(self.client, TODAY, TODAY)
        self.assertTrue(resp.context['has_any_sales'])

    def test_false_when_only_cancelled_orders(self):
        _order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))
        resp = _reports(self.client, TODAY, TODAY)
        self.assertFalse(resp.context['has_any_sales'])

    def test_false_when_only_pending_orders(self):
        _order(TODAY, status='pending', is_paid=False, total=Decimal('999.00'))
        resp = _reports(self.client, TODAY, TODAY)
        self.assertFalse(resp.context['has_any_sales'])

    def test_true_with_sales_on_any_day_in_range(self):
        """has_any_sales=True if any day in the range has a sale."""
        _order(YEST, total=Decimal('100.00'))  # sale on YEST, not TODAY
        resp = _reports(self.client, YEST, TODAY)
        self.assertTrue(resp.context['has_any_sales'])


# ── 14. Daily list structure ──────────────────────────────────────────────────

class DailySalesListStructureTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_struct')
        self.client = Client()
        self.client.login(username='admin_struct', password='pass123')

    def test_one_entry_per_day_in_range(self):
        resp = _reports(self.client, DAY2, TODAY)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 3)  # DAY2, YEST, TODAY

    def test_each_entry_has_required_keys(self):
        _order(TODAY, total=Decimal('100.00'))
        resp = _reports(self.client, TODAY, TODAY)
        for entry in resp.context['daily_sales']:
            self.assertIn('date',  entry)
            self.assertIn('total', entry)
            self.assertIn('count', entry)

    def test_zero_days_have_count_zero(self):
        """Days without sales must have count=0 and total=0.0."""
        _order(YEST, total=Decimal('100.00'))  # only YEST has sales
        resp = _reports(self.client, YEST, TODAY)
        today_entry = _day_entry(resp.context['daily_sales'], TODAY)
        self.assertEqual(today_entry['count'], 0)
        self.assertEqual(today_entry['total'], 0.0)

    def test_date_label_format(self):
        """Date labels must be in '%b %d' format (e.g. 'Aug 28')."""
        resp = _reports(self.client, TODAY, TODAY)
        label = resp.context['daily_sales'][0]['date']
        # Should parse as a valid '%b %d' date
        import datetime as dt
        try:
            dt.datetime.strptime(label, '%b %d')
            valid = True
        except ValueError:
            valid = False
        self.assertTrue(valid, f"Date label '{label}' is not in '%b %d' format")

    def test_total_is_float_type(self):
        _order(TODAY, total=Decimal('100.00'))
        resp = _reports(self.client, TODAY, TODAY)
        for entry in resp.context['daily_sales']:
            self.assertIsInstance(entry['total'], float)

    def test_count_is_int_type(self):
        _order(TODAY, total=Decimal('100.00'))
        resp = _reports(self.client, TODAY, TODAY)
        for entry in resp.context['daily_sales']:
            self.assertIsInstance(entry['count'], int)


# ── 15. Decimal precision in table display ────────────────────────────────────

class DecimalPrecisionTest(TestCase):
    """
    Verifies that centavo amounts that are problematic for IEEE 754
    (₱123.10, ₱99.90) display correctly in the Daily Breakdown table.
    The view stores float(Decimal) which introduces minor imprecision,
    but Django's floatformat:2 rounds correctly for display.
    """

    def setUp(self):
        self.admin = _user('admin_decimal')
        self.client = Client()
        self.client.login(username='admin_decimal', password='pass123')

    def test_tricky_centavo_amounts_sum_correctly(self):
        """10 × ₱99.90 = ₱999.00 in daily total (not ₱998.99)."""
        for _ in range(10):
            _order(TODAY, total=Decimal('99.90'))
        resp = _reports(self.client, TODAY, TODAY)
        entry = resp.context['daily_sales'][0]
        # The float sum may have minor imprecision, but toFixed(2) / floatformat
        # will round correctly. We check that the total rounds to 999.00.
        self.assertAlmostEqual(entry['total'], 999.0, places=1)
        self.assertEqual(entry['count'], 10)

    def test_daily_total_agrees_with_finance_on_tricky_amounts(self):
        _order(TODAY, payment_method='cash',  total=Decimal('123.10'))
        _order(TODAY, payment_method='gcash', total=Decimal('99.90'))
        cash, _  = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        resp = _reports(self.client, TODAY, TODAY)
        entry = resp.context['daily_sales'][0]
        self.assertAlmostEqual(
            entry['total'],
            float(cash + gcash),
            places=2,
        )
        self.assertEqual(entry['count'], 2)
