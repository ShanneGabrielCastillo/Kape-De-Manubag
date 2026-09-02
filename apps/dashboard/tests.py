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

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.db.models import Count, Sum
from django.template.context import Context
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import CustomUser
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
#   6  view queries (sales stats, status counts, chart data, top products,
#      recent orders, low stock -- all single consolidated queries)
#   2  session SELECT + user SELECT (auth middleware)
#   3  session UPDATE for the idle-timeout middleware's last-activity stamp
#      (savepoint + UPDATE + release; only on the first request in a window)
# Total: 11. The budget is exact (not a bound), so re-adding per-request
# queries (e.g. unused context-stat COUNTs or a cart lookup for staff) fails
# the suite. Because it is exact, re-verify the count after upgrading Django
# or changing middleware (session-write behaviour can shift it by +/- 1).
DASHBOARD_QUERY_BUDGET = 11
# The chart endpoint needs only the session/user lookups plus ONE grouped
# query per request. Measured at 3; the first authenticated request in an
# idle-timeout window adds the session UPDATE (savepoint/update/release), so
# the budget allows 6 for that worst case.
CHART_QUERY_BUDGET = 6
# The realtime summary endpoint runs the five consolidated view queries
# (sales stats, status counts, chart data, top products, low stock) plus the
# same auth/session lookups as the page. Budget: 5 view + 2 auth + 3 session
# UPDATE worst case = 10.
SUMMARY_QUERY_BUDGET = 10
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
    make_order(50, now, paid=False, status='pending')
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
        make_order(50, 0, paid=False, status='pending')
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
        self.today = now.date()
        self.week_start = self.today - timedelta(days=self.today.weekday())
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
        expected = []
        for i in range(6, -1, -1):
            day = self.today - timedelta(days=i)
            total = Order.objects.filter(
                is_paid=True, created_at__date=day,
            ).aggregate(t=Sum('total'))['t'] or 0
            expected.append(float(total))
        self.assertEqual(data, expected)


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
        'No orders recorded yet',     # Total Revenue stat hint
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
        self.assertContains(response, '1 total order')


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
        self.assertEqual(data['chart_data'][-1], 100.0)  # today's paid sales

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

    def test_summary_month_period_returns_thirty_days(self):
        self._seed()
        response, _ = self._get_summary('month')
        data = response.json()
        self.assertEqual(len(data['chart_labels']), 30)
        self.assertEqual(len(data['chart_data']), 30)
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
            status='pending', is_paid=False,
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
        order = Order.objects.get(status='pending')
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
        order = Order.objects.get(status='pending')
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
        }
        with mock.patch.multiple('apps.dashboard.views', **failures):
            response = self.client.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key in ('daily_sales', 'daily_orders', 'pending_count',
                    'chart_data', 'top_products', 'low_stock'):
            self.assertIsNone(data[key])

    # ── Chart endpoint ─────────────────────────────────────────────────────

    def test_chart_endpoint_returns_503_on_failure(self):
        with mock.patch('apps.dashboard.views._chart_series', side_effect=Exception('boom')):
            response = self.client.get(CHART_URL)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'error': 'chart unavailable'})
