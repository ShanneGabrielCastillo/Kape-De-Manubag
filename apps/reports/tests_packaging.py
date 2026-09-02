"""
Takeout packaging charge tests — Kape De Manubag Sales Reports.

Verifies that the ₱6-per-eligible-item packaging charge is handled
consistently across Orders, Finance, Dashboard, and Sales Reports.

Business rule:
  - Eligible items: Category.is_packaging_required=True (meals, snacks, etc.)
  - Non-eligible:   Category.is_packaging_required=False (coffee, drinks)
  - Dine-in:        always ₱0 regardless of category
  - Rate:           ₱6.00 per eligible item (from SystemSetting)
  - Order.total     INCLUDES packaging fee
  - OrderItem.subtotal DOES NOT include packaging fee

Accounting treatment in each section:
  total_revenue, daily_sales, Finance, Dashboard  → Order.total (includes packaging)
  Top Products, Sales by Category                 → OrderItem.subtotal (excludes packaging)

The intended gap:
  total_revenue - sum(top_products) = total packaging fees collected

Scenarios:
  1.  Dine-in — packaging_fee=0, Order.total==item_subtotals
  2.  Takeout meal — packaging_fee=₱6, Order.total==item_subtotals+₱6
  3.  Takeout multiple meals — packaging_fee=₱6×qty
  4.  Takeout drinks only — packaging_fee=0 (is_packaging_required=False)
  5.  Takeout mixed (meal + drink) — only meal charged
  6.  Cash payment with packaging
  7.  GCash payment with packaging
  8.  Reports total_revenue includes packaging
  9.  Finance cash_sales includes packaging
  10. Dashboard daily_sales includes packaging
  11. Top Products excludes packaging (uses OrderItem.subtotal)
  12. Category sales excludes packaging
  13. No double-counting of packaging fee
  14. Reconciliation: total_revenue - sum(top_products) == packaging_fees
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
FEE   = Decimal('6.00')   # packaging fee per eligible item


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _meal_category(name='Food'):
    """Category that IS eligible for packaging (meals)."""
    return Category.objects.create(
        name=name,
        slug=name.lower().replace(' ', '-'),
        is_packaging_required=True,
    )


def _drink_category(name='Coffee'):
    """Category that is NOT eligible for packaging (drinks)."""
    return Category.objects.create(
        name=name,
        slug=name.lower().replace(' ', '-'),
        is_packaging_required=False,
    )


def _product(name, price, category):
    return Product.objects.create(
        category=category, name=name, price=price, stock_quantity=100,
    )


def _order(order_type='dine_in', payment='cash',
           subtotal=Decimal('0.00'), packaging_fee=Decimal('0.00'),
           total=None, status='completed', is_paid=True, date=TODAY):
    """Create a completed order with explicit financials."""
    if total is None:
        total = subtotal + packaging_fee
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(10, 0))
    )
    o = Order.objects.create(
        customer_name='Test',
        status=status, is_paid=is_paid,
        payment_method=payment,
        order_type=order_type,
        total=total,
        subtotal=subtotal,
        packaging_fee=packaging_fee,
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


def _item(order, product, quantity, unit_price=None, packaging_eligible=None):
    """Add an OrderItem to an order with explicit snapshot values."""
    price = unit_price or product.price
    sub   = price * quantity
    elig  = packaging_eligible if packaging_eligible is not None \
            else product.category.is_packaging_required
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        category_name=product.category.name,
        packaging_eligible=elig,
        size='none',
        quantity=quantity,
        unit_price=price,
        subtotal=sub,
    )
    return sub


def _reports(client, start=None, end=None):
    return client.get(
        reverse('reports:index'),
        {'start': str(start or TODAY), 'end': str(end or TODAY)},
    )


# ── 1. Dine-in — no packaging fee ────────────────────────────────────────────

class DineInNoPackagingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_dinein')
        self.client = Client()
        self.client.login(username='admin_dinein', password='pass123')
        meal_cat = _meal_category()
        meal     = _product('Burger', Decimal('120.00'), meal_cat)
        # Dine-in: packaging_fee=0 even though meal category is eligible
        o    = _order(order_type='dine_in', subtotal=Decimal('120.00'),
                      packaging_fee=Decimal('0.00'))
        _item(o, meal, quantity=1)

    def test_order_total_equals_item_subtotal(self):
        o = Order.objects.get()
        self.assertEqual(o.total,        Decimal('120.00'))
        self.assertEqual(o.subtotal,     Decimal('120.00'))
        self.assertEqual(o.packaging_fee, Decimal('0.00'))

    def test_reports_total_revenue_no_packaging(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('120.00'))

    def test_reports_top_products_matches_revenue(self):
        """For dine-in there is no gap — top product revenue == total_revenue."""
        resp    = _reports(self.client)
        top     = list(resp.context['top_products'])
        top_sum = sum(float(p['total_revenue']) for p in top)
        self.assertAlmostEqual(top_sum, float(resp.context['total_revenue']), places=2)

    def test_finance_no_packaging_in_cash_sales(self):
        """Finance cash_sales = Order.total which has no packaging for dine-in."""
        cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash, Decimal('120.00'))


# ── 2. Takeout meal — ₱6 packaging fee ───────────────────────────────────────

class TakeoutMealPackagingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_takeout')
        self.client = Client()
        self.client.login(username='admin_takeout', password='pass123')
        meal_cat = _meal_category()
        meal     = _product('Burger', Decimal('120.00'), meal_cat)
        # Takeout: 1 eligible meal → packaging_fee = ₱6
        o = _order(order_type='takeout',
                   subtotal=Decimal('120.00'),
                   packaging_fee=FEE,
                   total=Decimal('126.00'))
        _item(o, meal, quantity=1, packaging_eligible=True)

    def test_order_total_includes_packaging(self):
        o = Order.objects.get()
        self.assertEqual(o.packaging_fee, FEE)
        self.assertEqual(o.total,         Decimal('126.00'))
        self.assertEqual(o.subtotal,      Decimal('120.00'))

    def test_reports_total_revenue_includes_packaging(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('126.00'))

    def test_top_products_excludes_packaging(self):
        resp = _reports(self.client)
        top  = list(resp.context['top_products'])
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['total_revenue'], Decimal('120.00'))

    def test_gap_equals_packaging_fee(self):
        resp    = _reports(self.client)
        top_sum = sum(float(p['total_revenue']) for p in resp.context['top_products'])
        gap     = float(resp.context['total_revenue']) - top_sum
        self.assertAlmostEqual(gap, float(FEE), places=2)

    def test_finance_cash_includes_packaging(self):
        cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash, Decimal('126.00'))

    def test_category_excludes_packaging(self):
        resp    = _reports(self.client)
        cats    = list(resp.context['category_sales'])
        cat_sum = sum(c['total'] for c in cats)
        self.assertAlmostEqual(cat_sum, 120.0, places=2)


# ── 3. Takeout multiple meals — ₱6 × qty ─────────────────────────────────────

class TakeoutMultipleMealsTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_multi_meal')
        self.client = Client()
        self.client.login(username='admin_multi_meal', password='pass123')
        meal_cat = _meal_category()
        meal     = _product('Burger', Decimal('100.00'), meal_cat)
        # 3 eligible meals → packaging = 3 × ₱6 = ₱18
        o = _order(order_type='takeout',
                   subtotal=Decimal('300.00'),
                   packaging_fee=Decimal('18.00'),
                   total=Decimal('318.00'))
        _item(o, meal, quantity=3, packaging_eligible=True)

    def test_total_includes_all_packaging(self):
        o = Order.objects.get()
        self.assertEqual(o.packaging_fee, Decimal('18.00'))
        self.assertEqual(o.total,         Decimal('318.00'))

    def test_reports_total_revenue_correct(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('318.00'))

    def test_top_products_correct_qty_and_revenue(self):
        resp = _reports(self.client)
        top  = list(resp.context['top_products'])
        self.assertEqual(top[0]['total_qty'],     3)
        self.assertEqual(top[0]['total_revenue'], Decimal('300.00'))

    def test_gap_equals_all_packaging_fees(self):
        resp    = _reports(self.client)
        top_sum = sum(float(p['total_revenue']) for p in resp.context['top_products'])
        gap     = float(resp.context['total_revenue']) - top_sum
        self.assertAlmostEqual(gap, 18.0, places=2)


# ── 4. Takeout drinks only — no packaging fee ─────────────────────────────────

class TakeoutDrinksNoPackagingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_drinks')
        self.client = Client()
        self.client.login(username='admin_drinks', password='pass123')
        drink_cat = _drink_category()
        drink     = _product('Latte', Decimal('120.00'), drink_cat)
        # Drink category: is_packaging_required=False → packaging_fee=0
        o = _order(order_type='takeout',
                   subtotal=Decimal('120.00'),
                   packaging_fee=Decimal('0.00'),
                   total=Decimal('120.00'))
        _item(o, drink, quantity=1, packaging_eligible=False)

    def test_no_packaging_for_drinks(self):
        o = Order.objects.get()
        self.assertEqual(o.packaging_fee, Decimal('0.00'))
        self.assertEqual(o.total, Decimal('120.00'))

    def test_reports_total_revenue_no_gap(self):
        """For drink-only takeout there is no packaging gap."""
        resp    = _reports(self.client)
        top_sum = sum(float(p['total_revenue']) for p in resp.context['top_products'])
        total   = float(resp.context['total_revenue'])
        self.assertAlmostEqual(top_sum, total, places=2)

    def test_drink_category_not_eligible(self):
        """packaging_eligible snapshot must be False for drink items."""
        item = OrderItem.objects.get()
        self.assertFalse(item.packaging_eligible)


# ── 5. Takeout mixed (meal + drink) — only meal charged ───────────────────────

class TakeoutMixedTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_mixed')
        self.client = Client()
        self.client.login(username='admin_mixed', password='pass123')
        meal_cat  = _meal_category('Food')
        drink_cat = _drink_category('Coffee')
        meal  = _product('Burger', Decimal('100.00'), meal_cat)
        drink = _product('Latte',  Decimal('80.00'),  drink_cat)
        # 1 meal + 1 drink → packaging = 1 × ₱6 = ₱6 (drink not charged)
        o = _order(order_type='takeout',
                   subtotal=Decimal('180.00'),   # 100 + 80
                   packaging_fee=FEE,            # ₱6 for meal only
                   total=Decimal('186.00'))
        _item(o, meal,  quantity=1, packaging_eligible=True)
        _item(o, drink, quantity=1, packaging_eligible=False)

    def test_total_correct(self):
        o = Order.objects.get()
        self.assertEqual(o.total,         Decimal('186.00'))
        self.assertEqual(o.subtotal,      Decimal('180.00'))
        self.assertEqual(o.packaging_fee, FEE)

    def test_reports_total_includes_packaging(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('186.00'))

    def test_top_products_sum_excludes_packaging(self):
        resp    = _reports(self.client)
        top_sum = sum(float(p['total_revenue']) for p in resp.context['top_products'])
        self.assertAlmostEqual(top_sum, 180.0, places=2)   # 100 + 80

    def test_gap_equals_meal_packaging_only(self):
        resp    = _reports(self.client)
        top_sum = sum(float(p['total_revenue']) for p in resp.context['top_products'])
        gap     = float(resp.context['total_revenue']) - top_sum
        self.assertAlmostEqual(gap, float(FEE), places=2)

    def test_drink_item_packaging_eligible_is_false(self):
        drink_item = OrderItem.objects.filter(product_name='Latte').get()
        self.assertFalse(drink_item.packaging_eligible)

    def test_meal_item_packaging_eligible_is_true(self):
        meal_item = OrderItem.objects.filter(product_name='Burger').get()
        self.assertTrue(meal_item.packaging_eligible)


# ── 6. Cash payment with packaging ───────────────────────────────────────────

class CashPaymentPackagingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cash_pkg')
        self.client = Client()
        self.client.login(username='admin_cash_pkg', password='pass123')
        meal_cat = _meal_category()
        meal     = _product('Burger', Decimal('150.00'), meal_cat)
        o = _order(order_type='takeout', payment='cash',
                   subtotal=Decimal('150.00'),
                   packaging_fee=FEE,
                   total=Decimal('156.00'))
        _item(o, meal, quantity=1, packaging_eligible=True)

    def test_finance_cash_includes_packaging(self):
        cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash, Decimal('156.00'))   # 150 + 6

    def test_reports_total_revenue_cash_with_packaging(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('156.00'))

    def test_no_double_counting(self):
        """Order.total (which includes packaging) must be counted exactly once."""
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_orders'], 1)
        self.assertEqual(resp.context['total_revenue'], Decimal('156.00'))


# ── 7. GCash payment with packaging ──────────────────────────────────────────

class GCashPaymentPackagingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_gcash_pkg')
        self.client = Client()
        self.client.login(username='admin_gcash_pkg', password='pass123')
        meal_cat = _meal_category()
        meal     = _product('Burger', Decimal('150.00'), meal_cat)
        o = _order(order_type='takeout', payment='gcash',
                   subtotal=Decimal('150.00'),
                   packaging_fee=FEE,
                   total=Decimal('156.00'))
        _item(o, meal, quantity=1, packaging_eligible=True)

    def test_finance_gcash_includes_packaging(self):
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(gcash, Decimal('156.00'))   # 150 + 6

    def test_reports_total_revenue_gcash_with_packaging(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('156.00'))

    def test_gcash_not_in_cash_sales(self):
        cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash, Decimal('0.00'))


# ── 8. No double-counting of packaging fee ───────────────────────────────────

class NoDoubleCountingTest(TestCase):
    """
    Order.packaging_fee IS included in Order.total.
    No view adds packaging_fee a second time — total_revenue == Sum(Order.total).
    """

    def setUp(self):
        self.admin = _user('admin_no_double')
        self.client = Client()
        self.client.login(username='admin_no_double', password='pass123')
        meal_cat = _meal_category()
        meal     = _product('Burger', Decimal('100.00'), meal_cat)
        # Two takeout orders each with 1 eligible meal
        for _ in range(2):
            o = _order(order_type='takeout',
                       subtotal=Decimal('100.00'),
                       packaging_fee=FEE,
                       total=Decimal('106.00'))
            _item(o, meal, quantity=1, packaging_eligible=True)

    def test_total_revenue_not_doubled(self):
        resp = _reports(self.client)
        # 2 × 106 = 212 (not 212 + 12 = 224 if packaging were added twice)
        self.assertEqual(resp.context['total_revenue'], Decimal('212.00'))

    def test_order_count_correct(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_orders'], 2)

    def test_finance_cash_not_doubled(self):
        cash, count = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash,  Decimal('212.00'))
        self.assertEqual(count, 2)


# ── 9. Full cross-module reconciliation with packaging ────────────────────────

class FullPackagingReconciliationTest(TestCase):
    """
    Controlled dataset:
      Order A: Dine-in, Cash, 1×Burger(₱100)         → total=₱100, packaging=₱0
      Order B: Takeout, Cash, 2×Burger(₱100)          → total=₱206, packaging=₱12
      Order C: Takeout, GCash, 1×Latte(₱80, drink)   → total=₱80,  packaging=₱0

    Known values:
      Total Revenue       = 100 + 206 + 80 = ₱386
      Finance cash        = 100 + 206      = ₱306  (A+B)
      Finance GCash       = 80             = ₱80   (C)
      Finance cash+gcash  = 386            = ₱386
      Item subtotals      = 100 + 200 + 80 = ₱380  (no packaging)
      Total packaging     = 0 + 12 + 0     = ₱12
      Gap (total-subtotals) = 386-380      = ₱6 ← wait: 386-380=6, not 12?
        Actually: A subtotal=100, B subtotal=200, C subtotal=80 → 380
                  A total=100, B total=206, C total=80 → 386
                  Gap = 386-380 = 6? NO: B packaging=12, so gap=12
        Let me recheck: A=100+0=100, B=200+12=212? No: 2×100=200, packaging=2×6=12, total=212
        Corrected known values:
          Order B: Takeout, 2×Burger(₱100) → subtotal=200, packaging=12, total=212
          Total Revenue = 100 + 212 + 80 = ₱392
          Finance cash  = 100 + 212      = ₱312
          Finance GCash = 80             = ₱80
          Item subtotals = 100+200+80    = ₱380
          Gap = 392-380 = ₱12 = packaging fees (correct)
    """

    def setUp(self):
        self.admin = _user('admin_full_pkg')
        self.client = Client()
        self.client.login(username='admin_full_pkg', password='pass123')
        meal_cat  = _meal_category('Food')
        drink_cat = _drink_category('Coffee')
        burger = _product('Burger', Decimal('100.00'), meal_cat)
        latte  = _product('Latte',  Decimal('80.00'),  drink_cat)

        # Order A: Dine-in, Cash, 1×Burger — no packaging
        oa = _order(order_type='dine_in', payment='cash',
                    subtotal=Decimal('100.00'), packaging_fee=Decimal('0.00'),
                    total=Decimal('100.00'))
        _item(oa, burger, quantity=1, packaging_eligible=True)

        # Order B: Takeout, Cash, 2×Burger — packaging = 2×6 = ₱12
        ob = _order(order_type='takeout', payment='cash',
                    subtotal=Decimal('200.00'), packaging_fee=Decimal('12.00'),
                    total=Decimal('212.00'))
        _item(ob, burger, quantity=2, packaging_eligible=True)

        # Order C: Takeout, GCash, 1×Latte (drink) — no packaging
        oc = _order(order_type='takeout', payment='gcash',
                    subtotal=Decimal('80.00'), packaging_fee=Decimal('0.00'),
                    total=Decimal('80.00'))
        _item(oc, latte, quantity=1, packaging_eligible=False)

        self.TOTAL_REVENUE  = Decimal('392.00')
        self.FINANCE_CASH   = Decimal('312.00')
        self.FINANCE_GCASH  = Decimal('80.00')
        self.ITEM_SUBTOTALS = Decimal('380.00')
        self.TOTAL_PACKAGING = Decimal('12.00')

    def test_total_revenue_correct(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], self.TOTAL_REVENUE)

    def test_finance_cash_includes_packaging(self):
        cash, _ = _get_cash_sales_for_date(TODAY)
        self.assertEqual(cash, self.FINANCE_CASH)

    def test_finance_gcash_no_packaging(self):
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(gcash, self.FINANCE_GCASH)

    def test_finance_cash_plus_gcash_equals_total_revenue(self):
        cash,  _ = _get_cash_sales_for_date(TODAY)
        gcash, _ = _get_gcash_sales_for_date(TODAY)
        self.assertEqual(cash + gcash, self.TOTAL_REVENUE)

    def test_dashboard_daily_equals_total_revenue(self):
        from apps.dashboard.views import _sales_stats
        stats = _sales_stats()
        resp  = _reports(self.client)
        self.assertEqual(stats['daily_sales'], resp.context['total_revenue'])

    def test_top_products_sum_equals_item_subtotals(self):
        resp    = _reports(self.client)
        top_sum = sum(float(p['total_revenue'])
                      for p in resp.context['top_products'])
        self.assertAlmostEqual(top_sum, float(self.ITEM_SUBTOTALS), places=2)

    def test_category_sum_equals_item_subtotals(self):
        resp    = _reports(self.client)
        cat_sum = sum(c['total'] for c in resp.context['category_sales'])
        self.assertAlmostEqual(cat_sum, float(self.ITEM_SUBTOTALS), places=2)

    def test_gap_equals_total_packaging_fees(self):
        resp    = _reports(self.client)
        top_sum = sum(float(p['total_revenue'])
                      for p in resp.context['top_products'])
        gap     = float(resp.context['total_revenue']) - top_sum
        self.assertAlmostEqual(gap, float(self.TOTAL_PACKAGING), places=2)

    def test_dine_in_burger_no_gap(self):
        """
        The dine-in burger contributes ₱100 to both total_revenue AND
        top_products — no packaging gap for dine-in items.
        """
        # Order A (dine-in, 1 burger ₱100) has no packaging.
        # Order B (takeout, 2 burgers ₱200) has ₱12 packaging.
        # Burger total in top_products = 100+200 = 300
        # Burger total in order totals  = 100+212 = 312
        # Gap attributable to burgers   = 12 = Order B packaging only
        resp   = _reports(self.client)
        burger = next(
            p for p in resp.context['top_products']
            if p['product_name'] == 'Burger'
        )
        self.assertEqual(burger['total_revenue'], Decimal('300.00'))
        self.assertEqual(burger['total_qty'], 3)

    def test_drink_no_packaging_gap(self):
        """Latte (drink) appears in top products with same revenue as in total."""
        resp  = _reports(self.client)
        latte = next(
            p for p in resp.context['top_products']
            if p['product_name'] == 'Latte'
        )
        # Latte: subtotal=80, packaging=0, so no gap for this product
        self.assertEqual(latte['total_revenue'], Decimal('80.00'))
