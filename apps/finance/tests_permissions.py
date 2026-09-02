"""
Finance security and permission tests — Kape De Manubag.

Tests every role against every Finance endpoint and HTTP method.

Roles tested:
  - Anonymous (not logged in)
  - Customer  (authenticated, role='customer')
  - Cashier   (authenticated, role='cashier')
  - Admin     (authenticated, role='admin')

Endpoints covered:
  GET  /finance/                  → finance_index
  POST /finance/                  → finance_index (save/update)
  GET  /finance/api/cash-sales/   → finance_api_cash_sales
  GET  /finance/<pk>/print/       → finance_print
  GET  /finance/history/          → finance_history

Expected behaviour:
  Anonymous  → 302 redirect to /accounts/login/?next=<url>  (all endpoints)
  Customer   → 302 redirect to menu:index                   (all endpoints)
  Cashier    → 200/302 success                              (all endpoints)
  Admin      → 200/302 success                              (all endpoints)

Key invariants:
  - No Finance data is returned to anonymous or customer users
  - Direct POST by customer must not create or modify Finance records
  - API endpoint behaves identically to HTML endpoints (same decorators)
"""

import datetime
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DailyFinance

User = get_user_model()

TODAY = datetime.date(2026, 8, 28)
API_URL      = reverse('finance:api_cash_sales')
INDEX_URL    = reverse('finance:index')
HISTORY_URL  = reverse('finance:history')
LOGIN_PREFIX = '/accounts/login/'
MENU_URL     = reverse('menu:index')


# ── User factory ──────────────────────────────────────────────────────────────

def _make_user(username, role):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


# ── Finance record fixture ────────────────────────────────────────────────────

def _finance(date=TODAY):
    return DailyFinance.objects.create(
        date=date, previous_coh=Decimal('1000.00')
    )


def _print_url(pk):
    return reverse('finance:print', kwargs={'pk': pk})


# ── Shared POST payload ───────────────────────────────────────────────────────

def _post_data(date=TODAY):
    return {
        'date':           str(date),
        'previous_coh':   '1000.00',
        'expenses':       '0.00',
        'expenses_notes': '',
        'gcash_payments': '0.00',
        'coins':          '0.00',
        'cash_advance':   '0.00',
        'floating_cash':  '0.00',
    }


# ══════════════════════════════════════════════════════════════════════════════
# ANONYMOUS — all endpoints must redirect to login
# ══════════════════════════════════════════════════════════════════════════════

class AnonymousAccessTest(TestCase):
    """Anonymous users must be redirected to the login page for every endpoint."""

    def setUp(self):
        self.client = Client()  # not logged in
        self.rec = _finance()

    def _assert_redirects_to_login(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302,
            f"Expected 302 for anonymous GET {url}, got {resp.status_code}")
        self.assertIn(LOGIN_PREFIX, resp['Location'],
            f"Expected redirect to login for anonymous GET {url}")

    def test_anon_index_get_redirects_to_login(self):
        self._assert_redirects_to_login(f'{INDEX_URL}?date={TODAY}')

    def test_anon_index_post_redirects_to_login(self):
        resp = self.client.post(f'{INDEX_URL}?date={TODAY}', _post_data())
        self.assertEqual(resp.status_code, 302)
        self.assertIn(LOGIN_PREFIX, resp['Location'])

    def test_anon_index_post_does_not_create_record(self):
        """A POST without authentication must never touch the database."""
        self.client.post(f'{INDEX_URL}?date={TODAY}', _post_data())
        # Only the fixture record exists
        self.assertEqual(
            DailyFinance.objects.exclude(pk=self.rec.pk).count(), 0
        )

    def test_anon_api_get_redirects_to_login(self):
        self._assert_redirects_to_login(f'{API_URL}?date={TODAY}')

    def test_anon_api_does_not_return_json_data(self):
        """Anonymous must not receive any financial JSON."""
        resp = self.client.get(f'{API_URL}?date={TODAY}')
        self.assertNotEqual(resp.status_code, 200,
            "Anonymous must not receive 200 from the Finance API")

    def test_anon_print_redirects_to_login(self):
        self._assert_redirects_to_login(_print_url(self.rec.pk))

    def test_anon_history_redirects_to_login(self):
        self._assert_redirects_to_login(HISTORY_URL)

    def test_anon_login_redirect_includes_next_param(self):
        """The login redirect must include ?next= so the user returns after login."""
        resp = self.client.get(HISTORY_URL)
        self.assertIn('next=', resp['Location'])

    def test_anon_print_login_redirect_includes_next(self):
        resp = self.client.get(_print_url(self.rec.pk))
        self.assertIn('next=', resp['Location'])


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER — all endpoints must redirect to menu
# ══════════════════════════════════════════════════════════════════════════════

