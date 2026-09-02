"""
Historical Finance data protection tests — Kape De Manubag.

Verifies that historical Finance records are preserved and accurate
under every scenario that could theoretically affect them:

  1.  Finance record deletion blocked (instance, bulk, signal)
  2.  Previous COH stored and unchanged by later events
  3.  Old orders: deactivating a product does not change Finance totals
  4.  Old expenses remain unchanged when Finance is edited on another date
  5.  Product changes (deactivation, price change) do not corrupt Finance
  6.  User deactivation does not affect Finance calculations or prepared_by
  7.  Cancelled orders correctly excluded from Finance
  8.  Finance COH chain integrity across multiple days
  9.  Stored deductions remain unchanged after saving a different day
  10. Finance data survives order status transitions
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DailyFinance, FINANCE_DELETE_ERROR
from apps.finance.views import _get_previous_coh_info
from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem

User = get_user_model()

TODAY  = datetime.date(2026, 8, 28)
YEST   = TODAY - datetime.timedelta(days=1)
DAY2   = TODAY - datetime.timedelta(days=2)
TMRW   = TODAY + datetime.timedelta(days=1)


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


def _category():
    return Category.objects.create(name='Coffee', slug='coffee')


def _product(category=None, price=Decimal('100.00')):
    if category is None:
        category = _category()
    return Product.objects.create(
        category=category, name='Latte', price=price,
        stock_quantity=100,
    )


def _order(date, payment_method='cash', total=Decimal('200.00'),
           status='completed', is_paid=True, product=None):
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(10, 0))
    )
    o = Order.objects.create(
        customer_name='Test',
        status=status,
        is_paid=is_paid,
        payment_method=payment_method,
        total=total,
        subtotal=total,
    )
    if product:
        OrderItem.objects.create(
            order=o,
            product=product,
            product_name=product.name,
            size='none',
            quantity=1,
            unit_price=total,
            subtotal=total,
        )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


# ── 1. Finance record deletion blocked ────────────────────────────────────────

class FinanceDeleteProtectionTest(TestCase):
    """
    DailyFinance records must be impossible to delete via any code path.
    The delete() override and pre_delete signal both raise ValidationError.
    """

    def setUp(self):
        self.rec = _finance(TODAY, previous_coh=Decimal('1000.00'),
                            expenses=Decimal('200.00'))

    def test_instance_delete_raises(self):
        """record.delete() must raise ValidationError."""
        with self.assertRaises(ValidationError) as ctx:
            self.rec.delete()
        self.assertIn('cannot be deleted', str(ctx.exception))

    def test_instance_delete_does_not_remove_record(self):
        try:
            self.rec.delete()
        except (ValidationError, Exception):
            pass
        self.assertTrue(DailyFinance.objects.filter(pk=self.rec.pk).exists())

    def test_bulk_queryset_delete_raises(self):
        """DailyFinance.objects.filter(...).delete() must raise ValidationError."""
        try:
            with transaction.atomic():
                DailyFinance.objects.filter(pk=self.rec.pk).delete()
            self.fail("Expected ValidationError was not raised")
        except ValidationError as e:
            self.assertIn('cannot be deleted', str(e))

    def test_bulk_delete_does_not_remove_record(self):
        try:
            with transaction.atomic():
                DailyFinance.objects.all().delete()
        except (ValidationError, Exception):
            pass
        self.assertTrue(DailyFinance.objects.filter(pk=self.rec.pk).exists())

    def test_error_message_is_descriptive(self):
        """Error message must mention COH chain and financial records."""
        with self.assertRaises(ValidationError) as ctx:
            self.rec.delete()
        msg = str(ctx.exception)
        self.assertIn('Finance records', msg)

    def test_multiple_records_all_protected(self):
        r2 = _finance(YEST, previous_coh=Decimal('500.00'))
        r3 = _finance(DAY2, previous_coh=Decimal('300.00'))
        for rec in [self.rec, r2, r3]:
            with self.assertRaises(ValidationError):
                try:
                    with transaction.atomic():
                        rec.delete()
                except ValidationError:
                    raise
        self.assertEqual(DailyFinance.objects.count(), 3)

    def test_delete_protection_does_not_block_save(self):
        """The delete guard must not interfere with normal saves."""
        self.rec.expenses = Decimal('300.00')
        self.rec.save()
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.expenses, Decimal('300.00'))


# ── 2. Previous COH preserved ─────────────────────────────────────────────────

class PreviousCOHPreservationTest(TestCase):
    """
    previous_coh is a stored value set once at creation time.
    No subsequent event — order changes, product changes, user changes —
    should ever modify a saved previous_coh value.
    """

    def test_previous_coh_unchanged_after_new_orders(self):
        rec = _finance(TODAY, previous_coh=Decimal('1500.00'))
        _order(TODAY, total=Decimal('999.00'))
        rec.refresh_from_db()
        self.assertEqual(rec.previous_coh, Decimal('1500.00'))

    def test_previous_coh_unchanged_after_editing_expenses(self):
        rec = _finance(TODAY, previous_coh=Decimal('2000.00'),
                       expenses=Decimal('100.00'))
        rec.expenses = Decimal('500.00')
        rec.save()
        rec.refresh_from_db()
        self.assertEqual(rec.previous_coh, Decimal('2000.00'))

    def test_previous_coh_carried_forward_correctly(self):
        """Day N ending_coh → Day N+1 suggested previous_coh."""
        _order(TODAY, total=Decimal('400.00'))
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'),
                       expenses=Decimal('200.00'))
        # ending = 1000+400-200 = 1200
        self.assertEqual(rec.ending_coh, Decimal('1200.00'))

        suggested, _, is_auto = _get_previous_coh_info(TMRW)
        self.assertEqual(suggested, Decimal('1200.00'))
        self.assertTrue(is_auto)

    def test_historical_previous_coh_unchanged_by_later_day_edit(self):
        """Editing a later day must not change an earlier day's previous_coh."""
        r1 = _finance(YEST,  previous_coh=Decimal('500.00'),
                      expenses=Decimal('100.00'))
        r2 = _finance(TODAY, previous_coh=Decimal('400.00'))

        # Edit TODAY's record
        r2.expenses = Decimal('250.00')
        r2.save()

        r1.refresh_from_db()
        self.assertEqual(r1.previous_coh, Decimal('500.00'))
        self.assertEqual(r1.expenses,     Decimal('100.00'))


