"""
Dashboard query-performance and correctness tests.

The dashboard is the busiest staff page, so its database behaviour is pinned
down by tests:

* the number of SQL queries executed for one dashboard load is bounded
  (regression guard against N+1 and per-day query loops),
* every statistic shown stays correct (sales sums, order counts,
  pending/preparing badges, top products, low stock, chart data),
* both admin and cashier dashboards render successfully (customers are
  redirected away),
* the AJAX chart endpoint stays fast and correct for week and month.

Note: this module applies the same test-client workaround as
apps/accounts/tests.py: ``Context.__copy__`` is patched because Django's own
implementation does ``copy(super())``, which crashes under Python 3.14 (PEP
667 made ``super`` objects immutable). The patch fixes that crash while
KEEPING the real ``Template._render``, so the test client still captures
``response.context`` (needed by the statistics tests).
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.db.models import Count, Sum
from django.template.context import Context
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import CustomUser
from apps.inventory.models import IngredientMovementLog, InventoryItem
from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem

# ── Python 3.14 test-client workaround (see apps/accounts/tests.py) ─────────
# Django 4.2.16 + Python 3.14 crash when the test client copies the render
# context (``Context.__copy__`` calls ``copy(super())``, which PEP 667 made
# immutable). Patching ``Context.__copy__`` with a working equivalent fixes
# the crash while KEEPING the real ``Template._render`` so the test client
# still captures ``response.context`` (needed by the statistics tests).


def _plain_context_copy(self):
    duplicate = self.__class__.__new__(self.__class__)
    duplicate.__dict__.update(self.__dict__)
    duplicate.dicts = self.dicts.copy()
    return duplicate


Context.__copy__ = _plain_context_copy

# ── Constants ────────────────────────────────────────────────────────────────

PASSWORD = 'kdm-dash-pass-123'
DASHBOARD_URL = '/dashboard/'
CHART_URL = '/dashboard/chart-data/'

# Bounded query budget for one dashboard page load. Measured breakdown on
# the supported stack (Django 5.0, SQLite):
#   9  view queries (sales stats, status counts, chart data, top products,
#      recent orders, legacy low stock, ingredient low stock, movement
#      summary, recent ingredient activity -- all single consolidated
#      queries)
#   2  session SELECT + user SELECT (auth middleware)
#   3  session UPDATE for the idle-timeout middleware's last-activity stamp
#      (savepoint + UPDATE + release; only on the first request in a window)
# Total: 14. The budget is exact (not a bound), so re-adding per-request
# queries (e.g. unused context-stat COUNTs or a cart lookup for staff) fails
# the suite. Because it is exact, re-verify the count after upgrading Django
# or changing middleware (session-write behaviour can shift it by +/- 1).
DASHBOARD_QUERY_BUDGET = 14
# The chart endpoint needs only the session/user lookups plus ONE grouped
# query per request. Measured at 3; the first authenticated request in an
# idle-timeout window adds the session UPDATE (savepoint/update/release), so
# the budget allows 6 for that worst case.
CHART_QUERY_BUDGET = 6
# The realtime summary endpoint runs the eight consolidated view queries
# (sales stats, status counts, chart data, top products, legacy low stock,
# ingredient low stock, movement summary, recent ingredient activity) plus
# the same auth/session lookups as the page.
# Budget: 8 view + 2 auth + 3 session UPDATE worst case = 13.
SUMMARY_QUERY_BUDGET = 13
SUMMARY_URL = '/dashboard/summary/'


def _create_user(username, role):
    return CustomUser.objects.create_user(
        username=username, password=PASSWORD, role=role,
    )


def _seed_orders_and_items():
    """Create the standard sales dataset shared by the statistics and summary
    tests: six orders spread across today / this week / this month / older
    (four paid, one pending, one preparing) plus three order items and one
    low-stock product.

    ``created_at`` is auto_now_add, so it is backdated with ``update()`` AFTER
    all orders are created -- doing it between creates would confuse the
    order-number sequence (which counts today's orders at insert time).

    Returns ``(category, low_product, now)`` so callers can keep references
    and derive date boundaries from the same clock the orders were seeded
    against (avoids a midnight-rollover mismatch between seed and assertion).
    """
    now = timezone.now()
    category = Category.objects.create(name='Drinks', slug='drinks')
    low_product = Product.objects.create(
        category=category, name='Low Stock Item', price='50.00',
        stock_quantity=3, low_stock_threshold=10,
    )
    backdates = []

    def make_order(total, created_at, *, paid=True, status='completed'):
        order = Order.objects.create(
            customer_name='Test Customer', order_type='dine_in',
            status=status, is_paid=paid,
            subtotal=Decimal(str(total)), total=Decimal(str(total)),
        )
        backdates.append((order.pk, created_at))
        return order

    order_today = make_order(100, now)
    order_week = make_order(200, now - timedelta(days=1))
    make_order(300, now - timedelta(days=10))
    make_order(400, now - timedelta(days=40))
    make_order(50, now, paid=False, status='awaiting_payment')
    make_order(70, now, paid=False, status='preparing')

    for pk, created_at in backdates:
        Order.objects.filter(pk=pk).update(created_at=created_at)

    OrderItem.objects.create(
        order=order_today, product_name='Americano', quantity=2,
        unit_price=Decimal('30.00'), subtotal=Decimal('60.00'),
    )
    OrderItem.objects.create(
        order=order_today, product_name='Latte', quantity=1,
        unit_price=Decimal('40.00'), subtotal=Decimal('40.00'),
    )
    OrderItem.objects.create(
        order=order_week, product_name='Americano', quantity=4,
        unit_price=Decimal('50.00'), subtotal=Decimal('200.00'),
    )
    return category, low_product, now


class DashboardQueryPerformanceTests(TestCase):
    """Query counts stay bounded (no N+1, no per-day loops)."""

    def setUp(self):
        self.admin = _create_user('dash_admin', 'admin')
        self.cashier = _create_user('dash_cashier', 'cashier')

    def _seed_sales_data(self):
        now = timezone.now()
        category = Category.objects.create(name='Drinks', slug='drinks')
        Product.objects.create(
            category=category, name='Low Stock Item', price='50.00',
            stock_quantity=3, low_stock_threshold=10,
        )
        Product.objects.create(
            category=category, name='Well Stocked Item', price='60.00',
            stock_quantity=100, low_stock_threshold=10,
        )

        # created_at is auto_now_add, so it is backdated with update() AFTER
        # all orders are created -- doing it between creates would confuse the
        # order-number sequence (which counts today's orders at insert time).
        backdates = []

        def make_order(total, days_ago, *, paid=True, status='completed'):
            order = Order.objects.create(
                customer_name='Test Customer', order_type='dine_in',
                status=status, is_paid=paid,
                subtotal=Decimal(str(total)), total=Decimal(str(total)),
            )
            if days_ago:
                backdates.append((order.pk, now - timedelta(days=days_ago)))
            return order

        order_today = make_order(100, 0)
        order_week = make_order(200, 3)
        make_order(300, 10)
        make_order(400, 40)
        make_order(50, 0, paid=False, status='awaiting_payment')
        make_order(70, 0, paid=False, status='preparing')

        for pk, created_at in backdates:
            Order.objects.filter(pk=pk).update(created_at=created_at)

        OrderItem.objects.create(
            order=order_today, product_name='Americano', quantity=2,
            unit_price=Decimal('30.00'), subtotal=Decimal('60.00'),
        )
        OrderItem.objects.create(
            order=order_today, product_name='Latte', quantity=1,
            unit_price=Decimal('40.00'), subtotal=Decimal('40.00'),
        )
        OrderItem.objects.create(
            order=order_week, product_name='Americano', quantity=4,
            unit_price=Decimal('50.00'), subtotal=Decimal('200.00'),
        )

    def _dashboard_get(self):
        """GET /dashboard/ and return (response, captured queries)."""
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(DASHBOARD_URL)
        return response, ctx

    def test_admin_dashboard_query_count_is_bounded(self):
        self._seed_sales_data()
        self.client.force_login(self.admin)
        response, ctx = self._dashboard_get()
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(ctx.captured_queries), DASHBOARD_QUERY_BUDGET,
            f'dashboard executed {len(ctx.captured_queries)} queries',
        )

    def test_cashier_dashboard_query_count_is_bounded(self):
        self._seed_sales_data()
        self.client.force_login(self.cashier)
        response, ctx = self._dashboard_get()
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(ctx.captured_queries), DASHBOARD_QUERY_BUDGET,
            f'dashboard executed {len(ctx.captured_queries)} queries',
        )

    def test_many_low_stock_products_do_not_add_queries(self):
        # 5 low-stock products: their category lookups (template renders
        # product.category.name) must not add a query per product.
        category = Category.objects.create(name='Drinks', slug='drinks')
        for i in range(5):
            Product.objects.create(
                category=category, name=f'Low {i}', price='10.00',
                stock_quantity=2, low_stock_threshold=10,
            )
        self.client.force_login(self.admin)
        response, ctx = self._dashboard_get()
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(ctx.captured_queries), DASHBOARD_QUERY_BUDGET,
            f'dashboard executed {len(ctx.captured_queries)} queries',
        )

    def test_chart_endpoint_query_count_is_bounded(self):
        self._seed_sales_data()
        self.client.force_login(self.admin)
        for period in ('week', 'month'):
            with self.subTest(period=period):
                with CaptureQueriesContext(connection) as ctx:
                    response = self.client.get(f'{CHART_URL}?period={period}')
                self.assertEqual(response.status_code, 200)
                self.assertLessEqual(
                    len(ctx.captured_queries), CHART_QUERY_BUDGET,
                    f'chart-data ({period}) executed '
                    f'{len(ctx.captured_queries)} queries',
                )


class DashboardStatisticsTests(TestCase):
    """All dashboard statistics stay accurate after optimization."""

    def setUp(self):
        self.admin = _create_user('dash_admin', 'admin')
        self.client.force_login(self.admin)
        self.category, self.low_product, now = _seed_orders_and_items()
        # The dashboard aggregates resolve __date lookups in the active
        # local timezone (Asia/Manila) from timezone.localdate()
        # (views.py _sales_stats). `now` from the seed is a UTC instant,
        # whose .date() is the UTC date — during Manila 00:00–08:00 that
        # differs from the Manila date and the assertion windows skew by a
        # day. Use the same local-date basis the view uses. The weekly
        # window is Sunday-start (isoweekday % 7), matching the view.
        self.today = timezone.localdate()
        self.week_start = self.today - timedelta(days=self.today.isoweekday() % 7)
        self.month_start = self.today.replace(day=1)
        # An extra, well-stocked product keeps the seed close to production.
        Product.objects.create(
            category=self.category, name='Well Stocked Item', price='60.00',
            stock_quantity=100, low_stock_threshold=10,
        )

    def _paid_totals(self, start_date=None):
        """Independently recompute paid-sales totals with the same day rules
        the dashboard uses, straight from the database."""
        qs = Order.objects.filter(is_paid=True)
        if start_date is not None:
            qs = qs.filter(created_at__date__gte=start_date)
        agg = qs.aggregate(total=Sum('total'), count=Count('id'))
        return agg['total'] or 0, agg['count'] or 0

    def test_sales_statistics_match_database(self):
        response = self.client.get(DASHBOARD_URL)
        ctx = response.context
        daily, daily_orders = self._paid_totals(self.today)
        weekly, weekly_orders = self._paid_totals(self.week_start)
        monthly, monthly_orders = self._paid_totals(self.month_start)
        total, total_orders = self._paid_totals()

        self.assertEqual(ctx['daily_sales'], daily)
        self.assertEqual(ctx['daily_orders'], daily_orders)
        self.assertEqual(ctx['weekly_sales'], weekly)
        self.assertEqual(ctx['weekly_orders'], weekly_orders)
        self.assertEqual(ctx['monthly_sales'], monthly)
        self.assertEqual(ctx['monthly_orders'], monthly_orders)
        self.assertEqual(ctx['total_sales'], total)
        self.assertEqual(ctx['total_orders'], total_orders)

        # Sanity checks with fully deterministic values.
        self.assertEqual(daily, Decimal('100.00'))
        self.assertEqual(daily_orders, 1)
        self.assertEqual(total, Decimal('1000.00'))
        self.assertEqual(total_orders, 4)

    def test_status_badges_are_correct(self):
        response = self.client.get(DASHBOARD_URL)
        ctx = response.context
        self.assertEqual(ctx['pending_count'], 1)
        self.assertEqual(ctx['preparing_count'], 1)

    def test_top_products_ranking_is_correct(self):
        response = self.client.get(DASHBOARD_URL)
        top = list(response.context['top_products'])
        self.assertEqual(top[0]['product_name'], 'Americano')
        self.assertEqual(top[0]['total_qty'], 6)
        self.assertEqual(top[0]['total_revenue'], Decimal('260.00'))
        self.assertEqual(top[1]['product_name'], 'Latte')

    def test_low_stock_list_is_correct(self):
        response = self.client.get(DASHBOARD_URL)
        low = list(response.context['low_stock'])
        self.assertEqual([p.pk for p in low], [self.low_product.pk])
        # Category name must resolve without an extra query per product.
        self.assertEqual(low[0].category.name, 'Drinks')

    def test_chart_data_covers_last_seven_days(self):
        response = self.client.get(DASHBOARD_URL)
        labels = response.context['chart_labels']
        data = response.context['chart_data']
        self.assertEqual(len(labels), 7)
        self.assertEqual(len(data), 7)
        self.assertEqual(labels, ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'])
        # Compute the Sunday-start week_start using the same formula as the view.
        week_start = self.today - timedelta(days=self.today.isoweekday() % 7)
        expected = []
        for i in range(7):
            day = week_start + timedelta(days=i)
            total = Order.objects.filter(
                is_paid=True, status='completed', created_at__date=day,
            ).aggregate(t=Sum('total'))['t'] or 0
            expected.append(float(total))
        self.assertEqual(data, expected)


class LegacyLowStockWidgetSeparationTests(TestCase):
    """Phase 7.1 — the legacy low-stock widget must show only products still
    tracked on the legacy stock ledger.

    Ingredient-tracked products keep their (frozen, non-authoritative)
    Product.stock_quantity but their real stock lives in the ingredient
    ledger (InventoryItem with its own low_stock_threshold), so they must
    never appear in this widget (Rev 3 §C.4, §E.3b). The ingredient widget
    itself is covered by IngredientLowStockWidgetTests (Phase 7.2).
    """

    def setUp(self):
        self.admin = _create_user('lowstock_admin', 'admin')
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')

    def _make_product(self, name, *, mode=Product.INVENTORY_TRACKING_LEGACY,
                      stock=3, threshold=10):
        """Sellable product below its threshold by default (stock 3 < 10)."""
        return Product.objects.create(
            category=self.category, name=name, price='50.00',
            stock_quantity=stock, low_stock_threshold=threshold,
            inventory_tracking_mode=mode,
        )

    def _get_low_stock_pks(self, url=DASHBOARD_URL):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return [p.pk for p in response.context['low_stock']]

    def test_legacy_low_stock_product_included(self):
        """A sellable legacy-tracked product below its threshold appears."""
        legacy = self._make_product('Legacy Low')
        self.assertIn(legacy.pk, self._get_low_stock_pks())

    def test_ingredient_tracked_low_stock_product_excluded(self):
        """An otherwise-qualifying ingredient-tracked product does not appear
        in the legacy widget even though stock_quantity is below threshold."""
        self._make_product('Ingredient Low',
                           mode=Product.INVENTORY_TRACKING_INGREDIENT)
        self.assertEqual(self._get_low_stock_pks(), [])

    def test_mixed_modes_include_legacy_exclude_ingredient(self):
        """With both modes present, only the legacy product is listed."""
        legacy = self._make_product('Legacy Low')
        self._make_product('Ingredient Low',
                           mode=Product.INVENTORY_TRACKING_INGREDIENT)
        self.assertEqual(self._get_low_stock_pks(), [legacy.pk])

    def test_summary_endpoint_low_stock_matches_page(self):
        """The realtime summary endpoint serialises low_stock from the same
        consolidated loader — the exclusion must hold there too."""
        legacy = self._make_product('Legacy Low')
        self._make_product('Ingredient Low',
                           mode=Product.INVENTORY_TRACKING_INGREDIENT)
        response = self.client.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNotNone(data['low_stock'])
        self.assertEqual([row['pk'] for row in data['low_stock']], [legacy.pk])


class IngredientLowStockWidgetTests(TestCase):
    """Phase 7.2 — ingredients and packaging low-stock widget.

    Covers the availability predicate (on_hand - reserved, not on-hand),
    threshold filtering, active-item filtering, both item types, ordering,
    the five-row limit, Critical classification, page/summary parity, error
    isolation, and the one-query guarantee.
    """

    def setUp(self):
        self.admin = _create_user('ingredient_low_admin', 'admin')
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')

    def _make_item(self, name, *, on_hand='50.000', reserved='0.000',
                   threshold='50.000',
                   item_type=InventoryItem.ITEM_TYPE_INGREDIENT,
                   base_unit=InventoryItem.BASE_UNIT_G, is_active=True):
        return InventoryItem.objects.create(
            name=name,
            item_type=item_type,
            base_unit=base_unit,
            on_hand_quantity=Decimal(on_hand),
            reserved_quantity=Decimal(reserved),
            low_stock_threshold=Decimal(threshold),
            is_active=is_active,
        )

    def _page_rows(self):
        response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        return response.context['ingredient_low_stock']

    # ── A. Threshold behavior ─────────────────────────────────────────────

    def test_items_at_or_below_threshold_are_included_above_are_not(self):
        at_threshold = self._make_item(
            'At Threshold', on_hand='50.000', threshold='50.000',
        )
        below = self._make_item(
            'Below Threshold', on_hand='20.000', threshold='50.000',
        )
        self._make_item('Above Threshold', on_hand='51.000', threshold='50.000')

        self.assertEqual(
            [row['pk'] for row in self._page_rows()],
            [below.pk, at_threshold.pk],
        )

    # ── B. Reservation-driven low stock ───────────────────────────────────

    def test_availability_accounts_for_reservations(self):
        item = self._make_item(
            'Reserved Coffee', on_hand='100.000', reserved='80.000',
            threshold='30.000',
        )

        rows = self._page_rows()
        self.assertEqual([row['pk'] for row in rows], [item.pk])
        self.assertEqual(rows[0]['available_quantity'], Decimal('20.000'))
        self.assertEqual(rows[0]['quantity_display'], '20')
        self.assertFalse(rows[0]['is_critical'])

    # ── C. Active/inactive behavior ───────────────────────────────────────

    def test_inactive_items_are_excluded(self):
        active = self._make_item('Active Item', on_hand='10.000', threshold='20.000')
        self._make_item(
            'Inactive Item', on_hand='10.000', threshold='20.000',
            is_active=False,
        )

        self.assertEqual([row['pk'] for row in self._page_rows()], [active.pk])

    # ── D. Packaging inclusion ────────────────────────────────────────────

    def test_ingredients_and_packaging_are_both_included(self):
        ingredient = self._make_item(
            'Coffee Beans', base_unit=InventoryItem.BASE_UNIT_G,
            on_hand='100.000', threshold='500.000',
        )
        packaging = self._make_item(
            'Takeaway Cups', item_type=InventoryItem.ITEM_TYPE_PACKAGING,
            base_unit=InventoryItem.BASE_UNIT_PCS,
            on_hand='10.000', threshold='20.000',
        )

        rows = self._page_rows()
        self.assertEqual([row['pk'] for row in rows], [packaging.pk, ingredient.pk])
        by_pk = {row['pk']: row for row in rows}
        self.assertEqual(by_pk[ingredient.pk]['item_type'], 'Ingredient')
        self.assertEqual(by_pk[ingredient.pk]['base_unit'], 'g')
        self.assertEqual(by_pk[packaging.pk]['item_type'], 'Packaging')
        self.assertEqual(by_pk[packaging.pk]['base_unit'], 'pcs')

    # ── E. Ordering and limit ─────────────────────────────────────────────

    def test_results_are_sorted_by_available_then_limited_to_five(self):
        for i in range(1, 10):
            self._make_item(
                f'Order Item {i}', on_hand=f'{i}.000', threshold='10.000',
            )

        self.assertEqual(
            [row['name'] for row in self._page_rows()],
            ['Order Item 1', 'Order Item 2', 'Order Item 3',
             'Order Item 4', 'Order Item 5'],
        )

    def test_equal_availability_uses_name_order(self):
        self._make_item('Zeta Item', on_hand='3.000', threshold='5.000')
        self._make_item('Alpha Item', on_hand='3.000', threshold='5.000')

        self.assertEqual(
            [row['name'] for row in self._page_rows()],
            ['Alpha Item', 'Zeta Item'],
        )

    # ── F. Critical classification ────────────────────────────────────────

    def test_critical_is_available_at_or_below_zero(self):
        out_of_stock = self._make_item(
            'Out Of Stock', on_hand='0.000', threshold='0.000',
        )
        low = self._make_item(
            'Still Positive', on_hand='5.000', threshold='5.000',
        )

        by_pk = {row['pk']: row for row in self._page_rows()}
        self.assertTrue(by_pk[out_of_stock.pk]['is_critical'])
        self.assertFalse(by_pk[low.pk]['is_critical'])

    # ── G. Quantity formatting and page/summary parity ────────────────────

    def test_quantity_formatting_removes_trailing_zeros(self):
        self._make_item('Milk', on_hand='12.500', threshold='250.000')

        row = self._page_rows()[0]
        self.assertEqual(row['quantity_display'], '12.5')
        self.assertEqual(row['threshold_display'], '250')

    def test_page_and_summary_expose_the_same_ordered_rows(self):
        self._make_item('Rice', on_hand='250.000', threshold='500.000')
        self._make_item(
            'Cups', item_type=InventoryItem.ITEM_TYPE_PACKAGING,
            base_unit=InventoryItem.BASE_UNIT_PCS,
            on_hand='12.500', reserved='2.500', threshold='20.000',
        )

        page_rows = self._page_rows()
        response = self.client.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 200)
        json_rows = response.json()['ingredient_low_stock']

        self.assertEqual(
            [row['pk'] for row in page_rows], [row['pk'] for row in json_rows],
        )
        for page_row, json_row in zip(page_rows, json_rows):
            self.assertEqual(page_row['name'], json_row['name'])
            self.assertEqual(page_row['item_type'], json_row['item_type'])
            self.assertEqual(page_row['base_unit'], json_row['base_unit'])
            self.assertEqual(
                page_row['quantity_display'], json_row['quantity_display'],
            )
            self.assertEqual(
                page_row['threshold_display'], json_row['threshold_display'],
            )
            self.assertEqual(page_row['is_critical'], json_row['is_critical'])
            self.assertIsInstance(json_row['quantity_display'], str)
            self.assertIsInstance(json_row['threshold_display'], str)
        self.assertIsInstance(page_rows[0]['available_quantity'], Decimal)

    # ── H. Error isolation ────────────────────────────────────────────────

    def test_page_renders_when_ingredient_widget_fails(self):
        Product.objects.create(
            category=self.category, name='Legacy Low', price='50.00',
            stock_quantity=3, low_stock_threshold=10,
        )
        with mock.patch(
            'apps.dashboard.views._ingredient_low_stock',
            side_effect=Exception('boom'),
        ):
            response = self.client.get(DASHBOARD_URL)

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'Ingredient stock data is temporarily unavailable',
            response.content.decode(),
        )
        self.assertIn('ingredient_low_stock', response.context['widget_errors'])
        self.assertEqual(response.context['ingredient_low_stock'], [])
        self.assertEqual(
            [p.name for p in response.context['low_stock']], ['Legacy Low'],
        )

    def test_summary_returns_null_only_for_failed_ingredient_widget(self):
        with mock.patch(
            'apps.dashboard.views._ingredient_low_stock',
            side_effect=Exception('boom'),
        ):
            response = self.client.get(SUMMARY_URL)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data['ingredient_low_stock'])
        self.assertIsNotNone(data['low_stock'])
        self.assertEqual(data['daily_sales'], 0.0)
        self.assertEqual(data['pending_count'], 0)

    # ── I. Query performance ──────────────────────────────────────────────

    def test_widget_uses_one_query_regardless_of_item_count(self):
        from apps.dashboard.views import _ingredient_low_stock

        self._make_item('Single Item', on_hand='1.000', threshold='10.000')
        with CaptureQueriesContext(connection) as first:
            _ingredient_low_stock()
        self.assertEqual(len(first.captured_queries), 1)

        for i in range(10):
            self._make_item(
                f'Many Item {i}', on_hand='1.000', threshold='10.000',
            )
        with CaptureQueriesContext(connection) as many:
            _ingredient_low_stock()
        self.assertEqual(len(many.captured_queries), 1)

    def test_dashboard_query_budget_with_many_low_stock_items(self):
        for i in range(10):
            self._make_item(
                f'Budget Item {i}', on_hand='1.000', threshold='10.000',
            )

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(DASHBOARD_URL)

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(ctx.captured_queries), DASHBOARD_QUERY_BUDGET,
            f'dashboard executed {len(ctx.captured_queries)} queries',
        )


class DashboardAccessTests(TestCase):
    """Admin and cashier can load the dashboard; customers cannot."""

    def setUp(self):
        self.admin = _create_user('dash_admin', 'admin')
        self.cashier = _create_user('dash_cashier', 'cashier')
        self.customer = _create_user('dash_customer', 'customer')

    def test_admin_dashboard_loads(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(DASHBOARD_URL).status_code, 200)

    def test_cashier_dashboard_loads(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(DASHBOARD_URL).status_code, 200)

    def test_customer_is_redirected_away(self):
        self.client.force_login(self.customer)
        response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')


class DashboardEmptyStateTests(TestCase):
    """Empty-state messaging: with no data every widget shows a friendly
    placeholder instead of a blank section, and with data the placeholders
    disappear. This is a pure template concern, so the assertions inspect the
    rendered HTML plus the context values the widgets are built from."""

    # Message fragments that must appear ONLY when the corresponding widget
    # has no data. (The chart placeholder is always in the DOM and toggled by
    # JS, so it is asserted separately.)
    EMPTY_MARKERS = [
        'No best-sellers yet',        # Top Products
        'No orders yet',              # Recent Orders
        'All items well-stocked',     # Low Stock
        'No orders yet today',        # Today's orders stat hint
        'No orders this month',       # This month's orders stat hint
    ]

    def setUp(self):
        self.admin = _create_user('empty_admin', 'admin')
        self.client.force_login(self.admin)

    def _seed(self):
        category = Category.objects.create(name='Drinks', slug='drinks')
        Product.objects.create(
            category=category, name='Low Stock Item', price='50.00',
            stock_quantity=3, low_stock_threshold=10,
        )
        order = Order.objects.create(
            customer_name='Test Customer', order_type='dine_in',
            status='completed', is_paid=True,
            subtotal=Decimal('100.00'), total=Decimal('100.00'),
        )
        OrderItem.objects.create(
            order=order, product_name='Americano', quantity=2,
            unit_price=Decimal('30.00'), subtotal=Decimal('60.00'),
        )

    def test_empty_database_renders_friendly_empty_states(self):
        response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        ctx = response.context
        # Widget data is genuinely empty -- zeros and empty querysets, no errors.
        self.assertEqual(ctx['total_sales'], 0)
        self.assertEqual(ctx['total_orders'], 0)
        self.assertEqual(ctx['pending_count'], 0)
        self.assertEqual(ctx['preparing_count'], 0)
        self.assertEqual(list(ctx['recent_orders']), [])
        self.assertEqual(list(ctx['top_products']), [])
        self.assertEqual(list(ctx['low_stock']), [])
        self.assertEqual(ctx['chart_data'], [0.0] * 7)
        content = response.content.decode()
        for marker in self.EMPTY_MARKERS:
            self.assertIn(marker, content)
        # The chart placeholder is server-rendered; JS reveals it when all
        # chart values are zero (the canvas is always kept for realtime data).
        self.assertContains(response, 'id="chart-empty"')
        self.assertContains(response, 'the chart will appear here once orders are placed')

    def test_seeded_data_hides_empty_states(self):
        self._seed()
        response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_orders'], 1)
        content = response.content.decode()
        for marker in self.EMPTY_MARKERS:
            self.assertNotIn(marker, content)
        # Stats show real counts instead of the "No orders" hints.
        self.assertContains(response, '1 order today')
        # The monthly stat hint switches to a real count too (the redesigned
        # dashboard has no all-time total hint; today/week/month/year only).
        self.assertContains(response, '1 order this month')


class DashboardSummaryEndpointTests(TestCase):
    """The realtime summary endpoint serves the dashboard's event-driven
    refresh: it returns every widget's current value from the same
    consolidated queries the page render uses, stays within a bounded query
    budget, and is staff-only (reusing the page's access rules)."""

    def setUp(self):
        self.admin = _create_user('sum_admin', 'admin')
        self.client.force_login(self.admin)

    def _seed(self):
        # Tests that need sales data call this explicitly; the "new order"
        # test deliberately runs against an empty database.
        _seed_orders_and_items()

    def _get_summary(self, period='week'):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(f'{SUMMARY_URL}?period={period}')
        return response, ctx

    def test_summary_matches_page_statistics(self):
        self._seed()
        response, _ = self._get_summary()
        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Same values the page renders (see DashboardStatisticsTests)
        self.assertEqual(data['daily_sales'], 100.0)
        self.assertEqual(data['daily_orders'], 1)
        self.assertEqual(data['weekly_sales'], 300.0)
        self.assertEqual(data['weekly_orders'], 2)
        self.assertEqual(data['total_sales'], 1000.0)
        self.assertEqual(data['total_orders'], 4)
        self.assertEqual(data['pending_count'], 1)
        self.assertEqual(data['preparing_count'], 1)

        # Chart series for the requested period (7 days for week)
        self.assertEqual(len(data['chart_labels']), 7)
        self.assertEqual(len(data['chart_data']), 7)
        # today's sales appear at today's index within the Sun–Sat week
        today_index = timezone.localdate().isoweekday() % 7  # Sun=0 … Sat=6
        self.assertEqual(data['chart_data'][today_index], 100.0)

        # Top products (Americano 6 sold / ₱260; Latte 1 / ₱40)
        self.assertEqual(data['top_products'][0]['product_name'], 'Americano')
        self.assertEqual(data['top_products'][0]['total_qty'], 6)
        self.assertEqual(data['top_products'][0]['total_revenue'], 260.0)
        self.assertEqual(data['top_products'][1]['product_name'], 'Latte')

        # Low stock (the only product under its threshold)
        self.assertEqual(len(data['low_stock']), 1)
        self.assertEqual(data['low_stock'][0]['name'], 'Low Stock Item')
        self.assertEqual(data['low_stock'][0]['stock_quantity'], 3)
        self.assertTrue(data['low_stock'][0]['is_critical'])

    def test_summary_month_period_is_month_to_date(self):
        # The 'month' period is the current calendar month TO DATE (Rev 3
        # frontend contract: heading "This Month", _period_start docstring
        # "'month' → 1st of the current calendar month"). Labels run from
        # the 1st through today inclusive — NOT a rolling 30-day window.
        self._seed()
        response, _ = self._get_summary('month')
        data = response.json()
        expected_days = timezone.localdate().day
        self.assertEqual(len(data['chart_labels']), expected_days)
        self.assertEqual(len(data['chart_data']), expected_days)
        # The seeded completed order placed today is the last bar. (Earlier
        # bars may be non-zero too: the seed also backdates paid orders to
        # yesterday and 10/40 days ago, which can fall inside this month.)
        self.assertEqual(data['chart_data'][-1], 100.0)

    def test_summary_query_count_is_bounded(self):
        self._seed()
        response, ctx = self._get_summary()
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(ctx.captured_queries), SUMMARY_QUERY_BUDGET,
            f'summary executed {len(ctx.captured_queries)} queries',
        )

    def test_summary_requires_staff(self):
        customer = _create_user('sum_customer', 'customer')
        self.client.force_login(customer)
        response = self.client.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

    def test_summary_reflects_new_order(self):
        # No data yet: everything zero
        response, _ = self._get_summary()
        self.assertEqual(response.json()['pending_count'], 0)
        self.assertEqual(response.json()['daily_orders'], 0)

        # A new unpaid order appears as pending and in recent data
        Order.objects.create(
            customer_name='New Customer', order_type='take_out',
            status='awaiting_payment', is_paid=False,
            subtotal=Decimal('25.00'), total=Decimal('25.00'),
        )
        response, _ = self._get_summary()
        data = response.json()
        self.assertEqual(data['pending_count'], 1)
        # Unpaid orders do not count toward sales totals
        self.assertEqual(data['daily_orders'], 0)
        self.assertEqual(data['daily_sales'], 0.0)

    def test_summary_reflects_payment(self):
        self._seed()
        response, _ = self._get_summary()
        self.assertEqual(response.json()['pending_count'], 1)
        self.assertEqual(response.json()['daily_orders'], 1)

        # Pay the pending order: it leaves pending and enters today's sales
        order = Order.objects.get(status='awaiting_payment')
        order.is_paid = True
        order.status = 'completed'
        order.save(update_fields=['is_paid', 'status'])

        response, _ = self._get_summary()
        data = response.json()
        self.assertEqual(data['pending_count'], 0)
        self.assertEqual(data['daily_orders'], 2)
        self.assertEqual(data['daily_sales'], 150.0)  # 100 + 50

    def test_summary_reflects_status_change(self):
        self._seed()
        response, _ = self._get_summary()
        data = response.json()
        self.assertEqual(data['pending_count'], 1)
        self.assertEqual(data['preparing_count'], 1)

        # Move the pending order to preparing: pending drops, preparing rises
        order = Order.objects.get(status='awaiting_payment')
        order.status = 'preparing'
        order.save(update_fields=['status'])

        response, _ = self._get_summary()
        data = response.json()
        self.assertEqual(data['pending_count'], 0)
        self.assertEqual(data['preparing_count'], 2)


class DashboardErrorResilienceTests(TestCase):
    """A failure in one widget must never take down the rest of the dashboard.

    Every widget is computed independently (see ``_widget`` in views.py): the
    page render keeps a friendly fallback per failed widget, the summary
    endpoint returns ``None`` for the failed widget only, and unexpected
    errors are logged. These tests simulate widget failures by patching the
    widget helpers to raise.
    """

    def setUp(self):
        self.admin = _create_user('resil_admin', 'admin')
        self.client.force_login(self.admin)
        self._seed()

    def _seed(self):
        now = timezone.now()
        category = Category.objects.create(name='Drinks', slug='drinks')
        Product.objects.create(
            category=category, name='Low Stock Item', price='50.00',
            stock_quantity=3, low_stock_threshold=10,
        )
        order = Order.objects.create(
            customer_name='Test Customer', order_type='dine_in',
            status='completed', is_paid=True,
            subtotal=Decimal('100.00'), total=Decimal('100.00'),
        )
        OrderItem.objects.create(
            order=order, product_name='Americano', quantity=2,
            unit_price=Decimal('30.00'), subtotal=Decimal('60.00'),
        )

    # ── Page render ────────────────────────────────────────────────────────

    def test_page_renders_when_one_widget_fails(self):
        with mock.patch('apps.dashboard.views._top_products', side_effect=Exception('boom')):
            response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # The failed widget shows a fallback message...
        self.assertIn('Top products are temporarily unavailable', content)
        # ...while the healthy widgets still render real data.
        self.assertIn('1 order today', content)
        self.assertIn('Low Stock Item', content)
        self.assertContains(response, 'id="recent-orders-body"')

    def test_page_renders_when_sales_stats_fail(self):
        with mock.patch('apps.dashboard.views._sales_stats', side_effect=Exception('boom')):
            response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Stat cards fall back to a placeholder (not a misleading ₱0.00)...
        self.assertEqual(content.count('>—<'), 4)
        self.assertIn('Unavailable', content)
        # ...and the rest of the dashboard is untouched.
        self.assertIn('Low Stock Item', content)
        self.assertIn('Americano', content)

    def test_page_renders_when_multiple_widgets_fail(self):
        with mock.patch('apps.dashboard.views._low_stock', side_effect=Exception('boom')), \
             mock.patch('apps.dashboard.views._chart_series', side_effect=Exception('boom')):
            response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Low stock data is temporarily unavailable', content)
        self.assertIn('Chart data is temporarily unavailable', content)
        # Healthy widgets unaffected.
        self.assertIn('Americano', content)
        self.assertIn('1 order today', content)

    def test_chart_canvas_survives_chart_widget_failure(self):
        """The chart <canvas> stays in the DOM even when the chart widget
        fails, so a later period-switch or realtime refresh can rebuild the
        chart without a page reload (the error overlay is what is shown)."""
        with mock.patch('apps.dashboard.views._chart_series', side_effect=Exception('boom')):
            response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="sales-chart"', content)
        self.assertIn('Chart data is temporarily unavailable', content)
        # The rest of the dashboard keeps rendering real data.
        self.assertIn('Americano', content)
        self.assertIn('1 order today', content)

    def test_widget_failures_are_logged(self):
        with self.assertLogs('apps.dashboard', level='ERROR') as logs, \
             mock.patch('apps.dashboard.views._top_products', side_effect=Exception('boom')):
            response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any('Dashboard widget failed' in line for line in logs.output))

    # ── Summary endpoint ───────────────────────────────────────────────────

    def test_summary_returns_none_for_failed_widget_only(self):
        with mock.patch('apps.dashboard.views._top_products', side_effect=Exception('boom')):
            response = self.client.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # The failed widget is null; every other widget still reports values.
        self.assertIsNone(data['top_products'])
        self.assertEqual(data['daily_sales'], 100.0)
        self.assertEqual(data['daily_orders'], 1)
        self.assertIsNotNone(data['low_stock'])
        self.assertEqual(data['pending_count'], 0)

    def test_summary_isolates_sales_stats_failure(self):
        with mock.patch('apps.dashboard.views._sales_stats', side_effect=Exception('boom')):
            response = self.client.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data['daily_sales'])
        self.assertIsNone(data['total_orders'])
        # Independent widgets still work.
        self.assertEqual(data['top_products'][0]['product_name'], 'Americano')
        self.assertIsNotNone(data['low_stock'])

    def test_summary_failure_does_not_raise(self):
        # Even when ALL widgets fail, the endpoint returns 200 with nulls
        # (never a 500) so the client can show fallback messages.
        failures = {
            '_sales_stats': Exception('boom'),
            '_status_counts': Exception('boom'),
            '_chart_series': Exception('boom'),
            '_top_products': Exception('boom'),
            '_low_stock': Exception('boom'),
            '_ingredient_low_stock': Exception('boom'),
            '_ingredient_movement_summary': Exception('boom'),
            '_recent_ingredient_activity': Exception('boom'),
        }
        with mock.patch.multiple('apps.dashboard.views', **failures):
            response = self.client.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key in ('daily_sales', 'daily_orders', 'pending_count',
                    'chart_data', 'top_products', 'low_stock',
                    'ingredient_low_stock', 'ingredient_movement_summary',
                    'recent_ingredient_activity'):
            self.assertIsNone(data[key])

    # ── Chart endpoint ─────────────────────────────────────────────────────

    def test_chart_endpoint_returns_503_on_failure(self):
        with mock.patch('apps.dashboard.views._chart_series', side_effect=Exception('boom')):
            response = self.client.get(CHART_URL)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'error': 'chart unavailable'})


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar Navigation Active-State Tests
#
# Verifies that exactly ONE sidebar navigation item carries the ``active``
# class on every admin page.  The critical regression case is GCash Settings:
# it must activate *only* "GCash Settings", not Dashboard or Settings.
#
# How the check works:
#   Each sidebar link is identified by its stable ``aria-label`` attribute.
#   We fetch the page, scan the rendered HTML, and assert that the single
#   expected label's ``<a>`` tag contains the class "active", while every
#   other label's ``<a>`` tag does NOT.
#
# Implementation detail: ``re.search`` is used rather than a full HTML parser
# because (a) the sidebar markup is predictable/stable and (b) it avoids an
# extra test dependency.  The regex matches ``<a … class="… active …"`` blocks
# that contain the given aria-label on the same element.
# ─────────────────────────────────────────────────────────────────────────────

