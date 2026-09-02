"""
Finance Expenses tests — Kape De Manubag.

Expenses in this system are a single aggregate DecimalField (``expenses``)
plus a free-text note (``expenses_notes``) on the DailyFinance record.
There is no separate Expense model — "editing an expense" means updating
the daily amount; "deleting an expense" means setting it back to 0.00.

Scenarios covered:
  1.  Normal expense
  2.  Zero amount
  3.  Negative amount (must be rejected)
  4.  Very large amount
  5.  Multiple days — each day's expense is independent
  6.  Editing an expense (update via POST)
  7.  "Deleting" an expense (reset to 0.00)
  8.  Expenses on different dates
  9.  Empty field submission (coercion fix regression)
  10. Finance calculation after every scenario
  11. All other monetary fields coerce empty to 0.00 (regression)
  12. Decimal arithmetic — no float contamination
  13. Expenses counted exactly once in total_deductions
  14. No regressions in existing test suites
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.finance.forms import DailyFinanceForm
from apps.finance.models import DailyFinance
from apps.orders.models import Order
from django.utils import timezone

User = get_user_model()

TODAY  = datetime.date(2026, 8, 28)
YEST   = TODAY - datetime.timedelta(days=1)
D_PREV = TODAY - datetime.timedelta(days=2)

URL = reverse('finance:index')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username='cashier', role='cashier'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _finance(date, previous_coh=Decimal('1000.00'), **kwargs):
    return DailyFinance.objects.create(
        date=date, previous_coh=previous_coh, **kwargs
    )


def _order(date, total=Decimal('200.00'), payment_method='cash'):
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(10, 0))
    )
    o = Order.objects.create(
        customer_name='Test', status='completed',
        is_paid=True, payment_method=payment_method,
        total=total, subtotal=total,
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


def _post(client, date, **field_overrides):
    """Submit a finance form POST for the given date."""
    data = {
        'date':           str(date),
        'previous_coh':   '1000.00',
        'expenses':       '0.00',
        'expenses_notes': '',
        'gcash_payments': '0.00',
        'coins':          '0.00',
        'cash_advance':   '0.00',
        'floating_cash':  '0.00',
    }
    data.update(field_overrides)
    return client.post(f'{URL}?date={date}', data)


# ── 1. Normal expense ─────────────────────────────────────────────────────────

class NormalExpenseTest(TestCase):
    def test_normal_expense_stored(self):
        rec = _finance(TODAY, expenses=Decimal('250.00'))
        self.assertEqual(rec.expenses, Decimal('250.00'))

    def test_normal_expense_in_total_deductions(self):
        rec = _finance(TODAY, expenses=Decimal('250.00'))
        self.assertEqual(rec.total_deductions, Decimal('250.00'))

    def test_normal_expense_reduces_ending_coh(self):
        """previous_coh=1000, sales=200, expenses=250 → ending=950"""
        _order(TODAY, total=Decimal('200.00'))
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'),
                       expenses=Decimal('250.00'))
        self.assertEqual(rec.running_total, Decimal('1200.00'))
        self.assertEqual(rec.ending_coh,    Decimal('950.00'))

    def test_expense_with_notes(self):
        rec = _finance(TODAY, expenses=Decimal('150.00'),
                       expenses_notes='Cleaning supplies')
        self.assertEqual(rec.expenses_notes, 'Cleaning supplies')

    def test_expense_via_form_post(self):
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        resp = _post(client, TODAY, expenses='350.00',
                     expenses_notes='Staff meal')
        self.assertEqual(resp.status_code, 302)
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.expenses, Decimal('350.00'))
        self.assertEqual(rec.expenses_notes, 'Staff meal')


# ── 2. Zero amount ────────────────────────────────────────────────────────────

class ZeroExpenseTest(TestCase):
    def test_zero_expense_accepted(self):
        rec = _finance(TODAY, expenses=Decimal('0.00'))
        self.assertEqual(rec.expenses, Decimal('0.00'))

    def test_zero_expense_does_not_change_coh(self):
        _order(TODAY, total=Decimal('500.00'))
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'),
                       expenses=Decimal('0.00'))
        self.assertEqual(rec.ending_coh, Decimal('1500.00'))

    def test_zero_expense_form_valid(self):
        form = DailyFinanceForm(data={
            'date': str(TODAY), 'previous_coh': '1000.00',
            'expenses': '0.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['expenses'], Decimal('0.00'))

    def test_zero_expense_via_post(self):
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        resp = _post(client, TODAY, expenses='0.00')
        self.assertEqual(resp.status_code, 302)
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.expenses, Decimal('0.00'))


# ── 3. Negative amount ────────────────────────────────────────────────────────

class NegativeExpenseTest(TestCase):
    def test_negative_expense_form_invalid(self):
        form = DailyFinanceForm(data={
            'date': str(TODAY), 'previous_coh': '1000.00',
            'expenses': '-100.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('expenses', form.errors)

    def test_negative_expense_rejected_via_post(self):
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        resp = _post(client, TODAY, expenses='-50.00')
        # Form invalid — stays on page (200) and does not save
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(DailyFinance.objects.filter(date=TODAY).exists())

    def test_negative_expense_does_not_corrupt_model(self):
        """The model itself has no constraint, but the form blocks it."""
        form = DailyFinanceForm(data={
            'date': str(TODAY), 'previous_coh': '500.00',
            'expenses': '-999.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        })
        self.assertFalse(form.is_valid())
        # No record created
        self.assertEqual(DailyFinance.objects.count(), 0)


# ── 4. Very large amount ──────────────────────────────────────────────────────

class LargeExpenseTest(TestCase):
    def test_large_expense_stored_as_decimal(self):
        """max_digits=12 allows up to 9,999,999,999.99."""
        large = Decimal('9999999.99')
        rec = _finance(TODAY, expenses=large)
        self.assertEqual(rec.expenses, large)

    def test_large_expense_in_calculation(self):
        rec = _finance(TODAY, previous_coh=Decimal('10000000.00'),
                       expenses=Decimal('9999999.99'))
        # ending_coh = 10000000 + 0 sales - 9999999.99 = 0.01
        self.assertEqual(rec.ending_coh, Decimal('0.01'))

    def test_large_expense_form_valid(self):
        form = DailyFinanceForm(data={
            'date': str(TODAY), 'previous_coh': '10000000.00',
            'expenses': '9999999.99', 'expenses_notes': 'Major capital expense',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_large_expense_uses_decimal_not_float(self):
        """Verify no float imprecision on large values."""
        large = Decimal('1234567.89')
        rec = _finance(TODAY, expenses=large)
        self.assertIsInstance(rec.expenses, Decimal)
        self.assertEqual(rec.expenses, large)


# ── 5. Multiple days — independent expenses ───────────────────────────────────

class MultipleDaysExpenseTest(TestCase):
    def test_each_day_has_independent_expense(self):
        r1 = _finance(D_PREV, expenses=Decimal('100.00'))
        r2 = _finance(YEST,   expenses=Decimal('250.00'))
        r3 = _finance(TODAY,  expenses=Decimal('0.00'))
        self.assertEqual(r1.expenses, Decimal('100.00'))
        self.assertEqual(r2.expenses, Decimal('250.00'))
        self.assertEqual(r3.expenses, Decimal('0.00'))

    def test_expense_on_one_day_does_not_affect_others(self):
        """Editing TODAY's expense must not change D_PREV or YEST."""
        r1 = _finance(D_PREV, previous_coh=Decimal('500.00'),
                      expenses=Decimal('100.00'))
        r2 = _finance(YEST,   previous_coh=Decimal('400.00'),
                      expenses=Decimal('50.00'))
        r3 = _finance(TODAY,  previous_coh=Decimal('350.00'),
                      expenses=Decimal('200.00'))

        # Verify isolation
        self.assertEqual(r1.ending_coh, Decimal('400.00'))   # 500-100
        self.assertEqual(r2.ending_coh, Decimal('350.00'))   # 400-50
        self.assertEqual(r3.ending_coh, Decimal('150.00'))   # 350-200

    def test_different_dates_expense_notes_independent(self):
        r1 = _finance(YEST,  expenses=Decimal('80.00'),
                      expenses_notes='Ice delivery')
        r2 = _finance(TODAY, expenses=Decimal('120.00'),
                      expenses_notes='Cups')
        self.assertEqual(r1.expenses_notes, 'Ice delivery')
        self.assertEqual(r2.expenses_notes, 'Cups')