class CustomerAccessTest(TestCase):
    """
    Authenticated customers must be denied access to all Finance endpoints.
    The system redirects them to the menu page rather than raising 403.
    """

    def setUp(self):
        self.customer = _make_user('cust', 'customer')
        self.client = Client()
        self.client.login(username='cust', password='pass123')
        self.rec = _finance()

    def _assert_denied(self, url, method='GET', data=None):
        if method == 'POST':
            resp = self.client.post(url, data or {})
        else:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302,
            f"Expected 302 for customer {method} {url}, got {resp.status_code}")
        self.assertNotIn(LOGIN_PREFIX, resp['Location'],
            "Customer should not be sent to login (they are authenticated)")
        return resp

    def test_customer_index_get_denied(self):
        resp = self._assert_denied(f'{INDEX_URL}?date={TODAY}')
        # Redirected away from Finance — not to login
        self.assertNotIn('/finance/', resp['Location'])

    def test_customer_index_post_denied(self):
        self._assert_denied(
            f'{INDEX_URL}?date={TODAY}', method='POST', data=_post_data()
        )

    def test_customer_post_does_not_create_record(self):
        """A customer POST must never reach the view body."""
        before = DailyFinance.objects.count()
        self.client.post(f'{INDEX_URL}?date={TODAY}', _post_data())
        after = DailyFinance.objects.count()
        self.assertEqual(before, after,
            "Customer POST must not create a Finance record")

    def test_customer_post_does_not_modify_existing_record(self):
        """A customer POST must not change an existing record's values."""
        original_expenses = self.rec.expenses
        self.client.post(f'{INDEX_URL}?date={TODAY}', {
            **_post_data(), 'expenses': '9999.00'
        })
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.expenses, original_expenses,
            "Customer POST must not modify existing Finance record")

    def test_customer_api_get_denied(self):
        resp = self._assert_denied(f'{API_URL}?date={TODAY}')
        self.assertNotIn('/finance/', resp['Location'])

    def test_customer_api_does_not_return_financial_json(self):
        """Customer must not receive any financial data from the API."""
        resp = self.client.get(f'{API_URL}?date={TODAY}')
        self.assertNotEqual(resp.status_code, 200,
            "Customer must not receive 200 from the Finance API")
        # If somehow 200 was returned, verify no financial keys are present
        if resp.status_code == 200:
            try:
                data = resp.json()
                self.assertNotIn('cash_sales', data)
                self.assertNotIn('gcash_sales', data)
            except Exception:
                pass

    def test_customer_print_denied(self):
        self._assert_denied(_print_url(self.rec.pk))

    def test_customer_history_denied(self):
        self._assert_denied(HISTORY_URL)

    def test_customer_redirect_destination_is_not_finance(self):
        """Every Finance endpoint redirects customer away from /finance/."""
        for url in [
            f'{INDEX_URL}?date={TODAY}',
            f'{API_URL}?date={TODAY}',
            _print_url(self.rec.pk),
            HISTORY_URL,
        ]:
            resp = self.client.get(url)
            self.assertNotIn(
                '/finance/', resp['Location'],
                f"Customer redirect from {url} must not go back to /finance/"
            )

    def test_customer_cannot_access_other_date_finance(self):
        """Customer direct-access attempt for any date is blocked."""
        other_date = TODAY - datetime.timedelta(days=1)
        resp = self.client.get(f'{INDEX_URL}?date={other_date}')
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('/finance/', resp['Location'])