# ── 3. Product deactivation does not corrupt Finance ─────────────────────────

class ProductDeactivationTest(TestCase):
    """
    Deactivating a product must not change any Finance cash_sales totals
    because Finance queries Order.total (stored), not OrderItem fields.
    """

    def setUp(self):
        self.prod = _product()
        self.order = _order(TODAY, total=Decimal('350.00'), product=self.prod)
        self.rec = _finance(TODAY, previous_coh=Decimal('1000.00'))

    def test_cash_sales_before_deactivation(self):
        self.assertEqual(self.rec.cash_sales, Decimal('350.00'))

    def test_deactivate_product_does_not_change_cash_sales(self):
        self.prod.deactivate()
        self.assertEqual(self.rec.cash_sales, Decimal('350.00'))

    def test_deactivate_product_does_not_change_ending_coh(self):
        original_ending = self.rec.ending_coh
        self.prod.deactivate()
        self.assertEqual(self.rec.ending_coh, original_ending)

    def test_deactivate_product_orderitem_snapshot_preserved(self):
        """OrderItem snapshot fields survive product deactivation."""
        self.prod.deactivate()
        item = OrderItem.objects.get(order=self.order)
        self.assertEqual(item.product_name, 'Latte')
        self.assertEqual(item.unit_price,   Decimal('350.00'))
        self.assertEqual(item.subtotal,     Decimal('350.00'))

    def test_order_total_preserved_after_product_deactivation(self):
        self.prod.deactivate()
        self.order.refresh_from_db()
        self.assertEqual(self.order.total, Decimal('350.00'))

    def test_price_change_does_not_affect_historical_finance(self):
        """Changing a product's price must not affect historical order totals."""
        original_cash_sales = self.rec.cash_sales

        self.prod.price = Decimal('999.00')
        self.prod.save()

        # Finance cash_sales still based on the stored Order.total
        self.assertEqual(self.rec.cash_sales, original_cash_sales)


