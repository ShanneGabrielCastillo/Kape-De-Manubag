"""
Tests for the sales reports page.

Covers the daily-sales grouping (one query for the whole range instead of
two per day) and the sales-by-category section, plus a query-count guard so
the per-day N+1 cannot silently come back.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import CustomUser
from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem

PASSWORD = 'kdm-report-pass-123'


class ReportsIndexTests(TestCase):
    """Daily sales and category sales render correct figures."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = CustomUser.objects.create_user(
            username='report_admin', password=PASSWORD, role='admin',
        )
        cls.coffee = Category.objects.create(name='Coffee', slug='coffee')
        cls.snacks = Category.objects.create(name='Snacks', slug='snacks')
        cls.latte = Product.objects.create(
            category=cls.coffee, name='Latte', price='120.00',
        )
        cls.chips = Product.objects.create(
            category=cls.snacks, name='Chips', price='80.00',
        )

        today = timezone.now().date()
        cls.day1 = today - timedelta(days=2)
        cls.day2 = today - timedelta(days=1)

        # Create all orders first (so Order.save() assigns sequential unique
        # order numbers), then backdate created_at afterwards.
        # Day 1: one latte + one chips = 200.00 (paid, completed).
        order1 = Order.objects.create(
            customer_name='A', is_paid=True, status='completed',
        )
        OrderItem.objects.create(
            order=order1, product=cls.latte, product_name='Latte', size='none',
            quantity=1, unit_price=Decimal('120.00'), subtotal=Decimal('120.00'),
        )
        OrderItem.objects.create(
            order=order1, product=cls.chips, product_name='Chips', size='none',
            quantity=1, unit_price=Decimal('80.00'), subtotal=Decimal('80.00'),
        )
        order1.calculate_total()

        # Day 2: three lattes = 360.00 (paid, completed).
        order2 = Order.objects.create(
            customer_name='B', is_paid=True, status='completed',
        )
        OrderItem.objects.create(
            order=order2, product=cls.latte, product_name='Latte', size='none',
            quantity=3, unit_price=Decimal('120.00'), subtotal=Decimal('360.00'),
        )
        order2.calculate_total()

        # Today: unpaid/pending order — must be excluded from every figure.
        order3 = Order.objects.create(customer_name='C', is_paid=False)
        OrderItem.objects.create(
            order=order3, product=cls.latte, product_name='Latte', size='none',
            quantity=1, unit_price=Decimal('120.00'), subtotal=Decimal('120.00'),
        )
        order3.calculate_total()

        Order.objects.filter(pk=order1.pk).update(
            created_at=timezone.make_aware(
                datetime.combine(cls.day1, datetime.min.time()),
            ),
        )
        Order.objects.filter(pk=order2.pk).update(
            created_at=timezone.make_aware(
                datetime.combine(cls.day2, datetime.min.time()),
            ),
        )
        Order.objects.filter(pk=order3.pk).update(
            created_at=timezone.make_aware(
                datetime.combine(today, datetime.min.time()),
            ),
        )

    def _get_report(self):
        today = timezone.now().date()
        return self.client.get(
            reverse('reports:index'),
            {
                'start': self.day1.isoformat(),
                'end': today.isoformat(),
            },
        )

    def test_totals_cover_only_paid_orders(self):
        self.client.force_login(self.admin)
        response = self._get_report()
        self.assertEqual(response.status_code, 200)
        # 200 + 360 = 560; the unpaid 120 must not appear.
        self.assertContains(response, '₱560.00')
        self.assertNotContains(response, '₱680.00')

    def test_sales_by_category_section(self):
        self.client.force_login(self.admin)
        response = self._get_report()
        self.assertContains(response, '₱480.00')   # Coffee: 120 + 360
        self.assertContains(response, '4 items sold')
        self.assertContains(response, '₱80.00')    # Snacks
        self.assertContains(response, '1 items sold')

    def test_daily_sales_chart_data(self):
        self.client.force_login(self.admin)
        response = self._get_report()
        # The chart payload is the daily_sales list rendered via |safe; the
        # grouped query must reproduce the old per-day values.
        self.assertContains(response, '200.0')
        self.assertContains(response, '360.0')

    def test_reports_use_fixed_query_count(self):
        # Guard against the old two-queries-per-day N+1: with a 3-day range
        # the per-day loop cost 6+ queries; the grouped query must not scale
        # with the date range at all.
        #
        # After the category snapshot fix (sales by category now aggregates on
        # OrderItem.category_name instead of product__category__name), the
        # Category.objects.all() query was removed.  menu_category is no longer
        # queried by the view — it only appears if the ORM join traverses the
        # live product→category FK, which the fix eliminated.
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            response = self._get_report()
        self.assertEqual(response.status_code, 200)
        menu_queries = sum(
            1 for q in ctx.captured_queries
            if 'menu_category' in q['sql'] or 'menu_product' in q['sql']
        )
        # The category aggregation now uses the snapshot field — no live join
        # against menu_category needed.  menu_product may appear for the FK
        # join in the category_name aggregation on older SQLite versions but is
        # not required; assert 0 or 1 (not the old value of 2).
        self.assertLessEqual(menu_queries, 1,
            f"Expected ≤1 menu_category/product queries after snapshot fix, "
            f"got {menu_queries}")
        self.assertLessEqual(len(ctx.captured_queries), 12)