import re

from django.urls import reverse


# All sidebar nav items that carry an aria-label.
# The list is used to assert that all *other* items are NOT active.
ALL_NAV_LABELS = [
    'Dashboard',
    'Order Management',
    'POS Terminal',
    'Products',
    'Categories',
    'Inventory',
    'Sales Reports',
    'Staff Accounts',
    'Settings',
    'GCash Settings',
    'Activity Log',
    'Finance',
]


def _active_labels(html: str) -> list[str]:
    """Return every aria-label whose <a> element carries the 'active' class."""
    found = []
    for label in ALL_NAV_LABELS:
        # Match an <a …> opening tag that contains BOTH the aria-label and
        # the 'active' CSS class.  The tag ends at the first '>'.
        # We use re.DOTALL so the dot matches newlines inside long class attrs.
        pattern = (
            r'<a\b[^>]*\baria-label="' + re.escape(label) + r'"[^>]*\bactive\b[^>]*>'
            r'|'
            r'<a\b[^>]*\bactive\b[^>]*\baria-label="' + re.escape(label) + r'"[^>]*>'
        )
        if re.search(pattern, html, re.DOTALL):
            found.append(label)
    return found


class SidebarNavigationActiveStateTests(TestCase):
    """Exactly one sidebar nav item is active on each admin page.

    Critical regression: visiting GCash Settings must NOT activate Dashboard
    or Settings — only GCash Settings should be active.
    """

    def setUp(self):
        # Admin user has access to all sidebar items.
        self.admin = CustomUser.objects.create_user(
            username='nav_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)

    def _assert_only_active(self, url, expected_label, *, msg_prefix=''):
        """GET ``url``, assert ``expected_label`` is active and no other is."""
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200,
                         f'{msg_prefix}Expected 200 for {url}')
        html = response.content.decode()
        active = _active_labels(html)
        self.assertEqual(
            active, [expected_label],
            f'{msg_prefix}URL {url!r}: expected only [{expected_label!r}] '
            f'active, got {active}',
        )

    # ── Dashboard ──────────────────────────────────────────────────────────

    def test_dashboard_activates_only_dashboard(self):
        self._assert_only_active(
            reverse('dashboard:index'), 'Dashboard',
        )

    # ── Settings (the critical overlap cases) ─────────────────────────────

    def test_settings_activates_only_settings(self):
        self._assert_only_active(
            reverse('dashboard:system_settings'), 'Settings',
        )

    def test_gcash_settings_activates_only_gcash_settings(self):
        """The main regression: GCash Settings must NOT also activate
        Dashboard or Settings."""
        self._assert_only_active(
            reverse('dashboard:gcash_settings'), 'GCash Settings',
        )

    def test_gcash_settings_does_not_activate_dashboard(self):
        response = self.client.get(reverse('dashboard:gcash_settings'))
        html = response.content.decode()
        active = _active_labels(html)
        self.assertNotIn('Dashboard', active,
                         'Dashboard must NOT be active on the GCash Settings page')
        self.assertNotIn('Settings', active,
                         'Settings must NOT be active on the GCash Settings page')
        self.assertIn('GCash Settings', active,
                      'GCash Settings MUST be active on the GCash Settings page')

    # ── Orders ─────────────────────────────────────────────────────────────

    def test_order_list_activates_only_order_management(self):
        self._assert_only_active(
            reverse('orders:order_list'), 'Order Management',
        )

    def test_pos_activates_only_pos_terminal(self):
        self._assert_only_active(
            reverse('orders:pos'), 'POS Terminal',
        )

    # ── Menu ───────────────────────────────────────────────────────────────

    def test_products_activates_only_products(self):
        self._assert_only_active(
            reverse('menu:product_list'), 'Products',
        )

    def test_categories_activates_only_categories(self):
        self._assert_only_active(
            reverse('menu:category_list'), 'Categories',
        )

    # ── Stock & Reports ────────────────────────────────────────────────────

    def test_inventory_activates_only_inventory(self):
        self._assert_only_active(
            reverse('inventory:list'), 'Inventory',
        )

    def test_reports_activates_only_sales_reports(self):
        self._assert_only_active(
            reverse('reports:index'), 'Sales Reports',
        )

    # ── Staff Accounts ─────────────────────────────────────────────────────

    def test_staff_list_activates_only_staff_accounts(self):
        self._assert_only_active(
            reverse('accounts:staff_list'), 'Staff Accounts',
        )

    # ── Finance ────────────────────────────────────────────────────────────

    def test_finance_activates_only_finance(self):
        self._assert_only_active(
            reverse('finance:index'), 'Finance',
        )

    # ── Navigation sequence regression ────────────────────────────────────

    def test_navigation_sequence_settings_to_gcash(self):
        """Navigating Settings → GCash Settings must shift active correctly."""
        response = self.client.get(reverse('dashboard:system_settings'))
        self.assertEqual(_active_labels(response.content.decode()), ['Settings'])

        response = self.client.get(reverse('dashboard:gcash_settings'))
        self.assertEqual(_active_labels(response.content.decode()), ['GCash Settings'])

    def test_navigation_sequence_gcash_back_to_dashboard(self):
        """Navigating GCash Settings → Dashboard restores Dashboard as active."""
        response = self.client.get(reverse('dashboard:gcash_settings'))
        self.assertEqual(_active_labels(response.content.decode()), ['GCash Settings'])

        response = self.client.get(reverse('dashboard:index'))
        self.assertEqual(_active_labels(response.content.decode()), ['Dashboard'])

    def test_navigation_sequence_full(self):
        """Full navigation sequence: every page activates only its own item."""
        sequence = [
            (reverse('dashboard:index'),           'Dashboard'),
            (reverse('dashboard:system_settings'), 'Settings'),
            (reverse('dashboard:gcash_settings'),  'GCash Settings'),
            (reverse('dashboard:index'),           'Dashboard'),
            (reverse('orders:order_list'),         'Order Management'),
            (reverse('dashboard:gcash_settings'),  'GCash Settings'),
            (reverse('orders:pos'),                'POS Terminal'),
            (reverse('inventory:list'),            'Inventory'),
            (reverse('reports:index'),             'Sales Reports'),
            (reverse('accounts:staff_list'),       'Staff Accounts'),
            (reverse('menu:product_list'),         'Products'),
            (reverse('menu:category_list'),        'Categories'),
            (reverse('finance:index'),             'Finance'),
        ]
        for url, expected in sequence:
            with self.subTest(url=url, expected=expected):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                active = _active_labels(response.content.decode())
                self.assertEqual(
                    active, [expected],
                    f'After navigating to {url!r}: expected [{expected!r}] '
                    f'active, got {active}',
                )

    # ── Query-parameter stability ──────────────────────────────────────────

    def test_order_list_with_query_param_stays_active(self):
        """Query parameters must not break the active state."""
        self._assert_only_active(
            reverse('orders:order_list') + '?page=2', 'Order Management',
        )

    def test_gcash_settings_page_query_param_stays_active(self):
        self._assert_only_active(
            reverse('dashboard:gcash_settings') + '?saved=1', 'GCash Settings',
        )


