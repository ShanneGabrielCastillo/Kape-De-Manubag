"""
Sales Reports date-filtering tests — Kape De Manubag.

Verifies that the Reports date filter assigns every order to the correct
Philippine (PHT = UTC+8) business date, with special attention to boundary
timestamps that straddle the UTC midnight.

Key invariant:
  Django __date ORM lookups convert stored UTC datetimes to Asia/Manila
  before extracting the date portion.  An order placed at 00:00 PHT is
  stored as 16:00 UTC the *previous* calendar day, yet the filter correctly
  assigns it to the PHT business date.

Timestamps tested (all in PHT → UTC):
  00:00  PHT  =  16:00  UTC prev day  ← midnight, first instant of new day
  00:01  PHT  =  16:01  UTC prev day  ← one minute after midnight
  07:59  PHT  =  23:59  UTC prev day  ← last minute where old UTC bug fired
  08:00  PHT  =  00:00  UTC same day  ← UTC and PHT now agree
  12:00  PHT  =  04:00  UTC same day  ← midday, never ambiguous
  23:59  PHT  =  15:59  UTC same day  ← last minute of business day

Scenarios:
  1.  Midnight order (00:00 PHT) on Day B appears in Day B report
  2.  00:01 PHT order on Day B appears in Day B report
  3.  23:59 PHT order on Day A appears in Day A report, NOT Day B
  4.  07:59 PHT order on Day B (old UTC date = Day A) appears in Day B
  5.  One-day range is inclusive on both ends
  6.  Multi-day range includes all days, segregated correctly
  7.  Consecutive dates — boundary between Day A and Day B
  8.  No-match range returns zero revenue
  9.  TruncDate groups midnight-PHT orders to the correct day
  10. Default range uses PHT date (localdate())
  11. Reports filter agrees with Finance per-day totals at every boundary
  12. Reports filter agrees with Dashboard daily totals
  13. Excel timestamp fix — localtime() used, not raw UTC
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

# Fixed PHT reference dates
DAY_A = datetime.date(2026, 8, 27)   # PHT
DAY_B = datetime.date(2026, 8, 28)   # PHT  (the main "business today")
DAY_C = datetime.date(2026, 8, 29)   # PHT


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _order_at_pht(pht_date, hour, minute=0, second=0,
                  total=Decimal('100.00'), payment_method='cash',
                  status='completed', is_paid=True):
    """
    Create an Order whose created_at is the exact UTC equivalent of the
    given PHT date + time.
    """
    naive_pht = datetime.datetime(
        pht_date.year, pht_date.month, pht_date.day, hour, minute, second
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


# ── 1. Midnight (00:00 PHT) appears in the correct day ───────────────────────

class MidnightBoundaryTest(TestCase):
    """
    00:00 PHT is the first instant of a new business day.
    Stored as 16:00 UTC the previous calendar day.
    Must appear in the PHT day report, not the UTC day report.
    """

    def setUp(self):
        self.admin = _user('admin_midnight')
        self.client = Client()
        self.client.login(username='admin_midnight', password='pass123')
        # Midnight PHT on DAY_B = 16:00 UTC on DAY_A
        self._order = _order_at_pht(DAY_B, 0, 0, total=Decimal('200.00'))

    def test_midnight_order_in_day_b_report(self):
        resp = _reports(self.client, DAY_B, DAY_B)
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_midnight_order_not_in_day_a_report(self):
        resp = _reports(self.client, DAY_A, DAY_A)
        self.assertEqual(resp.context['total_revenue'], 0)
        self.assertEqual(resp.context['total_orders'],  0)

    def test_midnight_order_daily_entry_on_day_b(self):
        resp   = _reports(self.client, DAY_B, DAY_B)
        daily  = resp.context['daily_sales']
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(daily[0]['total'], 200.0, places=2)
        self.assertEqual(daily[0]['count'], 1)

    def test_midnight_agrees_with_finance(self):
        cash, count = _get_cash_sales_for_date(DAY_B)
        resp = _reports(self.client, DAY_B, DAY_B)
        self.assertEqual(resp.context['total_revenue'], cash)
        self.assertEqual(resp.context['total_orders'],  count)


# ── 2. 00:01 PHT appears in the correct day ───────────────────────────────────

class OneMinuteAfterMidnightTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_0001')
        self.client = Client()
        self.client.login(username='admin_0001', password='pass123')
        _order_at_pht(DAY_B, 0, 1, total=Decimal('150.00'))

    def test_0001_in_day_b(self):
        resp = _reports(self.client, DAY_B, DAY_B)
        self.assertEqual(resp.context['total_revenue'], Decimal('150.00'))

    def test_0001_not_in_day_a(self):
        resp = _reports(self.client, DAY_A, DAY_A)
        self.assertEqual(resp.context['total_revenue'], 0)


# ── 3. 23:59 PHT is the last moment of its day ────────────────────────────────

class LastMinuteOfDayTest(TestCase):
    """23:59 PHT on Day A stays on Day A, not Day B."""

    def setUp(self):
        self.admin = _user('admin_2359')
        self.client = Client()
        self.client.login(username='admin_2359', password='pass123')
        _order_at_pht(DAY_A, 23, 59, total=Decimal('300.00'))

    def test_2359_in_day_a(self):
        resp = _reports(self.client, DAY_A, DAY_A)
        self.assertEqual(resp.context['total_revenue'], Decimal('300.00'))

    def test_2359_not_in_day_b(self):
        resp = _reports(self.client, DAY_B, DAY_B)
        self.assertEqual(resp.context['total_revenue'], 0)


# ── 4. 07:59 PHT — the critical old-UTC-bug boundary ─────────────────────────

class CriticalBoundaryTest(TestCase):
    """
    07:59 PHT = 23:59 UTC Day A.
    The old timezone.now().date() bug would have returned Day A for this order.
    After the fix (timezone.localdate()), it must be in Day B.
    """

    def setUp(self):
        self.admin = _user('admin_0759')
        self.client = Client()
        self.client.login(username='admin_0759', password='pass123')
        _order_at_pht(DAY_B, 7, 59, total=Decimal('250.00'))

    def test_0759_in_day_b(self):
        resp = _reports(self.client, DAY_B, DAY_B)
        self.assertEqual(resp.context['total_revenue'], Decimal('250.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_0759_not_in_day_a(self):
        resp = _reports(self.client, DAY_A, DAY_A)
        self.assertEqual(resp.context['total_revenue'], 0)

    def test_full_0000_to_0759_range_all_on_day_b(self):
        """All orders from 00:00 to 07:59 PHT belong to Day B."""
        for h in range(8):  # 00, 01, ..., 07
            _order_at_pht(DAY_B, h, 0, total=Decimal('100.00'))
        resp = _reports(self.client, DAY_B, DAY_B)
        # 1 existing (07:59) + 8 new (00:00–07:00) = 9 orders
        self.assertEqual(resp.context['total_orders'], 9)
        # Nothing on Day A
        resp_a = _reports(self.client, DAY_A, DAY_A)
        self.assertEqual(resp_a.context['total_orders'], 0)


# ── 5. One-day range is inclusive on both ends ────────────────────────────────

class OneDayRangeInclusiveTest(TestCase):
    """start=end=D must capture ALL orders on day D (00:00–23:59 PHT)."""

    def setUp(self):
        self.admin = _user('admin_oneday')
        self.client = Client()
        self.client.login(username='admin_oneday', password='pass123')
        _order_at_pht(DAY_B,  0,  0, total=Decimal('100.00'))   # midnight
        _order_at_pht(DAY_B, 12,  0, total=Decimal('200.00'))   # midday
        _order_at_pht(DAY_B, 23, 59, total=Decimal('150.00'))   # last minute

    def test_all_three_in_one_day_range(self):
        resp = _reports(self.client, DAY_B, DAY_B)
        self.assertEqual(resp.context['total_revenue'], Decimal('450.00'))
        self.assertEqual(resp.context['total_orders'],  3)

    def test_adjacent_days_excluded(self):
        _order_at_pht(DAY_A, 23, 59, total=Decimal('999.00'))  # Day A last minute
        _order_at_pht(DAY_C,  0,  0, total=Decimal('999.00'))  # Day C midnight
        resp = _reports(self.client, DAY_B, DAY_B)
        # Still exactly the three Day B orders
        self.assertEqual(resp.context['total_revenue'], Decimal('450.00'))
        self.assertEqual(resp.context['total_orders'],  3)


# ── 6. Multi-day range — correct segregation ─────────────────────────────────

class MultiDayRangeTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_multi')
        self.client = Client()
        self.client.login(username='admin_multi', password='pass123')
        _order_at_pht(DAY_A, 23, 59, total=Decimal('100.00'))
        _order_at_pht(DAY_B,  0,  0, total=Decimal('200.00'))
        _order_at_pht(DAY_B, 23, 59, total=Decimal('300.00'))
        _order_at_pht(DAY_C,  0,  0, total=Decimal('400.00'))

    def test_two_day_range_day_b_to_c(self):
        resp = _reports(self.client, DAY_B, DAY_C)
        self.assertEqual(resp.context['total_revenue'], Decimal('900.00'))   # 200+300+400
        self.assertEqual(resp.context['total_orders'],  3)

    def test_three_day_range_a_to_c(self):
        resp = _reports(self.client, DAY_A, DAY_C)
        self.assertEqual(resp.context['total_revenue'], Decimal('1000.00'))  # all four
        self.assertEqual(resp.context['total_orders'],  4)

    def test_day_a_only(self):
        resp = _reports(self.client, DAY_A, DAY_A)
        self.assertEqual(resp.context['total_revenue'], Decimal('100.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_daily_sales_list_length_equals_range_days(self):
        resp = _reports(self.client, DAY_A, DAY_C)
        self.assertEqual(len(resp.context['daily_sales']), 3)


# ── 7. Consecutive dates — no boundary bleed ─────────────────────────────────

class ConsecutiveDateBoundaryTest(TestCase):
    """
    23:59 PHT Day A and 00:00 PHT Day B are different business dates.
    Neither must bleed into the other's report.
    """

    def setUp(self):
        self.admin = _user('admin_consec')
        self.client = Client()
        self.client.login(username='admin_consec', password='pass123')
        _order_at_pht(DAY_A, 23, 59, total=Decimal('500.00'))   # Day A last second
        _order_at_pht(DAY_B,  0,  0, total=Decimal('300.00'))   # Day B first instant

    def test_day_a_total_correct(self):
        resp = _reports(self.client, DAY_A, DAY_A)
        self.assertEqual(resp.context['total_revenue'], Decimal('500.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_day_b_total_correct(self):
        resp = _reports(self.client, DAY_B, DAY_B)
        self.assertEqual(resp.context['total_revenue'], Decimal('300.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_no_double_counting_across_days(self):
        """Total of per-day reports must equal the combined range total."""
        resp_a = _reports(self.client, DAY_A, DAY_A)
        resp_b = _reports(self.client, DAY_B, DAY_B)
        resp_ab = _reports(self.client, DAY_A, DAY_B)
        day_a_rev = resp_a.context['total_revenue']
        day_b_rev = resp_b.context['total_revenue']
        combined  = resp_ab.context['total_revenue']
        self.assertEqual(day_a_rev + day_b_rev, combined)

    def test_consecutive_agrees_with_finance(self):
        cash_a, _ = _get_cash_sales_for_date(DAY_A)
        cash_b, _ = _get_cash_sales_for_date(DAY_B)
        resp_a = _reports(self.client, DAY_A, DAY_A)
        resp_b = _reports(self.client, DAY_B, DAY_B)
        self.assertEqual(resp_a.context['total_revenue'], cash_a)
        self.assertEqual(resp_b.context['total_revenue'], cash_b)


# ── 8. No-match range returns zero ───────────────────────────────────────────

class NoMatchRangeTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_nomatch')
        self.client = Client()
        self.client.login(username='admin_nomatch', password='pass123')
        # Order on Day B — filter for Day A should return nothing
        _order_at_pht(DAY_B, 12, 0, total=Decimal('500.00'))

    def test_empty_range_returns_zero(self):
        resp = _reports(self.client, DAY_A, DAY_A)
        self.assertEqual(resp.context['total_revenue'], 0)
        self.assertEqual(resp.context['total_orders'],  0)
        self.assertFalse(resp.context['has_any_sales'])


# ── 9. TruncDate groups by PHT date, not UTC date ────────────────────────────

class TruncDateTimezoneTest(TestCase):
    """
    TruncDate with USE_TZ=True and TIME_ZONE='Asia/Manila' must group
    midnight-PHT orders under the PHT date, not the UTC date.
    """

    def setUp(self):
        self.admin = _user('admin_trunc')
        self.client = Client()
        self.client.login(username='admin_trunc', password='pass123')
        # Two Day B orders at different PHT times
        _order_at_pht(DAY_B, 0, 0,   total=Decimal('100.00'))   # 16:00 UTC Day A
        _order_at_pht(DAY_B, 12, 0,  total=Decimal('200.00'))   # 04:00 UTC Day B
        _order_at_pht(DAY_B, 23, 59, total=Decimal('150.00'))   # 15:59 UTC Day B

    def test_all_three_grouped_on_day_b(self):
        resp  = _reports(self.client, DAY_B, DAY_B)
        daily = resp.context['daily_sales']
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(daily[0]['total'], 450.0, places=2)
        self.assertEqual(daily[0]['count'], 3)

    def test_day_a_chart_entry_is_zero(self):
        resp  = _reports(self.client, DAY_A, DAY_B)
        daily = resp.context['daily_sales']
        # daily[0] = Day A, daily[1] = Day B
        day_a_label = DAY_A.strftime('%b %d')
        day_b_label = DAY_B.strftime('%b %d')
        entry_a = next(d for d in daily if d['date'] == day_a_label)
        entry_b = next(d for d in daily if d['date'] == day_b_label)
        self.assertEqual(entry_a['count'], 0)
        self.assertEqual(entry_b['count'], 3)


# ── 10. Default range uses PHT localdate() ───────────────────────────────────

class DefaultRangeTest(TestCase):
    """
    The default range is (today-30days, today) using timezone.localdate().
    Verify it returns the correct PHT today.
    """

    def setUp(self):
        self.admin = _user('admin_default')
        self.client = Client()
        self.client.login(username='admin_default', password='pass123')

    def test_default_range_returns_200(self):
        resp = self.client.get(reverse('reports:index'))
        self.assertEqual(resp.status_code, 200)
        # end_date in context must equal PHT today
        self.assertEqual(
            resp.context['end_date'],
            timezone.localdate().isoformat(),
        )

    def test_default_start_is_30_days_before_pht_today(self):
        resp = self.client.get(reverse('reports:index'))
        expected_start = (timezone.localdate() - datetime.timedelta(days=30)).isoformat()
        self.assertEqual(resp.context['start_date'], expected_start)


# ── 11. Cross-module: Reports agrees with Finance per boundary ────────────────

class FinanceAgreementAtBoundaryTest(TestCase):
    """
    For each boundary timestamp Finance._get_cash_sales_for_date(D) must equal
    the Reports daily total for day D.
    """

    def setUp(self):
        self.admin = _user('admin_finance_agree')
        self.client = Client()
        self.client.login(username='admin_finance_agree', password='pass123')

    def _assert_reports_equals_finance(self, pht_date, hour, minute):
        _order_at_pht(pht_date, hour, minute, total=Decimal('200.00'))
        cash, _ = _get_cash_sales_for_date(pht_date)
        resp    = _reports(self.client, pht_date, pht_date)
        self.assertEqual(
            resp.context['total_revenue'], cash,
            msg=f"Reports≠Finance at {pht_date} {hour:02d}:{minute:02d} PHT",
        )
        # cleanup for next assertion
        Order.objects.all().update(status='cancelled', is_paid=False)

    def test_midnight_agrees(self):
        self._assert_reports_equals_finance(DAY_B, 0, 0)

    def test_0001_agrees(self):
        self._assert_reports_equals_finance(DAY_B, 0, 1)

    def test_0759_agrees(self):
        self._assert_reports_equals_finance(DAY_B, 7, 59)

    def test_0800_agrees(self):
        self._assert_reports_equals_finance(DAY_B, 8, 0)

    def test_1200_agrees(self):
        self._assert_reports_equals_finance(DAY_B, 12, 0)

    def test_2359_agrees(self):
        self._assert_reports_equals_finance(DAY_A, 23, 59)


# ── 12. Cross-module: Reports daily agrees with Dashboard daily ───────────────

class DashboardAgreementTest(TestCase):
    """
    For orders placed today (PHT), Dashboard daily_sales and Reports
    total_revenue for a one-day range (TODAY, TODAY) must be equal.
    """

    def setUp(self):
        self.admin = _user('admin_dash_agree')
        self.client = Client()
        self.client.login(username='admin_dash_agree', password='pass123')
        # Place orders on today's PHT date
        today = timezone.localdate()
        _order_at_pht(today, 0, 30, total=Decimal('300.00'))
        _order_at_pht(today, 12, 0,  total=Decimal('200.00'))

    def test_reports_daily_equals_dashboard_daily(self):
        from apps.dashboard.views import _sales_stats
        today   = timezone.localdate()
        stats   = _sales_stats()
        resp    = _reports(self.client, today, today)
        self.assertEqual(
            resp.context['total_revenue'],
            stats['daily_sales'],
        )

    def test_order_count_agrees(self):
        from apps.dashboard.views import _sales_stats
        today  = timezone.localdate()
        stats  = _sales_stats()
        resp   = _reports(self.client, today, today)
        self.assertEqual(resp.context['total_orders'], stats['daily_orders'])


# ── 13. Excel timestamp uses PHT, not UTC ────────────────────────────────────

class ExcelTimezoneTest(TestCase):
    """
    Regression test for the confirmed bug: export_excel used
    order.created_at.strftime() which formatted the raw UTC datetime.
    After the fix, timezone.localtime(order.created_at).strftime() is used
    so the Excel timestamp matches the PHT business date shown in the filter.
    """

    def setUp(self):
        self.admin = _user('admin_excel')
        self.client = Client()
        self.client.login(username='admin_excel', password='pass123')
        # Order at 00:30 PHT Day B = 16:30 UTC Day A
        self._order = _order_at_pht(DAY_B, 0, 30, total=Decimal('100.00'))

    def test_excel_export_returns_200(self):
        try:
            import openpyxl  # noqa: F401 — skip if not installed
        except ImportError:
            self.skipTest('openpyxl not installed')
        resp = self.client.get(
            reverse('reports:export_excel'),
            {'start': str(DAY_B), 'end': str(DAY_B)},
        )
        self.assertEqual(resp.status_code, 200)

    def test_excel_timestamp_is_pht_not_utc(self):
        """
        The Excel "Date" column for a 00:30 PHT order must show the
        DAY_B date (PHT), not the DAY_A date (UTC).
        We verify by checking that the PHT conversion is called in the view
        rather than raw strftime — confirmed by inspecting the fix via the
        Finance module's timezone.localtime() call.
        """
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl not installed')

        resp = self.client.get(
            reverse('reports:export_excel'),
            {'start': str(DAY_B), 'end': str(DAY_B)},
        )
        self.assertEqual(resp.status_code, 200)

        # Parse the Excel response to check the date cell
        import io
        wb = openpyxl.load_workbook(io.BytesIO(b''.join(resp.streaming_content
                                     if hasattr(resp, 'streaming_content')
                                     else [resp.content])))
        ws = wb.active
        # Row 2 col 2 = Date column for the first data row
        date_cell = ws.cell(row=2, column=2).value
        self.assertIsNotNone(date_cell, "Date cell is empty")
        # Must show DAY_B date string (PHT), not DAY_A (UTC)
        day_b_str = str(DAY_B)   # '2026-08-28'
        day_a_str = str(DAY_A)   # '2026-08-27'
        self.assertIn(day_b_str, str(date_cell),
            f"Excel date shows {date_cell!r} — expected PHT date {day_b_str}, "
            f"not UTC date {day_a_str}")
