"""
Timezone and date boundary tests — Kape De Manubag.

Philippines Standard Time (PST) = UTC+8, no DST.

KEY INVARIANT:
  Django stores all datetimes as UTC.  With USE_TZ=True and
  TIME_ZONE='Asia/Manila', every __date ORM lookup converts the stored
  UTC value to PHT before extracting the date.  A 00:30 PHT order is
  stored as 16:30 UTC the *previous* calendar day, yet the __date filter
  correctly assigns it to the PHT date — the Philippine business date.

  timezone.localdate() returns the current *local* (PHT) date.
  The old timezone.now().date() returned the UTC date, which was one day
  behind PHT for the first eight hours of every local day.

BOUNDARY TIMESTAMPS (all PHT → UTC):
  23:59 PHT  =  15:59 UTC same day     ← last minute of business day
  00:00 PHT  =  16:00 UTC previous day ← midnight, first of new day
  00:30 PHT  =  16:30 UTC previous day ← 30 min into new day
  07:59 PHT  =  23:59 UTC previous day ← last minute where old bug fired
  08:00 PHT  =  00:00 UTC same day     ← both UTC and PHT now agree
  12:00 PHT  =  04:00 UTC same day     ← midday, never ambiguous

Scenarios:
  1.  Order at 23:59 PHT → business date = same PHT day
  2.  Order at 00:00 PHT → business date = NEXT PHT day
  3.  Order at 00:30 PHT → business date = next PHT day
  4.  Order at 07:59 PHT → business date = next PHT day (old bug: UTC yesterday)
  5.  Finance __date filter respects PHT at every boundary
  6.  Finance cash_sales correctly spans 00:00-23:59 PHT
  7.  Dashboard daily_sales uses localdate (PHT) boundary
  8.  Reports date filter consistent with Finance
  9.  Consecutive business days: orders on Day N and Day N+1 segregated
  10. localdate() regression: no occurrences of now().date() remain
"""

import datetime
from decimal import Decimal

from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DailyFinance
from apps.finance.views import _get_cash_sales_for_date
from apps.orders.models import Order

User = get_user_model()

PHT = ZoneInfo('Asia/Manila')

# ── PHT reference dates used across tests ────────────────────────────────────
# Day A: 2026-08-27 PHT
# Day B: 2026-08-28 PHT  (the "business today")
# Day C: 2026-08-29 PHT

DAY_A = datetime.date(2026, 8, 27)   # PHT
DAY_B = datetime.date(2026, 8, 28)   # PHT
DAY_C = datetime.date(2026, 8, 29)   # PHT


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pht(date, hour, minute=0):
    """Return a timezone-aware UTC datetime for a given PHT date + time."""
    naive = datetime.datetime(date.year, date.month, date.day, hour, minute)
    aware_pht = naive.replace(tzinfo=PHT)
    return aware_pht.astimezone(datetime.timezone.utc)


def _order_at(pht_date, hour, minute=0, total=Decimal('100.00'),
              payment_method='cash', status='completed', is_paid=True):
    """Create an Order whose created_at is the exact UTC equivalent of the
    supplied PHT date + time."""
    utc_dt = _pht(pht_date, hour, minute)
    o = Order.objects.create(
        customer_name='Test',
        status=status,
        is_paid=is_paid,
        payment_method=payment_method,
        total=total,
        subtotal=total,
    )
    Order.objects.filter(pk=o.pk).update(created_at=utc_dt)
    o.refresh_from_db()
    return o


def _finance(date, previous_coh=Decimal('1000.00'), **kwargs):
    return DailyFinance.objects.create(
        date=date, previous_coh=previous_coh, **kwargs
    )