class SidebarJSRegressionTest(TestCase):
    """Guard against the JS active-state override being re-introduced.

    The root cause of the multiple-active-items bug was a block in main.js:

        document.querySelectorAll('.sidebar-link').forEach(link => {
          if (link.href === window.location.href ||
              window.location.pathname.startsWith(link.getAttribute('href'))) {
            link.classList.add('active');
          }
        });

    ``startsWith`` causes every ancestor path to also receive the active class
    (e.g. visiting /dashboard/settings/gcash/ makes Dashboard, Settings AND
    GCash Settings all active because the path starts with /dashboard/ and
    /dashboard/settings/).

    This test reads main.js directly and fails if that pattern is ever
    re-introduced, ensuring the fix cannot silently regress.
    """

    def test_main_js_does_not_contain_startswith_active_override(self):
        import os
        js_path = os.path.join(
            os.path.dirname(__file__),   # apps/dashboard/
            '..', '..',                   # project root
            'static', 'js', 'main.js',
        )
        js_path = os.path.normpath(js_path)
        with open(js_path, 'r', encoding='utf-8') as f:
            js_source = f.read()

        # The offending pattern: startsWith on a link's href to decide active.
        # We check for the combination rather than startsWith alone (which is
        # used legitimately elsewhere).
        bad_pattern = "pathname.startsWith(link.getAttribute('href'))"
        self.assertNotIn(
            bad_pattern, js_source,
            "main.js contains the startsWith-based sidebar active override "
            "that causes multiple items to appear active simultaneously. "
            "This block must be removed — active state is set server-side "
            "by Django's request.resolver_match in base_admin.html."
        )


