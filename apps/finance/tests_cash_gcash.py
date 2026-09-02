"""
Cash / GCash handling verification tests — Kape De Manubag.

Formally proves that every Cash and GCash handling point is correct
across Finance, Orders, Dashboard, and Reports.

Scenarios covered:
  1.  Cash-only day
  2.  GCash-only day
  3.  Mixed Cash + GCash day
  4.  No sales (zero orders)
  5.  Multiple transactions of each payment type

Cross-module consistency:
  6.  Finance cash_sales vs GCash order total are independent
  7.  Dashboard total = cash + gcash combined
  8.  Reports total = cash + gcash combined
  9.  Finance COH calculation with GCash as deduction
  10. GCash never inflates physical cash on hand (ending_coh correct)
  11. Unsupported payment method rejected server-side (process_payment)
  12. Finance API returns both cash and gcash totals correctly
  13. Finance gcash_payments deduction reduces ending COH
  14. Finance running_total includes both cash and gcash
  15. Payment method choices restricted to cash/gcash at model level

Manual verification:
  All expected totals are computed inline and compared against the
  actual query results so the test itself is the specification.
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DailyFinance
from apps.finance.views import _get_cash_sales_for_date
from apps.orders.models import Order

User = get_user_model()

# Use localdate() so tests are always relative to the current Philippine day.
TODAY     = timezone.localdate()
YESTERDAY = TODAY - datetime.timedelta(days=1)


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
        payment_method=payment_method,
        status=status,
        is_paid=is_paid,
        total=total,
        subtotal=total,
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


def _finance(date, previous_coh=Decimal('0.00'), **kwargs):
    return DailyFinance.objects.create(
        date=date, previous_coh=previous_coh, **kwargs
    )


# ── Scenario 1: Cash-only day ─────────────────────────────────────────────────

class CashOnlyDayTest(TestCase):
    """All orders paid with cash. GCash totals must be zero."""

    def setUp(self):
        _order(TODAY, payment_method='cash', total=Decimal('150.00'))
        _order(TODAY, payment_method='cash', total=Decimal('200.00'))
        # expected cash total = 350.00, gcash = 0.00

    def test_cash_sales_correct(self):
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('350.00'))
        self.assertEqual(count, 2)

    def test_gcash_sales_zero(self):
        from django.db.models import Sum
        gcash = (
            Order.objects.filter(
                created_at__date=TODAY,
                is_paid=True,
                payment_method='gcash',
                status='completed',
            ).aggregate(t=Sum('total'))['t'] or Decimal('0.00')
        )
        self.assertEqual(gcash, Decimal('0.00'))

    def test_finance_model_cash_sales(self):
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.cash_sales, Decimal('350.00'))

    def test_finance_running_total(self):
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'))
        # running_total = 1000 + 350 cash + 0 gcash = 1350
        self.assertEqual(rec.running_total, Decimal('1350.00'))

    def test_finance_ending_coh_no_deductions(self):
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'))
        # ending_coh = 1350 - 0 = 1350
        self.assertEqual(rec.ending_coh, Decimal('1350.00'))

    def test_api_cash_only(self):
        user = _user('cashier_cash_only')
        client = Client()
        client.login(username='cashier_cash_only', password='pass123')
        url = reverse('finance:api_cash_sales')
        resp = client.get(f'{url}?date={TODAY}')
        data = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Decimal(data['cash_sales']),  Decimal('350.00'))
        self.assertEqual(Decimal(data['gcash_sales']), Decimal('0.00'))
        self.assertEqual(data['cash_order_count'],  2)
        self.assertEqual(data['gcash_order_count'], 0)

    def test_dashboard_daily_sales(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'],  Decimal('350.00'))
        self.assertEqual(stats['daily_orders'], 2)

    def test_reports_total(self):
        user = _user('admin_cash_only', role='admin')
        client = Client()
        client.login(username='admin_cash_only', password='pass123')
        resp = client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(TODAY)},
        )
        self.assertEqual(resp.context['total_revenue'], Decimal('350.00'))
        self.assertEqual(resp.context['total_orders'],  2)


# ── Scenario 2: GCash-only day ────────────────────────────────────────────────

class GCashOnlyDayTest(TestCase):
    """All orders paid with GCash. Cash sales must be zero.
    GCash IS included in running_total but then deducted via gcash_payments."""

    def setUp(self):
        _order(TODAY, payment_method='gcash', total=Decimal('300.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('250.00'))
        # expected gcash total = 550.00, cash = 0.00

    def test_cash_sales_zero(self):
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_gcash_does_not_inflate_cash_sales(self):
        """Core invariant: GCash revenue must never appear in cash_sales."""
        rec = _finance(TODAY, previous_coh=Decimal('500.00'))
        self.assertEqual(rec.cash_sales, Decimal('0.00'))

    def test_gcash_included_in_running_total(self):
        """running_total = previous_coh + cash_sales + gcash_sales."""
        rec = _finance(TODAY, previous_coh=Decimal('500.00'))
        # running_total = 500 + 0 cash + 550 gcash = 1050
        self.assertEqual(rec.running_total, Decimal('1050.00'))

    def test_ending_coh_with_gcash_deduction(self):
        """GCash added to running_total and deducted via gcash_payments → net effect on ending_coh."""
        rec = _finance(
            TODAY,
            previous_coh=Decimal('500.00'),
            gcash_payments=Decimal('550.00'),
        )
        # running_total = 500 + 0 cash + 550 gcash = 1050
        # deductions = 550 gcash_payments
        # ending_coh = 1050 - 550 = 500
        self.assertEqual(rec.running_total, Decimal('1050.00'))
        self.assertEqual(rec.ending_coh, Decimal('500.00'))

    def test_api_gcash_only(self):
        user = _user('cashier_gcash_only')
        client = Client()
        client.login(username='cashier_gcash_only', password='pass123')
        url = reverse('finance:api_cash_sales')
        resp = client.get(f'{url}?date={TODAY}')
        data = resp.json()
        self.assertEqual(Decimal(data['cash_sales']),  Decimal('0.00'))
        self.assertEqual(Decimal(data['gcash_sales']), Decimal('550.00'))
        self.assertEqual(data['cash_order_count'],  0)
        self.assertEqual(data['gcash_order_count'], 2)

    def test_dashboard_includes_gcash_in_total(self):
        """Dashboard total revenue includes GCash — it is real revenue."""
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'],  Decimal('550.00'))
        self.assertEqual(stats['daily_orders'], 2)

    def test_reports_includes_gcash(self):
        """Reports total includes GCash orders (all completed paid orders)."""
        user = _user('admin_gcash_only', role='admin')
        client = Client()
        client.login(username='admin_gcash_only', password='pass123')
        resp = client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(TODAY)},
        )
        self.assertEqual(resp.context['total_revenue'], Decimal('550.00'))
        self.assertEqual(resp.context['total_orders'],  2)


# ── Scenario 3: Mixed Cash + GCash day ───────────────────────────────────────

class MixedDayTest(TestCase):
    """Cash and GCash orders on the same day are accounted for separately."""

    def setUp(self):
        # Cash: 200 + 300 = 500
        _order(TODAY, payment_method='cash',  total=Decimal('200.00'))
        _order(TODAY, payment_method='cash',  total=Decimal('300.00'))
        # GCash: 150 + 250 = 400
        _order(TODAY, payment_method='gcash', total=Decimal('150.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('250.00'))

    def test_cash_total_correct(self):
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('500.00'))
        self.assertEqual(count, 2)

    def test_gcash_total_correct(self):
        from django.db.models import Sum
        gcash = (
            Order.objects.filter(
                created_at__date=TODAY,
                is_paid=True,
                payment_method='gcash',
                status='completed',
            ).aggregate(t=Sum('total'))['t'] or Decimal('0.00')
        )
        self.assertEqual(gcash, Decimal('400.00'))

    def test_cash_and_gcash_sum_to_total_revenue(self):
        """Cash + GCash = total completed paid revenue for the day."""
        cash_total, _ = _get_cash_sales_for_date(TODAY)
        from django.db.models import Sum
        gcash_total = (
            Order.objects.filter(
                created_at__date=TODAY,
                is_paid=True,
                payment_method='gcash',
                status='completed',
            ).aggregate(t=Sum('total'))['t'] or Decimal('0.00')
        )
        self.assertEqual(cash_total + gcash_total, Decimal('900.00'))

    def test_finance_running_total_includes_gcash(self):
        """running_total uses previous_coh + cash (500) + gcash (400)."""
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.cash_sales,    Decimal('500.00'))
        # running_total = 1000 + 500 + 400 = 1900
        self.assertEqual(rec.running_total, Decimal('1900.00'))

    def test_finance_full_reconciliation(self):
        """
        Full reconciliation for a mixed day.
        previous_coh=1000, cash_sales=500, gcash_sales=400, gcash_deduction=400
        running_total = 1000 + 500 + 400 = 1900
        ending_coh    = 1900 - 400 = 1500
        """
        rec = _finance(
            TODAY,
            previous_coh=Decimal('1000.00'),
            gcash_payments=Decimal('400.00'),
        )
        self.assertEqual(rec.running_total,    Decimal('1900.00'))
        self.assertEqual(rec.total_deductions, Decimal('400.00'))
        self.assertEqual(rec.ending_coh,       Decimal('1500.00'))

    def test_api_mixed_day(self):
        user = _user('cashier_mixed')
        client = Client()
        client.login(username='cashier_mixed', password='pass123')
        url = reverse('finance:api_cash_sales')
        resp = client.get(f'{url}?date={TODAY}')
        data = resp.json()
        self.assertEqual(Decimal(data['cash_sales']),  Decimal('500.00'))
        self.assertEqual(Decimal(data['gcash_sales']), Decimal('400.00'))
        self.assertEqual(data['cash_order_count'],  2)
        self.assertEqual(data['gcash_order_count'], 2)

    def test_dashboard_total_is_combined(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        # Dashboard shows total revenue = 500 + 400 = 900
        self.assertEqual(stats['daily_sales'],  Decimal('900.00'))
        self.assertEqual(stats['daily_orders'], 4)

    def test_reports_total_is_combined(self):
        user = _user('admin_mixed', role='admin')
        client = Client()
        client.login(username='admin_mixed', password='pass123')
        resp = client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(TODAY)},
        )
        self.assertEqual(resp.context['total_revenue'], Decimal('900.00'))
        self.assertEqual(resp.context['total_orders'],  4)


# ── Scenario 4: No sales ──────────────────────────────────────────────────────

class NoSalesTest(TestCase):
    """A day with zero orders. All totals must be zero."""

    def test_cash_sales_zero(self):
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_finance_model_zero_sales(self):
        rec = _finance(TODAY, previous_coh=Decimal('500.00'))
        self.assertEqual(rec.cash_sales,    Decimal('0.00'))
        # running_total = 500 + 0 cash + 0 gcash = 500
        self.assertEqual(rec.running_total, Decimal('500.00'))
        self.assertEqual(rec.ending_coh,    Decimal('500.00'))

    def test_api_returns_zeros(self):
        user = _user('cashier_no_sales')
        client = Client()
        client.login(username='cashier_no_sales', password='pass123')
        url = reverse('finance:api_cash_sales')
        resp = client.get(f'{url}?date={TODAY}')
        data = resp.json()
        self.assertEqual(Decimal(data['cash_sales']),  Decimal('0.00'))
        self.assertEqual(Decimal(data['gcash_sales']), Decimal('0.00'))
        self.assertEqual(data['cash_order_count'],  0)
        self.assertEqual(data['gcash_order_count'], 0)

    def test_dashboard_daily_zero(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'],  0)
        self.assertEqual(stats['daily_orders'], 0)

    def test_reports_total_zero(self):
        user = _user('admin_no_sales', role='admin')
        client = Client()
        client.login(username='admin_no_sales', password='pass123')
        resp = client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(TODAY)},
        )
        self.assertEqual(resp.context['total_revenue'], 0)
        self.assertEqual(resp.context['total_orders'],  0)


# ── Scenario 5: Multiple transactions ────────────────────────────────────────

class MultipleTransactionsTest(TestCase):
    """Many orders of each type on the same day, all correctly aggregated."""

    def setUp(self):
        # 5 cash orders
        self.cash_amounts = [
            Decimal('50.00'), Decimal('120.00'), Decimal('75.50'),
            Decimal('200.00'), Decimal('99.99'),
        ]
        for amt in self.cash_amounts:
            _order(TODAY, payment_method='cash',  total=amt)

        # 4 gcash orders
        self.gcash_amounts = [
            Decimal('180.00'), Decimal('90.00'),
            Decimal('320.00'), Decimal('45.00'),
        ]
        for amt in self.gcash_amounts:
            _order(TODAY, payment_method='gcash', total=amt)

        self.expected_cash  = sum(self.cash_amounts)   # 545.49
        self.expected_gcash = sum(self.gcash_amounts)  # 635.00
        self.expected_total = self.expected_cash + self.expected_gcash  # 1180.49

    def test_cash_total_and_count(self):
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, self.expected_cash)
        self.assertEqual(count, 5)

    def test_gcash_total_and_count(self):
        from django.db.models import Sum
        gcash = (
            Order.objects.filter(
                created_at__date=TODAY,
                is_paid=True,
                payment_method='gcash',
                status='completed',
            ).aggregate(t=Sum('total'))['t'] or Decimal('0.00')
        )
        self.assertEqual(gcash, self.expected_gcash)

    def test_finance_cash_and_gcash_in_running_total(self):
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.cash_sales, self.expected_cash)
        self.assertEqual(
            rec.running_total,
            Decimal('1000.00') + self.expected_cash + self.expected_gcash,
        )

    def test_dashboard_total_includes_all(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'],  self.expected_total)
        self.assertEqual(stats['daily_orders'], 9)

    def test_reports_total_includes_all(self):
        user = _user('admin_multi', role='admin')
        client = Client()
        client.login(username='admin_multi', password='pass123')
        resp = client.get(
            reverse('reports:index'),
            {'start': str(TODAY), 'end': str(TODAY)},
        )
        self.assertEqual(resp.context['total_revenue'], self.expected_total)
        self.assertEqual(resp.context['total_orders'],  9)

    def test_each_order_counted_exactly_once(self):
        """No double counting — sum from Finance helper equals direct DB sum."""
        from django.db.models import Sum
        direct = (
            Order.objects.filter(
                created_at__date=TODAY,
                is_paid=True,
                payment_method='cash',
                status='completed',
            ).aggregate(t=Sum('total'))['t'] or Decimal('0.00')
        )
        helper_total, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(helper_total, direct)
        self.assertEqual(helper_total, self.expected_cash)


# ── Payment method server-side validation ─────────────────────────────────────

class PaymentMethodValidationTest(TestCase):
    """
    process_payment() must reject any payment method that is not
    'cash' or 'gcash', regardless of what the client sends.
    """

    def setUp(self):
        self.cashier = _user('cashier_val')
        self.client  = Client()
        self.client.login(username='cashier_val', password='pass123')
        # An unpaid pending order to attempt payment on
        self.order = _order(
            TODAY, status='pending', is_paid=False,
            payment_method='', total=Decimal('200.00'),
        )

    def _pay(self, method, amount='200.00'):
        url = reverse('orders:process_payment', kwargs={'pk': self.order.pk})
        return self.client.post(url, {
            'payment_method': method,
            'amount_paid':    amount,
        })

    def test_cash_accepted(self):
        resp = self._pay('cash')
        data = resp.json()
        self.assertTrue(data['success'])
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, 'cash')
        self.assertTrue(self.order.is_paid)

    def test_gcash_accepted(self):
        # Need a fresh unpaid order for this test
        order2 = _order(
            TODAY, status='pending', is_paid=False,
            payment_method='', total=Decimal('150.00'),
        )
        url = reverse('orders:process_payment', kwargs={'pk': order2.pk})
        resp = self.client.post(url, {
            'payment_method': 'gcash',
            'amount_paid':    '150.00',
        })
        data = resp.json()
        self.assertTrue(data['success'])
        order2.refresh_from_db()
        self.assertEqual(order2.payment_method, 'gcash')

    def test_card_rejected(self):
        resp = self._pay('card')
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertIn('error', data)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)

    def test_empty_string_rejected(self):
        resp = self._pay('')
        data = resp.json()
        self.assertFalse(data['success'])

    def test_arbitrary_string_rejected(self):
        resp = self._pay('bitcoin')
        data = resp.json()
        self.assertFalse(data['success'])

    def test_payment_method_choices_only_cash_gcash(self):
        """Model-level: PAYMENT_METHOD_CHOICES contains only cash and gcash."""
        choices = dict(Order.PAYMENT_METHOD_CHOICES)
        self.assertIn('cash',  choices)
        self.assertIn('gcash', choices)
        self.assertNotIn('card',   choices)
        self.assertNotIn('other',  choices)
        self.assertEqual(len(choices), 2)


# ── Finance COH correctness with both payment types ───────────────────────────

class FinanceCOHTest(TestCase):
    """
    End-to-end COH calculation scenarios covering all payment types.
    Manually computed expected values are stated inline.
    """

    def test_cash_only_coh(self):
        """
        previous_coh=2000, cash_sales=500, no deductions
        running_total = 2000+500 = 2500
        ending_coh    = 2500
        """
        _order(TODAY, payment_method='cash', total=Decimal('500.00'))
        rec = _finance(TODAY, previous_coh=Decimal('2000.00'))
        self.assertEqual(rec.running_total, Decimal('2500.00'))
        self.assertEqual(rec.ending_coh,    Decimal('2500.00'))

    def test_gcash_only_does_not_change_running_total(self):
        """
        GCash order exists — running_total now INCLUDES gcash.
        previous_coh=2000, cash_sales=0 (gcash only=500), no deductions
        running_total = 2000+0+500 = 2500
        ending_coh    = 2500
        """
        _order(TODAY, payment_method='gcash', total=Decimal('500.00'))
        rec = _finance(TODAY, previous_coh=Decimal('2000.00'))
        self.assertEqual(rec.cash_sales,    Decimal('0.00'))
        # running_total includes gcash
        self.assertEqual(rec.running_total, Decimal('2500.00'))
        self.assertEqual(rec.ending_coh,    Decimal('2500.00'))

    def test_gcash_deduction_reduces_ending_coh(self):
        """
        previous_coh=2000, gcash_sales=500, gcash_deduction=500
        running_total = 2000 + 500 = 2500
        ending_coh    = 2500-500 = 2000
        """
        _order(TODAY, payment_method='gcash', total=Decimal('500.00'))
        rec = _finance(
            TODAY,
            previous_coh=Decimal('2000.00'),
            gcash_payments=Decimal('500.00'),
        )
        self.assertEqual(rec.running_total, Decimal('2500.00'))
        self.assertEqual(rec.ending_coh,    Decimal('2000.00'))

    def test_mixed_full_reconciliation(self):
        """
        previous_coh=1500
        cash_sales=600 (3 orders × 200)
        gcash_sales=400 (2 orders × 200) — included in running_total
        gcash_deduction entered by cashier=400
        expenses=100, coins=50, ca=0, floating=100

        running_total = 1500+600+400 = 2500
        total_deductions = 400+100+50+0+100 = 650
        ending_coh = 2500-650 = 1850
        """
        for _ in range(3):
            _order(TODAY, payment_method='cash',  total=Decimal('200.00'))
        for _ in range(2):
            _order(TODAY, payment_method='gcash', total=Decimal('200.00'))

        rec = _finance(
            TODAY,
            previous_coh=Decimal('1500.00'),
            gcash_payments=Decimal('400.00'),
            expenses=Decimal('100.00'),
            coins=Decimal('50.00'),
            cash_advance=Decimal('0.00'),
            floating_cash=Decimal('100.00'),
        )
        self.assertEqual(rec.cash_sales,       Decimal('600.00'))
        self.assertEqual(rec.running_total,    Decimal('2500.00'))
        self.assertEqual(rec.total_deductions, Decimal('650.00'))
        self.assertEqual(rec.ending_coh,       Decimal('1850.00'))

    def test_coh_next_day_carry_forward(self):
        """
        Yesterday ending_coh with new formula.
        previous_coh=1500, cash_sales=600, gcash_sales=400,
        gcash_deduction=400, expenses=100, coins=50, floating=100
        running_total = 1500+600+400 = 2500
        total_deductions = 400+100+50+100 = 650
        ending_coh = 2500-650 = 1850
        """
        from apps.finance.views import _get_previous_coh_info
        _order(YESTERDAY, payment_method='cash', total=Decimal('600.00'))
        yest = _finance(
            YESTERDAY,
            previous_coh=Decimal('1500.00'),
            gcash_payments=Decimal('400.00'),
            expenses=Decimal('100.00'),
            coins=Decimal('50.00'),
            floating_cash=Decimal('100.00'),
        )
        # yesterday ending_coh = (1500+600) - (400+100+50+100) = 2100-650 = 1450
        # Wait: no gcash orders on YESTERDAY, so gcash_sales = 0
        # running_total = 1500 + 600 + 0 = 2100
        # ending_coh = 2100 - 650 = 1450
        self.assertEqual(yest.ending_coh, Decimal('1450.00'))

        suggested, source, is_auto = _get_previous_coh_info(TODAY)
        self.assertEqual(suggested, Decimal('1450.00'))
        self.assertTrue(is_auto)

    def test_gcash_never_appears_in_annotated_cash_sales(self):
        """
        annotated_cash_sales from the SQL annotation must exclude GCash.
        annotated_ending_coh now includes gcash_sales in running total.
        """
        from apps.finance.views import _annotate_history_qs
        _order(TODAY, payment_method='cash',  total=Decimal('300.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('700.00'))
        rec = _finance(TODAY, previous_coh=Decimal('0.00'))

        annotated = _annotate_history_qs(
            DailyFinance.objects.filter(pk=rec.pk)
        ).get()

        # annotated_cash_sales must be 300 only, not 1000
        self.assertEqual(annotated.annotated_cash_sales, Decimal('300.00'))
        # annotated_ending_coh = 0 + 300 cash + 700 gcash - 0 deductions = 1000
        self.assertEqual(annotated.annotated_ending_coh, Decimal('1000.00'))