# ── 6. Editing an expense ─────────────────────────────────────────────────────

class EditExpenseTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.client = Client()
        self.client.login(username='cashier', password='pass123')

    def test_edit_expense_amount(self):
        # Create then update
        _post(self.client, TODAY, expenses='100.00')
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.expenses, Decimal('100.00'))

        _post(self.client, TODAY, expenses='350.00',
              previous_coh=str(rec.previous_coh))
        rec.refresh_from_db()
        self.assertEqual(rec.expenses, Decimal('350.00'))

    def test_edit_expense_does_not_create_duplicate(self):
        _post(self.client, TODAY, expenses='100.00')
        _post(self.client, TODAY, expenses='200.00')
        self.assertEqual(DailyFinance.objects.filter(date=TODAY).count(), 1)

    def test_edit_updates_ending_coh(self):
        _order(TODAY, total=Decimal('500.00'))
        _post(self.client, TODAY, expenses='100.00')
        rec = DailyFinance.objects.get(date=TODAY)
        # 1000 + 500 - 100 = 1400
        self.assertEqual(rec.ending_coh, Decimal('1400.00'))

        _post(self.client, TODAY, expenses='400.00',
              previous_coh=str(rec.previous_coh))
        rec.refresh_from_db()
        # 1000 + 500 - 400 = 1100
        self.assertEqual(rec.ending_coh, Decimal('1100.00'))

    def test_edit_notes(self):
        _post(self.client, TODAY, expenses='100.00',
              expenses_notes='Original note')
        _post(self.client, TODAY, expenses='100.00',
              expenses_notes='Updated note')
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.expenses_notes, 'Updated note')

    def test_edit_does_not_change_prepared_by(self):
        """prepared_by is set on CREATE only and must not change on update."""
        _post(self.client, TODAY, expenses='100.00')
        rec = DailyFinance.objects.get(date=TODAY)
        original_prepared_by = rec.prepared_by

        # Second user also edits
        user2 = _user('cashier2')
        client2 = Client()
        client2.login(username='cashier2', password='pass123')
        client2.post(f'{URL}?date={TODAY}', {
            'date': str(TODAY),
            'previous_coh': '1000.00',
            'expenses': '200.00',
            'expenses_notes': '',
            'gcash_payments': '0.00',
            'coins': '0.00',
            'cash_advance': '0.00',
            'floating_cash': '0.00',
        })
        rec.refresh_from_db()
        self.assertEqual(rec.prepared_by, original_prepared_by)