# ── 4. Expenses unchanged on other dates ─────────────────────────────────────

class ExpenseIsolationTest(TestCase):
    """Changing expenses on one day must not affect any other day's record."""

    def setUp(self):
        self.user = _user()
        self.client = Client()
        self.client.login(username='cashier', password='pass123')

    def test_editing_today_expenses_does_not_change_yesterday_expenses(self):
        r_yest  = _finance(YEST,  previous_coh=Decimal('500.00'),
                           expenses=Decimal('100.00'))
        r_today = _finance(TODAY, previous_coh=Decimal('400.00'),
                           expenses=Decimal('50.00'))

        # Edit today's expenses
        self.client.post(f'{reverse("finance:index")}?date={TODAY}', {
            'date': str(TODAY), 'previous_coh': '400.00',
            'expenses': '999.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        })

        r_yest.refresh_from_db()
        self.assertEqual(r_yest.expenses, Decimal('100.00'))

    def test_historical_stored_deductions_preserved_after_new_save(self):
        r_old = _finance(DAY2,
                         expenses=Decimal('150.00'),
                         gcash_payments=Decimal('200.00'),
                         coins=Decimal('50.00'),
                         cash_advance=Decimal('75.00'),
                         floating_cash=Decimal('100.00'))

        # Save a brand new record for today
        self.client.post(f'{reverse("finance:index")}?date={TODAY}', {
            'date': str(TODAY), 'previous_coh': '1000.00',
            'expenses': '999.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        })

        r_old.refresh_from_db()
        self.assertEqual(r_old.expenses,       Decimal('150.00'))
        self.assertEqual(r_old.gcash_payments, Decimal('200.00'))
        self.assertEqual(r_old.coins,          Decimal('50.00'))
        self.assertEqual(r_old.cash_advance,   Decimal('75.00'))
        self.assertEqual(r_old.floating_cash,  Decimal('100.00'))


# ── 5. User deactivation does not affect Finance ─────────────────────────────

class UserDeactivationTest(TestCase):
    """
    Deactivating (soft-deleting) a user must not affect Finance calculations
    or the prepared_by field on existing Finance records.
    """

    def test_user_deactivation_preserves_prepared_by_reference(self):
        cashier = _user('cashier_deact', 'cashier')
        rec = DailyFinance.objects.create(
            date=TODAY,
            previous_coh=Decimal('1000.00'),
            prepared_by=cashier,
        )
        original_pk = cashier.pk

        # Deactivate the user (soft delete)
        cashier.deactivate()
        cashier.refresh_from_db()
        self.assertFalse(cashier.is_active)

        rec.refresh_from_db()
        # prepared_by FK must still point to the deactivated user
        self.assertIsNotNone(rec.prepared_by)
        self.assertEqual(rec.prepared_by.pk, original_pk)

    def test_user_deactivation_does_not_change_finance_calculations(self):
        cashier = _user('cashier_deact2', 'cashier')
        _order(TODAY, total=Decimal('400.00'))
        rec = DailyFinance.objects.create(
            date=TODAY,
            previous_coh=Decimal('1000.00'),
            expenses=Decimal('200.00'),
            prepared_by=cashier,
        )
        original_ending = rec.ending_coh

        cashier.deactivate()

        self.assertEqual(rec.ending_coh, original_ending)
        self.assertEqual(rec.previous_coh, Decimal('1000.00'))
        self.assertEqual(rec.expenses,     Decimal('200.00'))

    def test_hard_delete_of_user_is_impossible(self):
        """User hard-deletion is blocked — prepared_by FK is never nulled."""
        cashier = _user('cashier_nodelete', 'cashier')
        rec = DailyFinance.objects.create(
            date=TODAY,
            previous_coh=Decimal('1000.00'),
            prepared_by=cashier,
        )
        # CustomUser.delete() calls deactivate(), not a real DB delete
        cashier.delete()
        cashier.refresh_from_db()
        # User still exists in DB (soft delete)
        self.assertFalse(cashier.is_active)
        rec.refresh_from_db()
        self.assertIsNotNone(rec.prepared_by)


# ── 6. Cancelled orders ───────────────────────────────────────────────────────

class CancelledOrderTest(TestCase):
    """Cancelled orders must never appear in Finance cash_sales."""

    def test_cancelled_order_excluded_from_cash_sales(self):
        _order(TODAY, status='cancelled', is_paid=False, total=Decimal('500.00'))
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('200.00'))
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.cash_sales, Decimal('200.00'))

    def test_cancelling_completed_order_removes_it_from_finance(self):
        """
        If a completed+paid order is somehow cancelled after Finance is saved,
        Finance cash_sales reflects the correction (live query design).
        """
        order = _order(TODAY, status='completed', is_paid=True,
                       total=Decimal('300.00'))
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.cash_sales, Decimal('300.00'))

        # Simulate retroactive cancellation (allowed by the system for
        # correction purposes — Finance live query picks it up)
        Order.objects.filter(pk=order.pk).update(
            status='cancelled', is_paid=False
        )
        # Re-query cash_sales — must reflect the change
        self.assertEqual(rec.cash_sales, Decimal('0.00'))

    def test_pending_preparing_ready_orders_excluded(self):
        """Non-completed statuses must never count."""
        for status in ['pending', 'preparing', 'ready']:
            _order(TODAY, status=status, is_paid=False, total=Decimal('100.00'))
        rec = _finance(TODAY, previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.cash_sales, Decimal('0.00'))


# ── 7. COH chain integrity across multiple days ───────────────────────────────

class COHChainIntegrityTest(TestCase):
    """
    The COH chain (ending_coh → next-day previous_coh) must remain accurate
    across multiple days even after edits, product changes, or user changes.
    """

    def test_three_day_chain_integrity(self):
        """
        Day 1 ending → Day 2 suggestion → Day 3 suggestion.
        All values must be consistent.
        """
        _order(DAY2,  total=Decimal('200.00'))
        _order(YEST,  total=Decimal('300.00'))
        _order(TODAY, total=Decimal('400.00'))

        r1 = _finance(DAY2, previous_coh=Decimal('1000.00'),
                      expenses=Decimal('100.00'))
        # 1000 + 200 - 100 = 1100
        self.assertEqual(r1.ending_coh, Decimal('1100.00'))

        r2 = _finance(YEST, previous_coh=r1.ending_coh,
                      expenses=Decimal('150.00'))
        # 1100 + 300 - 150 = 1250
        self.assertEqual(r2.ending_coh, Decimal('1250.00'))

        r3 = _finance(TODAY, previous_coh=r2.ending_coh,
                      expenses=Decimal('200.00'))
        # 1250 + 400 - 200 = 1450
        self.assertEqual(r3.ending_coh, Decimal('1450.00'))

        # Tomorrow's suggestion
        suggested, _, is_auto = _get_previous_coh_info(TMRW)
        self.assertEqual(suggested, Decimal('1450.00'))
        self.assertTrue(is_auto)

    def test_editing_middle_day_propagates_to_next_suggestion(self):
        """Editing Day 1 expenses changes its ending_coh → Day 2 suggestion."""
        r1 = _finance(YEST,  previous_coh=Decimal('1000.00'),
                      expenses=Decimal('100.00'))
        # ending = 900

        # Edit: increase expenses to 400
        r1.expenses = Decimal('400.00')
        r1.save()
        # ending now = 600

        s, _, _ = _get_previous_coh_info(TODAY)
        self.assertEqual(s, Decimal('600.00'))

    def test_product_deactivation_does_not_break_chain(self):
        """Deactivating a product must not disrupt the COH chain."""
        prod = _product()
        _order(YEST, total=Decimal('500.00'), product=prod)
        r1 = _finance(YEST, previous_coh=Decimal('1000.00'))
        # ending = 1500

        prod.deactivate()  # should not affect Finance

        r2 = _finance(TODAY, previous_coh=r1.ending_coh)
        self.assertEqual(r2.previous_coh, Decimal('1500.00'))

    def test_historical_record_not_modified_by_chain_check(self):
        """_get_previous_coh_info reads but never modifies records."""
        r1 = _finance(YEST, previous_coh=Decimal('800.00'),
                      expenses=Decimal('100.00'))
        original_pk  = r1.pk
        original_pcoh = r1.previous_coh
        original_exp  = r1.expenses

        _get_previous_coh_info(TODAY)  # read only

        r1.refresh_from_db()
        self.assertEqual(r1.pk,           original_pk)
        self.assertEqual(r1.previous_coh, original_pcoh)
        self.assertEqual(r1.expenses,     original_exp)


# ── 8. Finance data survives order status transitions ─────────────────────────

class OrderStatusTransitionTest(TestCase):
    """
    Finance cash_sales correctly reflects order status transitions.
    Only is_paid=True + status='completed' orders count.
    """

    def test_only_completed_paid_orders_counted(self):
        """Status advancing to 'completed' without is_paid=True must not count."""
        # Advance without paying (quick_status_advance path)
        order = _order(TODAY, status='completed', is_paid=False,
                       total=Decimal('300.00'))
        rec = _finance(TODAY)
        self.assertEqual(rec.cash_sales, Decimal('0.00'))

    def test_payment_without_completion_not_counted(self):
        """is_paid=True but status != 'completed' must not count."""
        order = _order(TODAY, status='pending', is_paid=True,
                       total=Decimal('300.00'))
        rec = _finance(TODAY)
        self.assertEqual(rec.cash_sales, Decimal('0.00'))

    def test_completed_and_paid_counted(self):
        _order(TODAY, status='completed', is_paid=True, total=Decimal('300.00'))
        rec = _finance(TODAY)
        self.assertEqual(rec.cash_sales, Decimal('300.00'))

    def test_gcash_order_not_in_cash_sales(self):
        _order(TODAY, status='completed', is_paid=True,
               payment_method='gcash', total=Decimal('300.00'))
        rec = _finance(TODAY)
        self.assertEqual(rec.cash_sales, Decimal('0.00'))

    def test_cash_and_gcash_independent(self):
        _order(TODAY, payment_method='cash',  total=Decimal('200.00'))
        _order(TODAY, payment_method='gcash', total=Decimal('300.00'))
        rec = _finance(TODAY)
        self.assertEqual(rec.cash_sales, Decimal('200.00'))


# ── 9. Hard-delete guard regression ───────────────────────────────────────────

class DeleteGuardRegressionTest(TestCase):
    """
    Verify the delete guard matches the pattern used for Orders and Products:
    both the instance method and the bulk queryset delete are blocked.
    """

    def test_finance_delete_error_constant_exists(self):
        """FINANCE_DELETE_ERROR constant must be importable and descriptive."""
        self.assertIn('Finance records', FINANCE_DELETE_ERROR)

    def test_delete_method_raises_validation_error(self):
        rec = _finance(TODAY)
        with self.assertRaises(ValidationError):
            rec.delete()

    def test_delete_signal_blocks_queryset_delete(self):
        _finance(TODAY)
        _finance(YEST)
        try:
            with transaction.atomic():
                DailyFinance.objects.all().delete()
            self.fail("Expected ValidationError")
        except ValidationError:
            pass
        self.assertEqual(DailyFinance.objects.count(), 2)

    def test_normal_operations_still_work_after_guard_added(self):
        """The delete guard must not break create, save, or query operations."""
        rec = _finance(TODAY, expenses=Decimal('100.00'))
        self.assertTrue(DailyFinance.objects.filter(pk=rec.pk).exists())

        rec.expenses = Decimal('200.00')
        rec.save()
        rec.refresh_from_db()
        self.assertEqual(rec.expenses, Decimal('200.00'))

        self.assertEqual(
            DailyFinance.objects.filter(date=TODAY).count(), 1
        )
