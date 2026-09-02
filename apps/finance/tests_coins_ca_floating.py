"""
Coins / Cash Advance / Floating Cash verification tests — Kape De Manubag.

Formally proves that all three deduction fields are:
  - Stored independently in separate DB columns
  - Validated independently (negative rejected, empty coerced to 0)
  - Included in total_deductions and ending_coh exactly once
  - Isolated from each other (changing one never affects another)
  - Persisted and reloaded correctly across saves and page visits
  - Carried forward correctly to the next business day via ending_coh

Scenarios covered:
  1.  Normal values for each field
  2.  Zero values for each field
  3.  Multiple fields set simultaneously
  4.  Editing each field via POST
  5.  Saving and reopening (GET reloads stored values)
  6.  Moving to the next business day (ending_coh carry-forward)
  7.  Field isolation (changing one must not affect the others)
  8.  Negative values rejected for each field
  9.  Empty field coercion (→ 0.00)
  10. Manual COH calculation verification for every combination
  11. SQL annotation matches Python property (regression)
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.forms import DailyFinanceForm
from apps.finance.models import DailyFinance
from apps.finance.views import _annotate_history_qs, _get_previous_coh_info
from apps.orders.models import Order

User = get_user_model()

TODAY = datetime.date(2026, 8, 28)
YEST  = TODAY - datetime.timedelta(days=1)
TMRW  = TODAY + datetime.timedelta(days=1)

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


def _order(date, total=Decimal('300.00')):
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(10, 0))
    )
    o = Order.objects.create(
        customer_name='Test', status='completed',
        is_paid=True, payment_method='cash',
        total=total, subtotal=total,
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


def _post(client, date, **overrides):
    """Submit a finance form POST with sensible defaults."""
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
    data.update(overrides)
    return client.post(f'{URL}?date={date}', data)


# ── 1. Normal values ──────────────────────────────────────────────────────────

class NormalValuesTest(TestCase):
    """Each field accepts and stores a typical positive value."""

    def test_coins_stored(self):
        rec = _finance(TODAY, coins=Decimal('75.50'))
        self.assertEqual(rec.coins, Decimal('75.50'))

    def test_cash_advance_stored(self):
        rec = _finance(TODAY, cash_advance=Decimal('200.00'))
        self.assertEqual(rec.cash_advance, Decimal('200.00'))

    def test_floating_cash_stored(self):
        rec = _finance(TODAY, floating_cash=Decimal('500.00'))
        self.assertEqual(rec.floating_cash, Decimal('500.00'))

    def test_coins_in_deductions(self):
        rec = _finance(TODAY, coins=Decimal('75.50'))
        self.assertEqual(rec.total_deductions, Decimal('75.50'))

    def test_cash_advance_in_deductions(self):
        rec = _finance(TODAY, cash_advance=Decimal('200.00'))
        self.assertEqual(rec.total_deductions, Decimal('200.00'))

    def test_floating_cash_in_deductions(self):
        rec = _finance(TODAY, floating_cash=Decimal('500.00'))
        self.assertEqual(rec.total_deductions, Decimal('500.00'))

    def test_coins_reduces_ending_coh(self):
        # prev=1000, sales=0, coins=75.50 → ending=924.50
        rec = _finance(TODAY, coins=Decimal('75.50'))
        self.assertEqual(rec.ending_coh, Decimal('924.50'))

    def test_cash_advance_reduces_ending_coh(self):
        # prev=1000, sales=0, ca=200 → ending=800
        rec = _finance(TODAY, cash_advance=Decimal('200.00'))
        self.assertEqual(rec.ending_coh, Decimal('800.00'))

    def test_floating_cash_reduces_ending_coh(self):
        # prev=1000, sales=0, floating=500 → ending=500
        rec = _finance(TODAY, floating_cash=Decimal('500.00'))
        self.assertEqual(rec.ending_coh, Decimal('500.00'))

    def test_normal_values_via_post(self):
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        _post(client, TODAY,
              coins='75.50', cash_advance='200.00', floating_cash='500.00')
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.coins,        Decimal('75.50'))
        self.assertEqual(rec.cash_advance, Decimal('200.00'))
        self.assertEqual(rec.floating_cash, Decimal('500.00'))


# ── 2. Zero values ────────────────────────────────────────────────────────────

class ZeroValuesTest(TestCase):
    """Zero is a valid value meaning no amount for that category today."""

    def test_zero_coins_accepted(self):
        rec = _finance(TODAY, coins=Decimal('0.00'))
        self.assertEqual(rec.coins, Decimal('0.00'))

    def test_zero_cash_advance_accepted(self):
        rec = _finance(TODAY, cash_advance=Decimal('0.00'))
        self.assertEqual(rec.cash_advance, Decimal('0.00'))

    def test_zero_floating_cash_accepted(self):
        rec = _finance(TODAY, floating_cash=Decimal('0.00'))
        self.assertEqual(rec.floating_cash, Decimal('0.00'))

    def test_all_zero_ending_coh_equals_running_total(self):
        """If all three are zero, ending_coh = previous_coh + cash_sales."""
        _order(TODAY, total=Decimal('400.00'))
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'),
                       coins=Decimal('0.00'), cash_advance=Decimal('0.00'),
                       floating_cash=Decimal('0.00'))
        self.assertEqual(rec.running_total, Decimal('1400.00'))
        self.assertEqual(rec.ending_coh,    Decimal('1400.00'))

    def test_form_accepts_zero_for_each_field(self):
        form = DailyFinanceForm(data={
            'date': str(TODAY), 'previous_coh': '1000.00',
            'expenses': '0.00', 'expenses_notes': '',
            'gcash_payments': '0.00',
            'coins': '0.00', 'cash_advance': '0.00', 'floating_cash': '0.00',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['coins'],        Decimal('0.00'))
        self.assertEqual(form.cleaned_data['cash_advance'], Decimal('0.00'))
        self.assertEqual(form.cleaned_data['floating_cash'], Decimal('0.00'))


# ── 3. Multiple fields set simultaneously ─────────────────────────────────────

class MultipleFieldsTest(TestCase):
    """All three can be set on the same record without interfering."""

    def test_all_three_stored_correctly(self):
        rec = _finance(TODAY,
                       coins=Decimal('50.00'),
                       cash_advance=Decimal('300.00'),
                       floating_cash=Decimal('200.00'))
        self.assertEqual(rec.coins,         Decimal('50.00'))
        self.assertEqual(rec.cash_advance,  Decimal('300.00'))
        self.assertEqual(rec.floating_cash, Decimal('200.00'))

    def test_all_three_summed_in_total_deductions(self):
        rec = _finance(TODAY,
                       coins=Decimal('50.00'),
                       cash_advance=Decimal('300.00'),
                       floating_cash=Decimal('200.00'))
        # 50 + 300 + 200 = 550
        self.assertEqual(rec.total_deductions, Decimal('550.00'))

    def test_full_formula_with_all_five_deductions(self):
        """
        Manual verification:
        prev=2000, sales=500
        expenses=100, gcash=150, coins=50, ca=300, floating=200
        running   = 2000+500     = 2500
        deductions= 100+150+50+300+200 = 800
        ending    = 2500-800     = 1700
        """
        _order(TODAY, total=Decimal('500.00'))
        rec = _finance(TODAY,
                       previous_coh=Decimal('2000.00'),
                       expenses=Decimal('100.00'),
                       gcash_payments=Decimal('150.00'),
                       coins=Decimal('50.00'),
                       cash_advance=Decimal('300.00'),
                       floating_cash=Decimal('200.00'))
        self.assertEqual(rec.running_total,    Decimal('2500.00'))
        self.assertEqual(rec.total_deductions, Decimal('800.00'))
        self.assertEqual(rec.ending_coh,       Decimal('1700.00'))

    def test_all_three_via_post(self):
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        _post(client, TODAY,
              coins='50.00', cash_advance='300.00', floating_cash='200.00')
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.coins,         Decimal('50.00'))
        self.assertEqual(rec.cash_advance,  Decimal('300.00'))
        self.assertEqual(rec.floating_cash, Decimal('200.00'))


# ── 4. Editing values ─────────────────────────────────────────────────────────

class EditValuesTest(TestCase):
    """Updating each field via POST correctly replaces its stored value."""

    def setUp(self):
        self.user = _user()
        self.client = Client()
        self.client.login(username='cashier', password='pass123')
        # Initial save with all three non-zero
        _post(self.client, TODAY,
              coins='50.00', cash_advance='100.00', floating_cash='150.00')
        self.rec = DailyFinance.objects.get(date=TODAY)

    def test_edit_coins(self):
        _post(self.client, TODAY, coins='99.99',
              cash_advance='100.00', floating_cash='150.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.coins, Decimal('99.99'))
        # Other fields unchanged
        self.assertEqual(self.rec.cash_advance,  Decimal('100.00'))
        self.assertEqual(self.rec.floating_cash, Decimal('150.00'))

    def test_edit_cash_advance(self):
        _post(self.client, TODAY, coins='50.00',
              cash_advance='450.00', floating_cash='150.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.cash_advance, Decimal('450.00'))
        self.assertEqual(self.rec.coins,         Decimal('50.00'))
        self.assertEqual(self.rec.floating_cash, Decimal('150.00'))

    def test_edit_floating_cash(self):
        _post(self.client, TODAY, coins='50.00',
              cash_advance='100.00', floating_cash='999.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.floating_cash, Decimal('999.00'))
        self.assertEqual(self.rec.coins,        Decimal('50.00'))
        self.assertEqual(self.rec.cash_advance, Decimal('100.00'))

    def test_edit_does_not_create_duplicate(self):
        _post(self.client, TODAY, coins='25.00')
        self.assertEqual(DailyFinance.objects.filter(date=TODAY).count(), 1)

    def test_edit_updates_ending_coh(self):
        _order(TODAY, total=Decimal('500.00'))
        # Update: coins=200, ca=100, floating=50 → deductions=350
        _post(self.client, TODAY, previous_coh='1000.00',
              coins='200.00', cash_advance='100.00', floating_cash='50.00')
        self.rec.refresh_from_db()
        # 1000 + 500 - 350 = 1150
        self.assertEqual(self.rec.ending_coh, Decimal('1150.00'))

    def test_reset_to_zero_updates_ending_coh(self):
        """Setting a field to 0 effectively 'removes' that deduction."""
        _post(self.client, TODAY, previous_coh='1000.00',
              coins='0.00', cash_advance='0.00', floating_cash='0.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.ending_coh, Decimal('1000.00'))


# ── 5. Save and reopen (GET reloads stored values) ────────────────────────────

class SaveAndReopenTest(TestCase):
    """
    After saving, revisiting the Finance page for the same date must load
    the stored values — not the form defaults.
    """

    def setUp(self):
        self.user = _user()
        self.client = Client()
        self.client.login(username='cashier', password='pass123')

    def test_reopen_loads_coins(self):
        _post(self.client, TODAY, coins='88.00')
        resp = self.client.get(URL, {'date': str(TODAY)})
        rec = resp.context['existing_record']
        self.assertEqual(rec.coins, Decimal('88.00'))

    def test_reopen_loads_cash_advance(self):
        _post(self.client, TODAY, cash_advance='175.00')
        resp = self.client.get(URL, {'date': str(TODAY)})
        rec = resp.context['existing_record']
        self.assertEqual(rec.cash_advance, Decimal('175.00'))

    def test_reopen_loads_floating_cash(self):
        _post(self.client, TODAY, floating_cash='300.00')
        resp = self.client.get(URL, {'date': str(TODAY)})
        rec = resp.context['existing_record']
        self.assertEqual(rec.floating_cash, Decimal('300.00'))

    def test_reopen_loads_all_three_unchanged(self):
        _post(self.client, TODAY,
              coins='50.00', cash_advance='200.00', floating_cash='100.00')
        resp = self.client.get(URL, {'date': str(TODAY)})
        rec = resp.context['existing_record']
        self.assertEqual(rec.coins,         Decimal('50.00'))
        self.assertEqual(rec.cash_advance,  Decimal('200.00'))
        self.assertEqual(rec.floating_cash, Decimal('100.00'))

    def test_form_prepopulated_with_stored_values(self):
        """The form's instance fields on GET must match the stored record."""
        _post(self.client, TODAY,
              coins='60.00', cash_advance='120.00', floating_cash='80.00')
        resp = self.client.get(URL, {'date': str(TODAY)})
        form = resp.context['form']
        # Verify the bound instance carries the correct stored Decimal values
        self.assertEqual(form.instance.coins,         Decimal('60.00'))
        self.assertEqual(form.instance.cash_advance,  Decimal('120.00'))
        self.assertEqual(form.instance.floating_cash, Decimal('80.00'))