# ── 7. "Deleting" an expense (reset to 0.00) ─────────────────────────────────

class DeleteExpenseTest(TestCase):
    """
    There are no individual expense records to delete.
    "Deleting" an expense means setting expenses=0.00 via an update POST.
    """

    def setUp(self):
        self.user = _user()
        self.client = Client()
        self.client.login(username='cashier', password='pass123')

    def test_reset_expense_to_zero(self):
        _post(self.client, TODAY, expenses='500.00')
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.expenses, Decimal('500.00'))

        # "Delete": reset to zero
        _post(self.client, TODAY, expenses='0.00',
              previous_coh=str(rec.previous_coh))
        rec.refresh_from_db()
        self.assertEqual(rec.expenses, Decimal('0.00'))

    def test_reset_restores_ending_coh(self):
        _order(TODAY, total=Decimal('300.00'))
        _post(self.client, TODAY, expenses='200.00')
        rec = DailyFinance.objects.get(date=TODAY)
        # 1000 + 300 - 200 = 1100
        self.assertEqual(rec.ending_coh, Decimal('1100.00'))

        # Reset expenses to 0
        _post(self.client, TODAY, expenses='0.00',
              previous_coh=str(rec.previous_coh))
        rec.refresh_from_db()
        # 1000 + 300 - 0 = 1300
        self.assertEqual(rec.ending_coh, Decimal('1300.00'))

    def test_record_persists_after_reset(self):
        """The DailyFinance record itself must not be deleted, only updated."""
        _post(self.client, TODAY, expenses='500.00')
        _post(self.client, TODAY, expenses='0.00')
        self.assertTrue(DailyFinance.objects.filter(date=TODAY).exists())


