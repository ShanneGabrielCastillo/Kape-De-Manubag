"""
Sales Reports query performance tests — Kape De Manubag.

Verifies:
  OPT-1 regression: reports_index issues ≤ N queries regardless of order volume
  Correctness:      financial results are identical before and after optimisation
  No N+1:          query count must not grow with order count
  Correctness with many orders:  totals remain accurate at scale
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem

User = get_user_model()

TODAY = timezone.localdate()
YEST  = TODAY - datetime.timedelta(days=1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username='admin_perf', role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _category(name='Coffee'):
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _product(name, price=Decimal('100.00'), category=None):
    if category is None:
        category = _category()
    return Product.objects.create(
        category=category, name=name, price=price, stock_quantity=9999,
    )


def _order(date, total=Decimal('100.00'), payment_method='cash',
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


def _order_with_item(date, product, quantity=1, total=None,
                     payment_method='cash'):
    price    = product.price
    subtotal = price * quantity
    if total is None:
        total = subtotal
    o = _order(date, total=total, payment_method=payment_method)
    OrderItem.objects.create(
        order=o, product=product,
        product_name=product.name,
        category_name=product.category.name,
        size='none', quantity=quantity,
        unit_price=price, subtotal=subtotal,
    )
    return o


def _reports(client, start=None, end=None):
    return client.get(
        reverse('reports:index'),
        {'start': str(start or TODAY), 'end': str(end or TODAY)},
    )


# ══════════════════════════════════════════════════════════════════════════════
# OPT-1 REGRESSION: query count is bounded regardless of order volume
# ══════════════════════════════════════════════════════════════════════════════

class QueryCountBoundedTest(TestCase):
    """
    After OPT-1, total_revenue and total_orders come from a single aggregate
    query instead of two.  The total query count for the entire page must:
      - be bounded (≤ some fixed ceiling)
      - not grow as the number of orders increases
    """

    def setUp(self):
        self.admin = _user('admin_qcount')
        self.client = Client()
        self.client.login(username='admin_qcount', password='pass123')

    def test_query_count_bounded_no_orders(self):
        url = reverse('reports:index')
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # Upper bound — generous to allow session/auth/middleware queries.
        # The important thing is it does NOT scale with order count.
        self.assertLessEqual(len(ctx), 15,
            f"reports_index issued {len(ctx)} queries with 0 orders — "
            f"expected ≤ 15.\n" + '\n'.join(q['sql'][:120] for q in ctx))

    def test_query_count_stable_with_many_orders(self):
        """
        Adding 100 orders must not increase the query count.
        Proves there is no O(N) query pattern (no N+1).
        """
        cat  = _category('Meals')
        prod = _product('Burger', Decimal('80.00'), cat)
        for _ in range(100):
            _order_with_item(TODAY, prod, quantity=1)

        url = reverse('reports:index') + f'?start={TODAY}&end={TODAY}'

        # First warm request (establishes baseline including session overhead)
        with CaptureQueriesContext(connection) as ctx1:
            self.client.get(url)
        count_100 = len(ctx1)

        # Add 100 more orders — query count must not increase
        for _ in range(100):
            _order_with_item(TODAY, prod, quantity=1)

        with CaptureQueriesContext(connection) as ctx2:
            resp = self.client.get(url)
        count_200 = len(ctx2)
        self.assertEqual(resp.status_code, 200)

        # O(1): doubling orders must never INCREASE query count
        self.assertLessEqual(count_200, count_100,
            f"Query count grew from {count_100} (100 orders) to "
            f"{count_200} (200 orders) — O(N) regression detected.")

    def test_single_day_range_bounded(self):
        for _ in range(50):
            _order(TODAY, total=Decimal('100.00'))
        url = reverse('reports:index') + f'?start={TODAY}&end={TODAY}'
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(ctx), 15,
            f"Single-day range: {len(ctx)} queries — expected ≤ 15.")

    def test_long_range_bounded(self):
        """A 30-day range must not issue one query per day."""
        start_30 = TODAY - datetime.timedelta(days=29)
        for i in range(10):
            d = TODAY - datetime.timedelta(days=i)
            _order(d, total=Decimal('100.00'))

        url = reverse('reports:index') + f'?start={start_30}&end={TODAY}'
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # Must be bounded — NOT 30 queries for 30 days
        self.assertLessEqual(len(ctx), 15,
            f"30-day range: {len(ctx)} queries — expected ≤ 15 (not one per day).")


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTNESS: OPT-1 must not change any financial result
# ══════════════════════════════════════════════════════════════════════════════

class CorrectnessAfterOptimisationTest(TestCase):
    """
    OPT-1 changed how total_revenue and total_orders are fetched.
    All financial values must remain identical to what they were before.
    """

    def setUp(self):
        self.admin = _user('admin_correct')
        self.client = Client()
        self.client.login(username='admin_correct', password='pass123')
        cat  = _category('Coffee')
        prod = _product('Latte', Decimal('120.00'), cat)
        # Create 3 known orders
        _order_with_item(TODAY, prod, quantity=1)   # 120
        _order_with_item(TODAY, prod, quantity=2)   # 240
        _order(TODAY, total=Decimal('80.00'), payment_method='gcash')  # 80

    def test_total_revenue_correct(self):
        resp = _reports(self.client)
        # 120 + 240 + 80 = 440
        self.assertEqual(resp.context['total_revenue'], Decimal('440.00'))

    def test_total_orders_correct(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_orders'], 3)

    def test_avg_order_correct(self):
        resp = _reports(self.client)
        # 440 / 3 ≈ 146.67
        self.assertAlmostEqual(float(resp.context['avg_order']),
                                440/3, places=2)

    def test_daily_sales_sum_equals_total_revenue(self):
        resp      = _reports(self.client)
        daily_sum = sum(d['total'] for d in resp.context['daily_sales'])
        self.assertAlmostEqual(daily_sum,
                                float(resp.context['total_revenue']), places=2)

    def test_top_products_correct(self):
        resp = _reports(self.client)
        top  = list(resp.context['top_products'])
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['product_name'], 'Latte')
        # qty=1+2=3, revenue=120+240=360
        self.assertEqual(top[0]['total_qty'], 3)
        self.assertEqual(top[0]['total_revenue'], Decimal('360.00'))

    def test_category_sales_correct(self):
        resp = _reports(self.client)
        cats = list(resp.context['category_sales'])
        # Only Latte has a category — the GCash order has no OrderItem
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0]['name'], 'Coffee')
        self.assertAlmostEqual(cats[0]['total'], 360.0, places=2)


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTNESS WITH ZERO ORDERS
# ══════════════════════════════════════════════════════════════════════════════

class ZeroOrdersCorrectnessTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_zero')
        self.client = Client()
        self.client.login(username='admin_zero', password='pass123')

    def test_zero_orders_does_not_crash(self):
        resp = _reports(self.client)
        self.assertEqual(resp.status_code, 200)

    def test_zero_orders_total_revenue(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], 0)

    def test_zero_orders_total_orders(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_orders'], 0)

    def test_zero_orders_avg_order(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['avg_order'], 0)

    def test_zero_orders_daily_sales_all_zero(self):
        resp = _reports(self.client)
        for day in resp.context['daily_sales']:
            self.assertEqual(day['count'], 0)
            self.assertEqual(day['total'], 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTNESS WITH MANY ORDERS — scale test
# ══════════════════════════════════════════════════════════════════════════════

class ManyOrdersCorrectnessTest(TestCase):
    """
    100 cash orders of ₱99.90 each.
    Verifies that aggregation is exact even at scale.
    """

    def setUp(self):
        self.admin = _user('admin_many')
        self.client = Client()
        self.client.login(username='admin_many', password='pass123')
        for _ in range(100):
            _order(TODAY, total=Decimal('99.90'))

    def test_total_revenue_exact(self):
        resp = _reports(self.client)
        # 100 × 99.90 = 9990.00
        self.assertEqual(resp.context['total_revenue'], Decimal('9990.00'))

    def test_total_orders_exact(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_orders'], 100)

    def test_daily_sum_equals_total_revenue(self):
        resp      = _reports(self.client)
        daily_sum = sum(d['total'] for d in resp.context['daily_sales'])
        self.assertAlmostEqual(daily_sum,
                                float(resp.context['total_revenue']), places=1)

    def test_query_count_with_100_orders(self):
        url = reverse('reports:index') + f'?start={TODAY}&end={TODAY}'
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(ctx), 15,
            f"100 orders: {len(ctx)} queries — expected ≤ 15.")


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTNESS — multiple dates, short and long ranges
# ══════════════════════════════════════════════════════════════════════════════

class DateRangeCorrectnessTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_range')
        self.client = Client()
        self.client.login(username='admin_range', password='pass123')
        # Three different days
        _order(TODAY - datetime.timedelta(days=2), total=Decimal('100.00'))
        _order(TODAY - datetime.timedelta(days=1), total=Decimal('200.00'))
        _order(TODAY,                              total=Decimal('300.00'))

    def test_single_day_range(self):
        resp = _reports(self.client, TODAY, TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('300.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_two_day_range(self):
        resp = _reports(self.client,
                         TODAY - datetime.timedelta(days=1), TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('500.00'))
        self.assertEqual(resp.context['total_orders'],  2)

    def test_three_day_range(self):
        resp = _reports(self.client,
                         TODAY - datetime.timedelta(days=2), TODAY)
        self.assertEqual(resp.context['total_revenue'], Decimal('600.00'))
        self.assertEqual(resp.context['total_orders'],  3)

    def test_daily_breakdown_count_equals_range_days(self):
        resp = _reports(self.client,
                         TODAY - datetime.timedelta(days=2), TODAY)
        self.assertEqual(len(resp.context['daily_sales']), 3)

    def test_long_range_does_not_miss_orders(self):
        start_30 = TODAY - datetime.timedelta(days=29)
        resp = _reports(self.client, start_30, TODAY)
        # Only 3 of the 30 days have orders — but total must still be 600
        self.assertEqual(resp.context['total_revenue'], Decimal('600.00'))

    def test_long_range_daily_list_length(self):
        start_30 = TODAY - datetime.timedelta(days=29)
        resp = _reports(self.client, start_30, TODAY)
        self.assertEqual(len(resp.context['daily_sales']), 30)


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTNESS — cancelled and non-completed orders stay excluded
# ══════════════════════════════════════════════════════════════════════════════

class ExclusionCorrectnessTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_excl')
        self.client = Client()
        self.client.login(username='admin_excl', password='pass123')
        _order(TODAY, status='completed', is_paid=True,  total=Decimal('200.00'))
        _order(TODAY, status='cancelled', is_paid=False, total=Decimal('999.00'))
        _order(TODAY, status='pending',   is_paid=False, total=Decimal('999.00'))

    def test_only_completed_paid_counted(self):
        resp = _reports(self.client)
        self.assertEqual(resp.context['total_revenue'], Decimal('200.00'))
        self.assertEqual(resp.context['total_orders'],  1)

    def test_query_count_not_inflated_by_excluded_orders(self):
        url = reverse('reports:index') + f'?start={TODAY}&end={TODAY}'
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(ctx), 15)


# ══════════════════════════════════════════════════════════════════════════════
# EXISTING REPORTS TEST SUITE REGRESSION GUARD
# ══════════════════════════════════════════════════════════════════════════════

class ExistingTestsRegressionTest(TestCase):
    """
    Mirrors the key assertions from the original ReportsIndexTests
    (apps/reports/tests.py) to confirm OPT-1 introduced no regressions
    in the results that the original tests relied on.
    """

    def setUp(self):
        self.admin = _user('admin_regression')
        self.client = Client()
        self.client.login(username='admin_regression', password='pass123')
        from apps.menu.models import Category, Product
        self.coffee = Category.objects.create(name='Coffee', slug='coffee')
        self.snacks = Category.objects.create(name='Snacks', slug='snacks')
        self.latte  = Product.objects.create(
            category=self.coffee, name='Latte', price=Decimal('120.00'),
        )
        self.chips  = Product.objects.create(
            category=self.snacks, name='Chips', price=Decimal('80.00'),
        )
        day1 = TODAY - datetime.timedelta(days=2)
        day2 = TODAY - datetime.timedelta(days=1)
        o1 = _order(day1, total=Decimal('200.00'), status='completed', is_paid=True)
        OrderItem.objects.create(
            order=o1, product=self.latte, product_name='Latte',
            category_name='Coffee', size='none',
            quantity=1, unit_price=Decimal('120.00'), subtotal=Decimal('120.00'),
        )
        OrderItem.objects.create(
            order=o1, product=self.chips, product_name='Chips',
            category_name='Snacks', size='none',
            quantity=1, unit_price=Decimal('80.00'), subtotal=Decimal('80.00'),
        )
        o2 = _order(day2, total=Decimal('360.00'), status='completed', is_paid=True)
        OrderItem.objects.create(
            order=o2, product=self.latte, product_name='Latte',
            category_name='Coffee', size='none',
            quantity=3, unit_price=Decimal('120.00'), subtotal=Decimal('360.00'),
        )
        self.day1 = day1
        self.day2 = day2

    def _report(self):
        return _reports(self.client, self.day1, TODAY)

    def test_total_revenue(self):
        resp = self._report()
        # 200 + 360 = 560; the unpaid order must not appear
        self.assertEqual(resp.context['total_revenue'], Decimal('560.00'))

    def test_total_orders(self):
        resp = self._report()
        self.assertEqual(resp.context['total_orders'], 2)

    def test_avg_order(self):
        resp = self._report()
        self.assertAlmostEqual(float(resp.context['avg_order']), 280.0, places=2)

    def test_top_products_latte(self):
        resp = self._report()
        top  = {p['product_name']: p for p in resp.context['top_products']}
        self.assertIn('Latte', top)
        self.assertEqual(top['Latte']['total_qty'], 4)       # 1+3
        self.assertEqual(top['Latte']['total_revenue'], Decimal('480.00'))  # 4×120

    def test_top_products_chips(self):
        resp = self._report()
        top  = {p['product_name']: p for p in resp.context['top_products']}
        self.assertIn('Chips', top)
        self.assertEqual(top['Chips']['total_qty'], 1)
        self.assertEqual(top['Chips']['total_revenue'], Decimal('80.00'))

    def test_category_sales_coffee(self):
        resp   = self._report()
        coffee = next(c for c in resp.context['category_sales']
                      if c['name'] == 'Coffee')
        self.assertAlmostEqual(coffee['total'], 480.0, places=2)

    def test_category_sales_snacks(self):
        resp   = self._report()
        snacks = next(c for c in resp.context['category_sales']
                      if c['name'] == 'Snacks')
        self.assertAlmostEqual(snacks['total'], 80.0, places=2)

    def test_daily_sum_equals_total_revenue(self):
        resp      = self._report()
        daily_sum = sum(d['total'] for d in resp.context['daily_sales'])
        self.assertAlmostEqual(daily_sum,
                                float(resp.context['total_revenue']), places=2)