# ══════════════════════════════════════════════════════════════════════════════
# CASHIER — all endpoints must be accessible
# ══════════════════════════════════════════════════════════════════════════════

class CashierAccessTest(TestCase):
    """Cashier must have full access to all Finance endpoints."""

    def setUp(self):
        self.cashier = _make_user('cashier', 'cashier')
        self.client = Client()
        self.client.login(username='cashier', password='pass123')
        self.rec = _finance()

    def test_cashier_index_get_allowed(self):
        resp = self.client.get(f'{INDEX_URL}?date={TODAY}')
        self.assertEqual(resp.status_code, 200)

    def test_cashier_index_get_contains_finance_context(self):
        resp = self.client.get(f'{INDEX_URL}?date={TODAY}')
        self.assertIn('cash_sales', resp.context)
        self.assertIn('running_total', resp.context)
        self.assertIn('form', resp.context)

    def test_cashier_index_post_allowed(self):
        """Cashier can create a Finance record for a new date."""
        new_date = TODAY - datetime.timedelta(days=3)
        resp = self.client.post(
            f'{INDEX_URL}?date={new_date}', _post_data(new_date)
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(DailyFinance.objects.filter(date=new_date).exists())

    def test_cashier_index_post_update_allowed(self):
        """Cashier can update an existing Finance record."""
        resp = self.client.post(
            f'{INDEX_URL}?date={TODAY}',
            {**_post_data(), 'expenses': '150.00'}
        )
        self.assertEqual(resp.status_code, 302)
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.expenses, Decimal('150.00'))

    def test_cashier_api_allowed(self):
        resp = self.client.get(f'{API_URL}?date={TODAY}')
        self.assertEqual(resp.status_code, 200)

    def test_cashier_api_returns_financial_data(self):
        resp = self.client.get(f'{API_URL}?date={TODAY}')
        data = resp.json()
        self.assertIn('cash_sales', data)
        self.assertIn('gcash_sales', data)
        self.assertIn('cash_order_count', data)

    def test_cashier_print_allowed(self):
        resp = self.client.get(_print_url(self.rec.pk))
        self.assertEqual(resp.status_code, 200)

    def test_cashier_print_contains_record_data(self):
        resp = self.client.get(_print_url(self.rec.pk))
        self.assertIn('record', resp.context)
        self.assertEqual(resp.context['record'].pk, self.rec.pk)

    def test_cashier_history_allowed(self):
        resp = self.client.get(HISTORY_URL)
        self.assertEqual(resp.status_code, 200)

    def test_cashier_history_contains_records(self):
        resp = self.client.get(HISTORY_URL)
        self.assertIn('records', resp.context)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — all endpoints must be accessible
# ══════════════════════════════════════════════════════════════════════════════