# ── 8. Expenses on different dates ────────────────────────────────────────────

class ExpenseDifferentDatesTest(TestCase):
    def test_expense_tied_to_correct_date(self):
        r_yest  = _finance(YEST,  expenses=Decimal('150.00'))
        r_today = _finance(TODAY, expenses=Decimal('300.00'))
        self.assertEqual(
            DailyFinance.objects.get(date=YEST).expenses,
            Decimal('150.00'),
        )
        self.assertEqual(
            DailyFinance.objects.get(date=TODAY).expenses,
            Decimal('300.00'),
        )

    def test_next_day_coh_unaffected_by_wrong_date_expense(self):
        """
        Expense on YEST affects YEST's ending_coh.
        TODAY's previous_coh suggestion is YEST's ending_coh, not the raw previous_coh.
        """
        from apps.finance.views import _get_previous_coh_info
        _finance(YEST, previous_coh=Decimal('1000.00'),
                 expenses=Decimal('400.00'))
        # YEST ending = 1000 - 400 = 600
        suggested, _, is_auto = _get_previous_coh_info(TODAY)
        self.assertEqual(suggested, Decimal('600.00'))
        self.assertTrue(is_auto)


# ── 9. Empty field submission (coercion fix regression) ───────────────────────

class EmptyFieldCoercionTest(TestCase):
    """
    Submitting an empty monetary field must coerce to 0.00, not raise
    "Enter a number." The form marks all monetary fields required=False
    and per-field clean methods return Decimal('0.00') for None.
    """

    def _form(self, **overrides):
        data = {
            'date': str(TODAY),
            'previous_coh':   '1000.00',
            'expenses':       '0.00',
            'expenses_notes': '',
            'gcash_payments': '0.00',
            'coins':          '0.00',
            'cash_advance':   '0.00',
            'floating_cash':  '0.00',
        }
        data.update(overrides)
        return DailyFinanceForm(data=data)

    def test_empty_expenses_coerces_to_zero(self):
        form = self._form(expenses='')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['expenses'], Decimal('0.00'))

    def test_empty_gcash_payments_coerces_to_zero(self):
        form = self._form(gcash_payments='')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['gcash_payments'], Decimal('0.00'))

    def test_empty_coins_coerces_to_zero(self):
        form = self._form(coins='')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['coins'], Decimal('0.00'))

    def test_empty_cash_advance_coerces_to_zero(self):
        form = self._form(cash_advance='')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['cash_advance'], Decimal('0.00'))

    def test_empty_floating_cash_coerces_to_zero(self):
        form = self._form(floating_cash='')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['floating_cash'], Decimal('0.00'))

    def test_empty_previous_coh_coerces_to_zero(self):
        form = self._form(previous_coh='')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['previous_coh'], Decimal('0.00'))

    def test_all_fields_empty_coerces_to_zero(self):
        """All monetary fields blank at once → all become 0.00, form valid."""
        form = self._form(
            previous_coh='', expenses='', gcash_payments='',
            coins='', cash_advance='', floating_cash='',
        )
        self.assertTrue(form.is_valid(), form.errors)
        for field in ['previous_coh', 'expenses', 'gcash_payments',
                      'coins', 'cash_advance', 'floating_cash']:
            self.assertEqual(form.cleaned_data[field], Decimal('0.00'),
                             f'{field} should be 0.00 when empty')

    def test_empty_coercion_via_post(self):
        """End-to-end: blank expenses in POST body saves as 0.00."""
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        resp = client.post(f'{URL}?date={TODAY}', {
            'date':           str(TODAY),
            'previous_coh':   '500.00',
            'expenses':       '',       # deliberately blank
            'expenses_notes': '',
            'gcash_payments': '',       # deliberately blank
            'coins':          '',       # deliberately blank
            'cash_advance':   '',       # deliberately blank
            'floating_cash':  '',       # deliberately blank
        })
        self.assertEqual(resp.status_code, 302,
            f"Expected redirect, got 200 with errors: "
            f"{resp.context['form'].errors if hasattr(resp, 'context') and resp.context else 'N/A'}")
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.expenses,       Decimal('0.00'))
        self.assertEqual(rec.gcash_payments, Decimal('0.00'))
        self.assertEqual(rec.coins,          Decimal('0.00'))
        self.assertEqual(rec.cash_advance,   Decimal('0.00'))
        self.assertEqual(rec.floating_cash,  Decimal('0.00'))

    def test_negative_still_rejected_after_coercion(self):
        """The coercion fix must not allow negative values through."""
        form = self._form(expenses='-10.00')
        self.assertFalse(form.is_valid())
        self.assertIn('expenses', form.errors)


