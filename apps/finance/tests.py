"""
Finance module tests — Kape De Manubag.

Covers every scenario requested in the Finance audit:
  1.  Normal sales day
  2.  No sales day
  3.  Cash-only sales
  4.  GCash-only sales
  5.  Expenses deduction
  6.  Coins deduction
  7.  Cash Advance deduction
  8.  Floating Cash deduction
  9.  Previous COH carry-forward
  10. Next-day COH (multi-day chain)
  11. Multiple orders on one day
  12. Cancelled orders (must NOT count)
  13. annotated_ending_coh matches model property (BUG-1 regression)
  14. finance_print uses model properties — no formula drift (BUG-2 regression)
  15. API returns string values, not float (BUG-3 regression)
  16. API date validation
  17. Form rejects future dates
  18. Form rejects negative values
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DailyFinance
from apps.finance.views import _annotate_history_qs, _get_cash_sales_for_date
from apps.orders.models import Order

User = get_user_model()


# ── Shared helpers ─────────────────────────────────────────────────────────────

def make_user(username='cashier', role='cashier'):
    u = User.objects.create_user(username=username, password='testpass123')
    u.role = role
    u.save()
    return u


def make_order(date, payment_method='cash', status='completed',
               is_paid=True, total=Decimal('100.00')):
    """
    Create a minimal Order whose created_at falls on the given date.
    Uses update() after creation so auto_now_add isn't blocked.
    """
    today_dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(12, 0))
    )
    order = Order.objects.create(
        customer_name='Test',
        payment_method=payment_method,
        status=status,
        is_paid=is_paid,
        total=total,
        subtotal=total,
    )
    # Override auto_now_add via queryset update (bypasses the field constraint)
    Order.objects.filter(pk=order.pk).update(created_at=today_dt)
    order.refresh_from_db()
    return order


def make_finance(date, previous_coh=Decimal('0.00'), **kwargs):
    return DailyFinance.objects.create(date=date, previous_coh=previous_coh, **kwargs)


TODAY = datetime.date(2026, 8, 28)
YESTERDAY = TODAY - datetime.timedelta(days=1)
TWO_DAYS_AGO = TODAY - datetime.timedelta(days=2)


# ── 1. Model calculation tests ─────────────────────────────────────────────────

class DailyFinanceModelTest(TestCase):

    # ── Scenario 1: Normal sales day ──────────────────────────────────────────
    def test_normal_sales_day(self):
        """Previous COH + cash sales - deductions = correct ending COH."""
        make_order(TODAY, payment_method='cash', total=Decimal('350.00'))
        make_order(TODAY, payment_method='cash', total=Decimal('150.00'))
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('1000.00'),
            expenses=Decimal('200.00'),
            gcash_payments=Decimal('0.00'),
            coins=Decimal('50.00'),
            cash_advance=Decimal('0.00'),
            floating_cash=Decimal('100.00'),
        )
        # cash_sales = 350 + 150 = 500
        # running_total = 1000 + 500 = 1500
        # deductions = 200 + 0 + 50 + 0 + 100 = 350
        # ending_coh = 1500 - 350 = 1150
        self.assertEqual(rec.cash_sales, Decimal('500.00'))
        self.assertEqual(rec.running_total, Decimal('1500.00'))
        self.assertEqual(rec.total_deductions, Decimal('350.00'))
        self.assertEqual(rec.ending_coh, Decimal('1150.00'))

    # ── Scenario 2: No sales day ──────────────────────────────────────────────
    def test_no_sales_day(self):
        """With no orders, cash_sales is 0; ending_coh = previous_coh - deductions."""
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('500.00'),
            expenses=Decimal('100.00'),
        )
        self.assertEqual(rec.cash_sales, Decimal('0.00'))
        self.assertEqual(rec.running_total, Decimal('500.00'))
        self.assertEqual(rec.ending_coh, Decimal('400.00'))

    # ── Scenario 3: Cash-only sales ───────────────────────────────────────────
    def test_cash_sales_counted(self):
        """Cash orders (completed, is_paid) are included in cash_sales."""
        make_order(TODAY, payment_method='cash', total=Decimal('250.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('0.00'))
        self.assertEqual(rec.cash_sales, Decimal('250.00'))

    # ── Scenario 4: GCash-only sales ─────────────────────────────────────────
    def test_gcash_sales_not_in_running_total(self):
        """GCash orders do NOT contribute to cash_sales or running_total."""
        make_order(TODAY, payment_method='gcash', total=Decimal('300.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('500.00'))
        self.assertEqual(rec.cash_sales, Decimal('0.00'))
        self.assertEqual(rec.running_total, Decimal('500.00'))

    def test_gcash_as_deduction(self):
        """GCash amount entered as gcash_payments deduction reduces ending_coh."""
        make_order(TODAY, payment_method='gcash', total=Decimal('300.00'))
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('1000.00'),
            gcash_payments=Decimal('300.00'),
        )
        # running_total = 1000 + 0 cash = 1000
        # ending_coh = 1000 - 300 = 700
        self.assertEqual(rec.running_total, Decimal('1000.00'))
        self.assertEqual(rec.ending_coh, Decimal('700.00'))

    # ── Scenario 5: Expenses deduction ───────────────────────────────────────
    def test_expenses_deduction(self):
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('1000.00'),
            expenses=Decimal('450.00'),
        )
        self.assertEqual(rec.ending_coh, Decimal('550.00'))

    # ── Scenario 6: Coins deduction ───────────────────────────────────────────
    def test_coins_deduction(self):
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('1000.00'),
            coins=Decimal('75.50'),
        )
        self.assertEqual(rec.ending_coh, Decimal('924.50'))

    # ── Scenario 7: Cash Advance deduction ────────────────────────────────────
    def test_cash_advance_deduction(self):
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('1000.00'),
            cash_advance=Decimal('200.00'),
        )
        self.assertEqual(rec.ending_coh, Decimal('800.00'))

    # ── Scenario 8: Floating Cash deduction ───────────────────────────────────
    def test_floating_cash_deduction(self):
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('1000.00'),
            floating_cash=Decimal('300.00'),
        )
        self.assertEqual(rec.ending_coh, Decimal('700.00'))

    # ── All deductions together ───────────────────────────────────────────────
    def test_all_deductions_combined(self):
        """All five deduction categories applied at once."""
        make_order(TODAY, payment_method='cash', total=Decimal('500.00'))
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('2000.00'),
            expenses=Decimal('100.00'),
            gcash_payments=Decimal('200.00'),
            coins=Decimal('50.00'),
            cash_advance=Decimal('150.00'),
            floating_cash=Decimal('250.00'),
        )
        # running_total = 2000 + 500 = 2500
        # deductions = 100 + 200 + 50 + 150 + 250 = 750
        # ending_coh = 2500 - 750 = 1750
        self.assertEqual(rec.running_total, Decimal('2500.00'))
        self.assertEqual(rec.total_deductions, Decimal('750.00'))
        self.assertEqual(rec.ending_coh, Decimal('1750.00'))

    # ── Scenario 9: Previous COH carry-forward ────────────────────────────────
    def test_previous_coh_carry_forward(self):
        """
        Yesterday's ending_coh should equal today's previous_coh.
        _get_previous_coh_info() fetches yesterday's ending_coh as today's suggestion.
        """
        from apps.finance.views import _get_previous_coh_info

        # Create yesterday's record
        yest_rec = make_finance(
            YESTERDAY,
            previous_coh=Decimal('500.00'),
            expenses=Decimal('100.00'),
        )
        # yesterday ending_coh = 500 - 100 = 400
        self.assertEqual(yest_rec.ending_coh, Decimal('400.00'))

        # Today's suggested previous_coh should be yesterday's ending_coh
        suggested, source, is_auto = _get_previous_coh_info(TODAY)
        self.assertEqual(suggested, Decimal('400.00'))
        self.assertTrue(is_auto)
        self.assertIn(str(YESTERDAY), source)

    # ── Scenario 10: Next-day COH chain ───────────────────────────────────────
    def test_next_day_coh_chain(self):
        """
        Three-day chain: each day's ending_coh flows into the next day's
        previous_coh, and the ending values are all consistent.
        """
        make_order(TWO_DAYS_AGO, payment_method='cash', total=Decimal('200.00'))
        make_order(YESTERDAY, payment_method='cash', total=Decimal('300.00'))
        make_order(TODAY, payment_method='cash', total=Decimal('400.00'))

        day1 = make_finance(TWO_DAYS_AGO, previous_coh=Decimal('1000.00'), expenses=Decimal('50.00'))
        # day1: rt=1200, ending=1150
        self.assertEqual(day1.ending_coh, Decimal('1150.00'))

        day2 = make_finance(YESTERDAY, previous_coh=day1.ending_coh, expenses=Decimal('100.00'))
        # day2: rt=1150+300=1450, ending=1350
        self.assertEqual(day2.ending_coh, Decimal('1350.00'))

        day3 = make_finance(TODAY, previous_coh=day2.ending_coh, expenses=Decimal('200.00'))
        # day3: rt=1350+400=1750, ending=1550
        self.assertEqual(day3.ending_coh, Decimal('1550.00'))

    # ── Scenario 11: Multiple orders on one day ───────────────────────────────
    def test_multiple_cash_orders_summed(self):
        """Multiple completed cash orders are all summed into cash_sales."""
        amounts = [Decimal('50.00'), Decimal('75.25'), Decimal('120.00'), Decimal('204.75')]
        for amt in amounts:
            make_order(TODAY, payment_method='cash', total=amt)
        rec = make_finance(TODAY, previous_coh=Decimal('0.00'))
        self.assertEqual(rec.cash_sales, sum(amounts))

    # ── Scenario 12: Cancelled orders excluded ────────────────────────────────
    def test_cancelled_orders_excluded(self):
        """Cancelled orders must NOT be included in cash_sales."""
        make_order(TODAY, payment_method='cash', status='cancelled',
                   is_paid=False, total=Decimal('500.00'))
        make_order(TODAY, payment_method='cash', status='completed',
                   is_paid=True, total=Decimal('100.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('0.00'))
        # Only the completed order counts
        self.assertEqual(rec.cash_sales, Decimal('100.00'))

    def test_pending_orders_excluded(self):
        """Pending or preparing orders must NOT count as cash_sales."""
        make_order(TODAY, payment_method='cash', status='pending',
                   is_paid=False, total=Decimal('999.00'))
        rec = make_finance(TODAY)
        self.assertEqual(rec.cash_sales, Decimal('0.00'))

    def test_unpaid_completed_orders_excluded(self):
        """Completed but unpaid orders must NOT count."""
        make_order(TODAY, payment_method='cash', status='completed',
                   is_paid=False, total=Decimal('999.00'))
        rec = make_finance(TODAY)
        self.assertEqual(rec.cash_sales, Decimal('0.00'))

    def test_zero_default_deductions(self):
        """A record with all deductions at default (0) returns previous_coh + sales as ending_coh."""
        make_order(TODAY, payment_method='cash', total=Decimal('500.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.ending_coh, Decimal('1500.00'))

    def test_get_cash_order_count(self):
        """get_cash_order_count returns only completed cash orders."""
        make_order(TODAY, payment_method='cash', total=Decimal('100.00'))
        make_order(TODAY, payment_method='cash', total=Decimal('100.00'))
        make_order(TODAY, payment_method='gcash', total=Decimal('100.00'))
        make_order(TODAY, payment_method='cash', status='cancelled',
                   is_paid=False, total=Decimal('100.00'))
        rec = make_finance(TODAY)
        self.assertEqual(rec.get_cash_order_count(), 2)


# ── 2. Annotation tests (BUG-1 regression) ────────────────────────────────────

class AnnotatedEndingCohTest(TestCase):
    """
    Verify that annotated_ending_coh produced by _annotate_history_qs()
    matches the Python ending_coh property exactly.
    This is the regression test for BUG-1 (N+1 fix).
    """

    def _assert_annotation_matches_property(self, rec):
        qs = _annotate_history_qs(DailyFinance.objects.filter(pk=rec.pk))
        annotated = qs.get()
        self.assertEqual(
            annotated.annotated_ending_coh,
            rec.ending_coh,
            msg=(
                f"annotated_ending_coh={annotated.annotated_ending_coh} "
                f"!= property ending_coh={rec.ending_coh} for date={rec.date}"
            ),
        )

    def test_annotation_no_sales(self):
        rec = make_finance(TODAY, previous_coh=Decimal('500.00'), expenses=Decimal('100.00'))
        self._assert_annotation_matches_property(rec)

    def test_annotation_with_cash_sales(self):
        make_order(TODAY, payment_method='cash', total=Decimal('400.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('1000.00'), expenses=Decimal('200.00'))
        self._assert_annotation_matches_property(rec)

    def test_annotation_all_deductions(self):
        make_order(TODAY, payment_method='cash', total=Decimal('500.00'))
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('2000.00'),
            expenses=Decimal('100.00'),
            gcash_payments=Decimal('200.00'),
            coins=Decimal('50.00'),
            cash_advance=Decimal('150.00'),
            floating_cash=Decimal('250.00'),
        )
        self._assert_annotation_matches_property(rec)

    def test_annotation_zero_sales(self):
        """Date with no orders — annotated should coalesce NULL to 0."""
        rec = make_finance(TODAY, previous_coh=Decimal('1000.00'))
        self._assert_annotation_matches_property(rec)

    def test_annotation_gcash_not_in_cash_sales(self):
        """GCash order for same date must not inflate annotated_cash_sales."""
        make_order(TODAY, payment_method='gcash', total=Decimal('999.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('500.00'), gcash_payments=Decimal('999.00'))
        qs = _annotate_history_qs(DailyFinance.objects.filter(pk=rec.pk))
        annotated = qs.get()
        self.assertEqual(annotated.annotated_cash_sales, Decimal('0.00'))
        self._assert_annotation_matches_property(rec)

    def test_annotation_multiple_records(self):
        """annotated_ending_coh is correct for every row in a multi-row queryset."""
        make_order(TWO_DAYS_AGO, payment_method='cash', total=Decimal('200.00'))
        make_order(YESTERDAY, payment_method='cash', total=Decimal('300.00'))
        r1 = make_finance(TWO_DAYS_AGO, previous_coh=Decimal('500.00'), expenses=Decimal('50.00'))
        r2 = make_finance(YESTERDAY, previous_coh=Decimal('650.00'), expenses=Decimal('100.00'))
        r3 = make_finance(TODAY, previous_coh=Decimal('850.00'))  # no sales today

        qs = _annotate_history_qs(
            DailyFinance.objects.filter(pk__in=[r1.pk, r2.pk, r3.pk])
        )
        annotated = {a.pk: a for a in qs}
        for rec in [r1, r2, r3]:
            self.assertEqual(
                annotated[rec.pk].annotated_ending_coh,
                rec.ending_coh,
                msg=f"Mismatch for {rec.date}",
            )


# ── 3. View tests ──────────────────────────────────────────────────────────────

class FinanceViewTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.client = Client()
        self.client.login(username='cashier', password='testpass123')

    def _index_url(self, date=None):
        url = reverse('finance:index')
        if date:
            return f"{url}?date={date}"
        return url

    # ── Index GET ─────────────────────────────────────────────────────────────
    def test_index_get_today(self):
        response = self.client.get(self._index_url(str(TODAY)))
        self.assertEqual(response.status_code, 200)
        self.assertIn('cash_sales', response.context)
        self.assertIn('running_total', response.context)
        self.assertIn('finance_history', response.context)

    def test_index_cash_sales_in_context(self):
        make_order(TODAY, payment_method='cash', total=Decimal('350.00'))
        response = self.client.get(self._index_url(str(TODAY)))
        self.assertEqual(response.context['cash_sales'], Decimal('350.00'))

    def test_index_gcash_not_in_running_total(self):
        make_order(TODAY, payment_method='gcash', total=Decimal('500.00'))
        response = self.client.get(self._index_url(str(TODAY)))
        self.assertEqual(response.context['cash_sales'], Decimal('0.00'))
        # gcash_sales shown separately
        self.assertEqual(response.context['gcash_sales'], Decimal('500.00'))

    def test_index_running_total_is_prev_coh_plus_cash(self):
        make_order(TODAY, payment_method='cash', total=Decimal('300.00'))
        # Create yesterday's record so prev_coh is auto-suggested as 400
        make_finance(YESTERDAY, previous_coh=Decimal('600.00'), expenses=Decimal('200.00'))
        response = self.client.get(self._index_url(str(TODAY)))
        # yesterday ending_coh = 600 - 200 = 400
        # running_total = 400 + 300 = 700
        self.assertEqual(response.context['running_total'], Decimal('700.00'))

    # ── Index POST: save a new record ─────────────────────────────────────────
    def test_index_post_saves_record(self):
        data = {
            'date':           str(TODAY),
            'previous_coh':   '1000.00',
            'expenses':       '200.00',
            'expenses_notes': 'Supplies',
            'gcash_payments': '150.00',
            'coins':          '50.00',
            'cash_advance':   '0.00',
            'floating_cash':  '100.00',
        }
        response = self.client.post(self._index_url(str(TODAY)), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DailyFinance.objects.filter(date=TODAY).exists())

    def test_index_post_stores_previous_coh(self):
        data = {
            'date': str(TODAY), 'previous_coh': '750.00',
            'expenses': '0.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        }
        self.client.post(self._index_url(str(TODAY)), data)
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.previous_coh, Decimal('750.00'))

    def test_index_post_rejects_future_date(self):
        # Use a date that is always in the future regardless of when the test runs.
        future = timezone.localdate() + datetime.timedelta(days=1)
        data = {
            'date': str(future), 'previous_coh': '0.00',
            'expenses': '0.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        }
        response = self.client.post(self._index_url(str(future)), data)
        self.assertEqual(response.status_code, 200)  # re-renders with errors
        self.assertFalse(DailyFinance.objects.filter(date=future).exists())

    def test_index_post_rejects_negative_expenses(self):
        data = {
            'date': str(TODAY), 'previous_coh': '1000.00',
            'expenses': '-50.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        }
        response = self.client.post(self._index_url(str(TODAY)), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DailyFinance.objects.filter(date=TODAY).exists())

    # ── History page has annotated_ending_coh (BUG-1 regression) ─────────────
    def test_history_page_annotated_ending_coh_present(self):
        """Records in history queryset have annotated_ending_coh attribute."""
        make_order(TODAY, payment_method='cash', total=Decimal('400.00'))
        make_finance(TODAY, previous_coh=Decimal('1000.00'), expenses=Decimal('200.00'))
        response = self.client.get(reverse('finance:history'))
        self.assertEqual(response.status_code, 200)
        records = list(response.context['records'])
        self.assertTrue(len(records) > 0)
        for rec in records:
            self.assertTrue(
                hasattr(rec, 'annotated_ending_coh'),
                "History record missing annotated_ending_coh annotation",
            )
            self.assertTrue(
                hasattr(rec, 'annotated_cash_sales'),
                "History record missing annotated_cash_sales annotation",
            )

    def test_index_history_annotated_ending_coh_present(self):
        """Finance index history section also has annotated_ending_coh on each row."""
        make_finance(TODAY, previous_coh=Decimal('500.00'))
        response = self.client.get(self._index_url(str(TODAY)))
        for rec in response.context['finance_history']:
            self.assertTrue(hasattr(rec, 'annotated_ending_coh'))


# ── 4. Print view tests (BUG-2 regression) ────────────────────────────────────

class FinancePrintViewTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.client = Client()
        self.client.login(username='cashier', password='testpass123')

    def test_print_view_values_match_model_properties(self):
        """
        Print view must delegate to model properties.
        Context cash_sales / running_total / total_deductions / ending_coh must
        exactly match the model property values — not an independently computed copy.
        """
        make_order(TODAY, payment_method='cash', total=Decimal('400.00'))
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('1000.00'),
            expenses=Decimal('200.00'),
            gcash_payments=Decimal('100.00'),
            coins=Decimal('50.00'),
            cash_advance=Decimal('75.00'),
            floating_cash=Decimal('25.00'),
        )
        url = reverse('finance:print', kwargs={'pk': rec.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        ctx = response.context
        self.assertEqual(ctx['cash_sales'],       rec.cash_sales)
        self.assertEqual(ctx['running_total'],    rec.running_total)
        self.assertEqual(ctx['total_deductions'], rec.total_deductions)
        self.assertEqual(ctx['ending_coh'],       rec.ending_coh)

    def test_print_correct_ending_coh_value(self):
        """Spot-check print view computes the right ending COH number."""
        make_order(TODAY, payment_method='cash', total=Decimal('500.00'))
        rec = make_finance(
            TODAY,
            previous_coh=Decimal('2000.00'),
            expenses=Decimal('300.00'),
            gcash_payments=Decimal('200.00'),
        )
        # rt = 2000 + 500 = 2500; deductions = 300 + 200 = 500; ending = 2000
        url = reverse('finance:print', kwargs={'pk': rec.pk})
        response = self.client.get(url)
        self.assertEqual(response.context['ending_coh'], Decimal('2000.00'))

    def test_print_order_count_correct(self):
        make_order(TODAY, payment_method='cash', total=Decimal('100.00'))
        make_order(TODAY, payment_method='cash', total=Decimal('200.00'))
        make_order(TODAY, payment_method='cash', status='cancelled',
                   is_paid=False, total=Decimal('999.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('0.00'))
        url = reverse('finance:print', kwargs={'pk': rec.pk})
        response = self.client.get(url)
        # Only 2 completed cash orders should count
        self.assertEqual(response.context['order_count'], 2)


# ── 5. API tests (BUG-3 regression) ───────────────────────────────────────────

class FinanceApiTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.client = Client()
        self.client.login(username='cashier', password='testpass123')
        self.url = reverse('finance:api_cash_sales')

    def test_api_returns_string_not_float(self):
        """
        cash_sales and gcash_sales must be returned as strings (not floats)
        to prevent IEEE 754 imprecision in the JS live preview.
        BUG-3 regression test.
        """
        make_order(TODAY, payment_method='cash', total=Decimal('123.10'))
        make_order(TODAY, payment_method='gcash', total=Decimal('99.90'))
        response = self.client.get(f"{self.url}?date={TODAY}")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Values must be strings (JSON string type), not floats (JSON number)
        self.assertIsInstance(data['cash_sales'],  str,
            "cash_sales must be a string to avoid float imprecision")
        self.assertIsInstance(data['gcash_sales'], str,
            "gcash_sales must be a string to avoid float imprecision")

    def test_api_cash_sales_value_correct(self):
        make_order(TODAY, payment_method='cash', total=Decimal('250.00'))
        make_order(TODAY, payment_method='cash', total=Decimal('75.50'))
        response = self.client.get(f"{self.url}?date={TODAY}")
        data = response.json()
        self.assertEqual(Decimal(data['cash_sales']), Decimal('325.50'))
        self.assertEqual(data['cash_order_count'], 2)

    def test_api_gcash_separate_from_cash(self):
        make_order(TODAY, payment_method='gcash', total=Decimal('500.00'))
        response = self.client.get(f"{self.url}?date={TODAY}")
        data = response.json()
        self.assertEqual(Decimal(data['cash_sales']),  Decimal('0.00'))
        self.assertEqual(Decimal(data['gcash_sales']), Decimal('500.00'))
        self.assertEqual(data['cash_order_count'],  0)
        self.assertEqual(data['gcash_order_count'], 1)

    def test_api_invalid_date_returns_400(self):
        response = self.client.get(f"{self.url}?date=not-a-date")
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())

    def test_api_missing_date_returns_400(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)

    def test_api_no_orders_returns_zero(self):
        response = self.client.get(f"{self.url}?date={TODAY}")
        data = response.json()
        self.assertEqual(Decimal(data['cash_sales']),  Decimal('0.00'))
        self.assertEqual(Decimal(data['gcash_sales']), Decimal('0.00'))

    def test_api_cancelled_orders_not_counted(self):
        make_order(TODAY, payment_method='cash', status='cancelled',
                   is_paid=False, total=Decimal('999.00'))
        response = self.client.get(f"{self.url}?date={TODAY}")
        data = response.json()
        self.assertEqual(Decimal(data['cash_sales']), Decimal('0.00'))

    def test_api_formatted_strings_present(self):
        make_order(TODAY, payment_method='cash', total=Decimal('100.00'))
        response = self.client.get(f"{self.url}?date={TODAY}")
        data = response.json()
        self.assertIn('cash_sales_formatted',  data)
        self.assertIn('gcash_sales_formatted', data)
        self.assertTrue(data['cash_sales_formatted'].startswith('₱'))


# ── 6. Finance-Orders agreement test ──────────────────────────────────────────

class FinanceOrdersAgreementTest(TestCase):
    """
    Verify that Finance cash_sales always agrees with the underlying
    Order records — i.e. a direct DB sum matches what the model reports.
    """

    def test_finance_agrees_with_order_table(self):
        """Direct Order sum == DailyFinance.cash_sales for the same date."""
        from django.db.models import Sum as DSum
        amounts = [Decimal('100.00'), Decimal('200.50'), Decimal('75.25')]
        for amt in amounts:
            make_order(TODAY, payment_method='cash', total=amt)
        rec = make_finance(TODAY, previous_coh=Decimal('0.00'))

        order_sum = (
            Order.objects.filter(
                created_at__date=TODAY,
                is_paid=True,
                payment_method='cash',
                status='completed',
            ).aggregate(total=DSum('total'))['total'] or Decimal('0.00')
        )
        self.assertEqual(rec.cash_sales, order_sum)
        self.assertEqual(rec.cash_sales, sum(amounts))

    def test_cancelled_orders_do_not_affect_finance(self):
        make_order(TODAY, payment_method='cash', status='cancelled',
                   is_paid=False, total=Decimal('500.00'))
        make_order(TODAY, payment_method='cash', total=Decimal('100.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('0.00'))
        self.assertEqual(rec.cash_sales, Decimal('100.00'))

    def test_gcash_orders_do_not_inflate_cash_sales(self):
        make_order(TODAY, payment_method='cash',  total=Decimal('200.00'))
        make_order(TODAY, payment_method='gcash', total=Decimal('999.00'))
        rec = make_finance(TODAY, previous_coh=Decimal('0.00'))
        self.assertEqual(rec.cash_sales, Decimal('200.00'))