class AdminAccessTest(TestCase):
    """Admin must have full access to all Finance endpoints."""

    def setUp(self):
        self.admin = _make_user('admin', 'admin')
        self.client = Client()
        self.client.login(username='admin', password='pass123')
        self.rec = _finance()

    def test_admin_index_get_allowed(self):
        resp = self.client.get(f'{INDEX_URL}?date={TODAY}')
        self.assertEqual(resp.status_code, 200)

    def test_admin_index_post_allowed(self):
        new_date = TODAY - datetime.timedelta(days=5)
        resp = self.client.post(
            f'{INDEX_URL}?date={new_date}', _post_data(new_date)
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(DailyFinance.objects.filter(date=new_date).exists())

    def test_admin_api_allowed(self):
        resp = self.client.get(f'{API_URL}?date={TODAY}')
        self.assertEqual(resp.status_code, 200)

    def test_admin_api_returns_financial_data(self):
        resp = self.client.get(f'{API_URL}?date={TODAY}')
        data = resp.json()
        self.assertIn('cash_sales', data)
        self.assertIn('gcash_sales', data)

    def test_admin_print_allowed(self):
        resp = self.client.get(_print_url(self.rec.pk))
        self.assertEqual(resp.status_code, 200)

    def test_admin_history_allowed(self):
        resp = self.client.get(HISTORY_URL)
        self.assertEqual(resp.status_code, 200)

    def test_admin_can_create_and_cashier_can_view_same_record(self):
        """Records created by admin are visible to cashier — shared Finance table."""
        new_date = TODAY - datetime.timedelta(days=2)
        self.client.post(
            f'{INDEX_URL}?date={new_date}', _post_data(new_date)
        )
        self.assertTrue(DailyFinance.objects.filter(date=new_date).exists())

        # Cashier can also view it
        cashier = _make_user('cashier2', 'cashier')
        c2 = Client()
        c2.login(username='cashier2', password='pass123')
        resp = c2.get(f'{INDEX_URL}?date={new_date}')
        self.assertEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# CROSS-ROLE: finance data isolation
# ══════════════════════════════════════════════════════════════════════════════

class DataIsolationTest(TestCase):
    """
    Finance data must never be accessible to customers or anonymous users
    regardless of whether records exist.
    """

    def setUp(self):
        self.rec = _finance()

    def test_customer_cannot_read_saved_finance_record(self):
        customer = _make_user('cust2', 'customer')
        c = Client()
        c.login(username='cust2', password='pass123')
        # Try to read the record directly
        resp = c.get(f'{INDEX_URL}?date={TODAY}')
        self.assertNotEqual(resp.status_code, 200)

    def test_customer_cannot_print_existing_record(self):
        customer = _make_user('cust3', 'customer')
        c = Client()
        c.login(username='cust3', password='pass123')
        resp = c.get(_print_url(self.rec.pk))
        self.assertNotEqual(resp.status_code, 200)

    def test_anonymous_cannot_print_existing_record(self):
        c = Client()
        resp = c.get(_print_url(self.rec.pk))
        self.assertNotEqual(resp.status_code, 200)
        self.assertIn(LOGIN_PREFIX, resp['Location'])

    def test_customer_cannot_read_api_data(self):
        customer = _make_user('cust4', 'customer')
        c = Client()
        c.login(username='cust4', password='pass123')
        resp = c.get(f'{API_URL}?date={TODAY}')
        self.assertNotEqual(resp.status_code, 200)

    def test_anonymous_cannot_read_api_data(self):
        c = Client()
        resp = c.get(f'{API_URL}?date={TODAY}')
        self.assertNotEqual(resp.status_code, 200)

    def test_customer_cannot_see_history(self):
        customer = _make_user('cust5', 'customer')
        c = Client()
        c.login(username='cust5', password='pass123')
        resp = c.get(HISTORY_URL)
        self.assertNotEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# DECORATOR ORDER verification
# ══════════════════════════════════════════════════════════════════════════════

class DecoratorOrderTest(TestCase):
    """
    Verify that @login_required fires before @cashier_or_admin_required
    so anonymous users always get a login redirect, never a role-based
    redirect (which would reveal that the endpoint exists).
    """

    def test_anonymous_gets_login_redirect_not_menu_redirect(self):
        """
        Anonymous must be sent to /accounts/login/ not /menu/.
        Sending to menu would mean login_required did not fire first.
        """
        c = Client()
        for url in [
            f'{INDEX_URL}?date={TODAY}',
            f'{API_URL}?date={TODAY}',
            HISTORY_URL,
        ]:
            resp = c.get(url)
            self.assertIn(LOGIN_PREFIX, resp['Location'],
                f"Anonymous GET {url} must redirect to login, not menu")
            self.assertNotIn('/menu/', resp['Location'],
                f"Anonymous GET {url} must not redirect to menu")

    def test_customer_gets_menu_redirect_not_login_redirect(self):
        """
        Authenticated customers must be sent to menu:index, not login.
        Being sent to login would mean the decorator doesn't distinguish
        authenticated customers from anonymous.
        """
        customer = _make_user('cust_order', 'customer')
        c = Client()
        c.login(username='cust_order', password='pass123')
        for url in [
            f'{INDEX_URL}?date={TODAY}',
            f'{API_URL}?date={TODAY}',
            HISTORY_URL,
        ]:
            resp = c.get(url)
            self.assertNotIn(LOGIN_PREFIX, resp['Location'],
                f"Customer GET {url} must not go to login (they are authenticated)")

    def test_cashier_is_not_redirected_to_login(self):
        cashier = _make_user('cashier_dec', 'cashier')
        c = Client()
        c.login(username='cashier_dec', password='pass123')
        resp = c.get(f'{INDEX_URL}?date={TODAY}')
        self.assertNotIn(LOGIN_PREFIX, resp['Location']
                         if resp.status_code == 302 else '')
        self.assertEqual(resp.status_code, 200)

    def test_admin_is_not_redirected_at_all(self):
        admin = _make_user('admin_dec', 'admin')
        c = Client()
        c.login(username='admin_dec', password='pass123')
        resp = c.get(f'{INDEX_URL}?date={TODAY}')
        self.assertEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# SUPERUSER — also counts as admin
# ══════════════════════════════════════════════════════════════════════════════

class SuperuserAccessTest(TestCase):
    """
    A Django superuser (is_superuser=True) with any role must be treated
    as an admin and have full Finance access (is_admin_user property returns
    True for both role='admin' and is_superuser=True).
    """

    def test_superuser_with_customer_role_can_access_finance(self):
        """
        is_admin_user = role=='admin' OR is_superuser. A superuser flagged
        as customer role must still pass cashier_or_admin_required.
        """
        su = User.objects.create_superuser(
            username='superuser', password='pass123'
        )
        su.role = 'customer'  # unusual but superuser overrides role check
        su.save()
        c = Client()
        c.login(username='superuser', password='pass123')
        resp = c.get(f'{INDEX_URL}?date={TODAY}')
        self.assertEqual(resp.status_code, 200,
            "Superuser must have Finance access regardless of role field")


# ══════════════════════════════════════════════════════════════════════════════
# POST SECURITY — customer cannot create or modify Finance records
# ══════════════════════════════════════════════════════════════════════════════

class CustomerPostSecurityTest(TestCase):
    """
    Comprehensive POST security: customer POST to every Finance write
    endpoint must be blocked server-side before any data is touched.
    """

    def setUp(self):
        self.customer = _make_user('cust_post', 'customer')
        self.client = Client()
        self.client.login(username='cust_post', password='pass123')

    def test_customer_post_to_index_blocked(self):
        resp = self.client.post(
            f'{INDEX_URL}?date={TODAY}', _post_data()
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DailyFinance.objects.filter(date=TODAY).exists())

    def test_customer_post_with_zero_expenses_blocked(self):
        resp = self.client.post(
            f'{INDEX_URL}?date={TODAY}', {**_post_data(), 'expenses': '0.00'}
        )
        self.assertFalse(DailyFinance.objects.filter(date=TODAY).exists())

    def test_customer_post_with_large_amount_blocked(self):
        resp = self.client.post(
            f'{INDEX_URL}?date={TODAY}',
            {**_post_data(), 'previous_coh': '9999999.00', 'expenses': '999999.00'}
        )
        self.assertFalse(DailyFinance.objects.filter(date=TODAY).exists())

    def test_customer_post_cannot_modify_existing_record(self):
        rec = _finance(TODAY)
        original_previous_coh = rec.previous_coh
        self.client.post(
            f'{INDEX_URL}?date={TODAY}',
            {**_post_data(), 'previous_coh': '99999.00'}
        )
        rec.refresh_from_db()
        self.assertEqual(rec.previous_coh, original_previous_coh)

    def test_customer_cannot_trigger_finance_calculation(self):
        """
        Even if the customer bypasses the form, the decorator intercepts
        before the view body — no calculation runs, no data changes.
        """
        initial_count = DailyFinance.objects.count()
        for _ in range(5):
            self.client.post(f'{INDEX_URL}?date={TODAY}', _post_data())
        self.assertEqual(DailyFinance.objects.count(), initial_count)