# ── 6. Next business day carry-forward ───────────────────────────────────────

class NextDayCarryForwardTest(TestCase):
    """
    Today's ending_coh (which includes coins/ca/floating as deductions)
    becomes tomorrow's suggested previous_coh.
    """

    def test_coins_affect_next_day_suggestion(self):
        """
        Today: prev=1000, sales=0, coins=200 → ending=800
        Tomorrow's suggested previous_coh must be 800.
        """
        _finance(TODAY, previous_coh=Decimal('1000.00'),
                 coins=Decimal('200.00'))
        suggested, _, is_auto = _get_previous_coh_info(TMRW)
        self.assertEqual(suggested, Decimal('800.00'))
        self.assertTrue(is_auto)

    def test_cash_advance_affects_next_day_suggestion(self):
        _finance(TODAY, previous_coh=Decimal('1000.00'),
                 cash_advance=Decimal('300.00'))
        suggested, _, _ = _get_previous_coh_info(TMRW)
        self.assertEqual(suggested, Decimal('700.00'))

    def test_floating_cash_affects_next_day_suggestion(self):
        _finance(TODAY, previous_coh=Decimal('1000.00'),
                 floating_cash=Decimal('150.00'))
        suggested, _, _ = _get_previous_coh_info(TMRW)
        self.assertEqual(suggested, Decimal('850.00'))

    def test_all_three_combined_carry_forward(self):
        """
        Today: prev=2000, sales=500, coins=50, ca=300, floating=200
        ending = (2000+500) - (50+300+200) = 2500-550 = 1950
        Tomorrow suggested = 1950
        """
        _order(TODAY, total=Decimal('500.00'))
        _finance(TODAY, previous_coh=Decimal('2000.00'),
                 coins=Decimal('50.00'),
                 cash_advance=Decimal('300.00'),
                 floating_cash=Decimal('200.00'))
        suggested, _, is_auto = _get_previous_coh_info(TMRW)
        self.assertEqual(suggested, Decimal('1950.00'))
        self.assertTrue(is_auto)

    def test_changing_coins_changes_next_day_suggestion(self):
        """After editing coins, the next-day suggestion updates immediately."""
        # Create with coins=200 → ending=800
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'),
                       coins=Decimal('200.00'))
        s1, _, _ = _get_previous_coh_info(TMRW)
        self.assertEqual(s1, Decimal('800.00'))

        # Edit: coins=50 → ending=950
        rec.coins = Decimal('50.00')
        rec.save()
        s2, _, _ = _get_previous_coh_info(TMRW)
        self.assertEqual(s2, Decimal('950.00'))