def _user(username, role='cashier'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


# ── 1. Order at 23:59 PHT belongs to Day A ───────────────────────────────────

class EndOfDayBoundaryTest(TestCase):
    """23:59 PHT is the last minute of the business day — must stay on Day A."""

    def test_2359_pht_belongs_to_day_a(self):
        o = _order_at(DAY_A, 23, 59, total=Decimal('200.00'))
        # created_at stored as 15:59 UTC on Day A
        total, count = _get_cash_sales_for_date(DAY_A)
        self.assertEqual(total, Decimal('200.00'))
        self.assertEqual(count, 1)

    def test_2359_pht_not_on_day_b(self):
        _order_at(DAY_A, 23, 59, total=Decimal('200.00'))
        total, count = _get_cash_sales_for_date(DAY_B)
        self.assertEqual(total, Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_2359_pht_finance_model(self):
        _order_at(DAY_A, 23, 59, total=Decimal('200.00'))
        rec = _finance(DAY_A, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.cash_sales, Decimal('200.00'))
        self.assertEqual(rec.running_total, Decimal('1200.00'))


# ── 2. Order at 00:00 PHT belongs to Day B ───────────────────────────────────

class MidnightBoundaryTest(TestCase):
    """
    00:00 PHT midnight is the FIRST moment of a new business day.
    UTC equivalent: 16:00 UTC on Day A.
    The __date lookup must resolve this to Day B (PHT), not Day A (UTC).
    """

    def test_midnight_pht_belongs_to_day_b(self):
        o = _order_at(DAY_B, 0, 0, total=Decimal('150.00'))
        # stored as 16:00 UTC on Day A
        # must appear on Day B (PHT)
        total, count = _get_cash_sales_for_date(DAY_B)
        self.assertEqual(total, Decimal('150.00'))
        self.assertEqual(count, 1)

    def test_midnight_pht_not_on_day_a(self):
        _order_at(DAY_B, 0, 0, total=Decimal('150.00'))
        total, count = _get_cash_sales_for_date(DAY_A)
        self.assertEqual(total, Decimal('0.00'))

    def test_midnight_pht_finance_model(self):
        _order_at(DAY_B, 0, 0, total=Decimal('150.00'))
        rec = _finance(DAY_B, previous_coh=Decimal('500.00'))
        self.assertEqual(rec.cash_sales, Decimal('150.00'))


# ── 3. Order at 00:30 PHT belongs to Day B ───────────────────────────────────

class EarlyMorningBoundaryTest(TestCase):
    """
    00:30 PHT → 16:30 UTC Day A.
    This is the scenario most likely to expose the old UTC date bug:
    timezone.now().date() on the server at this moment returns Day A (UTC),
    but the business is operating on Day B (PHT).
    """

    def test_0030_pht_belongs_to_day_b(self):
        _order_at(DAY_B, 0, 30, total=Decimal('300.00'))
        total, count = _get_cash_sales_for_date(DAY_B)
        self.assertEqual(total, Decimal('300.00'))
        self.assertEqual(count, 1)

    def test_0030_pht_not_on_day_a(self):
        _order_at(DAY_B, 0, 30, total=Decimal('300.00'))
        total, count = _get_cash_sales_for_date(DAY_A)
        self.assertEqual(total, Decimal('0.00'))

    def test_0030_pht_finance_model_uses_correct_date(self):
        """Finance record for Day B correctly picks up the 00:30 order."""
        _order_at(DAY_B, 0, 30, total=Decimal('300.00'))
        rec_b = _finance(DAY_B, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec_b.cash_sales, Decimal('300.00'))

        # Day A Finance must have zero sales
        rec_a = _finance(DAY_A, previous_coh=Decimal('900.00'))
        self.assertEqual(rec_a.cash_sales, Decimal('0.00'))


# ── 4. Order at 07:59 PHT — the critical old-bug boundary ────────────────────

class CriticalBoundaryTest(TestCase):
    """
    07:59 PHT = 23:59 UTC on Day A.
    Under the old bug (timezone.now().date() returning UTC), the server
    would have computed 'today' as Day A during the 00:00-07:59 PHT window.
    Orders at 07:59 PHT MUST still belong to Day B (PHT).
    """

    def test_0759_pht_belongs_to_day_b(self):
        _order_at(DAY_B, 7, 59, total=Decimal('250.00'))
        total, count = _get_cash_sales_for_date(DAY_B)
        self.assertEqual(total, Decimal('250.00'))
        self.assertEqual(count, 1)

    def test_0759_pht_not_on_day_a(self):
        _order_at(DAY_B, 7, 59, total=Decimal('250.00'))
        total, count = _get_cash_sales_for_date(DAY_A)
        self.assertEqual(total, Decimal('0.00'))

    def test_full_early_morning_range_all_on_day_b(self):
        """All orders from 00:00–07:59 PHT must land on Day B."""
        for hour in [0, 1, 2, 3, 4, 5, 6, 7]:
            _order_at(DAY_B, hour, 0, total=Decimal('100.00'))
        total, count = _get_cash_sales_for_date(DAY_B)
        self.assertEqual(total, Decimal('800.00'))   # 8 × 100
        self.assertEqual(count, 8)
        # Nothing on Day A
        total_a, _ = _get_cash_sales_for_date(DAY_A)
        self.assertEqual(total_a, Decimal('0.00'))

    def test_0800_pht_also_on_day_b(self):
        """08:00 PHT = 00:00 UTC Day B — both UTC and PHT agree here."""
        _order_at(DAY_B, 8, 0, total=Decimal('100.00'))
        total, _ = _get_cash_sales_for_date(DAY_B)
        self.assertEqual(total, Decimal('100.00'))


# ── 5. Full business day boundary (00:00–23:59 PHT) ──────────────────────────

class FullDayBoundaryTest(TestCase):
    """
    Orders across the entire PHT business day must all land on Day B.
    No order must bleed onto Day A or Day C.
    """

    def test_full_business_day_contained_on_day_b(self):
        # Boundary points across the full PHT day
        times = [(0, 0), (0, 30), (7, 59), (8, 0), (12, 0), (18, 0), (23, 59)]
        for h, m in times:
            _order_at(DAY_B, h, m, total=Decimal('100.00'))

        b_total, b_count = _get_cash_sales_for_date(DAY_B)
        a_total, _       = _get_cash_sales_for_date(DAY_A)
        c_total, _       = _get_cash_sales_for_date(DAY_C)

        self.assertEqual(b_total, Decimal('700.00'))  # 7 × 100
        self.assertEqual(b_count, 7)
        self.assertEqual(a_total, Decimal('0.00'), "No Day B orders on Day A")
        self.assertEqual(c_total, Decimal('0.00'), "No Day B orders on Day C")


# ── 6. Consecutive business days — segregation ───────────────────────────────

class ConsecutiveDaysTest(TestCase):
    """
    Orders on Day A (end-of-day) and Day B (start-of-day) must stay on
    their respective PHT dates.
    """

    def test_day_boundary_segregation(self):
        """23:59 PHT Day A and 00:00 PHT Day B are different business dates."""
        _order_at(DAY_A, 23, 59, total=Decimal('500.00'))  # Day A last minute
        _order_at(DAY_B, 0,  0,  total=Decimal('300.00'))  # Day B first minute

        a_total, a_count = _get_cash_sales_for_date(DAY_A)
        b_total, b_count = _get_cash_sales_for_date(DAY_B)

        self.assertEqual(a_total, Decimal('500.00'))
        self.assertEqual(a_count, 1)
        self.assertEqual(b_total, Decimal('300.00'))
        self.assertEqual(b_count, 1)

    def test_coh_chain_across_midnight(self):
        """
        Day A ending_coh carries forward to Day B previous_coh suggestion.
        Orders at end of Day A and start of Day B are on separate Finance records.
        """
        from apps.finance.views import _get_previous_coh_info

        _order_at(DAY_A, 23, 59, total=Decimal('500.00'))
        _order_at(DAY_B, 0, 30,  total=Decimal('300.00'))

        rec_a = _finance(DAY_A, previous_coh=Decimal('1000.00'),
                         expenses=Decimal('200.00'))
        # 1000 + 500 - 200 = 1300
        self.assertEqual(rec_a.cash_sales, Decimal('500.00'))
        self.assertEqual(rec_a.ending_coh, Decimal('1300.00'))

        # Day B's suggested previous_coh must be Day A's ending_coh
        suggested, _, is_auto = _get_previous_coh_info(DAY_B)
        self.assertEqual(suggested, Decimal('1300.00'))
        self.assertTrue(is_auto)

        # Day B Finance correctly counts the 00:30 order
        rec_b = _finance(DAY_B, previous_coh=suggested)
        self.assertEqual(rec_b.cash_sales, Decimal('300.00'))
        self.assertEqual(rec_b.running_total, Decimal('1600.00'))

    def test_three_consecutive_days_fully_segregated(self):
        """Day A, B, C each have orders at various hours — none overlap."""
        _order_at(DAY_A, 23, 59, total=Decimal('100.00'))
        _order_at(DAY_B, 0,  0,  total=Decimal('200.00'))
        _order_at(DAY_B, 23, 59, total=Decimal('300.00'))
        _order_at(DAY_C, 0,  0,  total=Decimal('400.00'))

        a_total, _ = _get_cash_sales_for_date(DAY_A)
        b_total, _ = _get_cash_sales_for_date(DAY_B)
        c_total, _ = _get_cash_sales_for_date(DAY_C)

        self.assertEqual(a_total, Decimal('100.00'))
        self.assertEqual(b_total, Decimal('500.00'))   # 200 + 300
        self.assertEqual(c_total, Decimal('400.00'))


# ── 7. Dashboard uses PHT date boundary ──────────────────────────────────────

class DashboardDateBoundaryTest(TestCase):
    """
    Dashboard daily_sales must agree with Finance cash_sales for the same
    PHT business date, including orders before 08:00 PHT.
    Uses mock_today to freeze the local date to DAY_B.
    """

    def test_dashboard_daily_sales_includes_early_morning_pht_orders(self):
        """
        Order at 00:30 PHT Day B is a Day B order.
        Dashboard daily_sales must include it when querying for DAY_B.
        """
        from apps.dashboard.views import _sales_stats
        from unittest.mock import patch

        _order_at(DAY_B, 0, 30, total=Decimal('350.00'))
        _order_at(DAY_B, 12, 0, total=Decimal('150.00'))

        # Freeze localdate() to DAY_B so _sales_stats picks up Day B orders
        with patch('apps.dashboard.views.timezone.localdate', return_value=DAY_B):
            stats = _sales_stats()

        self.assertEqual(stats['daily_sales'],  Decimal('500.00'))
        self.assertEqual(stats['daily_orders'], 2)

    def test_dashboard_daily_excludes_day_a_orders(self):
        """Day A orders must not bleed into Day B daily stats."""
        from apps.dashboard.views import _sales_stats
        from unittest.mock import patch

        _order_at(DAY_A, 23, 59, total=Decimal('999.00'))   # Day A
        _order_at(DAY_B, 12, 0,  total=Decimal('200.00'))   # Day B

        with patch('apps.dashboard.views.timezone.localdate', return_value=DAY_B):
            stats = _sales_stats()

        self.assertEqual(stats['daily_sales'],  Decimal('200.00'))
        self.assertEqual(stats['daily_orders'], 1)


# ── 8. Finance form clean_date uses PHT date ──────────────────────────────────

class FormDateValidationTest(TestCase):
    """
    The Finance form clean_date validator must use localdate() (PHT).
    Before the fix, it used timezone.now().date() (UTC), which would reject
    a valid PHT today during the 00:00-07:59 PHT window.
    """

    def test_form_accepts_pht_today(self):
        """The form must accept a date equal to the current PHT date."""
        from apps.finance.forms import DailyFinanceForm
        from unittest.mock import patch

        with patch('apps.finance.forms.timezone.localdate', return_value=DAY_B):
            form = DailyFinanceForm(data={
                'date':           str(DAY_B),
                'previous_coh':   '1000.00',
                'expenses':       '0.00',
                'expenses_notes': '',
                'gcash_payments': '0.00',
                'coins':          '0.00',
                'cash_advance':   '0.00',
                'floating_cash':  '0.00',
            })
            self.assertTrue(form.is_valid(), form.errors)

    def test_form_rejects_pht_tomorrow(self):
        """The form must reject a date after the current PHT date."""
        from apps.finance.forms import DailyFinanceForm
        from unittest.mock import patch

        with patch('apps.finance.forms.timezone.localdate', return_value=DAY_B):
            form = DailyFinanceForm(data={
                'date':           str(DAY_C),   # DAY_C = tomorrow PHT
                'previous_coh':   '1000.00',
                'expenses':       '0.00',
                'expenses_notes': '',
                'gcash_payments': '0.00',
                'coins':          '0.00',
                'cash_advance':   '0.00',
                'floating_cash':  '0.00',
            })
            self.assertFalse(form.is_valid())
            self.assertIn('date', form.errors)

    def test_form_accepts_yesterday_pht(self):
        """The form must accept past dates."""
        from apps.finance.forms import DailyFinanceForm
        from unittest.mock import patch

        with patch('apps.finance.forms.timezone.localdate', return_value=DAY_B):
            form = DailyFinanceForm(data={
                'date':           str(DAY_A),
                'previous_coh':   '1000.00',
                'expenses':       '0.00',
                'expenses_notes': '',
                'gcash_payments': '0.00',
                'coins':          '0.00',
                'cash_advance':   '0.00',
                'floating_cash':  '0.00',
            })
            self.assertTrue(form.is_valid(), form.errors)


# ── 9. Finance and Reports agree on date boundaries ──────────────────────────

class FinanceReportsConsistencyTest(TestCase):
    """
    Finance cash_sales and Reports total_revenue must agree for the same PHT
    business date, including orders that straddle the UTC midnight.
    """

    def setUp(self):
        self.admin = _user('admin_tz', role='admin')
        self.client = Client()
        self.client.login(username='admin_tz', password='pass123')

    def test_reports_agrees_with_finance_for_midnight_orders(self):
        """
        Orders at 23:59 Day A and 00:00 Day B are on different business dates.
        Reports filtered to DAY_B must match Finance cash_sales for DAY_B.
        """
        _order_at(DAY_A, 23, 59, total=Decimal('500.00'))   # Day A
        _order_at(DAY_B, 0,  0,  total=Decimal('300.00'))   # Day B
        _order_at(DAY_B, 12, 0,  total=Decimal('200.00'))   # Day B

        # Finance cash_sales for Day B
        finance_cash, _ = _get_cash_sales_for_date(DAY_B)
        self.assertEqual(finance_cash, Decimal('500.00'))  # 300 + 200

        # Reports for Day B
        resp = self.client.get(
            reverse('reports:index'),
            {'start': str(DAY_B), 'end': str(DAY_B)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total_revenue'], Decimal('500.00'))
        self.assertEqual(resp.context['total_orders'],  2)

    def test_reports_day_a_excludes_midnight_orders(self):
        """Reports for Day A must not include the 00:00 Day B order."""
        _order_at(DAY_A, 23, 59, total=Decimal('500.00'))   # Day A
        _order_at(DAY_B, 0,  0,  total=Decimal('300.00'))   # Day B

        resp = self.client.get(
            reverse('reports:index'),
            {'start': str(DAY_A), 'end': str(DAY_A)},
        )
        self.assertEqual(resp.context['total_revenue'], Decimal('500.00'))
        self.assertEqual(resp.context['total_orders'],  1)


# ── 10. localdate() regression — no now().date() in production ───────────────

class LocaldateRegressionTest(TestCase):
    """
    Verify that no production .py file (outside test files) calls
    timezone.now().date() — every occurrence must have been replaced with
    timezone.localdate().
    """

    def test_no_timezone_now_date_in_production_files(self):
        import os
        import re

        apps_root = os.path.join(
            os.path.dirname(__file__),  # apps/finance/
            '..', '..',                  # project root
            'apps',
        )
        apps_root = os.path.normpath(apps_root)

        pattern = re.compile(r'timezone\.now\(\)\.date\(\)')
        violations = []

        for dirpath, dirnames, filenames in os.walk(apps_root):
            # Skip test files and __pycache__
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            for fname in filenames:
                if not fname.endswith('.py'):
                    continue
                if fname.startswith('test'):
                    continue
                filepath = os.path.join(dirpath, fname)
                with open(filepath, encoding='utf-8', errors='ignore') as f:
                    for lineno, line in enumerate(f, 1):
                        if pattern.search(line):
                            violations.append(f'{filepath}:{lineno}: {line.rstrip()}')

        self.assertEqual(
            violations, [],
            'Found timezone.now().date() in production files '
            '(should be timezone.localdate()):\n' + '\n'.join(violations),
        )

    def test_localdate_returns_pht_not_utc(self):
        """
        timezone.localdate() must match the PHT date of the current UTC moment.
        """
        utc_now  = timezone.now()
        pht_now  = timezone.localtime(utc_now)   # uses settings.TIME_ZONE = Asia/Manila
        expected = pht_now.date()
        self.assertEqual(timezone.localdate(), expected)