class Phase73MovementWidgetTestBase(TestCase):
    """Shared fixtures for the Phase 7.3 movement widgets."""

    def setUp(self):
        self.admin = _create_user('movement_admin', 'admin')
        self.client.force_login(self.admin)

    def _make_item(self, name, *, base_unit=InventoryItem.BASE_UNIT_G,
                   item_type=InventoryItem.ITEM_TYPE_INGREDIENT,
                   is_active=True):
        return InventoryItem.objects.create(
            name=name,
            item_type=item_type,
            base_unit=base_unit,
            on_hand_quantity=Decimal('100.000'),
            reserved_quantity=Decimal('0.000'),
            low_stock_threshold=Decimal('10.000'),
            is_active=is_active,
        )

    def _make_movement(self, item, movement_type, delta, *, created_at=None,
                       reason_code='', note='', order_number=''):
        """Create a constraint-valid movement row for widget tests."""
        delta = Decimal(str(delta))
        on_hand_before = Decimal('100.000')
        on_hand_delta = delta
        reserved_before = Decimal('0.000')
        reserved_delta = Decimal('0.000')

        if movement_type == IngredientMovementLog.MOVEMENT_CONSUMPTION:
            reserved_before = abs(delta)
            reserved_delta = delta
        elif movement_type == IngredientMovementLog.MOVEMENT_RESERVATION:
            on_hand_delta = Decimal('0.000')
            reserved_delta = abs(delta)
        elif movement_type == IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE:
            on_hand_delta = Decimal('0.000')
            reserved_before = abs(delta)
            reserved_delta = -abs(delta)

        movement = IngredientMovementLog.objects.create(
            inventory_item=item,
            item_name=item.name,
            base_unit=item.base_unit,
            movement_type=movement_type,
            on_hand_delta=on_hand_delta,
            reserved_delta=reserved_delta,
            on_hand_before=on_hand_before,
            on_hand_after=on_hand_before + on_hand_delta,
            reserved_before=reserved_before,
            reserved_after=reserved_before + reserved_delta,
            order_number=order_number,
            reason_code=reason_code,
            note=note,
        )
        if created_at is not None:
            IngredientMovementLog.objects.filter(pk=movement.pk).update(
                created_at=created_at,
            )
            movement.created_at = created_at
        return movement

    def _summary(self, period='week'):
        from apps.dashboard.views import _ingredient_movement_summary
        return _ingredient_movement_summary(period)