# ── 10. Finance calculation correctness ──────────────────────────────────────

class ExpenseCalculationTest(TestCase):
    """Verify the Finance formula is correct in every expense scenario."""

    def test_expenses_in_total_deductions_once(self):
        """Expenses appear exactly once in total_deductions."""
        rec = _finance(TODAY, expenses=Decimal('300.00'),
                       gcash_payments=Decimal('100.00'),
                       coins=Decimal('50.00'))
        # 300 + 100 + 50 + 0 + 0 = 450
        self.assertEqual(rec.total_deductions, Decimal('450.00'))

    def test_expenses_not_double_counted(self):
        """total_deductions must equal the sum of each field individually."""
        rec = _finance(TODAY,
                       expenses=Decimal('200.00'),
                       gcash_payments=Decimal('150.00'),
                       coins=Decimal('75.00'),
                       cash_advance=Decimal('50.00'),
                       floating_cash=Decimal('100.00'))
        expected = (Decimal('200.00') + Decimal('150.00') +
                    Decimal('75.00')  + Decimal('50.00')  +
                    Decimal('100.00'))
        self.assertEqual(rec.total_deductions, expected)
        self.assertEqual(rec.total_deductions, Decimal('575.00'))

    def test_ending_coh_formula(self):
        """ending_coh = previous_coh + cash_sales - total_deductions."""
        _order(TODAY, total=Decimal('400.00'))
        rec = _finance(TODAY, previous_coh=Decimal('2000.00'),
                       expenses=Decimal('300.00'),
                       gcash_payments=Decimal('200.00'))
        # rt = 2000 + 400 = 2400; deductions = 300+200 = 500; ending = 1900
        self.assertEqual(rec.running_total,    Decimal('2400.00'))
        self.assertEqual(rec.total_deductions, Decimal('500.00'))
        self.assertEqual(rec.ending_coh,       Decimal('1900.00'))

    def test_expense_uses_decimal_not_float(self):
        """Arithmetic path must remain Decimal throughout."""
        rec = _finance(TODAY,
                       previous_coh=Decimal('1234.56'),
                       expenses=Decimal('789.01'))
        self.assertIsInstance(rec.total_deductions, Decimal)
        self.assertIsInstance(rec.ending_coh,       Decimal)
        # 1234.56 + 0 sales - 789.01 = 445.55
        self.assertEqual(rec.ending_coh, Decimal('445.55'))

    def test_annotated_ending_coh_includes_expenses(self):
        """
        The SQL annotation (annotated_ending_coh) must match the Python
        property ending_coh when expenses are non-zero.
        """
        from apps.finance.views import _annotate_history_qs
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'),
                       expenses=Decimal('350.00'))
        annotated = _annotate_history_qs(
            DailyFinance.objects.filter(pk=rec.pk)
        ).get()
        self.assertEqual(annotated.annotated_ending_coh, rec.ending_coh)
        self.assertEqual(annotated.annotated_ending_coh, Decimal('650.00'))
