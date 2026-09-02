"""
Query-count regression guards for the category surfaces.

These tests assert that the menu, POS, category list and product list pages
issue a FIXED number of queries against the menu tables (menu_category /
menu_product) regardless of how many categories and products exist. They
guard the N+1 property: if a future change introduces a per-category or
per-product query, the count jumps by the number of seeded rows and the
test fails.

Only queries touching the menu tables are counted, so the framework
overhead of an authenticated request (session / user lookups, the idle
timeout middleware's session save) does not make the assertions brittle
across Django versions.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import CustomUser
from apps.menu.models import Category, Product

PASSWORD = 'kdm-query-pass-123'


def menu_table_query_count(queries_ctx):
    """Number of captured queries that touch a menu table."""
    return sum(
        1 for q in queries_ctx.captured_queries
        if 'menu_category' in q['sql'] or 'menu_product' in q['sql']
    )


class CategoryQueryCountTests(TestCase):
    """The selling surfaces issue a fixed number of menu-table queries."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = CustomUser.objects.create_user(
            username='query_admin', password=PASSWORD, role='admin',
        )
        cls.cashier = CustomUser.objects.create_user(
            username='query_cashier', password=PASSWORD, role='cashier',
        )
        # Enough rows that any per-row (N+1) query would blow the counts.
        for i in range(6):
            cat = Category.objects.create(
                name=f'Query Cat {i}', slug=f'query-cat-{i}', order=i,
            )
            for j in range(5):
                Product.objects.create(
                    category=cat, name=f'Query Product {i}-{j}',
                    price='50.00', is_available=True,
                )

    def test_customer_menu_uses_fixed_two_queries(self):
        # 1 categories + 1 prefetched products, independent of row counts.
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('menu:index'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(menu_table_query_count(ctx), 2)

    def test_pos_uses_fixed_two_queries(self):
        self.client.force_login(self.cashier)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('orders:pos'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(menu_table_query_count(ctx), 2)

    def test_category_list_uses_one_query(self):
        # The product_count annotation is folded into the single list query.
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('menu:category_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(menu_table_query_count(ctx), 1)

    def test_product_list_uses_fixed_three_queries(self):
        # products (+ category join), category dropdown, active-order count.
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('menu:product_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(menu_table_query_count(ctx), 3)