class Phase73MovementSummaryTests(Phase73MovementWidgetTestBase):
    """Per-unit physical movement summaries (Phase 7.3 decisions D1-D4)."""

    def test_physical_totals_are_per_unit_and_use_on_hand_delta(self):
        coffee = self._make_item('Coffee Beans')
        cups = self._make_item(
            'Takeaway Cups', base_unit=InventoryItem.BASE_UNIT_PCS,
            item_type=InventoryItem.ITEM_TYPE_PACKAGING,
        )
        self._make_movement(coffee, IngredientMovementLog.MOVEMENT_PURCHASE, 500)
        self._make_movement(coffee, IngredientMovementLog.MOVEMENT_PURCHASE, 200)
        self._make_movement(coffee, IngredientMovementLog.MOVEMENT_CONSUMPTION, -50)
        self._make_movement(coffee, IngredientMovementLog.MOVEMENT_CONSUMPTION, -30)
        self._make_movement(coffee, IngredientMovementLog.MOVEMENT_WASTAGE, -20)
        self._make_movement(coffee, IngredientMovementLog.MOVEMENT_ADJUSTMENT, 10)
        self._make_movement(coffee, IngredientMovementLog.MOVEMENT_ADJUSTMENT, -4)
        self._make_movement(coffee, IngredientMovementLog.MOVEMENT_RESERVATION, 50)
        self._make_movement(
            coffee, IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE, 50,
        )
        self._make_movement(cups, IngredientMovementLog.MOVEMENT_PURCHASE, 120)
        self._make_movement(cups, IngredientMovementLog.MOVEMENT_CONSUMPTION, -5)
        self._make_movement(cups, IngredientMovementLog.MOVEMENT_WASTAGE, -2)
        self._make_movement(cups, IngredientMovementLog.MOVEMENT_ADJUSTMENT, 3)

        rows = self._summary()
        self.assertEqual([row['base_unit'] for row in rows], ['g', 'pcs'])
        self.assertEqual(rows[0], {
            'base_unit': 'g',
            'consumed': '80',
            'purchased': '700',
            'wasted': '20',
            'net_adjustment': '6',
            'net_adjustment_display': '+6',
        })
        self.assertEqual(rows[1], {
            'base_unit': 'pcs',
            'consumed': '5',
            'purchased': '120',
            'wasted': '2',
            'net_adjustment': '3',
            'net_adjustment_display': '+3',
        })

    def test_reservations_and_releases_alone_create_no_summary_row(self):
        item = self._make_item('Reserved Only', base_unit=InventoryItem.BASE_UNIT_ML)
        self._make_movement(item, IngredientMovementLog.MOVEMENT_RESERVATION, 50)
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE, 50,
        )
        self.assertEqual(self._summary(), [])

    def test_inactive_item_history_is_included(self):
        item = self._make_item('Retired Item', is_active=False)
        self._make_movement(item, IngredientMovementLog.MOVEMENT_PURCHASE, 25)
        self.assertEqual(self._summary()[0]['purchased'], '25')

    def test_week_period_excludes_previous_week(self):
        item = self._make_item('Weekly Coffee')
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 40,
            created_at=timezone.now() - timedelta(days=7),
        )
        self._make_movement(item, IngredientMovementLog.MOVEMENT_PURCHASE, 60)
        self.assertEqual(self._summary('week')[0]['purchased'], '60')

    def test_month_period_excludes_previous_month(self):
        item = self._make_item('Monthly Milk', base_unit=InventoryItem.BASE_UNIT_ML)
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 40,
            created_at=timezone.now() - timedelta(days=40),
        )
        self._make_movement(item, IngredientMovementLog.MOVEMENT_PURCHASE, 60)
        self.assertEqual(self._summary('month')[0]['purchased'], '60')

    def test_local_period_boundary_is_inclusive_start_exclusive_end(self):
        from apps.dashboard.views import _period_start

        item = self._make_item('Boundary Coffee')
        today = timezone.localdate()
        start_date = _period_start('week', today)
        end_date = start_date + timedelta(days=7)
        start = timezone.make_aware(datetime.combine(start_date, time.min))
        end = timezone.make_aware(datetime.combine(end_date, time.min))
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 10,
            created_at=start - timedelta(seconds=1),
        )
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 20,
            created_at=start,
        )
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 30,
            created_at=end - timedelta(seconds=1),
        )
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 40,
            created_at=end,
        )
        self.assertEqual(self._summary('week')[0]['purchased'], '50')

    def test_empty_summary_is_empty_not_a_failure(self):
        self.assertEqual(self._summary(), [])

    def test_page_and_summary_expose_same_summary_dataset(self):
        item = self._make_item('Parity Beans')
        self._make_movement(item, IngredientMovementLog.MOVEMENT_PURCHASE, 75)

        page = self.client.get(DASHBOARD_URL)
        summary = self.client.get(SUMMARY_URL)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(
            page.context['ingredient_movement_summary'],
            summary.json()['ingredient_movement_summary'],
        )
        for row in summary.json()['ingredient_movement_summary']:
            for key in ('consumed', 'purchased', 'wasted',
                        'net_adjustment', 'net_adjustment_display'):
                self.assertIsInstance(row[key], str)

    def test_summary_uses_one_query_regardless_of_movement_count(self):
        item = self._make_item('Query Beans')
        for i in range(20):
            self._make_movement(item, IngredientMovementLog.MOVEMENT_PURCHASE, i + 1)
        with CaptureQueriesContext(connection) as ctx:
            self._summary()
        self.assertEqual(len(ctx.captured_queries), 1)

    def test_page_renders_when_summary_widget_fails(self):
        with mock.patch(
            'apps.dashboard.views._ingredient_movement_summary',
            side_effect=Exception('boom'),
        ):
            page = self.client.get(DASHBOARD_URL)
            summary = self.client.get(SUMMARY_URL)

        self.assertEqual(page.status_code, 200)
        self.assertContains(
            page, 'Movement summary is temporarily unavailable',
        )
        self.assertIn('ingredient_movement_summary', page.context['widget_errors'])
        self.assertIsNone(summary.json()['ingredient_movement_summary'])
        self.assertIsNotNone(summary.json()['recent_ingredient_activity'])


