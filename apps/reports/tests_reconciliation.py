"""
Sales Reports complete reconciliation tests — Kape De Manubag.

Uses controlled known-value transactions to verify every mathematical
relationship between all report sections and the underlying Orders.

Controlled dataset used in the main ReconciliationBaseTest:

  Order A — Cash, Dine-In
    Latte (Coffee)   qty=2, unit=₱120  subtotal=₱240
    Order.total = ₱240  (no packaging, no discount)

  Order B — GCash, Take-Out
    Cake (Food)      qty=1, unit=₱80   subtotal=₱80
    packaging_fee = ₱6 (one eligible item)
    Order.total = ₱80 + ₱6 = ₱86

  Order C — Cash, Dine-In
    Latte (Coffee)   qty=1, unit=₱120  subtotal=₱120
    Cake  (Food)     qty=2, unit=₱80   subtotal=₱160
    Order.total = ₱280  (no packaging, no discount)

  Order D — Cancelled (never paid)
    Total = ₱999 — must not appear anywhere

Known values from the above dataset:
  Total Revenue        = 240 + 86 + 280 = ₱606.00  (Order.total)
  Finance cash         = 240 + 280      = ₱520.00
  Finance GCash        = 86             = ₱86.00
  Finance cash+GCash   = 520 + 86       = ₱606.00
  Item subtotals       = 240 + 80 + 120 + 160 = ₱600.00  (OrderItem.subtotal)
  Packaging fees       = ₱6.00
  total_revenue - item_subtotals = 606 - 600 = ₱6.00  (= packaging fees)

  Top Products:
    Latte  qty=3, revenue=₱360  (orders A+C: 2×120 + 1×120)
    Cake   qty=3, revenue=₱240  (orders B+C: 1×80 + 2×80)
    sum(top_products) = ₱600.00  (item subtotals, NOT packaging)

  Sales by Category:
    Coffee  revenue=₱360 (Latte × 3)
    Food    revenue=₱240 (Cake × 3)
    sum(category_sales) = ₱600.00

  Daily Breakdown (one day):
    Total = ₱606.00  (matches total_revenue exactly)

Reconciliation rules verified:
  total_revenue == sum(daily_sales)                [EXACT: 606 == 606]
  total_revenue == finance_cash + finance_gcash     [EXACT: 606 == 520+86]
  total_revenue > sum(top_products)                [606 > 600 — packaging fee]
  sum(top_products) == sum(category_sales)          [EXACT: 600 == 600]
  total_revenue - sum(top_products) == packaging    [EXACT: 6 == 6]
  cancelled order appears in no section
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.views import _get_cash_sales_for_date, _get_gcash_sales_for_date
from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem

User = get_user_model()

TODAY = timezone.localdate()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _category(name):
    return Category.objects.create(
        name=name,
        slug=name.lower().replace(' ', '-'),
        is_packaging_required=(name == 'Food'),
    )


def _product(name, price, category):
    return Product.objects.create(
        category=category, name=name, price=price, stock_quantity=100,
    )


def _order(payment='cash', order_type='dine_in', total=Decimal('0.00'),
           subtotal=Decimal('0.00'), packaging_fee=Decimal('0.00'),
           discount=Decimal('0.00'), status='completed', is_paid=True,
           date=TODAY):
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(10, 0))
    )
    o = Order.objects.create(
        customer_name='Test',
        status=status, is_paid=is_paid,
        payment_method=payment,
        order_type=order_type,
        total=total, subtotal=subtotal,
        packaging_fee=packaging_fee,
        discount=discount,
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


def _add_item(order, product, quantity, unit_price, category_name):
    sub = unit_price * quantity
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        category_name=category_name,
        size='none',
        quantity=quantity,
        unit_price=unit_price,
        subtotal=sub,
    )
    return sub


def _reports(client, start=None, end=None):
    return client.get(
        reverse('reports:index'),
        {'start': str(start or TODAY), 'end': str(end or TODAY)},
    )


# ── Controlled dataset builder ────────────────────────────────────────────────

class ReconciliationBase(TestCase):
    """
    Sets up the controlled dataset described in the module docstring.
    All test classes that need the full dataset inherit from this.
    """

    def setUp(self):
        self.admin  = _user('admin_reconcile')
        self.client = Client()
        self.client.login(username='admin_reconcile', password='pass123')

        coffee = _category('Coffee')
        food   = _category('Food')   # is_packaging_required=True
        self.latte = _product('Latte', Decimal('120.00'), coffee)
        self.cake  = _product('Cake',  Decimal('80.00'),  food)

        # ── Order A: Cash, Dine-In, 2× Latte ──────────────────────────────
        # subtotal = 240, packaging = 0, total = 240
        self.order_a = _order(
            payment='cash', order_type='dine_in',
            subtotal=Decimal('240.00'),
            total=Decimal('240.00'),
        )
        _add_item(self.order_a, self.latte, 2, Decimal('120.00'), 'Coffee')

        # ── Order B: GCash, Take-Out, 1× Cake + packaging ─────────────────
        # subtotal = 80, packaging = 6, total = 86
        self.order_b = _order(
            payment='gcash', order_type='takeout',
            subtotal=Decimal('80.00'),
            packaging_fee=Decimal('6.00'),
            total=Decimal('86.00'),
        )
        _add_item(self.order_b, self.cake, 1, Decimal('80.00'), 'Food')

        # ── Order C: Cash, Dine-In, 1× Latte + 2× Cake ───────────────────
        # subtotal = 120 + 160 = 280, packaging = 0, total = 280
        self.order_c = _order(
            payment='cash', order_type='dine_in',
            subtotal=Decimal('280.00'),
            total=Decimal('280.00'),
        )
        _add_item(self.order_c, self.latte, 1, Decimal('120.00'), 'Coffee')
        _add_item(self.order_c, self.cake,  2, Decimal('80.00'),  'Food')

        # ── Order D: Cancelled — must never appear anywhere ───────────────
        self.order_d = _order(
            payment='cash', status='cancelled', is_paid=False,
            total=Decimal('999.00'), subtotal=Decimal('999.00'),
        )
        _add_item(self.order_d, self.latte, 10, Decimal('120.00'), 'Coffee')

        # ── Known values ──────────────────────────────────────────────────
        self.TOTAL_REVENUE   = Decimal('606.00')  # A+B+C order totals
        self.FINANCE_CASH    = Decimal('520.00')  # A+C
        self.FINANCE_GCASH   = Decimal('86.00')   # B
        self.ITEM_SUBTOTALS  = Decimal('600.00')  # all items, no packaging
        self.PACKAGING_FEES  = Decimal('6.00')    # Order B only
        self.LATTE_REVENUE   = Decimal('360.00')  # 3 × 120
        self.CAKE_REVENUE    = Decimal('240.00')  # 3 × 80
        self.COFFEE_REVENUE  = Decimal('360.00')  # same as Latte (only product in category)
        self.FOOD_REVENUE    = Decimal('240.00')  # same as Cake


# ══════════════════════════════════════════════════════════════════════════════
# RULE 1: total_revenue == sum(daily_sales)  [EXACT]
# ══════════════════════════════════════════════════════════════════════════════

class DailySalesReconciliationTest(ReconciliationBase):
    def test_total_revenue_correct(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], self.TOTAL_REVENUE)

    def test_total_orders_correct(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_orders'], 3)  # A, B, C only

    def test_sum_of_daily_equals_total_revenue(self):
        resp      = _reports(self.client)
        daily_sum = sum(d['total'] for d in resp.context['daily_sales'])
        self.assertAlmostEqual(daily_sum, float(self.TOTAL_REVENUE), places=2)

    def test_single_day_entry_with_correct_total(self):
        daily = list(resp.context['daily_sales']
                     for resp in [_reports(self.client)])[0]
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(daily[0]['total'], float(self.TOTAL_REVENUE), places=2)
        self.assertEqual(daily[0]['count'], 3)


# ══════════════════════════════════════════════════════════════════════════════
# RULE 2: total_revenue == finance_cash + finance_gcash  [EXACT]
# ══════════════════════════════════════════════════════════════════════════════

class FinanceReconciliationTest(ReconciliationBase):
    def test_finance_cash_correct(self):
        cash, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash,  self.FINANCE_CASH)
        self.assertEqual(count, 2)   # Order A + Order C

    def test_finance_gcash_correct(self):
        gcash, count = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(gcash, self.FINANCE_GCASH)
        self.assertEqual(count, 1)   # Order B only

    def test_finance_cash_plus_gcash_equals_total_revenue(self):
        cash,  _ = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        resp      = _reports(self.client)
        self.assertEqual(cash + gcash, resp.context['total_revenue'])

    def test_finance_agrees_with_reports_for_cash(self):
        """Finance cash_sales + gcash_sales == Reports total_revenue."""
        cash,  _ = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(cash + gcash, self.TOTAL_REVENUE)

    def test_cancelled_excluded_from_finance(self):
        cash, count = _get_cash_sales_for_date(TODAY)
        self.assertNotEqual(cash, Decimal('999.00'))
        self.assertEqual(count, 2)


# ══════════════════════════════════════════════════════════════════════════════
# RULE 3: total_revenue == dashboard_daily_sales  [EXACT, same day]
# ══════════════════════════════════════════════════════════════════════════════

class DashboardReconciliationTest(ReconciliationBase):
    def test_dashboard_daily_equals_reports_total(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        resp  = _reports(self.client)
        self.assertEqual(stats['daily_sales'],  resp.context['total_revenue'])
        self.assertEqual(stats['daily_orders'], resp.context['total_orders'])

    def test_dashboard_not_inflated_by_cancelled(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        self.assertEqual(stats['daily_sales'], self.TOTAL_REVENUE)
        self.assertEqual(stats['daily_orders'], 3)


# ══════════════════════════════════════════════════════════════════════════════
# RULE 4: top_products totals vs total_revenue — packaging fee gap
# ══════════════════════════════════════════════════════════════════════════════

class TopProductsReconciliationTest(ReconciliationBase):
    def _top(self):
        return list(_reports(self.client).context['top_products'])

    def test_latte_revenue_correct(self):
        top = {p['product_name']: p for p in self._top()}
        self.assertEqual(top['Latte']['total_revenue'], self.LATTE_REVENUE)
        self.assertEqual(top['Latte']['total_qty'], 3)

    def test_cake_revenue_correct(self):
        top = {p['product_name']: p for p in self._top()}
        self.assertEqual(top['Cake']['total_revenue'], self.CAKE_REVENUE)
        self.assertEqual(top['Cake']['total_qty'], 3)

    def test_sum_of_top_products_equals_item_subtotals(self):
        top     = self._top()
        top_sum = sum(p['total_revenue'] for p in top)
        self.assertEqual(top_sum, self.ITEM_SUBTOTALS)

    def test_total_revenue_exceeds_top_products_sum_by_packaging(self):
        """
        total_revenue > sum(top_products) because Order B has a
        packaging fee not captured in OrderItem.subtotal.
        Gap == packaging fee.
        """
        top     = self._top()
        top_sum = sum(p['total_revenue'] for p in top)
        gap     = self.TOTAL_REVENUE - top_sum
        self.assertEqual(gap, self.PACKAGING_FEES)

    def test_cancelled_items_excluded_from_top_products(self):
        top = self._top()
        latte = next((p for p in top if p['product_name'] == 'Latte'), None)
        self.assertIsNotNone(latte)
        # Only 3 lattes (A: 2 + C: 1), not 13 (cancelled D adds 10)
        self.assertEqual(latte['total_qty'], 3)

    def test_top_products_ranked_by_revenue_descending(self):
        top      = self._top()
        revenues = [p['total_revenue'] for p in top]
        self.assertEqual(revenues, sorted(revenues, reverse=True))


# ══════════════════════════════════════════════════════════════════════════════
# RULE 5: sum(category_sales) == sum(top_products)  [when no blank categories]
# ══════════════════════════════════════════════════════════════════════════════

class CategoryReconciliationTest(ReconciliationBase):
    def _cats(self):
        return list(_reports(self.client).context['category_sales'])

    def test_coffee_revenue_correct(self):
        cats = {c['name']: c for c in self._cats()}
        self.assertAlmostEqual(float(cats['Coffee']['total']),
                               float(self.COFFEE_REVENUE), places=2)

    def test_food_revenue_correct(self):
        cats = {c['name']: c for c in self._cats()}
        self.assertAlmostEqual(float(cats['Food']['total']),
                               float(self.FOOD_REVENUE), places=2)

    def test_sum_of_categories_equals_item_subtotals(self):
        cats     = self._cats()
        cat_sum  = sum(c['total'] for c in cats)
        self.assertAlmostEqual(cat_sum, float(self.ITEM_SUBTOTALS), places=2)

    def test_category_sum_equals_top_products_sum(self):
        cats     = self._cats()
        top      = list(_reports(self.client).context['top_products'])
        cat_sum  = sum(c['total'] for c in cats)
        top_sum  = sum(float(p['total_revenue']) for p in top)
        self.assertAlmostEqual(cat_sum, top_sum, places=2)

    def test_total_revenue_exceeds_category_sum_by_packaging(self):
        cats    = self._cats()
        cat_sum = sum(c['total'] for c in cats)
        gap     = float(self.TOTAL_REVENUE) - cat_sum
        self.assertAlmostEqual(gap, float(self.PACKAGING_FEES), places=2)

    def test_cancelled_excluded_from_categories(self):
        cats = {c['name']: c for c in self._cats()}
        # Cancelled order had 10× Latte (Coffee). If included, Coffee would be >360
        self.assertAlmostEqual(
            float(cats['Coffee']['total']),
            float(self.COFFEE_REVENUE), places=2,
        )


# ══════════════════════════════════════════════════════════════════════════════
# COMPLETE CROSS-SECTION RECONCILIATION in a single test
# ══════════════════════════════════════════════════════════════════════════════

class FullReconciliationTest(ReconciliationBase):
    """
    Verifies ALL reconciliation rules in one test using the controlled dataset.
    This is the primary regression guard.
    """

    def test_full_reconciliation(self):
        from apps.dashboard.views import _sales_stats

        resp       = _reports(self.client)
        daily      = list(resp.context['daily_sales'])
        top        = list(resp.context['top_products'])
        cats       = list(resp.context['category_sales'])
        total_rev  = resp.context['total_revenue']
        cash, _    = _get_cash_sales_for_date(TODAY)
        gcash, _   = _get_gcash_sales_for_date(TODAY)
        stats      = _sales_stats()

        # ── Rule 1: total_revenue == sum(daily) ──────────────────────────
        daily_sum = sum(d['total'] for d in daily)
        self.assertAlmostEqual(daily_sum, float(total_rev), places=2,
            msg="R1: sum(daily_sales) != total_revenue")

        # ── Rule 2: total_revenue == finance_cash + finance_gcash ─────────
        self.assertEqual(cash + gcash, total_rev,
            msg="R2: finance_cash + finance_gcash != total_revenue")

        # ── Rule 3: total_revenue == dashboard_daily_sales ────────────────
        self.assertEqual(stats['daily_sales'], total_rev,
            msg="R3: dashboard_daily_sales != total_revenue")

        # ── Rule 4: sum(top_products) == item subtotals ───────────────────
        top_sum = sum(float(p['total_revenue']) for p in top)
        self.assertAlmostEqual(top_sum, float(self.ITEM_SUBTOTALS), places=2,
            msg="R4: sum(top_products) != item subtotals")

        # ── Rule 4b: total_revenue - sum(top_products) == packaging ───────
        gap = float(total_rev) - top_sum
        self.assertAlmostEqual(gap, float(self.PACKAGING_FEES), places=2,
            msg=f"R4b: gap={gap} != packaging_fees={self.PACKAGING_FEES}")

        # ── Rule 5: sum(category_sales) == sum(top_products) ─────────────
        cat_sum = sum(c['total'] for c in cats)
        self.assertAlmostEqual(cat_sum, top_sum, places=2,
            msg="R5: sum(category_sales) != sum(top_products)")

        # ── Rule 6: specific known values ─────────────────────────────────
        self.assertEqual(total_rev, Decimal('606.00'), msg="R6a: total_revenue wrong")
        self.assertEqual(cash,      Decimal('520.00'), msg="R6b: finance_cash wrong")
        self.assertEqual(gcash,     Decimal('86.00'),  msg="R6c: finance_gcash wrong")
        self.assertAlmostEqual(top_sum, 600.0, places=2, msg="R6d: top_products sum wrong")
        self.assertAlmostEqual(cat_sum, 600.0, places=2, msg="R6e: category sum wrong")

        # ── Rule 7: cancelled order absent everywhere ──────────────────────
        self.assertEqual(resp.context['total_orders'], 3,  # not 4
            msg="R7a: cancelled order counted in total_orders")
        self.assertEqual(stats['daily_orders'], 3,
            msg="R7b: cancelled order counted in dashboard")
        latte = next((p for p in top if p['product_name'] == 'Latte'), None)
        self.assertIsNotNone(latte)
        self.assertEqual(latte['total_qty'], 3,   # not 13
            msg="R7c: cancelled latte qty leaked into top_products")


# ══════════════════════════════════════════════════════════════════════════════
# GCash handling — takeout order with packaging
# ══════════════════════════════════════════════════════════════════════════════

class GCashTakeoutReconciliationTest(ReconciliationBase):
    """
    Order B is GCash + Takeout + packaging fee.
    Verify it appears correctly in each section.
    """

    def test_gcash_order_in_total_revenue(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('606.00'))

    def test_gcash_in_finance_gcash_not_cash(self):
        cash, _  = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        # Order B is GCash, must not be in cash
        self.assertEqual(cash,  Decimal('520.00'))   # A + C only
        self.assertEqual(gcash, Decimal('86.00'))    # B only (80 + 6 packaging)

    def test_gcash_order_packaging_in_total_revenue_not_in_item_subtotals(self):
        """
        The ₱6 packaging fee on Order B is in total_revenue but NOT in
        sum(top_products) or sum(category_sales).
        """
        resp    = _reports(self.client)
        top_sum = sum(float(p['total_revenue'])
                      for p in resp.context['top_products'])
        cat_sum = sum(c['total'] for c in resp.context['category_sales'])
        total   = float(resp.context['total_revenue'])

        self.assertAlmostEqual(total - top_sum, 6.0, places=2,
            msg="Packaging fee not captured in total_revenue gap")
        self.assertAlmostEqual(total - cat_sum, 6.0, places=2,
            msg="Packaging fee not captured in category gap")

    def test_cake_in_food_category(self):
        cats = {c['name']: c for c in _reports(self.client).context['category_sales']}
        # Order B (1 cake) + Order C (2 cakes) = 3 cakes, 3 × 80 = 240
        self.assertAlmostEqual(float(cats['Food']['total']), 240.0, places=2)
        self.assertEqual(cats['Food']['qty'], 3)


# ══════════════════════════════════════════════════════════════════════════════
# Three-order known-value example (as specified in the task)
# ══════════════════════════════════════════════════════════════════════════════

class ThreeOrderKnownValueTest(TestCase):
    """
    Simple sanity check matching the task specification:
      Order A = ₱100, Order B = ₱200, Order C = ₱300
      Expected total = ₱600
    """

    def setUp(self):
        self.admin = _user('admin_knownval')
        self.client = Client()
        self.client.login(username='admin_knownval', password='pass123')
        cat  = Category.objects.create(name='Test', slug='test')
        prod = Product.objects.create(
            category=cat, name='Item', price=Decimal('100.00'), stock_quantity=100,
        )
        for total in [Decimal('100.00'), Decimal('200.00'), Decimal('300.00')]:
            dt = timezone.make_aware(
                datetime.datetime.combine(TODAY, datetime.time(10, 0))
            )
            o = Order.objects.create(
                customer_name='Test', status='completed', is_paid=True,
                payment_method='cash', total=total, subtotal=total,
            )
            Order.objects.filter(pk=o.pk).update(created_at=dt)
            o.refresh_from_db()
            qty = int(total / 100)
            OrderItem.objects.create(
                order=o, product=prod, product_name='Item',
                category_name='Test', size='none',
                quantity=qty, unit_price=Decimal('100.00'),
                subtotal=total,
            )

    def test_total_revenue_is_600(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('600.00'))

    def test_total_orders_is_3(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_orders'], 3)

    def test_daily_sum_is_600(self):
        resp      = _reports(self.client)
        daily_sum = sum(d['total'] for d in resp.context['daily_sales'])
        self.assertAlmostEqual(daily_sum, 600.0, places=2)

    def test_top_product_revenue_is_600(self):
        resp = _reports(self.client)
        top  = list(resp.context['top_products'])
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['product_name'], 'Item')
        self.assertEqual(top[0]['total_revenue'], Decimal('600.00'))

    def test_category_revenue_is_600(self):
        resp = _reports(self.client)
        cats = list(resp.context['category_sales'])
        self.assertEqual(len(cats), 1)
        self.assertAlmostEqual(cats[0]['total'], 600.0, places=2)

    def test_finance_cash_is_600(self):
        cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash, Decimal('600.00'))

    def test_all_sections_agree(self):
        """All sections show ₱600.00 when there are no packaging fees."""
        from apps.dashboard.views import _sales_stats
        resp      = _reports(self.client)
        cash, _   = _get_cash_sales_for_date(TODAY)
        stats     = _sales_stats()
        daily_sum = sum(d['total'] for d in resp.context['daily_sales'])
        top_sum   = sum(float(p['total_revenue'])
                        for p in resp.context['top_products'])
        cat_sum   = sum(c['total'] for c in resp.context['category_sales'])

        expected = 600.0
        self.assertEqual(float(resp.context['total_revenue']), expected)
        self.assertAlmostEqual(daily_sum, expected, places=2)
        self.assertAlmostEqual(float(cash), expected, places=2)
        self.assertAlmostEqual(float(stats['daily_sales']), expected, places=2)
        # Top products and categories also equal total_revenue when no packaging
        self.assertAlmostEqual(top_sum, expected, places=2)
        self.assertAlmostEqual(cat_sum, expected, places=2)
