"""
Sales Reports security and permission tests — Kape De Manubag.

Tests every role against every Reports endpoint.

Roles:
  Anonymous  — not logged in
  Customer   — authenticated, role='customer'
  Cashier    — authenticated, role='cashier'
  Admin      — authenticated, role='admin'
  Superuser  — is_superuser=True (any role field)

Endpoints:
  GET  /reports/              → reports_index
  GET  /reports/export/excel/ → export_excel

Expected behaviour:
  Anonymous  → 302 to /accounts/login/?next=<url>  (both endpoints)
  Customer   → 302 to dashboard:index              (both endpoints)
  Cashier    → 302 to dashboard:index              (both endpoints)
  Admin      → 200                                 (both endpoints)
  Superuser  → 200                                 (both endpoints)

Key invariants:
  - No report data is returned to anonymous or non-admin users
  - Server-side enforcement (not hidden-link only)
  - Decorator order: @login_required outer, @admin_required inner
    → anonymous goes to login, not dashboard
    → authenticated non-admin goes to dashboard, not login
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.orders.models import Order

User = get_user_model()

LOGIN_PREFIX  = '/accounts/login/'
INDEX_URL     = reverse('reports:index')
EXPORT_URL    = reverse('reports:export_excel')
TODAY         = timezone.localdate()


# ── User factories ────────────────────────────────────────────────────────────

def _make_user(username, role):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _make_superuser(username='superuser'):
    u = User.objects.create_superuser(username=username, password='pass123')
    # Deliberately leave role as default to prove is_superuser overrides role
    u.role = 'customer'
    u.save()
    return u


# ── Helper ────────────────────────────────────────────────────────────────────

def _completed_order():
    """Create one completed order so the report has data to return."""
    dt = timezone.make_aware(
        datetime.datetime.combine(TODAY, datetime.time(10, 0))
    )
    o = Order.objects.create(
        customer_name='Test',
        status='completed', is_paid=True,
        payment_method='cash',
        total=Decimal('100.00'), subtotal=Decimal('100.00'),
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    return o


# ══════════════════════════════════════════════════════════════════════════════
# ANONYMOUS — both endpoints must redirect to login
# ══════════════════════════════════════════════════════════════════════════════

class AnonymousAccessTest(TestCase):
    """Unauthenticated requests must be redirected to the login page."""

    def setUp(self):
        self.client = Client()   # not logged in
        _completed_order()

    def _assert_redirects_to_login(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302,
            f"Expected 302 for anonymous GET {url}, got {resp.status_code}")
        self.assertIn(LOGIN_PREFIX, resp['Location'],
            f"Expected redirect to login for anonymous GET {url}")

    def test_anon_index_redirects_to_login(self):
        self._assert_redirects_to_login(INDEX_URL)

    def test_anon_export_redirects_to_login(self):
        self._assert_redirects_to_login(EXPORT_URL)

    def test_anon_index_login_redirect_has_next(self):
        resp = self.client.get(INDEX_URL)
        self.assertIn('next=', resp['Location'])

    def test_anon_export_login_redirect_has_next(self):
        resp = self.client.get(EXPORT_URL)
        self.assertIn('next=', resp['Location'])

    def test_anon_does_not_receive_report_data(self):
        """Anonymous must never receive a 200 with report content."""
        resp = self.client.get(INDEX_URL)
        self.assertNotEqual(resp.status_code, 200)

    def test_anon_does_not_receive_excel_file(self):
        resp = self.client.get(EXPORT_URL)
        self.assertNotEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER — both endpoints must redirect to dashboard (not login)
# ══════════════════════════════════════════════════════════════════════════════

class CustomerAccessTest(TestCase):
    """Authenticated customers must be denied access to all Reports endpoints."""

    def setUp(self):
        self.customer = _make_user('cust', 'customer')
        self.client   = Client()
        self.client.login(username='cust', password='pass123')
        _completed_order()

    def _assert_denied(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302,
            f"Expected 302 for customer GET {url}, got {resp.status_code}")
        # Must NOT be redirected to login (they are authenticated)
        self.assertNotIn(LOGIN_PREFIX, resp['Location'],
            f"Customer should not be sent to login for {url}")
        return resp

    def test_customer_index_denied(self):
        self._assert_denied(INDEX_URL)

    def test_customer_export_denied(self):
        self._assert_denied(EXPORT_URL)

    def test_customer_does_not_receive_200(self):
        resp = self.client.get(INDEX_URL)
        self.assertNotEqual(resp.status_code, 200)

    def test_customer_does_not_receive_excel(self):
        resp = self.client.get(EXPORT_URL)
        self.assertNotEqual(resp.status_code, 200)

    def test_customer_index_redirect_not_reports(self):
        resp = self.client.get(INDEX_URL)
        self.assertNotIn('/reports/', resp['Location'],
            "Customer must not be redirected back into /reports/")

    def test_customer_export_redirect_not_reports(self):
        resp = self.client.get(EXPORT_URL)
        self.assertNotIn('/reports/', resp['Location'])

    def test_customer_with_date_params_still_denied(self):
        """Adding date params must not bypass the access check."""
        url = INDEX_URL + f'?start={TODAY}&end={TODAY}'
        resp = self.client.get(url)
        self.assertNotEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# CASHIER — both endpoints must redirect to dashboard
# ══════════════════════════════════════════════════════════════════════════════

class CashierAccessTest(TestCase):
    """Cashiers must be denied access to Sales Reports."""

    def setUp(self):
        self.cashier = _make_user('cashier', 'cashier')
        self.client  = Client()
        self.client.login(username='cashier', password='pass123')
        _completed_order()

    def test_cashier_index_denied(self):
        resp = self.client.get(INDEX_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(LOGIN_PREFIX, resp['Location'])

    def test_cashier_export_denied(self):
        resp = self.client.get(EXPORT_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(LOGIN_PREFIX, resp['Location'])

    def test_cashier_does_not_receive_200(self):
        resp = self.client.get(INDEX_URL)
        self.assertNotEqual(resp.status_code, 200)

    def test_cashier_does_not_receive_excel(self):
        resp = self.client.get(EXPORT_URL)
        self.assertNotEqual(resp.status_code, 200)

    def test_cashier_with_date_params_still_denied(self):
        url = INDEX_URL + f'?start={TODAY}&end={TODAY}'
        resp = self.client.get(url)
        self.assertNotEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — both endpoints must return 200
# ══════════════════════════════════════════════════════════════════════════════

class AdminAccessTest(TestCase):
    """Admins must have full access to all Reports endpoints."""

    def setUp(self):
        self.admin  = _make_user('admin', 'admin')
        self.client = Client()
        self.client.login(username='admin', password='pass123')
        _completed_order()

    def test_admin_index_allowed(self):
        resp = self.client.get(INDEX_URL)
        self.assertEqual(resp.status_code, 200)

    def test_admin_index_has_report_context(self):
        resp = self.client.get(INDEX_URL)
        self.assertIn('total_revenue', resp.context)
        self.assertIn('total_orders',  resp.context)
        self.assertIn('daily_sales',   resp.context)

    def test_admin_export_allowed(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest('openpyxl not installed — export returns 500')
        resp = self.client.get(EXPORT_URL)
        self.assertEqual(resp.status_code, 200)

    def test_admin_export_returns_xlsx_content_type(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest('openpyxl not installed')
        resp = self.client.get(EXPORT_URL)
        self.assertIn('spreadsheetml', resp.get('Content-Type', ''))

    def test_admin_with_date_params_allowed(self):
        url = INDEX_URL + f'?start={TODAY}&end={TODAY}'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_admin_index_sees_correct_data(self):
        resp = self.client.get(INDEX_URL + f'?start={TODAY}&end={TODAY}')
        self.assertEqual(resp.context['total_revenue'], Decimal('100.00'))
        self.assertEqual(resp.context['total_orders'],  1)


# ══════════════════════════════════════════════════════════════════════════════
# SUPERUSER — must have full access regardless of role field
# ══════════════════════════════════════════════════════════════════════════════

class SuperuserAccessTest(TestCase):
    """
    is_superuser=True grants admin-level access even if role='customer'.
    is_admin_user property checks: role=='admin' OR is_superuser.
    """

    def setUp(self):
        self.su     = _make_superuser()
        self.client = Client()
        self.client.login(username='superuser', password='pass123')
        _completed_order()

    def test_superuser_index_allowed(self):
        resp = self.client.get(INDEX_URL)
        self.assertEqual(resp.status_code, 200,
            "Superuser must access Reports even with role='customer'")

    def test_superuser_export_allowed(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest('openpyxl not installed')
        resp = self.client.get(EXPORT_URL)
        self.assertEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# DECORATOR ORDER — anonymous vs authenticated non-admin
# ══════════════════════════════════════════════════════════════════════════════

class DecoratorOrderTest(TestCase):
    """
    Verify the decorator stack fires in the correct order:
      @login_required → anonymous goes to login page
      @admin_required → authenticated non-admin goes to dashboard (not login)
    """

    def test_anonymous_goes_to_login_not_dashboard(self):
        c = Client()
        for url in [INDEX_URL, EXPORT_URL]:
            resp = c.get(url)
            self.assertIn(LOGIN_PREFIX, resp['Location'],
                f"Anonymous GET {url} must go to login, not dashboard")
            # Must NOT go to dashboard
            self.assertNotIn('/dashboard/', resp['Location'],
                f"Anonymous GET {url} must not go to dashboard")

    def test_customer_goes_to_dashboard_not_login(self):
        """Authenticated customers are redirected to dashboard, not login."""
        _make_user('cust_dec', 'customer')
        c = Client()
        c.login(username='cust_dec', password='pass123')
        for url in [INDEX_URL, EXPORT_URL]:
            resp = c.get(url)
            self.assertNotIn(LOGIN_PREFIX, resp['Location'],
                f"Authenticated customer GET {url} must not go to login")

    def test_cashier_goes_to_dashboard_not_login(self):
        _make_user('cash_dec', 'cashier')
        c = Client()
        c.login(username='cash_dec', password='pass123')
        for url in [INDEX_URL, EXPORT_URL]:
            resp = c.get(url)
            self.assertNotIn(LOGIN_PREFIX, resp['Location'],
                f"Authenticated cashier GET {url} must not go to login")

    def test_admin_is_not_redirected(self):
        _make_user('admin_dec', 'admin')
        c = Client()
        c.login(username='admin_dec', password='pass123')
        resp = c.get(INDEX_URL)
        self.assertEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# DATA ISOLATION — non-admin must never receive report data
# ══════════════════════════════════════════════════════════════════════════════

class DataIsolationTest(TestCase):
    """
    Verifies no report data leaks to unauthorised roles.
    Creates real orders so a permissive bug would return 200 with data.
    """

    def setUp(self):
        _completed_order()
        _completed_order()

    def test_customer_receives_no_report_context(self):
        _make_user('cust_iso', 'customer')
        c = Client()
        c.login(username='cust_iso', password='pass123')
        resp = c.get(INDEX_URL)
        # Must not be 200 with context
        self.assertNotEqual(resp.status_code, 200,
            "Customer must not receive a 200 response with report context")

    def test_cashier_receives_no_report_context(self):
        _make_user('cash_iso', 'cashier')
        c = Client()
        c.login(username='cash_iso', password='pass123')
        resp = c.get(INDEX_URL)
        self.assertNotEqual(resp.status_code, 200)

    def test_anonymous_receives_no_report_context(self):
        c = Client()
        resp = c.get(INDEX_URL)
        self.assertNotEqual(resp.status_code, 200)

    def test_customer_cannot_download_excel(self):
        """Customer must not receive the Excel file."""
        _make_user('cust_xl', 'customer')
        c = Client()
        c.login(username='cust_xl', password='pass123')
        resp = c.get(EXPORT_URL)
        self.assertNotEqual(resp.status_code, 200)
        if resp.get('Content-Type'):
            self.assertNotIn('spreadsheetml', resp['Content-Type'],
                "Customer must not receive Excel spreadsheet")

    def test_cashier_cannot_download_excel(self):
        _make_user('cash_xl', 'cashier')
        c = Client()
        c.login(username='cash_xl', password='pass123')
        resp = c.get(EXPORT_URL)
        self.assertNotEqual(resp.status_code, 200)
        if resp.get('Content-Type'):
            self.assertNotIn('spreadsheetml', resp['Content-Type'])