class Phase73RecentActivityTests(Phase73MovementWidgetTestBase):
    """Bounded recent physical activity feed (Phase 7.3 decisions D5-D7)."""

    def test_physical_types_are_signed_snapshotted_and_limited_to_five(self):
        items = [
            self._make_item(f'Activity Item {i}') for i in range(6)
        ]
        base = timezone.now() - timedelta(minutes=10)
        for i, item in enumerate(items):
            self._make_movement(
                item, IngredientMovementLog.MOVEMENT_PURCHASE, 100,
                created_at=base + timedelta(minutes=i),
            )
        # A newer non-physical event must not displace physical rows.
        self._make_movement(
            items[0], IngredientMovementLog.MOVEMENT_RESERVATION, 20,
            created_at=timezone.now(),
        )
        self._make_movement(
            items[0], IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE, 20,
            created_at=timezone.now(),
        )

        from apps.dashboard.views import _recent_ingredient_activity
        rows = _recent_ingredient_activity()
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            [row['item_name'] for row in rows],
            ['Activity Item 5', 'Activity Item 4', 'Activity Item 3',
             'Activity Item 2', 'Activity Item 1'],
        )
        self.assertTrue(all(
            row['movement_type'] == IngredientMovementLog.MOVEMENT_PURCHASE
            for row in rows
        ))

    def test_signed_quantities_and_snapshot_fields(self):
        item = self._make_item('Snapshot Beans')
        base = timezone.now() - timedelta(minutes=4)
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 500,
            created_at=base, order_number='ORD-001', note='Delivery',
        )
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_CONSUMPTION, -50,
            created_at=base + timedelta(minutes=1),
            reason_code='cancelled_prepared',
        )
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_WASTAGE, -20,
            created_at=base + timedelta(minutes=2), reason_code='expired',
        )
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_ADJUSTMENT, -4,
            created_at=base + timedelta(minutes=3),
            reason_code='stock_count_correction',
        )
        item.name = 'Renamed Beans'
        item.save(update_fields=['name'])

        from apps.dashboard.views import _recent_ingredient_activity
        rows = _recent_ingredient_activity()
        self.assertEqual([row['quantity_display'] for row in rows],
                         ['-4', '-20', '-50', '+500'])
        self.assertEqual(rows[0]['reason_display'], 'Stock count correction')
        self.assertEqual(rows[1]['reason_display'], 'Expired')
        self.assertEqual(rows[2]['reason_display'], 'Cancelled prepared')
        self.assertEqual(rows[3]['order_number'], 'ORD-001')
        self.assertEqual(rows[3]['note'], 'Delivery')
        self.assertTrue(all(row['item_name'] == 'Snapshot Beans' for row in rows))
        self.assertTrue(all(row['base_unit'] == 'g' for row in rows))
        self.assertTrue(all(isinstance(row['created_at'], str) for row in rows))

    def test_order_ties_use_descending_primary_key(self):
        item = self._make_item('Tie Item')
        same_time = timezone.now()
        first = self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 10,
            created_at=same_time,
        )
        second = self._make_movement(
            item, IngredientMovementLog.MOVEMENT_PURCHASE, 20,
            created_at=same_time,
        )

        from apps.dashboard.views import _recent_ingredient_activity
        rows = _recent_ingredient_activity()
        self.assertEqual([row['pk'] for row in rows], [second.pk, first.pk])

    def test_only_nonphysical_events_produce_empty_activity(self):
        item = self._make_item('No Physical')
        self._make_movement(item, IngredientMovementLog.MOVEMENT_RESERVATION, 10)
        self._make_movement(
            item, IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE, 10,
        )

        from apps.dashboard.views import _recent_ingredient_activity
        self.assertEqual(_recent_ingredient_activity(), [])

    def test_page_and_summary_expose_same_activity_dataset(self):
        item = self._make_item('Parity Activity')
        self._make_movement(item, IngredientMovementLog.MOVEMENT_PURCHASE, 25)

        page = self.client.get(DASHBOARD_URL)
        summary = self.client.get(SUMMARY_URL)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(
            page.context['recent_ingredient_activity'],
            summary.json()['recent_ingredient_activity'],
        )

    def test_activity_uses_one_query(self):
        item = self._make_item('Query Activity')
        for i in range(10):
            self._make_movement(item, IngredientMovementLog.MOVEMENT_PURCHASE, i + 1)

        from apps.dashboard.views import _recent_ingredient_activity
        with CaptureQueriesContext(connection) as ctx:
            _recent_ingredient_activity()
        self.assertEqual(len(ctx.captured_queries), 1)

    def test_page_renders_when_activity_widget_fails(self):
        with mock.patch(
            'apps.dashboard.views._recent_ingredient_activity',
            side_effect=Exception('boom'),
        ):
            page = self.client.get(DASHBOARD_URL)
            summary = self.client.get(SUMMARY_URL)

        self.assertEqual(page.status_code, 200)
        self.assertContains(
            page, 'Recent movement activity is temporarily unavailable',
        )
        self.assertIn('recent_ingredient_activity', page.context['widget_errors'])
        self.assertIsNone(summary.json()['recent_ingredient_activity'])
        self.assertIsNotNone(summary.json()['ingredient_movement_summary'])