# ── 7. Field isolation ────────────────────────────────────────────────────────

class FieldIsolationTest(TestCase):
    """Changing one field must not affect any other field."""

    def setUp(self):
        self.user = _user()
        self.client = Client()
        self.client.login(username='cashier', password='pass123')
        _post(self.client, TODAY,
              coins='100.00', cash_advance='200.00', floating_cash='300.00')
        self.rec = DailyFinance.objects.get(date=TODAY)

    def test_updating_coins_does_not_change_cash_advance(self):
        _post(self.client, TODAY, coins='999.00',
              cash_advance='200.00', floating_cash='300.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.cash_advance, Decimal('200.00'))

    def test_updating_coins_does_not_change_floating_cash(self):
        _post(self.client, TODAY, coins='999.00',
              cash_advance='200.00', floating_cash='300.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.floating_cash, Decimal('300.00'))

    def test_updating_cash_advance_does_not_change_coins(self):
        _post(self.client, TODAY, coins='100.00',
              cash_advance='999.00', floating_cash='300.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.coins, Decimal('100.00'))

    def test_updating_cash_advance_does_not_change_floating_cash(self):
        _post(self.client, TODAY, coins='100.00',
              cash_advance='999.00', floating_cash='300.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.floating_cash, Decimal('300.00'))

    def test_updating_floating_does_not_change_coins(self):
        _post(self.client, TODAY, coins='100.00',
              cash_advance='200.00', floating_cash='999.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.coins, Decimal('100.00'))

    def test_updating_floating_does_not_change_cash_advance(self):
        _post(self.client, TODAY, coins='100.00',
              cash_advance='200.00', floating_cash='999.00')
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.cash_advance, Decimal('200.00'))

    def test_separate_dates_completely_isolated(self):
        """Fields on YEST record must not be affected by TODAY's save."""
        yest_rec = _finance(YEST,
                            coins=Decimal('10.00'),
                            cash_advance=Decimal('20.00'),
                            floating_cash=Decimal('30.00'))
        # Save TODAY with different values
        _post(self.client, TODAY,
              coins='500.00', cash_advance='600.00', floating_cash='700.00')

        yest_rec.refresh_from_db()
        self.assertEqual(yest_rec.coins,         Decimal('10.00'))
        self.assertEqual(yest_rec.cash_advance,  Decimal('20.00'))
        self.assertEqual(yest_rec.floating_cash, Decimal('30.00'))


# ── 8. Negative values rejected ──────────────────────────────────────────────

class NegativeValuesTest(TestCase):
    def _form(self, **overrides):
        data = {
            'date': str(TODAY), 'previous_coh': '1000.00',
            'expenses': '0.00', 'expenses_notes': '',
            'gcash_payments': '0.00',
            'coins': '0.00', 'cash_advance': '0.00', 'floating_cash': '0.00',
        }
        data.update(overrides)
        return DailyFinanceForm(data=data)

    def test_negative_coins_rejected(self):
        form = self._form(coins='-10.00')
        self.assertFalse(form.is_valid())
        self.assertIn('coins', form.errors)

    def test_negative_cash_advance_rejected(self):
        form = self._form(cash_advance='-50.00')
        self.assertFalse(form.is_valid())
        self.assertIn('cash_advance', form.errors)

    def test_negative_floating_cash_rejected(self):
        form = self._form(floating_cash='-1.00')
        self.assertFalse(form.is_valid())
        self.assertIn('floating_cash', form.errors)

    def test_negative_rejected_via_post(self):
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        resp = _post(client, TODAY, coins='-10.00')
        self.assertEqual(resp.status_code, 200)  # re-renders with error
        self.assertFalse(DailyFinance.objects.filter(date=TODAY).exists())

    def test_other_fields_not_affected_by_one_negative(self):
        """A negative coins value must not accidentally save valid ca/floating."""
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        _post(client, TODAY,
              coins='-10.00', cash_advance='100.00', floating_cash='50.00')
        # Whole form rejected — nothing saved
        self.assertFalse(DailyFinance.objects.filter(date=TODAY).exists())


# ── 9. Empty field coercion ───────────────────────────────────────────────────

class EmptyFieldCoercionTest(TestCase):
    def _form(self, **overrides):
        data = {
            'date': str(TODAY), 'previous_coh': '1000.00',
            'expenses': '0.00', 'expenses_notes': '',
            'gcash_payments': '0.00',
            'coins': '0.00', 'cash_advance': '0.00', 'floating_cash': '0.00',
        }
        data.update(overrides)
        return DailyFinanceForm(data=data)

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

    def test_all_three_empty_coerce_to_zero(self):
        form = self._form(coins='', cash_advance='', floating_cash='')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['coins'],        Decimal('0.00'))
        self.assertEqual(form.cleaned_data['cash_advance'], Decimal('0.00'))
        self.assertEqual(form.cleaned_data['floating_cash'], Decimal('0.00'))

    def test_empty_via_post_saves_zero(self):
        user = _user()
        client = Client()
        client.login(username='cashier', password='pass123')
        resp = client.post(f'{URL}?date={TODAY}', {
            'date': str(TODAY), 'previous_coh': '1000.00',
            'expenses': '', 'expenses_notes': '',
            'gcash_payments': '', 'coins': '',
            'cash_advance': '', 'floating_cash': '',
        })
        self.assertEqual(resp.status_code, 302)
        rec = DailyFinance.objects.get(date=TODAY)
        self.assertEqual(rec.coins,        Decimal('0.00'))
        self.assertEqual(rec.cash_advance, Decimal('0.00'))
        self.assertEqual(rec.floating_cash, Decimal('0.00'))


# ── 10. Manual COH calculation verification ───────────────────────────────────

class ManualCOHCalculationTest(TestCase):
    """
    Manually compute expected ending_coh for every meaningful combination
    of coins, cash_advance, and floating_cash, then assert the model matches.
    """

    def _check(self, prev, sales_total, coins, ca, floating,
               expenses=Decimal('0.00'), gcash=Decimal('0.00')):
        """Helper: create record, add sales order, assert ending_coh."""
        if sales_total:
            _order(TODAY, total=sales_total)
        rec = _finance(TODAY,
                       previous_coh=prev,
                       expenses=expenses,
                       gcash_payments=gcash,
                       coins=coins,
                       cash_advance=ca,
                       floating_cash=floating)
        expected_rt   = prev + (sales_total or Decimal('0.00'))
        expected_ded  = expenses + gcash + coins + ca + floating
        expected_end  = expected_rt - expected_ded
        self.assertEqual(rec.running_total,    expected_rt,  'running_total')
        self.assertEqual(rec.total_deductions, expected_ded, 'total_deductions')
        self.assertEqual(rec.ending_coh,       expected_end, 'ending_coh')
        return rec

    def test_coins_only(self):
        self._check(Decimal('1000.00'), None,
                    coins=Decimal('75.00'), ca=Decimal('0.00'),
                    floating=Decimal('0.00'))

    def test_ca_only(self):
        self._check(Decimal('1000.00'), None,
                    coins=Decimal('0.00'), ca=Decimal('300.00'),
                    floating=Decimal('0.00'))

    def test_floating_only(self):
        self._check(Decimal('1000.00'), None,
                    coins=Decimal('0.00'), ca=Decimal('0.00'),
                    floating=Decimal('500.00'))

    def test_coins_and_ca(self):
        self._check(Decimal('2000.00'), Decimal('600.00'),
                    coins=Decimal('100.00'), ca=Decimal('250.00'),
                    floating=Decimal('0.00'))

    def test_coins_and_floating(self):
        self._check(Decimal('1500.00'), Decimal('300.00'),
                    coins=Decimal('80.00'), ca=Decimal('0.00'),
                    floating=Decimal('200.00'))

    def test_ca_and_floating(self):
        self._check(Decimal('1200.00'), Decimal('400.00'),
                    coins=Decimal('0.00'), ca=Decimal('150.00'),
                    floating=Decimal('300.00'))

    def test_all_three_with_all_deductions(self):
        """Full formula: all five deductions active simultaneously."""
        self._check(Decimal('3000.00'), Decimal('800.00'),
                    expenses=Decimal('200.00'),
                    gcash=Decimal('400.00'),
                    coins=Decimal('100.00'),
                    ca=Decimal('500.00'),
                    floating=Decimal('300.00'))
        # rt=3800, ded=1500, ending=2300

    def test_fractional_amounts(self):
        """Decimal precision: fractional values must not lose precision."""
        rec = self._check(Decimal('1234.56'), Decimal('789.01'),
                          coins=Decimal('12.34'),
                          ca=Decimal('56.78'),
                          floating=Decimal('90.12'))
        self.assertIsInstance(rec.ending_coh, Decimal)


# ── 11. SQL annotation matches Python property ────────────────────────────────

class AnnotationMatchesPropertyTest(TestCase):
    """
    annotated_ending_coh from the SQL ExpressionWrapper must equal the
    Python ending_coh property for every combination of the three fields.
    """

    def _assert_match(self, **kwargs):
        rec = _finance(TODAY, **kwargs)
        annotated = _annotate_history_qs(
            DailyFinance.objects.filter(pk=rec.pk)
        ).get()
        self.assertEqual(
            annotated.annotated_ending_coh, rec.ending_coh,
            msg=f"SQL annotation != Python property for {kwargs}",
        )

    def test_coins_only(self):
        self._assert_match(previous_coh=Decimal('1000.00'),
                           coins=Decimal('75.00'))

    def test_ca_only(self):
        self._assert_match(previous_coh=Decimal('1000.00'),
                           cash_advance=Decimal('300.00'))

    def test_floating_only(self):
        self._assert_match(previous_coh=Decimal('1000.00'),
                           floating_cash=Decimal('500.00'))

    def test_all_three(self):
        self._assert_match(previous_coh=Decimal('2000.00'),
                           coins=Decimal('50.00'),
                           cash_advance=Decimal('200.00'),
                           floating_cash=Decimal('150.00'))

    def test_all_five_deductions(self):
        self._assert_match(previous_coh=Decimal('3000.00'),
                           expenses=Decimal('100.00'),
                           gcash_payments=Decimal('200.00'),
                           coins=Decimal('50.00'),
                           cash_advance=Decimal('300.00'),
                           floating_cash=Decimal('150.00'))

    def test_all_zero(self):
        self._assert_match(previous_coh=Decimal('1000.00'))
