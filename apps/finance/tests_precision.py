"""
Monetary precision tests — Kape De Manubag Finance module.

Verifies that all monetary calculations use exact Decimal arithmetic
throughout the full stack: Order creation → payment → Finance cash_sales.

Scenarios covered:
  1.  Whole peso values (₱100.00, ₱500.00)
  2.  Decimal centavo values (₱123.10, ₱99.90, ₱376.80)
  3.  IEEE 754 problematic amounts (.10, .20, .30, .40, .70, .80, .90)
  4.  Multiple transactions — summation precision
  5.  Takeout packaging charges (₱6.00 × N items)
  6.  Cash payments — amount_paid and change_amount stored as exact Decimal
  7.  GCash payments — same precision guarantee
  8.  Expenses — Decimal arithmetic in Finance formula
  9.  Full COH calculation with mixed deductions
  10. process_payment() regression: amount stored as Decimal, not float
  11. No float contamination in Finance model properties
  12. Field type assertions — all monetary fields are DecimalField
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DailyFinance
from apps.finance.views import _get_cash_sales_for_date
from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem

User = get_user_model()

TODAY = datetime.date(2026, 8, 28)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username, role='cashier'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _finance(date=TODAY, previous_coh=Decimal('1000.00'), **kwargs):
    return DailyFinance.objects.create(
        date=date, previous_coh=previous_coh, **kwargs
    )


def _order(total, payment_method='cash', date=TODAY,
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


def _category(name='Coffee'):
    return Category.objects.create(name=name, slug=name.lower())


def _product(price, category=None, packaging=False):
    if category is None:
        category = _category()
        category.is_packaging_required = packaging
        category.save()
    return Product.objects.create(
        category=category, name='Test Product',
        price=price, stock_quantity=100,
    )


# ── 1. Whole peso values ──────────────────────────────────────────────────────

class WholePesoTest(TestCase):
    def test_whole_peso_stored_exactly(self):
        o = _order(Decimal('500.00'))
        o.refresh_from_db()
        self.assertEqual(o.total, Decimal('500.00'))
        self.assertIsInstance(o.total, Decimal)

    def test_whole_peso_cash_sales(self):
        _order(Decimal('500.00'))
        _order(Decimal('300.00'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('800.00'))
        self.assertIsInstance(total, Decimal)

    def test_whole_peso_finance_calculation(self):
        _order(Decimal('500.00'))
        rec = _finance(previous_coh=Decimal('1000.00'))
        self.assertEqual(rec.running_total, Decimal('1500.00'))
        self.assertEqual(rec.ending_coh,    Decimal('1500.00'))
        self.assertIsInstance(rec.running_total, Decimal)
        self.assertIsInstance(rec.ending_coh,    Decimal)


# ── 2. Decimal centavo values ─────────────────────────────────────────────────

class DecimalCentavoTest(TestCase):
    """
    Amounts like ₱123.10 are problematic for float (IEEE 754 gives
    123.09999999999999).  All arithmetic must remain Decimal.
    """

    TRICKY = [
        Decimal('123.10'),
        Decimal('99.90'),
        Decimal('376.80'),
        Decimal('45.70'),
        Decimal('200.20'),
    ]

    def test_tricky_amounts_stored_exactly(self):
        for amount in self.TRICKY:
            o = _order(amount)
            o.refresh_from_db()
            self.assertEqual(o.total, amount,
                f'Order total {amount} was stored imprecisely')

    def test_tricky_amounts_cash_sales_sum_exact(self):
        for amount in self.TRICKY:
            _order(amount)
        total, _ = _get_cash_sales_for_date(TODAY)
        expected = sum(self.TRICKY)
        self.assertEqual(total, expected)
        self.assertIsInstance(total, Decimal)

    def test_tricky_amount_finance_ending_coh_exact(self):
        """₱123.10 order with ₱99.90 expense: ending = 1000+123.10-99.90 = 1023.20"""
        _order(Decimal('123.10'))
        rec = _finance(previous_coh=Decimal('1000.00'),
                       expenses=Decimal('99.90'))
        self.assertEqual(rec.ending_coh, Decimal('1023.20'))
        self.assertIsInstance(rec.ending_coh, Decimal)


# ── 3. IEEE 754 problematic centavo values ────────────────────────────────────

class IEEE754BoundaryTest(TestCase):
    """
    These centavo values are the classic float precision traps.
    All must be stored and computed exactly as Decimal.
    """
    AMOUNTS = [
        Decimal('0.10'), Decimal('0.20'), Decimal('0.30'),
        Decimal('0.40'), Decimal('0.70'), Decimal('0.80'), Decimal('0.90'),
    ]

    def test_ieee754_amounts_stored_exactly(self):
        for amount in self.AMOUNTS:
            # Base price of ₱100 + tricky centavo
            total = Decimal('100.00') + amount
            o = _order(total)
            o.refresh_from_db()
            self.assertEqual(o.total, total,
                f'IEEE 754 trap: {total} stored imprecisely as {o.total}')

    def test_ieee754_sum_exact(self):
        """Sum of all tricky centavo amounts must be exact."""
        expected = Decimal('0.00')
        for amount in self.AMOUNTS:
            total = Decimal('100.00') + amount
            _order(total)
            expected += total
        cash_total, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, expected)

    def test_float_path_would_produce_wrong_result(self):
        """
        Document exactly where float fails.
        This test does NOT use the production code — it demonstrates why
        float must not be used in the payment path.
        """
        total_as_decimal = Decimal('123.10')
        # float path (what the old code did):
        amount_as_float   = float('500.00')
        total_as_float    = float(total_as_decimal)
        change_via_float  = amount_as_float - total_as_float
        # float subtraction gives 376.9 but may print as 376.89999...
        self.assertNotEqual(str(change_via_float), '376.90',
            "This test documents that float subtraction is imprecise")

        # Decimal path (what the fixed code does):
        amount_as_decimal = Decimal('500.00')
        change_via_decimal = amount_as_decimal - total_as_decimal
        self.assertEqual(change_via_decimal, Decimal('376.90'))
        self.assertEqual(str(change_via_decimal), '376.90')


# ── 4. Multiple transactions — summation precision ────────────────────────────

class MultipleTransactionPrecisionTest(TestCase):
    def test_many_tricky_amounts_sum_exactly(self):
        """10 orders of ₱99.90 = ₱999.00, not ₱998.99999..."""
        for _ in range(10):
            _order(Decimal('99.90'))
        total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(total, Decimal('999.00'))
        self.assertEqual(count, 10)

    def test_mixed_precision_amounts_sum_exactly(self):
        amounts = [
            Decimal('50.10'), Decimal('75.20'), Decimal('100.30'),
            Decimal('200.40'), Decimal('25.70'),
        ]
        for a in amounts:
            _order(a)
        cash_total, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, sum(amounts))
        self.assertEqual(cash_total, Decimal('451.70'))

    def test_finance_model_aggregation_exact(self):
        """DailyFinance.cash_sales (DB Sum) returns exact Decimal."""
        amounts = [Decimal('123.10'), Decimal('456.90'), Decimal('789.00')]
        for a in amounts:
            _order(a)
        rec = _finance(previous_coh=Decimal('0.00'))
        self.assertEqual(rec.cash_sales, Decimal('1369.00'))
        self.assertIsInstance(rec.cash_sales, Decimal)


# ── 5. Takeout packaging charges ─────────────────────────────────────────────

class TakeoutPrecisionTest(TestCase):
    def test_packaging_fee_decimal_arithmetic(self):
        """₱6.00 × N items must use Decimal throughout."""
        from apps.orders.services import calculate_packaging_fee_for_items, get_packaging_fee_per_item
        fee_per_item = get_packaging_fee_per_item()
        self.assertIsInstance(fee_per_item, Decimal)
        self.assertEqual(fee_per_item, Decimal('6.00'))

    def test_packaging_fee_calculation_exact(self):
        from apps.orders.services import calculate_packaging_fee_for_items, get_packaging_fee_per_item
        cat = _category('Meals')
        cat.is_packaging_required = True
        cat.save()
        prod = _product(Decimal('100.00'), category=cat, packaging=True)
        fee_per_item = get_packaging_fee_per_item()
        total_fee, eligible = calculate_packaging_fee_for_items(
            [(prod, 3)], fee_per_item
        )
        # 3 × ₱6.00 = ₱18.00 exactly
        self.assertEqual(total_fee, Decimal('18.00'))
        self.assertIsInstance(total_fee, Decimal)

    def test_packaging_fee_many_items_exact(self):
        """10 eligible items × ₱6.00 = ₱60.00 — no float drift."""
        from apps.orders.services import calculate_packaging_fee_for_items, get_packaging_fee_per_item
        cat = _category('Food')
        cat.is_packaging_required = True
        cat.save()
        prod = _product(Decimal('50.00'), category=cat, packaging=True)
        fee_per_item = get_packaging_fee_per_item()
        total_fee, _ = calculate_packaging_fee_for_items(
            [(prod, 10)], fee_per_item
        )
        self.assertEqual(total_fee, Decimal('60.00'))


# ── 6. Cash payment — process_payment precision (BUG-1 regression) ───────────

class CashPaymentPrecisionTest(TestCase):
    """
    Regression tests for BUG-1: process_payment() previously used float()
    to parse POST amount_paid, causing IEEE 754 imprecision in stored values.
    """

    def setUp(self):
        self.cashier = _user('cashier_precision')
        self.client = Client()
        self.client.login(username='cashier_precision', password='pass123')

    def _pay(self, order, amount_str):
        url = reverse('orders:process_payment', kwargs={'pk': order.pk})
        return self.client.post(url, {
            'payment_method': 'cash',
            'amount_paid': amount_str,
        })

    def _pending_order(self, total):
        return _order(total, status='pending', is_paid=False,
                      payment_method='')

    def test_exact_payment_stored_as_decimal(self):
        """Paying exactly ₱123.10 must store Decimal('123.10'), not float."""
        order = self._pending_order(Decimal('123.10'))
        self._pay(order, '123.10')
        order.refresh_from_db()
        self.assertIsInstance(order.amount_paid, Decimal)
        self.assertEqual(order.amount_paid, Decimal('123.10'))

    def test_change_calculated_exactly(self):
        """₱500.00 paid for ₱123.10 order → change = ₱376.90 exactly."""
        order = self._pending_order(Decimal('123.10'))
        self._pay(order, '500.00')
        order.refresh_from_db()
        self.assertEqual(order.change_amount, Decimal('376.90'))
        self.assertIsInstance(order.change_amount, Decimal)

    def test_tricky_change_calculation(self):
        """₱1000.00 paid for ₱99.90 order → change = ₱900.10 exactly."""
        order = self._pending_order(Decimal('99.90'))
        self._pay(order, '1000.00')
        order.refresh_from_db()
        self.assertEqual(order.change_amount, Decimal('900.10'))

    def test_exact_amount_accepted(self):
        """Paying exactly the order total is accepted."""
        order = self._pending_order(Decimal('376.80'))
        resp = self._pay(order, '376.80')
        data = resp.json()
        self.assertTrue(data['success'],
            f"Exact payment rejected: {data.get('error')}")
        order.refresh_from_db()
        self.assertEqual(order.change_amount, Decimal('0.00'))

    def test_insufficient_payment_rejected(self):
        """Payment below the order total must be rejected."""
        order = self._pending_order(Decimal('123.10'))
        resp = self._pay(order, '123.00')   # 10 centavos short
        data = resp.json()
        self.assertFalse(data['success'])
        order.refresh_from_db()
        self.assertFalse(order.is_paid)

    def test_invalid_amount_rejected(self):
        """Non-numeric amount_paid must return an error."""
        order = self._pending_order(Decimal('100.00'))
        resp = self._pay(order, 'abc')
        data = resp.json()
        self.assertFalse(data['success'])
        order.refresh_from_db()
        self.assertFalse(order.is_paid)

    def test_amount_paid_is_decimal_type_after_save(self):
        """After saving, order.amount_paid must be a Decimal, not a float."""
        order = self._pending_order(Decimal('200.00'))
        self._pay(order, '200.00')
        order.refresh_from_db()
        self.assertNotIsInstance(order.amount_paid, float,
            "amount_paid was stored as float — Decimal required")
        self.assertIsInstance(order.amount_paid, Decimal)

    def test_whole_peso_payment_exact(self):
        order = self._pending_order(Decimal('500.00'))
        self._pay(order, '500.00')
        order.refresh_from_db()
        self.assertEqual(order.amount_paid,  Decimal('500.00'))
        self.assertEqual(order.change_amount, Decimal('0.00'))

    def test_payment_feeds_finance_cash_sales_exactly(self):
        """A payment stored as Decimal is correctly picked up by Finance."""
        order = self._pending_order(Decimal('123.10'))
        self._pay(order, '200.00')
        order.refresh_from_db()

        # Finance cash_sales must see the stored order.total exactly
        cash_total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, Decimal('123.10'))
        self.assertEqual(count, 1)


# ── 7. GCash payment precision ────────────────────────────────────────────────

class GCashPaymentPrecisionTest(TestCase):
    def setUp(self):
        self.cashier = _user('cashier_gcash_p')
        self.client = Client()
        self.client.login(username='cashier_gcash_p', password='pass123')

    def _pay_gcash(self, order, amount_str):
        url = reverse('orders:process_payment', kwargs={'pk': order.pk})
        return self.client.post(url, {
            'payment_method': 'gcash',
            'amount_paid': amount_str,
        })

    def test_gcash_amount_stored_as_decimal(self):
        order = _order(Decimal('99.90'), status='pending',
                       is_paid=False, payment_method='')
        self._pay_gcash(order, '99.90')
        order.refresh_from_db()
        self.assertIsInstance(order.amount_paid, Decimal)
        self.assertEqual(order.amount_paid, Decimal('99.90'))
        self.assertEqual(order.payment_method, 'gcash')

    def test_gcash_not_in_cash_sales(self):
        """GCash payment must not appear in Finance cash_sales."""
        order = _order(Decimal('150.00'), status='pending',
                       is_paid=False, payment_method='')
        self._pay_gcash(order, '150.00')
        cash_total, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash_total, Decimal('0.00'))
        self.assertEqual(count, 0)

    def test_gcash_change_is_zero_for_exact_payment(self):
        order = _order(Decimal('200.00'), status='pending',
                       is_paid=False, payment_method='')
        self._pay_gcash(order, '200.00')
        order.refresh_from_db()
        self.assertEqual(order.change_amount, Decimal('0.00'))


# ── 8. Expenses Decimal precision ────────────────────────────────────────────

class ExpensePrecisionTest(TestCase):
    def test_expense_decimal_arithmetic_exact(self):
        """₱1000.00 previous_coh + ₱0 sales - ₱123.10 expense = ₱876.90"""
        rec = _finance(previous_coh=Decimal('1000.00'),
                       expenses=Decimal('123.10'))
        self.assertEqual(rec.ending_coh, Decimal('876.90'))
        self.assertIsInstance(rec.ending_coh, Decimal)

    def test_multiple_deductions_exact(self):
        """All five deduction fields summed exactly."""
        rec = _finance(
            previous_coh=Decimal('2000.00'),
            expenses=Decimal('123.10'),
            gcash_payments=Decimal('99.90'),
            coins=Decimal('50.20'),
            cash_advance=Decimal('200.30'),
            floating_cash=Decimal('100.40'),
        )
        # deductions = 123.10+99.90+50.20+200.30+100.40 = 573.90
        self.assertEqual(rec.total_deductions, Decimal('573.90'))
        self.assertEqual(rec.ending_coh, Decimal('1426.10'))

    def test_expense_type_is_decimal(self):
        rec = _finance(expenses=Decimal('123.10'))
        self.assertIsInstance(rec.expenses, Decimal)


# ── 9. Full COH calculation precision ────────────────────────────────────────

class FullCOHPrecisionTest(TestCase):
    """
    End-to-end: cash sales → running_total → ending_coh, all exact Decimal.
    """

    def test_full_formula_exact_with_tricky_amounts(self):
        """
        previous_coh=₱1234.50, cash_sales=₱567.80, expenses=₱123.10
        running = 1234.50 + 567.80 = 1802.30
        ending  = 1802.30 - 123.10 = 1679.20
        """
        _order(Decimal('567.80'))
        rec = _finance(previous_coh=Decimal('1234.50'),
                       expenses=Decimal('123.10'))
        self.assertEqual(rec.cash_sales,    Decimal('567.80'))
        self.assertEqual(rec.running_total, Decimal('1802.30'))
        self.assertEqual(rec.ending_coh,    Decimal('1679.20'))
        self.assertIsInstance(rec.ending_coh, Decimal)

    def test_coh_chain_preserves_precision(self):
        """Two-day chain with tricky amounts must not accumulate float errors."""
        from apps.finance.views import _get_previous_coh_info
        import datetime

        yest = TODAY - datetime.timedelta(days=1)
        dt_yest = timezone.make_aware(
            datetime.datetime.combine(yest, datetime.time(10, 0))
        )
        o = Order.objects.create(
            customer_name='Test', status='completed',
            is_paid=True, payment_method='cash',
            total=Decimal('99.90'), subtotal=Decimal('99.90'),
        )
        Order.objects.filter(pk=o.pk).update(created_at=dt_yest)

        r1 = DailyFinance.objects.create(
            date=yest,
            previous_coh=Decimal('1000.00'),
            expenses=Decimal('123.10'),
        )
        # 1000 + 99.90 - 123.10 = 976.80
        self.assertEqual(r1.ending_coh, Decimal('976.80'))

        suggested, _, _ = _get_previous_coh_info(TODAY)
        self.assertEqual(suggested, Decimal('976.80'))
        self.assertIsInstance(suggested, Decimal)

    def test_annotated_ending_coh_exact(self):
        """SQL annotation must produce same exact result as Python property."""
        from apps.finance.views import _annotate_history_qs
        _order(Decimal('123.10'))
        rec = _finance(previous_coh=Decimal('1000.00'),
                       expenses=Decimal('99.90'))
        annotated = _annotate_history_qs(
            DailyFinance.objects.filter(pk=rec.pk)
        ).get()
        self.assertEqual(annotated.annotated_ending_coh, rec.ending_coh)
        self.assertEqual(annotated.annotated_ending_coh, Decimal('1023.20'))


# ── 10. Field type assertions ─────────────────────────────────────────────────

class FieldTypeAssertionTest(TestCase):
    """All monetary model fields must be DecimalField instances."""

    def _get_field(self, model, name):
        return model._meta.get_field(name)

    def test_order_fields_are_decimal(self):
        from django.db.models import DecimalField
        from apps.orders.models import Order
        for fname in ['subtotal', 'discount', 'packaging_fee', 'total',
                      'amount_paid', 'change_amount']:
            f = self._get_field(Order, fname)
            self.assertIsInstance(f, DecimalField,
                f'Order.{fname} is not DecimalField')
            self.assertEqual(f.decimal_places, 2)

    def test_finance_fields_are_decimal(self):
        from django.db.models import DecimalField
        for fname in ['previous_coh', 'expenses', 'gcash_payments',
                      'coins', 'cash_advance', 'floating_cash']:
            f = self._get_field(DailyFinance, fname)
            self.assertIsInstance(f, DecimalField,
                f'DailyFinance.{fname} is not DecimalField')
            self.assertEqual(f.decimal_places, 2)

    def test_product_price_fields_are_decimal(self):
        from django.db.models import DecimalField
        from apps.menu.models import Product
        for fname in ['price', 'price_medium', 'price_large', 'price_hot']:
            f = self._get_field(Product, fname)
            self.assertIsInstance(f, DecimalField,
                f'Product.{fname} is not DecimalField')

    def test_finance_default_values_are_decimal_literals(self):
        """Model defaults must be Decimal instances, not floats."""
        rec = DailyFinance(date=TODAY, previous_coh=Decimal('0.00'))
        for attr in ['expenses', 'gcash_payments', 'coins',
                     'cash_advance', 'floating_cash']:
            val = getattr(rec, attr)
            self.assertIsInstance(val, Decimal,
                f'DailyFinance.{attr} default is not Decimal')
            self.assertNotIsInstance(val, float,
                f'DailyFinance.{attr} default is float — use Decimal literals')
