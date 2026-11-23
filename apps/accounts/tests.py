"""
Tests for the account login flow, focused on the post-login redirect.

Security focus: the ``next`` parameter must only ever redirect to safe,
internal URLs. Open redirects (external hosts, protocol-relative URLs,
non-HTTP schemes, backslash host tricks, URL fragments) must fall back to
the role-appropriate landing page instead of redirecting off-site.

Environment note: this project runs Python 3.14, where Django's test client's
template-context capture (``store_rendered_templates`` → ``copy(context)``)
crashes in ``BaseContext.__copy__`` because it does ``copy(super())`` and
``super`` instances no longer expose a writable ``__dict__`` (PEP 667). The
live server never copies contexts, so it is unaffected.

Only ``Context.__copy__`` is patched (below) with an equivalent
implementation -- that is enough to fix the crash, and it deliberately keeps
the client's context capture working so ``response.context`` stays available
for other test modules (e.g. the dashboard statistics tests). The admin site
additionally clones contexts in its inclusion tags, which the same patch
covers. ``assertTemplateUsed`` is not used in these tests.

Once Django is upgraded past this incompatibility, the ``Context.__copy__``
patch below should be removed.
"""
import copy as _copy
import io
import os
import shutil
import tempfile
import time
import warnings
from datetime import timedelta
from urllib.parse import urlencode

from PIL import Image

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.template.context import Context
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import CustomUser, FailedLoginAttempt


def _plain_context_copy(self):
    """Working Context.__copy__ for Python 3.14.

    Django's own implementation does ``copy(super())`` which crashes under
    Python 3.14 (PEP 667 made ``super`` objects immutable). It is triggered
    whenever a template tag clones the context -- e.g. the Django admin's
    inclusion tags -- even though the live server renders these fine.
    """
    duplicate = self.__class__.__new__(self.__class__)
    duplicate.__dict__.update(self.__dict__)
    duplicate.dicts = self.dicts.copy()
    return duplicate


Context.__copy__ = _plain_context_copy


PASSWORD = 'kdm-test-pass-123'


def _create_user(username, role):
    return CustomUser.objects.create_user(
        username=username, password=PASSWORD, role=role,
    )


class LoginRedirectTests(TestCase):
    """The security-critical behavior: 'next' is validated before use."""

    def setUp(self):
        self.admin = _create_user('admin_test', 'admin')
        self.cashier = _create_user('cashier_test', 'cashier')
        self.customer = _create_user('customer_test', 'customer')

    def _login(self, username='admin_test', next_url=None):
        url = '/accounts/login/'
        if next_url is not None:
            url += '?' + urlencode({'next': next_url})
        return self.client.post(url, {
            'username': username,
            'password': PASSWORD,
        })

    # --- Login without 'next' ---

    def test_login_without_next_redirects_staff_to_dashboard(self):
        for username in ('admin_test', 'cashier_test'):
            with self.subTest(username=username):
                response = self._login(username)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, '/dashboard/')

    def test_login_without_next_redirects_customer_to_menu(self):
        response = self._login('customer_test')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

    def test_customer_with_external_next_falls_back_to_menu(self):
        response = self._login('customer_test', 'https://evil.example.com/phish')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

    # --- Valid 'next' targets ---

    def test_login_with_valid_internal_next_is_respected(self):
        response = self._login('admin_test', '/orders/pos/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/orders/pos/')

    def test_login_with_internal_next_and_query_is_respected(self):
        response = self._login('admin_test', '/menu/?q=coffee')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/menu/?q=coffee')

    def test_login_with_same_host_absolute_next_is_respected(self):
        response = self._login('admin_test', 'http://testserver/orders/pos/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'http://testserver/orders/pos/')

    # --- Invalid 'next' targets must fall back ---

    def test_login_with_external_next_falls_back_to_dashboard(self):
        evil_nexts = [
            'https://evil.example.com/phish',
            'http://evil.example.com',
            '//evil.example.com/path',                       # protocol-relative
            'https://evil.example.com@testserver/dashboard/',  # userinfo trick
        ]
        for next_url in evil_nexts:
            with self.subTest(next_url=next_url):
                response = self._login('admin_test', next_url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, '/dashboard/')

    def test_login_with_non_http_scheme_falls_back(self):
        for next_url in ('javascript:alert(1)', 'data:text/html,evil', 'ftp://evil.example.com'):
            with self.subTest(next_url=next_url):
                response = self._login('admin_test', next_url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, '/dashboard/')

    def test_login_with_backslash_host_trick_falls_back(self):
        response = self._login('admin_test', r'\\evil.example.com\path')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/dashboard/')

    def test_login_with_fragment_next_stays_internal(self):
        # A URL fragment is resolved by the browser against the same URL, so
        # it can never send the user off-site; Django treats it as safe (the
        # request itself would hit /dashboard/ with no fragment).
        response = self._login('admin_test', '/dashboard/#fragment')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/dashboard/#fragment')


class LoginWorkflowTests(TestCase):
    """The existing login UX must keep working for every role."""

    def setUp(self):
        self.admin = _create_user('admin_wf', 'admin')
        self.cashier = _create_user('cashier_wf', 'cashier')
        self.customer = _create_user('customer_wf', 'customer')

    def test_login_page_renders(self):
        response = self.client.get('/accounts/login/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sign In')

    def test_invalid_credentials_show_error_and_stay_on_login(self):
        response = self.client.post('/accounts/login/', {
            'username': 'admin_wf', 'password': 'wrong-password',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Invalid username or password.')

    def test_admin_login_reaches_dashboard(self):
        self.client.post('/accounts/login/', {'username': 'admin_wf', 'password': PASSWORD})
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)

    def test_cashier_login_reaches_pos(self):
        self.client.post('/accounts/login/', {'username': 'cashier_wf', 'password': PASSWORD})
        response = self.client.get('/orders/pos/')
        self.assertEqual(response.status_code, 200)

    def test_customer_login_is_denied_staff_pages(self):
        self.client.post('/accounts/login/', {'username': 'customer_wf', 'password': PASSWORD})
        response = self.client.get('/dashboard/')
        # cashier_or_admin_required bounces customers back to the menu.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

    def test_authenticated_user_visiting_login_is_redirected(self):
        self.client.post('/accounts/login/', {'username': 'customer_wf', 'password': PASSWORD})
        response = self.client.get('/accounts/login/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

    def test_logout_still_works(self):
        self.client.post('/accounts/login/', {'username': 'admin_wf', 'password': PASSWORD})
        response = self.client.get('/accounts/logout/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/accounts/login/')


class BruteForceTests(TestCase):
    """Brute-force protection: counters, lockout, and cooldown recovery.

    Lockout semantics under test:
    * Failed logins are counted per username AND per client IP.
    * Reaching a threshold locks that entity for the cooldown window, and a
      lockout blocks *all* further attempts -- even with the right password.
    * The lockout message and behaviour are identical for known and unknown
      usernames (no account enumeration).
    * A lockout expires on its own; after the cooldown the legitimate user
      can log in again.
    """

    LOCKED_MSG = 'Too many failed login attempts.'

    def setUp(self):
        self.user = _create_user('brute_user', 'customer')
        self.url = '/accounts/login/'

    def _post(self, username, password, ip='127.0.0.1'):
        return self.client.post(
            self.url, {'username': username, 'password': password}, REMOTE_ADDR=ip,
        )

    def _fail(self, times=1, username='brute_user', ip='127.0.0.1'):
        for _ in range(times):
            self._post(username, 'wrong-password', ip=ip)

    def _row(self, scope, value):
        return FailedLoginAttempt.objects.get(scope=scope, value=value)

    # --- Counting ---

    def test_successful_login_counts_nothing(self):
        response = self._post('brute_user', PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FailedLoginAttempt.objects.count(), 0)

    def test_failed_attempts_recorded_for_username_and_ip(self):
        self._fail(3)
        self.assertEqual(self._row('username', 'brute_user').attempts, 3)
        self.assertEqual(self._row('ip', '127.0.0.1').attempts, 3)

    def test_empty_username_submission_counts_only_the_ip(self):
        # No username to count, so no username row is created, but the IP is
        # still recorded (every failed POST is a failed attempt from that IP).
        self.client.post(self.url, {'username': '', 'password': 'x'})
        self.assertEqual(FailedLoginAttempt.objects.filter(scope='username').count(), 0)
        self.assertEqual(self._row('ip', '127.0.0.1').attempts, 1)

    def test_get_login_page_does_not_count(self):
        self.client.get(self.url)
        self.assertEqual(FailedLoginAttempt.objects.count(), 0)

    def test_stale_failures_never_accumulate_into_lockout(self):
        # A few mistakes spread over days (older than the cooldown window)
        # must not add up to a lockout -- the counter window slides.
        for _ in range(10):
            self._post('brute_user', 'wrong', ip='10.0.0.1')
            FailedLoginAttempt.objects.update(
                last_attempt_at=timezone.now() - timedelta(minutes=16),
            )
        response = self._post('brute_user', PASSWORD, ip='10.0.0.1')
        self.assertEqual(response.status_code, 302)

    # --- Lockout behaviour ---

    def test_lockout_blocks_even_the_correct_password(self):
        self._fail(5)
        response = self._post('brute_user', PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.LOCKED_MSG)
        self.assertNotIn('_auth_user_id', self.client.session)

    @override_settings(LOGIN_LOCKOUT_MINUTES=2)
    def test_lockout_message_shows_remaining_minutes(self):
        self._fail(5)
        response = self._post('brute_user', 'wrong')
        self.assertContains(
            response, 'Too many failed login attempts. Please try again in 2 minutes.',
        )

    def test_attempts_during_lockout_do_not_extend_it(self):
        self._fail(5)
        before = self._row('username', 'brute_user').attempts
        response = self._post('brute_user', 'wrong')   # blocked, never counted
        self._post('brute_user', 'wrong')
        self.assertEqual(self._row('username', 'brute_user').attempts, before)
        # Regression: during a lockout the friendly notice is the ONLY message
        # shown -- the generic 'invalid credentials' form error (which would
        # imply a credential check actually ran) must not appear.
        self.assertContains(response, 'Too many failed login attempts')
        self.assertNotContains(response, 'Please enter a correct username')

    def test_username_lockout_works_across_ips(self):
        # Distributed attack: one account hammered from many machines. The
        # per-username counter (5) trips even though each IP stays below 10.
        for i in range(5):
            self._post('brute_user', 'wrong', ip=f'10.0.0.{i + 1}')
        response = self._post('brute_user', PASSWORD, ip='10.0.0.9')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.LOCKED_MSG)

    @override_settings(LOGIN_MAX_ATTEMPTS_PER_USERNAME=100, LOGIN_MAX_ATTEMPTS_PER_IP=3)
    def test_ip_lockout_blocks_every_username_from_that_ip(self):
        # One machine trying many accounts: the per-IP counter (3) trips and
        # blocks every username from that IP, while other IPs stay free.
        for username in ('one', 'two', 'three'):
            self._post(username, 'wrong', ip='10.0.0.7')
        response = self._post('brute_user', PASSWORD, ip='10.0.0.7')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.LOCKED_MSG)
        ok = self._post('brute_user', PASSWORD, ip='10.0.0.8')
        self.assertEqual(ok.status_code, 302)

    # --- No account enumeration ---

    def test_known_and_unknown_usernames_lock_out_identically(self):
        unknown = self._lockout_response(username='ghost_user_xyz', ip='10.0.0.1')
        known = self._lockout_response(username='brute_user', ip='10.0.0.2')
        self.assertContains(unknown, self.LOCKED_MSG)
        self.assertContains(known, self.LOCKED_MSG)
        # Both produce the same friendly message -- nothing distinguishes an
        # existing account from a made-up one.
        self.assertContains(unknown, 'Please try again in')
        self.assertContains(known, 'Please try again in')

    def _lockout_response(self, username, ip):
        for _ in range(5):
            self._post(username, 'wrong-password', ip=ip)
        return self._post(username, 'wrong-password', ip=ip)

    # --- Cooldown recovery ---

    def test_legitimate_login_succeeds_after_lockout_expires(self):
        self._fail(5)
        blocked = self._post('brute_user', PASSWORD)
        self.assertEqual(blocked.status_code, 200)   # still locked out
        # Simulate the cooldown elapsing (rows age past the 15-minute window).
        FailedLoginAttempt.objects.update(
            last_attempt_at=timezone.now() - timedelta(minutes=16),
        )
        response = self._post('brute_user', PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)
        # Counters are cleared on success.
        self.assertEqual(FailedLoginAttempt.objects.count(), 0)

    def test_successful_login_clears_counters(self):
        self._fail(3)
        self.assertEqual(FailedLoginAttempt.objects.count(), 2)  # user + IP rows
        response = self._post('brute_user', PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FailedLoginAttempt.objects.count(), 0)


class AdminLoginBruteForceTests(TestCase):
    """The standard Django Admin login uses the shared brute-force guard."""

    LOCKED_MSG = 'Too many failed login attempts.'
    URL = '/admin/login/'

    def setUp(self):
        self.admin = CustomUser.objects.create_superuser(
            username='admin_login_user', password=PASSWORD, role='admin',
        )
        self.non_staff = _create_user('admin_login_cashier', 'cashier')

    def _post(self, username, password, ip='127.0.0.1', next_url=None):
        data = {'username': username, 'password': password}
        if next_url:
            data['next'] = next_url
        return self.client.post(self.URL, data, REMOTE_ADDR=ip)

    def _row(self, scope, value):
        return FailedLoginAttempt.objects.get(scope=scope, value=value)

    def test_repeated_failures_trigger_admin_lockout(self):
        for _ in range(5):
            response = self._post('admin_login_user', 'wrong-password')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._row('username', 'admin_login_user').attempts, 5,
        )
        self.assertEqual(self._row('ip', '127.0.0.1').attempts, 5)

    def test_correct_credentials_rejected_while_locked(self):
        for _ in range(5):
            self._post('admin_login_user', 'wrong-password')

        response = self._post('admin_login_user', PASSWORD)

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, self.LOCKED_MSG, status_code=429)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_admin_login_clears_counters(self):
        self._post('admin_login_user', 'wrong-password')
        self._post('admin_login_user', 'wrong-password')
        self.assertEqual(FailedLoginAttempt.objects.count(), 2)  # user + IP rows

        response = self._post(
            'admin_login_user', PASSWORD, next_url=reverse('admin:index'),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)
        self.assertEqual(FailedLoginAttempt.objects.count(), 0)

    def test_unknown_username_locks_out_like_known_username(self):
        for _ in range(5):
            self._post('ghost_admin_user', 'wrong-password', ip='10.20.30.40')
        unknown = self._post(
            'ghost_admin_user', PASSWORD, ip='10.20.30.40',
        )

        for _ in range(5):
            self._post('admin_login_user', 'wrong-password', ip='10.20.30.41')
        known = self._post(
            'admin_login_user', PASSWORD, ip='10.20.30.41',
        )

        self.assertEqual(unknown.status_code, 429)
        self.assertEqual(known.status_code, 429)
        self.assertContains(unknown, self.LOCKED_MSG, status_code=429)
        self.assertContains(known, self.LOCKED_MSG, status_code=429)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_admin_login_preserves_next_redirect(self):
        target = reverse('admin:accounts_customuser_changelist')

        response = self._post('admin_login_user', PASSWORD, next_url=target)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, target)

    def test_non_staff_user_cannot_enter_admin(self):
        self.assertFalse(self.non_staff.is_staff)

        response = self._post('admin_login_cashier', PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)


class RoleManagementTests(TestCase):
    """Role-change validation: last-administrator protection and authorization.

    The last-admin rule lives on the model (clean/save/delete + pre_delete
    signal) so every path -- forms, views, the Django admin site and direct
    ORM calls -- validates role changes identically. An "administrator" is an
    active user with role='admin' or is_superuser (matching is_admin_user).
    """

    def setUp(self):
        self.admin = _create_user('role_admin', 'admin')
        self.cashier = _create_user('role_cashier', 'cashier')
        self.customer = _create_user('role_customer', 'customer')

    # --- Authorization: only administrators may manage roles/staff ---

    def test_only_admins_can_open_staff_create(self):
        self.client.force_login(self.cashier)
        response = self.client.get('/accounts/staff/create/')
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.customer)
        response = self.client.get('/accounts/staff/create/')
        self.assertEqual(response.status_code, 302)

    def test_admin_can_create_staff(self):
        self.client.force_login(self.admin)
        response = self.client.post('/accounts/staff/create/', {
            'username': 'new_staff', 'first_name': 'New', 'last_name': 'Staff',
            'email': 'new@example.com', 'role': 'cashier',
            'password1': 'staff-pass-123', 'password2': 'staff-pass-123',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            CustomUser.objects.filter(username='new_staff', role='cashier').exists()
        )

    def test_customer_cannot_toggle_staff(self):
        self.client.force_login(self.customer)
        response = self.client.post(f'/accounts/staff/{self.admin.pk}/toggle/')
        self.assertEqual(response.status_code, 302)  # admin_required bounces

    def test_cannot_toggle_self(self):
        self.client.force_login(self.admin)
        response = self.client.post(f'/accounts/staff/{self.admin.pk}/toggle/')
        self.assertEqual(response.json()['success'], False)

    def test_get_request_cannot_toggle_staff(self):
        """GET must be rejected: staff activation/deactivation requires POST."""
        self.client.force_login(self.admin)
        response = self.client.get(f'/accounts/staff/{self.cashier.pk}/toggle/')

        self.assertEqual(response.status_code, 405)
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_active)
        self.assertIsNone(self.cashier.deactivated_at)

    def test_admin_can_deactivate_cashier(self):
        self.client.force_login(self.admin)
        response = self.client.post(f'/accounts/staff/{self.cashier.pk}/toggle/')
        self.assertEqual(response.json(), {'success': True, 'status': 'deactivated'})
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.is_active)

    def test_profile_update_cannot_escalate_role(self):
        self.client.force_login(self.customer)
        response = self.client.post('/accounts/profile/', {
            'first_name': 'Hacker', 'last_name': 'McHack', 'email': 'h@example.com',
        })
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.role, 'customer')

    # --- Last-administrator: demotion / deactivation / deletion ---

    def test_demote_last_admin_via_save_is_blocked(self):
        # self.admin is the only admin-capable user (cashier/customer are not).
        self.admin.role = 'customer'
        with self.assertRaises(ValidationError):
            self.admin.save()
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'admin')

    def test_demote_last_admin_via_clean_is_blocked(self):
        self.admin.role = 'cashier'
        with self.assertRaises(ValidationError):
            self.admin.full_clean()

    def test_demote_admin_is_allowed_when_another_admin_exists(self):
        _create_user('role_admin2', 'admin')
        self.admin.role = 'cashier'
        self.admin.save()  # must not raise
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'cashier')
        self.assertTrue(CustomUser.objects.filter(role='admin').exists())

    def test_deactivate_last_admin_via_save_is_blocked(self):
        self.admin.is_active = False
        with self.assertRaises(ValidationError):
            self.admin.save()
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_deactivate_admin_is_allowed_when_another_admin_exists(self):
        _create_user('role_admin2', 'admin')
        self.admin.is_active = False
        self.admin.save()  # must not raise
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_active)

    def test_delete_last_admin_is_blocked(self):
        # Django runs deletes inside an internal atomic block with no
        # savepoint, so the guard's ValidationError marks the connection as
        # needing rollback. Letting a nested atomic() exit with the exception
        # rolls back to its savepoint and leaves the connection usable.
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                self.admin.delete()
        self.assertTrue(CustomUser.objects.filter(pk=self.admin.pk).exists())

    def test_bulk_delete_last_admin_is_blocked(self):
        # Any bulk delete of a user is blocked (soft-deactivation only) -- the
        # pre_delete signal raises before anything is removed.
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                CustomUser.objects.filter(pk=self.admin.pk).delete()
        self.assertTrue(CustomUser.objects.filter(pk=self.admin.pk).exists())

    def test_delete_admin_soft_deactivates_when_another_admin_exists(self):
        # delete() never removes the row -- it soft-deactivates instead, so
        # the account (and its historical records) is preserved.
        _create_user('role_admin2', 'admin')
        self.admin.delete()  # must not raise
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_active)
        self.assertIsNotNone(self.admin.deactivated_at)
        self.assertTrue(CustomUser.objects.filter(pk=self.admin.pk).exists())

    def test_non_role_updates_on_last_admin_are_allowed(self):
        self.admin.email = 'new@example.com'
        self.admin.save()  # must not raise
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, 'new@example.com')

    def test_creating_an_admin_is_always_allowed(self):
        _create_user('brand_new_admin', 'admin')  # must not raise
        self.assertEqual(
            CustomUser.objects.filter(role='admin').count(), 2,
        )

    # --- Superuser counts as an administrator ---

    def test_superuser_is_protected_when_last_admin(self):
        su = CustomUser.objects.create_superuser(
            username='su_root', password=PASSWORD, role='admin',
        )
        self.admin.delete()  # su remains, so this is fine
        su.role = 'customer'
        su.is_superuser = False
        su.is_staff = False
        with self.assertRaises(ValidationError):
            su.save()

    def test_superuser_removal_allowed_while_admin_exists(self):
        su = CustomUser.objects.create_superuser(
            username='su_root', password=PASSWORD, role='admin',
        )
        su.role = 'customer'
        su.is_superuser = False
        su.is_staff = False
        su.save()  # self.admin (role=admin) still exists -- allowed
        su.refresh_from_db()
        self.assertFalse(su.is_superuser)

    # --- Django admin site (the primary role-edit UI) ---

    def test_admin_site_blocks_last_admin_self_demotion(self):
        su = CustomUser.objects.create_superuser(
            username='su_root', password=PASSWORD, role='admin',
        )
        self.admin.delete()  # su is now the only administrator
        self.client.force_login(su)
        url = reverse('admin:accounts_customuser_change', args=[su.pk])
        response = self.client.post(url, {
            'username': 'su_root',
            'first_name': '', 'last_name': '', 'email': '',
            'is_active': 'on',
            'role': 'customer',
            'is_staff': '',
            'is_superuser': '',
            'phone': '',
            'profile_image': '',
            'date_joined_0': su.date_joined.strftime('%Y-%m-%d'),
            'date_joined_1': su.date_joined.strftime('%H:%M:%S'),
            'groups': [],
            'user_permissions': [],
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with errors
        self.assertContains(response, 'Cannot remove the last administrator')
        su.refresh_from_db()
        self.assertTrue(su.is_superuser)
        self.assertEqual(su.role, 'admin')

    def test_admin_site_allows_demotion_when_another_admin_exists(self):
        su = CustomUser.objects.create_superuser(
            username='su_root', password=PASSWORD, role='admin',
        )
        target = self.admin  # role='admin', another admin (su) still exists
        self.client.force_login(su)
        url = reverse('admin:accounts_customuser_change', args=[target.pk])
        response = self.client.post(url, {
            'username': target.username,
            'first_name': '', 'last_name': '', 'email': '',
            'is_active': 'on',
            'role': 'customer',
            'is_staff': '',
            'is_superuser': '',
            'phone': '',
            'profile_image': '',
            'date_joined_0': target.date_joined.strftime('%Y-%m-%d'),
            'date_joined_1': target.date_joined.strftime('%H:%M:%S'),
            'groups': [],
            'user_permissions': [],
        })
        self.assertEqual(response.status_code, 302)  # saved -> redirect to list
        target.refresh_from_db()
        self.assertEqual(target.role, 'customer')

    def test_admin_site_hides_delete_for_last_admin(self):
        su = CustomUser.objects.create_superuser(
            username='su_root', password=PASSWORD, role='admin',
        )
        self.admin.delete()  # su is now the only administrator
        self.client.force_login(su)
        url = reverse('admin:accounts_customuser_delete', args=[su.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)  # permission denied

    def test_admin_bulk_delete_blocks_last_admin(self):
        # With a single last administrator, the bulk action is unavailable
        # (has_delete_permission is always False, so the action never even
        # appears) -- a crafted POST is refused and nothing is deleted.
        su = CustomUser.objects.create_superuser(
            username='su_root', password=PASSWORD, role='admin',
        )
        self.admin.delete()  # su is now the only administrator
        self.client.force_login(su)
        url = reverse('admin:accounts_customuser_changelist')
        payload = {'action': 'delete_selected', '_selected_action': [str(su.pk)]}
        response = self.client.post(url, {**payload, 'post': 'yes'})
        # has_delete_permission is always False, so the action is not even
        # registered -- Django redirects back with a warning, deleting nothing.
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CustomUser.objects.filter(pk=su.pk).exists())

    def test_admin_has_no_delete_permission_for_any_user(self):
        # Soft-deactivation replaces permanent deletion, so the delete action
        # is removed from the admin site for every user -- including ordinary
        # staff and customers (not just the last administrator).
        su = CustomUser.objects.create_superuser(
            username='su_root', password=PASSWORD, role='admin',
        )
        self.client.force_login(su)
        for target in (su, self.admin, self.cashier, self.customer):
            url = reverse('admin:accounts_customuser_delete', args=[target.pk])
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, target.username)

    def test_admin_bulk_delete_denied_for_everyone(self):
        # has_delete_permission is False for all users, so Django's own bulk
        # action is refused before anything is touched -- the account and its
        # records stay completely intact.
        su = CustomUser.objects.create_superuser(
            username='su_root', password=PASSWORD, role='admin',
        )
        self.client.force_login(su)
        url = reverse('admin:accounts_customuser_changelist')
        payload = {'action': 'delete_selected', '_selected_action': [str(self.cashier.pk)]}
        response = self.client.post(url, {**payload, 'post': 'yes'})
        # Same as above: the action is not registered, so Django redirects back
        # with a warning -- the account and its records stay completely intact.
        self.assertEqual(response.status_code, 302)
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_active)
        self.assertTrue(CustomUser.objects.filter(pk=self.cashier.pk).exists())


class SoftDeactivationTests(TestCase):
    """Soft-deactivation replaces permanent deletion.

    Accounts are deactivated (``is_active=False``) rather than deleted, so:
    * The user row is never removed from the database.
    * Historical records (orders, finance records, reports) keep their
      references to the account intact.
    * Deactivated accounts cannot log in, and any still-open session is
      terminated on its next request (ActiveUserMiddleware).
    """

    def setUp(self):
        self.admin = _create_user('soft_admin', 'admin')
        self.cashier = _create_user('soft_cashier', 'cashier')

    # --- Deactivate / activate helpers ---

    def test_deactivate_sets_flag_and_timestamp(self):
        self.cashier.deactivate()
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.is_active)
        self.assertIsNotNone(self.cashier.deactivated_at)

    def test_activate_clears_flag_and_timestamp(self):
        self.cashier.deactivate()
        self.cashier.activate()
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_active)
        self.assertIsNone(self.cashier.deactivated_at)

    def test_toggle_deactivates_and_reactivates(self):
        self.client.force_login(self.admin)
        url = f'/accounts/staff/{self.cashier.pk}/toggle/'
        self.assertEqual(
            self.client.post(url).json(),
            {'success': True, 'status': 'deactivated'},
        )
        self.cashier.refresh_from_db()
        self.assertIsNotNone(self.cashier.deactivated_at)
        self.assertEqual(
            self.client.post(url).json(),
            {'success': True, 'status': 'activated'},
        )
        self.cashier.refresh_from_db()
        self.assertIsNone(self.cashier.deactivated_at)
        self.assertTrue(self.cashier.is_active)

    # --- Permanent deletion is disabled ---

    def test_delete_soft_deactivates_and_keeps_row(self):
        self.cashier.delete()
        self.assertTrue(CustomUser.objects.filter(pk=self.cashier.pk).exists())
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.is_active)
        self.assertIsNotNone(self.cashier.deactivated_at)

    def test_bulk_delete_of_any_user_is_blocked(self):
        # Even a plain (non-admin) user cannot be hard-deleted via the ORM;
        # the pre_delete signal raises so no row is ever removed.
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                CustomUser.objects.filter(pk=self.cashier.pk).delete()
        self.assertTrue(CustomUser.objects.filter(pk=self.cashier.pk).exists())

    def test_delete_of_customer_also_soft_deactivates(self):
        customer = _create_user('soft_customer', 'customer')
        customer.delete()
        self.assertTrue(CustomUser.objects.filter(pk=customer.pk).exists())
        customer.refresh_from_db()
        self.assertFalse(customer.is_active)

    # --- Historical records stay intact ---

    def test_order_history_keeps_reference_after_deactivation(self):
        from apps.orders.models import Order
        order = Order.objects.create(cashier=self.cashier, status='completed')
        self.cashier.deactivate()
        order.refresh_from_db()
        self.assertEqual(order.cashier, self.cashier)   # FK never NULLed
        self.assertFalse(self.cashier.is_active)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_finance_history_keeps_reference_after_deactivation(self):
        from apps.finance.models import DailyFinance
        from datetime import date
        record = DailyFinance.objects.create(
            date=date.today(), prepared_by=self.cashier,
        )
        self.cashier.deactivate()
        record.refresh_from_db()
        self.assertEqual(record.prepared_by, self.cashier)
        self.assertTrue(DailyFinance.objects.filter(pk=record.pk).exists())

    # --- Deactivated accounts cannot log in ---

    def test_inactive_user_cannot_log_in(self):
        self.cashier.deactivate()
        response = self.client.post('/accounts/login/', {
            'username': 'soft_cashier', 'password': PASSWORD,
        })
        self.assertEqual(response.status_code, 200)    # stays on login page
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_reactivated_user_can_log_in_again(self):
        self.cashier.deactivate()
        self.cashier.activate()
        response = self.client.post('/accounts/login/', {
            'username': 'soft_cashier', 'password': PASSWORD,
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    # --- Existing sessions are terminated (ActiveUserMiddleware) ---

    def test_inactive_user_with_open_session_is_logged_out(self):
        self.client.force_login(self.cashier)
        # Still usable before deactivation.
        self.assertEqual(self.client.get('/orders/pos/').status_code, 200)
        self.cashier.deactivate()
        # Next request is bounced to the login page and the session cleared.
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/accounts/login/')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_active_users_are_not_affected_by_middleware(self):
        self.client.force_login(self.cashier)
        response = self.client.get('/orders/pos/')
        self.assertEqual(response.status_code, 200)

    # --- Admin soft-deactivation actions ---

    def test_admin_deactivate_action(self):
        su = CustomUser.objects.create_superuser(
            username='soft_su', password=PASSWORD, role='admin',
        )
        self.client.force_login(su)
        url = reverse('admin:accounts_customuser_changelist')
        response = self.client.post(url, {
            'action': 'deactivate_users',
            '_selected_action': [str(self.cashier.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.is_active)
        self.assertIsNotNone(self.cashier.deactivated_at)

    def test_admin_activate_action(self):
        su = CustomUser.objects.create_superuser(
            username='soft_su', password=PASSWORD, role='admin',
        )
        self.cashier.deactivate()
        self.client.force_login(su)
        url = reverse('admin:accounts_customuser_changelist')
        response = self.client.post(url, {
            'action': 'activate_users',
            '_selected_action': [str(self.cashier.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_active)
        self.assertIsNone(self.cashier.deactivated_at)

    def test_admin_deactivate_action_blocks_last_admin(self):
        su = CustomUser.objects.create_superuser(
            username='soft_su', password=PASSWORD, role='admin',
        )
        # self.admin has role='admin' and su is a superuser -- demote the
        # regular admin first so su is the ONLY administrator left.
        self.admin.role = 'cashier'
        self.admin.save()
        self.client.force_login(su)
        url = reverse('admin:accounts_customuser_changelist')
        response = self.client.post(url, {
            'action': 'deactivate_users',
            '_selected_action': [str(su.pk)],
        })
        self.assertEqual(response.status_code, 302)   # message shown, action skipped
        su.refresh_from_db()
        self.assertTrue(su.is_active)

    # --- Staff list reflects deactivation ---

    def test_staff_list_shows_inactive_badge_and_since_date(self):
        self.cashier.deactivate()
        self.client.force_login(self.admin)
        response = self.client.get('/accounts/staff/')
        self.assertContains(response, 'Inactive')
        self.assertContains(response, 'since')
        self.assertContains(response, 'Activate')     # context-aware button


class ProfileImageUploadTests(TestCase):
    """Profile picture upload validation on the profile page.

    Covers the server-side guards for the profile image upload:
    * valid JPG/PNG/GIF/WEBP images upload successfully,
    * non-image files and unsupported formats (e.g. BMP, SVG) are rejected,
    * files whose contents don't match their extension are rejected,
    * oversized files and oversized dimensions are rejected with friendly
      messages,
    * saving the profile without a new file keeps any existing image and
      never re-validates stored files (existing workflow unchanged).
    """

    PROFILE_URL = '/accounts/profile/'

    def setUp(self):
        self.user = _create_user('pic_user', 'customer')
        self.client.force_login(self.user)
        # Keep uploaded files out of the repo's media/ directory.
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

    def tearDown(self):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _image_bytes(self, fmt='PNG', size=(32, 32), color='red'):
        buf = io.BytesIO()
        Image.new('RGB', size, color).save(buf, format=fmt)
        return buf.getvalue()

    def _upload(self, name, content, content_type='image/png'):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def _post(self, upload=None):
        # Django's test client has no ``files`` keyword: file objects go
        # straight into ``data`` and are encoded as multipart form-data.
        data = {
            'first_name': 'Test', 'last_name': 'User',
            'email': 'pic@example.com', 'phone': '09171234567',
        }
        if upload:
            data['profile_image'] = upload
        return self.client.post(self.PROFILE_URL, data)

    def _post_valid_avatar(self):
        """Upload a valid PNG via the form and refresh the user."""
        response = self._post(self._upload('avatar.png', self._image_bytes('PNG')))
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        return self.user.profile_image.name

    # ── valid uploads ────────────────────────────────────────────────────

    def test_valid_png_upload_updates_profile(self):
        name = self._post_valid_avatar()
        self.assertTrue(name.startswith('profiles/'))
        self.assertTrue(self.user.profile_image)

    def test_valid_jpeg_upload_updates_profile(self):
        # .jpg and .jfif (a common JPEG variant) are both accepted.
        for ext in ('jpg', 'jfif'):
            with self.subTest(ext=ext):
                upload = self._upload(f'avatar.{ext}', self._image_bytes('JPEG'), 'image/jpeg')
                response = self._post(upload)
                self.assertEqual(response.status_code, 302)
                self.user.refresh_from_db()
                self.assertTrue(self.user.profile_image)

    def test_valid_gif_and_webp_uploads_are_accepted(self):
        for fmt, ext, content_type in (
            ('GIF', 'gif', 'image/gif'),
            ('WEBP', 'webp', 'image/webp'),
        ):
            with self.subTest(fmt=fmt):
                upload = self._upload(f'avatar.{ext}', self._image_bytes(fmt), content_type)
                response = self._post(upload)
                self.assertEqual(response.status_code, 302)
                self.user.refresh_from_db()
                self.assertTrue(self.user.profile_image)

    # ── invalid uploads ──────────────────────────────────────────────────

    def test_non_image_file_is_rejected(self):
        response = self._post(self._upload('avatar.png', b'not an image at all'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Upload a valid image')
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_image)

    def test_svg_with_script_is_rejected(self):
        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
            b'<script>alert(1)</script></svg>'
        )
        response = self._post(self._upload('avatar.svg', svg, 'image/svg+xml'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Upload a valid image')
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_image)

    def test_unsupported_format_is_rejected_with_friendly_message(self):
        # A real BMP: Pillow can decode it, but BMP is not an allowed
        # profile-picture format, so the extension allowlist rejects it.
        upload = self._upload('avatar.bmp', self._image_bytes('BMP'), 'image/bmp')
        response = self._post(upload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Unsupported file format')
        self.assertContains(response, 'JPG, PNG, GIF, or WEBP')
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_image)

    def test_extension_content_mismatch_is_rejected(self):
        # PNG bytes disguised as a .jpg: contents must match the extension.
        upload = self._upload('avatar.jpg', self._image_bytes('PNG'), 'image/jpeg')
        response = self._post(upload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'contents do not match its file name')
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_image)

    def test_oversized_file_is_rejected_with_friendly_message(self):
        # Random noise compresses poorly, so this PNG is ~6.8 MB -- safely
        # above the 5 MB cap while still being a fully valid image.
        raw = os.urandom(1500 * 1500 * 3)
        buf = io.BytesIO()
        Image.frombytes('RGB', (1500, 1500), raw).save(buf, format='PNG')
        upload = self._upload('big.png', buf.getvalue())
        response = self._post(upload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Your image is too large')
        self.assertContains(response, '5 MB')
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_image)

    def test_oversized_dimensions_are_rejected(self):
        # A 1-bit 11000x11000 image encodes to a few KB, but decoding it
        # would need an enormous buffer -- the dimension cap rejects it.
        buf = io.BytesIO()
        Image.new('1', (11000, 11000)).save(buf, format='PNG')
        upload = self._upload('huge.png', buf.getvalue())
        # Pillow itself flags >89 MP images with a DecompressionBombWarning
        # while verifying -- expected here, so keep the test output clean.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            response = self._post(upload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'dimensions are too large')
        self.assertContains(response, '8000 x 8000')
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_image)

    # ── existing workflow still works ────────────────────────────────────

    def test_profile_fields_still_update_without_image(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Test')
        self.assertEqual(self.user.last_name, 'User')
        self.assertEqual(self.user.email, 'pic@example.com')
        self.assertEqual(self.user.phone, '09171234567')
        self.assertFalse(self.user.profile_image)

    def test_saving_without_new_file_keeps_existing_image(self):
        old_name = self._post_valid_avatar()
        response = self._post()   # no new file selected
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile_image.name, old_name)

    def test_failed_upload_keeps_existing_image(self):
        old_name = self._post_valid_avatar()
        upload = self._upload('avatar.bmp', self._image_bytes('BMP'), 'image/bmp')
        response = self._post(upload)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile_image.name, old_name)


class SessionSecurityTests(TestCase):
    """Session expiry, cookie flags, and complete logout invalidation.

    * Absolute session lifetime is bounded (SESSION_COOKIE_AGE) and sessions
      also expire after SESSION_IDLE_TIMEOUT_MINUTES of inactivity.
    * Logout flushes the session (no residual authentication data).
    * Session cookie is HttpOnly + SameSite=Lax, and Secure outside DEBUG.
    """

    def setUp(self):
        self.admin = _create_user('sess_admin', 'admin')
        self.cashier = _create_user('sess_cashier', 'cashier')

    def _login(self, username='sess_admin'):
        return self.client.post('/accounts/login/', {
            'username': username, 'password': PASSWORD,
        })

    def _idle_session(self, minutes_idle):
        """Backdate the idle-activity timestamp in the current session."""
        session = self.client.session
        session['last_activity'] = time.time() - minutes_idle * 60
        session.save()

    # --- Login still works and establishes an authenticated session ---

    def test_login_sets_authenticated_session(self):
        response = self._login()
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_all_role_logins_still_work(self):
        for username, url in (('sess_admin', '/dashboard/'),
                              ('sess_cashier', '/orders/pos/')):
            with self.subTest(username=username):
                self._login(username)
                self.assertEqual(self.client.get(url).status_code, 200)

    # --- Logout completely invalidates the session ---

    def test_logout_flushes_session_and_key(self):
        self._login()
        self.assertIn('_auth_user_id', self.client.session)
        key_before = self.client.session.session_key
        response = self.client.get('/accounts/logout/')
        self.assertEqual(response.status_code, 302)
        # No residual authentication data, and the session key was rotated.
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertNotEqual(self.client.session.session_key, key_before)

    def test_protected_page_requires_login_after_logout(self):
        self._login()
        self.client.get('/accounts/logout/')
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/accounts/login/?next=/dashboard/')

    # --- Idle timeout ---

    def test_active_session_within_timeout_is_untouched(self):
        self._login()
        self._idle_session(minutes_idle=5)   # well inside the 30-min default
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('_auth_user_id', self.client.session)

    def test_idle_timeout_logs_out_user(self):
        self._login()
        self._idle_session(minutes_idle=31)  # past the 30-min default
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/accounts/login/')
        self.assertNotIn('_auth_user_id', self.client.session)

    @override_settings(SESSION_IDLE_TIMEOUT_MINUTES=5)
    def test_idle_timeout_uses_configured_minutes(self):
        self._login()
        self._idle_session(minutes_idle=6)   # past the overridden 5-min window
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/accounts/login/')
        self.assertNotIn('_auth_user_id', self.client.session)

    @override_settings(SESSION_IDLE_TIMEOUT_MINUTES=0)
    def test_idle_timeout_disabled_when_zero(self):
        self._login()
        self._idle_session(minutes_idle=999)
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)   # still logged in

    def test_activity_refreshes_the_idle_timer(self):
        self._login()
        self._idle_session(minutes_idle=29)
        self.client.get('/dashboard/')                 # activity refreshes it
        self._idle_session(minutes_idle=29)           # ...again
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)

    def test_idle_timeout_does_not_affect_anonymous_users(self):
        response = self.client.get('/accounts/login/')
        self.assertEqual(response.status_code, 200)   # no redirect loop

    # --- Cookie security flags ---

    def test_session_cookie_is_httponly_and_samesite_lax(self):
        response = self._login()
        cookie = response.cookies['sessionid']
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'].lower(), 'lax')

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_session_cookie_is_secure_when_configured(self):
        response = self._login()
        self.assertTrue(response.cookies['sessionid']['secure'])

    @override_settings(SESSION_COOKIE_SECURE=False)
    def test_session_cookie_not_secure_in_development(self):
        # Morsel always lists 'secure' as a reserved key, so check its VALUE
        # (empty string means the Secure attribute was not set on the cookie).
        response = self._login()
        self.assertEqual(response.cookies['sessionid']['secure'], '')


class StaffListSearchTests(TestCase):
    """Regression tests for the Staff Accounts automatic search (Phase 6A)."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='admin_user', password=PASSWORD,
            first_name='Alice', last_name='Admin',
            email='alice@example.com', role='admin',
        )
        self.cashier = CustomUser.objects.create_user(
            username='cashier_user', password=PASSWORD,
            first_name='Bob', last_name='Cash',
            email='bob@example.com', role='cashier',
        )
        self.kitchen = CustomUser.objects.create_user(
            username='kitchen_user', password=PASSWORD,
            first_name='Carol', last_name='Kitchen',
            email='carol@example.com', role='kitchen_staff',
        )
        self.customer = CustomUser.objects.create_user(
            username='customer_user', password=PASSWORD,
            role='customer',
        )
        self.url = reverse('accounts:staff_list')

    def _get(self, params=None):
        self.client.force_login(self.admin)
        return self.client.get(self.url, params or {})

    def test_staff_list_requires_login(self):
        """Unauthenticated request redirects to login."""
        response = self.client.get(self.url)
        self.assertNotEqual(response.status_code, 200)

    def test_staff_list_requires_admin(self):
        """Non-admin (cashier) is denied."""
        self.client.force_login(self.cashier)
        response = self.client.get(self.url)
        self.assertNotEqual(response.status_code, 200)

    def test_staff_list_no_filters_returns_all_staff(self):
        """No params returns all staff (admin + cashier + kitchen_staff, not customer)."""
        response = self._get()
        self.assertEqual(response.status_code, 200)
        staff = response.context['staff']
        usernames = [u.username for u in staff]
        self.assertIn('admin_user', usernames)
        self.assertIn('cashier_user', usernames)
        self.assertIn('kitchen_user', usernames)
        self.assertNotIn('customer_user', usernames)

    def test_staff_list_search_by_username(self):
        """?q=cashier_user returns only the matching user."""
        response = self._get({'q': 'cashier_user'})
        self.assertEqual(response.status_code, 200)
        usernames = [u.username for u in response.context['staff']]
        self.assertIn('cashier_user', usernames)
        self.assertNotIn('admin_user', usernames)

    def test_staff_list_search_by_first_name(self):
        """?q=Carol matches by first_name."""
        response = self._get({'q': 'Carol'})
        self.assertEqual(response.status_code, 200)
        usernames = [u.username for u in response.context['staff']]
        self.assertIn('kitchen_user', usernames)
        self.assertNotIn('admin_user', usernames)

    def test_staff_list_search_by_email(self):
        """?q=alice@example.com matches by email."""
        response = self._get({'q': 'alice@example.com'})
        self.assertEqual(response.status_code, 200)
        usernames = [u.username for u in response.context['staff']]
        self.assertIn('admin_user', usernames)
        self.assertNotIn('cashier_user', usernames)

    def test_staff_list_search_case_insensitive(self):
        """Search is case-insensitive (icontains)."""
        response = self._get({'q': 'ALICE'})
        self.assertEqual(response.status_code, 200)
        usernames = [u.username for u in response.context['staff']]
        self.assertIn('admin_user', usernames)

    def test_staff_list_search_no_results(self):
        """?q=zzznomatch returns empty list with 200."""
        response = self._get({'q': 'zzznomatch'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(list(response.context['staff'])), 0)

    def test_staff_list_role_filter_admin(self):
        """?role=admin returns only admin-role staff."""
        response = self._get({'role': 'admin'})
        self.assertEqual(response.status_code, 200)
        for u in response.context['staff']:
            self.assertEqual(u.role, 'admin')

    def test_staff_list_role_filter_cashier(self):
        """?role=cashier returns only cashier-role staff."""
        response = self._get({'role': 'cashier'})
        self.assertEqual(response.status_code, 200)
        for u in response.context['staff']:
            self.assertEqual(u.role, 'cashier')

    def test_staff_list_combined_search_and_role(self):
        """?q=Bob&role=cashier returns intersection."""
        response = self._get({'q': 'Bob', 'role': 'cashier'})
        self.assertEqual(response.status_code, 200)
        usernames = [u.username for u in response.context['staff']]
        self.assertIn('cashier_user', usernames)
        self.assertNotIn('admin_user', usernames)

    def test_staff_list_clear_resets_filters(self):
        """Bare URL (no params) returns all staff."""
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(list(response.context['staff'])), 3)

    def test_staff_list_whitespace_search(self):
        """?q=+%20 (whitespace only) is stripped and returns all staff."""
        response = self._get({'q': '   '})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(list(response.context['staff'])), 3)

    def test_staff_list_invalid_role_ignored(self):
        """?role=customer is rejected (not in allowlist) — all staff returned."""
        response = self._get({'role': 'customer'})
        self.assertEqual(response.status_code, 200)
        # customer_user must NOT appear (view always restricts to staff roles)
        usernames = [u.username for u in response.context['staff']]
        self.assertNotIn('customer_user', usernames)
        # but the three staff should still appear
        self.assertGreaterEqual(len(usernames), 3)

    def test_staff_list_search_does_not_modify_records(self):
        """GET search must not modify any user records."""
        count_before = CustomUser.objects.count()
        self._get({'q': 'admin_user', 'role': 'admin'})
        self.assertEqual(CustomUser.objects.count(), count_before)


class StaffListAjaxTests(TestCase):
    """AJAX partial-replacement: confirm results-region is present in responses.

    These tests verify that GET /staff/ (and its filtered variants) always
    returns a page containing id="results-region" so the JS fetch-replace
    pattern has a valid target to inject into.
    """

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='ajax_staff_admin', password=PASSWORD, role='admin',
        )
        self.cashier = CustomUser.objects.create_user(
            username='ajax_staff_cashier', password=PASSWORD,
            first_name='Cashier', last_name='User', role='cashier',
        )
        self.client.force_login(self.admin)

    def test_results_region_present_on_base_page(self):
        """Unfiltered GET returns id="results-region"."""
        response = self.client.get(reverse('accounts:staff_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_results_region_present_with_q_filter(self):
        """GET ?q=foo returns id="results-region"."""
        response = self.client.get(reverse('accounts:staff_list'), {'q': 'foo'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_results_region_present_with_role_filter(self):
        """GET ?role=admin returns id="results-region"."""
        response = self.client.get(reverse('accounts:staff_list'), {'role': 'admin'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_results_region_present_with_combined_filters(self):
        """GET ?q=cash&role=cashier returns id="results-region"."""
        response = self.client.get(
            reverse('accounts:staff_list'), {'q': 'cash', 'role': 'cashier'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_filter_bar_outside_results_region(self):
        """The filter-bar form appears before results-region in the HTML source.

        This guarantees the filter controls are never replaced by the JS
        innerHTML swap, so focus is always preserved in the search input.
        """
        response = self.client.get(reverse('accounts:staff_list'))
        content = response.content.decode()
        filter_bar_pos = content.find('class="filter-bar"')
        region_pos = content.find('id="results-region"')
        self.assertGreater(region_pos, filter_bar_pos,
            'filter-bar must appear before results-region (i.e. is not nested inside it)')



# ── Phase 5C — password-reset request throttling ──────────────────────────────

from unittest import mock  # noqa: E402  (used by the resend patches below)

from apps.accounts.models import PasswordResetAttempt  # noqa: E402


class PasswordResetThrottleTests(TestCase):
    """Phase 5C: reset requests are throttled per keyed-email-hash and per
    IP. The outward response never reveals eligibility or throttling, and
    throttled requests never send mail. All email delivery is mocked — no
    real email can leave the test environment."""

    URL = '/accounts/password/reset/'
    DONE_URL = '/accounts/password/reset/done/'

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='reset_admin', password=PASSWORD, role='admin',
            email='Reset.Admin@Example.COM',
        )
        # Patch the resend SDK at the module attribute the view imports.
        patcher = mock.patch('resend.Emails.send')
        self.send_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, email, ip='192.0.2.1'):
        return self.client.post(
            self.URL, {'email': email}, REMOTE_ADDR=ip)

    def _backdate(self, minutes):
        """Age every counter past the window (cooldown expiry simulation)."""
        past = timezone.now() - timedelta(minutes=minutes)
        PasswordResetAttempt.objects.update(last_attempt_at=past)

    # 1. Eligible email can request a reset within the allowed limit.

    def test_eligible_email_sends_reset_within_limit(self):
        for _ in range(2):                     # limit is 3 per email
            response = self._post('reset.admin@example.com')
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, self.DONE_URL)
        self.assertEqual(self.send_mock.call_count, 2)

    # 2. Unknown email receives the same outward response as an eligible one.

    def test_unknown_email_same_response_and_no_email(self):
        known = self._post('reset.admin@example.com')
        unknown = self._post('ghost@example.com')
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.url, unknown.url)
        # Only the eligible address produced an email.
        self.assertEqual(self.send_mock.call_count, 1)
        # The email goes to the ACCOUNT's stored address (the view looks
        # the user up by email and sends to user.email).
        self.assertEqual(self.send_mock.call_args.args[0]['to'],
                         [self.admin.email])

    # 3. Ineligible / inactive accounts receive the same outward response.

    def test_ineligible_and_inactive_same_response_and_no_email(self):
        CustomUser.objects.create_user(
            username='reset_customer', password=PASSWORD, role='customer',
            email='customer@example.com',
        )
        CustomUser.objects.create_user(
            username='reset_inactive', password=PASSWORD, role='cashier',
            email='inactive@example.com', is_active=False,
        )
        for email in ('customer@example.com', 'inactive@example.com'):
            response = self._post(email)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, self.DONE_URL)
        self.assertEqual(self.send_mock.call_count, 0)

    # 4. Repeated requests for one email eventually stop sending emails.

    def test_per_email_limit_stops_emails(self):
        for _ in range(3):                     # exactly the per-email limit
            self._post('reset.admin@example.com')
        self.assertEqual(self.send_mock.call_count, 3)
        # The 4th request: same outward redirect, no additional email.
        throttled = self._post('reset.admin@example.com')
        self.assertEqual(throttled.status_code, 302)
        self.assertEqual(throttled.url, self.DONE_URL)
        self.assertEqual(self.send_mock.call_count, 3)

    # 5. Repeated requests from one IP eventually stop reset-email sends.

    def test_per_ip_limit_stops_emails(self):
        for i in range(10):                    # exactly the per-IP limit
            self._post(f'staff{i}@example.com', ip='203.0.113.9')
        self.assertEqual(self.send_mock.call_count, 0)  # none were eligible
        # The 11th, now for the ELIGIBLE account, is throttled by IP: no
        # lookup happens and no email is sent.
        throttled = self._post(
            'reset.admin@example.com', ip='203.0.113.9')
        self.assertEqual(throttled.status_code, 302)
        self.assertEqual(throttled.url, self.DONE_URL)
        self.assertEqual(self.send_mock.call_count, 0)

    # 6. Different email addresses from one IP share the IP limit.

    def test_ip_limit_applies_across_emails(self):
        for i in range(10):
            self._post(f'other{i}@example.com', ip='198.51.100.7')
        row = PasswordResetAttempt.objects.get(
            scope='ip', value='198.51.100.7')
        self.assertEqual(row.attempts, 10)
        # A different fresh email from the same IP is still throttled.
        response = self._post('brandnew@example.com', ip='198.51.100.7')
        self.assertEqual(response.url, self.DONE_URL)
        self.assertEqual(self.send_mock.call_count, 0)

    # 7. Email normalization is consistent for the per-email limit.

    def test_email_normalization_shared_counter(self):
        self._post('reset.admin@example.com')
        self._post('  Reset.Admin@Example.COM ')     # same address, typed
        self._post('RESET.ADMIN@example.com')        # differently each time
        # One keyed email-scope row, attempts == 3 (at the limit)...
        rows = PasswordResetAttempt.objects.filter(scope='email')
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows[0].attempts, 3)
        # ...so all three actually sent (each request is checked BEFORE the
        # counter increments — the limit bites on the NEXT request) and a
        # differently-cased 4th is throttled with no extra email.
        self.assertEqual(self.send_mock.call_count, 3)
        throttled = self._post('reset.ADMIN@example.com')
        self.assertEqual(throttled.url, self.DONE_URL)
        self.assertEqual(self.send_mock.call_count, 3)
        # The stored identifier is a keyed hash, never the raw address.
        self.assertNotIn('reset.admin', rows[0].value)
        self.assertEqual(len(rows[0].value), 64)

    # 8. A throttled request does not generate or send another email.

    def test_throttled_request_never_touches_users_or_email(self):
        for _ in range(3):
            self._post('reset.admin@example.com')
        with mock.patch(
            'apps.accounts.views.CustomUser.objects.filter'
        ) as lookup_mock:
            throttled = self._post('reset.admin@example.com')
        self.assertEqual(throttled.url, self.DONE_URL)
        lookup_mock.assert_not_called()       # no account lookup at all
        self.assertEqual(self.send_mock.call_count, 3)

    # 9. Requests can be made again after the cooldown expires.

    def test_window_expiry_allows_requests_again(self):
        for _ in range(3):
            self._post('reset.admin@example.com')
        self._post('reset.admin@example.com')            # throttled
        self.assertEqual(self.send_mock.call_count, 3)
        self._backdate(minutes=16)                        # > 15-minute window
        response = self._post('reset.admin@example.com')
        self.assertEqual(response.url, self.DONE_URL)
        self.assertEqual(self.send_mock.call_count, 4)    # sent again
        row = PasswordResetAttempt.objects.get(scope='email')
        self.assertEqual(row.attempts, 1)                 # fresh window

    # 10. Existing reset-token behavior remains functional.

    def test_reset_token_flow_still_works(self):
        self._post('reset.admin@example.com')
        self.assertEqual(self.send_mock.call_count, 1)
        body = self.send_mock.call_args.args[0]['text']
        # Extract the reset URL from the rendered email body.
        start = body.index('http')
        end = body.index('\n', start) if '\n' in body[start:] else len(body)
        reset_url = body[start:end].strip()
        path = reset_url.split('/accounts/', 1)[1].rstrip('/')
        uidb64, token = path.split('/')[-2:]

        from django.contrib.auth import get_user_model
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_str
        from django.utils.http import urlsafe_base64_decode
        user = get_user_model().objects.get(
            pk=force_str(urlsafe_base64_decode(uidb64)))
        self.assertEqual(user.pk, self.admin.pk)
        self.assertTrue(default_token_generator.check_token(user, token))

        # The confirm page renders the valid form (not the invalid-link page).
        response = self.client.get(f'/accounts/password/reset/{uidb64}/{token}/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'invalid')  # no invalid-link marker


# ── Phase 5D — multi-factor authentication ───────────────────────────────────

import base64  # noqa: E402
import io  # noqa: E402
import logging  # noqa: E402
from unittest import mock  # noqa: E402

from cryptography.fernet import Fernet  # noqa: E402
from django.contrib.auth.hashers import identify_hasher  # noqa: E402
from django.contrib.auth.tokens import default_token_generator  # noqa: E402
from django.contrib.sessions.models import Session  # noqa: E402
from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.test import Client, SimpleTestCase  # noqa: E402
from django.utils.encoding import force_bytes  # noqa: E402
from django.utils.http import urlsafe_base64_encode  # noqa: E402
from django_otp.oath import TOTP  # noqa: E402

from apps.accounts import checks, mfa  # noqa: E402
from apps.accounts.models import (  # noqa: E402
    MFARecoveryCode, MFAVerificationAttempt, StaffMFADevice,
)
from apps.audit.models import AuditLog  # noqa: E402


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@override_settings(
    PASSWORD_HASHERS=[
        'django.contrib.auth.hashers.MD5PasswordHasher',
    ],
    # The test runner forces DEBUG=False, so the SECRET_KEY-derived key is
    # explicitly allowed here; production requires MFA_ENCRYPTION_KEY.
    MFA_ALLOW_SECRET_KEY_FALLBACK=True,
)
class MFATests(TestCase):
    """Phase 5D: TOTP enrolment, login challenge, recovery, management.

    All codes are generated locally with django-otp's TOTP primitive; no
    network, email or external service is used. The fast password hasher is
    enabled for this class only (recovery-code hashing + login speed);
    production hashers are untouched.
    """

    LOGIN_URL = '/accounts/login/'
    CHALLENGE_URL = '/accounts/mfa/challenge/'

    def setUp(self):
        self.admin = CustomUser.objects.create_superuser(
            username='mfa_admin', password=PASSWORD, role='admin',
        )
        self.cashier = _create_user('mfa_cashier', 'cashier')
        self.customer = _create_user('mfa_customer', 'customer')

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _code(secret, drift=0):
        padded = secret + '=' * (-len(secret) % 8)
        totp = TOTP(base64.b32decode(padded), drift=drift)
        return f'{totp.token():06d}'

    def _enroll(self, user=None):
        """Create a confirmed device + recovery codes directly via the service."""
        user = user or self.admin
        secret = mfa.generate_secret()
        _device, codes = mfa.activate_device(user, secret)
        return mfa.get_device(user), secret, codes

    def _begin_enrollment(self, user=None, password=PASSWORD):
        """Sign in and confirm the current password to start enrollment."""
        self.client.force_login(user or self.admin)
        return self.client.post(
            reverse('accounts:mfa_setup'), {'password': password})

    def _login(self, username, password=PASSWORD, ip='192.0.2.10', next_url=None):
        data = {'username': username, 'password': password}
        if next_url is not None:
            data['next'] = next_url
        return self.client.post(self.LOGIN_URL, data, REMOTE_ADDR=ip)

    def _challenge(self, *, code=None, recovery=None, ip='192.0.2.10'):
        data = {}
        if code is not None:
            data['code'] = code
        if recovery is not None:
            data['recovery_code'] = recovery
        return self.client.post(self.CHALLENGE_URL, data, REMOTE_ADDR=ip)

    def _session_secret(self):
        data = self.client.session['mfa_setup_secret']
        return mfa.decrypt_secret(data['token'])

    def _mark_session_verified(self, user=None, age=0):
        user = user or self.admin
        session = self.client.session
        session['mfa_verified'] = {'uid': user.pk, 'at': int(time.time()) - age}
        session.save()

    # ── 1-6: enrolment ───────────────────────────────────────────────────────

    def test_setup_requires_authentication(self):
        response = self.client.get(reverse('accounts:mfa_setup'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_customer_cannot_enroll(self):
        self.client.force_login(self.customer)
        self.assertEqual(
            self.client.get(reverse('accounts:mfa_setup')).status_code, 403)
        self.assertEqual(
            self.client.post(reverse('accounts:mfa_setup'), {'code': '000000'}).status_code,
            403,
        )
        self.assertFalse(StaffMFADevice.objects.exists())

    @override_settings(MFA_SETUP_SECRET_TIMEOUT_SECONDS=900)
    def test_setup_generates_secret_but_does_not_enable_mfa(self):
        self.client.force_login(self.admin)
        gate = self.client.get(reverse('accounts:mfa_setup'))
        self.assertEqual(gate.status_code, 200)
        self.assertContains(gate, 'Current password')
        self.assertNotIn('mfa_setup_secret', self.client.session)

        begin = self.client.post(
            reverse('accounts:mfa_setup'), {'password': PASSWORD})
        self.assertEqual(begin.status_code, 302)

        first = self.client.get(reverse('accounts:mfa_setup'))
        self.assertEqual(first.status_code, 200)
        secret = self._session_secret()
        self.assertContains(first, secret)                  # owner sees own key
        self.assertFalse(StaffMFADevice.objects.exists())   # not enabled
        # Reloading keeps the same pending secret (stable QR/scan).
        self.client.get(reverse('accounts:mfa_setup'))
        self.assertEqual(self._session_secret(), secret)
        # The secret is encrypted at rest in the session, never plaintext.
        token = self.client.session['mfa_setup_secret']['token']
        self.assertNotIn(secret, token)

    def test_valid_code_activates_mfa_and_issues_recovery_codes(self):
        self._begin_enrollment()
        self.client.get(reverse('accounts:mfa_setup'))
        secret = self._session_secret()

        response = self.client.post(
            reverse('accounts:mfa_setup'), {'code': self._code(secret)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Recovery Codes')
        device = StaffMFADevice.objects.get(user=self.admin)
        self.assertIsNotNone(device.confirmed_at)
        self.assertNotIn(secret, device.secret_encrypted)
        self.assertEqual(mfa.decrypt_secret(device.secret_encrypted), secret)
        self.assertEqual(
            MFARecoveryCode.objects.filter(user=self.admin).count(),
            mfa.unused_recovery_code_count(self.admin),
        )
        self.assertTrue(mfa.session_is_verified(self._request_with_session()))
        self.assertNotIn('mfa_setup_secret', self.client.session)

    def test_invalid_code_does_not_activate_mfa(self):
        self._begin_enrollment()
        self.client.get(reverse('accounts:mfa_setup'))
        secret = self._session_secret()

        response = self.client.post(
            reverse('accounts:mfa_setup'), {'code': self._code(secret, drift=5)})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(StaffMFADevice.objects.exists())
        self.assertFalse(mfa.session_is_verified(self._request_with_session()))

    def test_enrolment_is_rate_limited(self):
        self._begin_enrollment()
        self.client.get(reverse('accounts:mfa_setup'))
        secret = self._session_secret()
        bad = self._code(secret, drift=5)
        for _ in range(5):
            self.client.post(reverse('accounts:mfa_setup'), {'code': bad})
        response = self.client.post(
            reverse('accounts:mfa_setup'), {'code': self._code(secret)})
        self.assertEqual(response.status_code, 429)
        self.assertFalse(StaffMFADevice.objects.exists())

    def test_secret_and_codes_never_logged_or_leaked(self):
        capture = _LogCapture()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            self._begin_enrollment()
            self.client.get(reverse('accounts:mfa_setup'))
            secret = self._session_secret()
            code = self._code(secret)
            self.client.post(reverse('accounts:mfa_setup'), {'code': code})

            for url in (reverse('accounts:profile'),
                        reverse('accounts:mfa_manage'),
                        reverse('dashboard:index')):
                body = self.client.get(url).content.decode()
                self.assertNotIn(secret, body)
            self.assertNotIn(secret, ' '.join(capture.messages))
            self.assertNotIn(code, ' '.join(capture.messages))
        finally:
            root.removeHandler(capture)
            root.setLevel(old_level)

    # ── enrollment password confirmation (remediation) ───────────────────────

    def test_enrollment_requires_current_password(self):
        self.client.force_login(self.admin)
        self.client.get(reverse('accounts:mfa_setup'))
        self.assertNotIn('mfa_setup_secret', self.client.session)

        wrong = self.client.post(
            reverse('accounts:mfa_setup'), {'password': 'wrong-password'})
        self.assertEqual(wrong.status_code, 400)
        self.assertContains(wrong, 'not correct', status_code=400)
        self.assertNotIn('mfa_setup_secret', self.client.session)
        self.assertFalse(StaffMFADevice.objects.exists())

        # A code POST without the password confirmation changes nothing.
        self.client.post(reverse('accounts:mfa_setup'), {'code': '123456'})
        self.assertNotIn('mfa_setup_secret', self.client.session)
        self.assertFalse(StaffMFADevice.objects.exists())

    def test_enrollment_password_confirmation_is_rate_limited(self):
        self.client.force_login(self.admin)
        for _ in range(5):
            self.client.post(
                reverse('accounts:mfa_setup'), {'password': 'wrong-password'})
        response = self.client.post(
            reverse('accounts:mfa_setup'), {'password': PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('mfa_setup_secret', self.client.session)
        self.assertFalse(StaffMFADevice.objects.exists())

    def test_get_cannot_start_enrollment(self):
        self.client.force_login(self.admin)
        for _ in range(2):
            self.assertEqual(
                self.client.get(reverse('accounts:mfa_setup')).status_code, 200)
        self.assertNotIn('mfa_enroll_verified', self.client.session)
        self.assertNotIn('mfa_setup_secret', self.client.session)
        self.assertFalse(StaffMFADevice.objects.exists())

    def test_inactive_user_cannot_enroll(self):
        self.cashier.is_active = False
        self.cashier.save(update_fields=['is_active'])
        self.client.force_login(self.cashier)
        # An inactive account is not authenticated by the middleware, so the
        # request is redirected to login (or rejected with 403) -- never 200.
        self.assertIn(
            self.client.get(reverse('accounts:mfa_setup')).status_code,
            (302, 403),
        )
        self.assertIn(
            self.client.post(
                reverse('accounts:mfa_setup'), {'password': PASSWORD}).status_code,
            (302, 403),
        )
        self.assertFalse(StaffMFADevice.objects.exists())

    def test_expired_enrollment_reauth_is_rejected(self):
        self._begin_enrollment()
        session = self.client.session
        proof = dict(session['mfa_enroll_verified'])
        proof['at'] = int(time.time()) - 3600      # past the 600 s window
        session['mfa_enroll_verified'] = proof
        session.save()

        response = self.client.post(
            reverse('accounts:mfa_setup'), {'code': '123456'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Current password')
        self.assertNotIn('mfa_setup_secret', self.client.session)
        self.assertFalse(StaffMFADevice.objects.exists())

    def test_existing_device_cannot_be_replaced_with_password(self):
        _device, secret, _codes = self._enroll()

        response = self._begin_enrollment()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('accounts:mfa_manage'))
        self.assertNotIn('mfa_setup_secret', self.client.session)
        self.assertEqual(StaffMFADevice.objects.filter(user=self.admin).count(), 1)
        stored = StaffMFADevice.objects.get(user=self.admin)
        self.assertEqual(mfa.decrypt_secret(stored.secret_encrypted), secret)

    def test_totp_permits_disable_then_reenroll(self):
        _device, old_secret, _codes = self._enroll()
        self.client.force_login(self.admin)
        self._mark_session_verified()
        self.client.post(
            reverse('accounts:mfa_disable'), {'code': self._code(old_secret)})
        self.assertFalse(StaffMFADevice.objects.filter(user=self.admin).exists())

        self._begin_enrollment()
        self.client.get(reverse('accounts:mfa_setup'))
        new_secret = self._session_secret()
        self.assertNotEqual(new_secret, old_secret)
        response = self.client.post(
            reverse('accounts:mfa_setup'), {'code': self._code(new_secret)})
        self.assertEqual(response.status_code, 200)
        # Query the row directly: ``self.admin.mfa_device`` may still be the
        # stale (deleted) device object cached by ``_enroll``.
        stored = StaffMFADevice.objects.get(user=self.admin)
        self.assertEqual(mfa.decrypt_secret(stored.secret_encrypted), new_secret)

    def test_recovery_code_permits_disable_then_reenroll(self):
        _device, _secret, codes = self._enroll()
        self.client.force_login(self.admin)
        self._mark_session_verified()
        response = self.client.post(
            reverse('accounts:mfa_disable'), {'recovery_code': codes[0]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(StaffMFADevice.objects.filter(user=self.admin).exists())

        self._begin_enrollment()
        self.assertEqual(
            self.client.get(reverse('accounts:mfa_setup')).status_code, 200)
        self.assertIn('mfa_setup_secret', self.client.session)

    # ── 7-15: login and admin flows ──────────────────────────────────────────

    def test_password_alone_does_not_create_session_when_enrolled(self):
        _device, secret, _codes = self._enroll()
        response = self._login('mfa_admin')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.CHALLENGE_URL)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertNotIn('mfa_verified', self.client.session)

    def test_valid_totp_completes_login(self):
        _device, secret, _codes = self._enroll()
        self._login('mfa_admin')
        response = self._challenge(code=self._code(secret))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('dashboard:index'))
        self.assertIn('_auth_user_id', self.client.session)
        self.assertTrue(mfa.session_is_verified(self._request_with_session()))
        self.assertEqual(self.client.get(reverse('dashboard:index')).status_code, 200)
        self.assertNotIn('mfa_pending', self.client.session)

    def test_invalid_totp_is_rejected(self):
        _device, secret, _codes = self._enroll()
        self._login('mfa_admin')
        response = self._challenge(code=self._code(secret, drift=5))
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_expired_challenge_is_rejected(self):
        _device, secret, _codes = self._enroll()
        self._login('mfa_admin')

        session = self.client.session
        pending = dict(session['mfa_pending'])
        pending['issued'] = int(time.time()) - 400   # past the 300 s timeout
        pending['sig'] = mfa._sign_pending(
            pending['uid'], pending['nonce'], pending['issued'], pending['mode'])
        session['mfa_pending'] = pending
        session.save()

        self.assertEqual(
            self.client.get(self.CHALLENGE_URL).status_code, 302)  # back to login
        response = self._challenge(code=self._code(secret))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_tampered_pending_challenge_is_rejected(self):
        _device, secret, _codes = self._enroll(self.cashier)
        self.client.force_login(self.admin)
        self.client.logout()

        self._login('mfa_cashier')
        self.assertIn('mfa_pending', self.client.session)
        session = self.client.session
        pending = dict(session['mfa_pending'])
        pending['uid'] = self.admin.pk          # try to switch identity
        session['mfa_pending'] = pending
        session.save()

        response = self._challenge(code=self._code(secret))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_customer_login_unchanged(self):
        response = self._login('mfa_customer')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('menu:index'))
        self.assertIn('_auth_user_id', self.client.session)
        self.assertNotIn('mfa_pending', self.client.session)

    def test_nonstaff_and_unenrolled_cashier_cannot_reach_admin(self):
        self.client.force_login(self.cashier)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, (302, 403))
        self.assertNotEqual(response.status_code, 200)

    def test_safe_next_is_preserved_and_external_next_rejected(self):
        _device, secret, _codes = self._enroll()
        self._login('mfa_admin', next_url=reverse('accounts:profile'))
        response = self._challenge(code=self._code(secret))
        self.assertEqual(response.url, reverse('accounts:profile'))

        self.client.logout()
        self._login('mfa_admin', next_url='https://evil.example/steal')
        # The earlier code is spent (replay protection), so use the next step.
        response = self._challenge(code=self._code(secret, drift=1))
        self.assertEqual(response.url, reverse('dashboard:index'))
        self.assertNotIn('evil.example', response.url)

    def test_admin_login_lockout_still_applies_to_enrolled_admin(self):
        self._enroll()
        for _ in range(5):
            response = self.client.post('/admin/login/', {
                'username': 'mfa_admin', 'password': 'wrong-password',
            }, REMOTE_ADDR='198.51.100.20')
        self.assertEqual(response.status_code, 200)
        response = self.client.post('/admin/login/', {
            'username': 'mfa_admin', 'password': PASSWORD,
        }, REMOTE_ADDR='198.51.100.20')
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('mfa_pending', self.client.session)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_enrolled_admin_login_requires_totp_before_session(self):
        _device, secret, _codes = self._enroll()

        first = self.client.post('/admin/login/', {
            'username': 'mfa_admin', 'password': PASSWORD,
            'next': reverse('admin:index'),
        })
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, self.CHALLENGE_URL)
        self.assertNotIn('_auth_user_id', self.client.session)

        second = self._challenge(code=self._code(secret))
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second.url, reverse('admin:index'))
        self.assertEqual(self.client.get('/admin/').status_code, 200)

    def test_direct_admin_url_blocked_until_verified(self):
        self._enroll()
        self.client.force_login(self.admin)     # simulates a pre-MFA session
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.CHALLENGE_URL, response.url)
        self.assertNotEqual(self.client.get('/admin/').status_code, 200)

    def test_mfa_session_verification_expires(self):
        self._enroll()
        self.client.force_login(self.admin)
        self._mark_session_verified(age=13 * 60 * 60)   # past the 12 h limit
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.CHALLENGE_URL, response.url)

    @override_settings(MFA_REQUIRE_ADMIN_ENROLLMENT=True)
    def test_unenrolled_admin_is_sent_to_enrolment_when_required(self):
        self.client.force_login(self.admin)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('accounts:mfa_setup'))

    @override_settings(MFA_REQUIRE_ADMIN_ENROLLMENT=False)
    def test_unenrolled_admin_keeps_existing_access_by_default(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/admin/').status_code, 200)

    # ── pending-eligibility consistency (remediation) ────────────────────────

    def _admin_login(self, username, password=PASSWORD):
        return self.client.post('/admin/login/', {
            'username': username, 'password': password,
        }, REMOTE_ADDR='198.51.100.30')

    def test_cashier_with_mfa_is_challenged_on_app_login(self):
        _device, secret, _codes = self._enroll(self.cashier)
        response = self._login('mfa_cashier')
        self.assertEqual(response.url, self.CHALLENGE_URL)
        self.assertNotIn('_auth_user_id', self.client.session)
        completed = self._challenge(code=self._code(secret))
        self.assertEqual(completed.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_kitchen_staff_with_mfa_is_challenged_on_app_login(self):
        kitchen = _create_user('mfa_kitchen', 'kitchen_staff')
        _device, secret, _codes = self._enroll(kitchen)
        response = self._login('mfa_kitchen')
        self.assertEqual(response.url, self.CHALLENGE_URL)
        completed = self._challenge(code=self._code(secret))
        self.assertEqual(completed.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_superuser_with_customer_role_can_complete_admin_mfa(self):
        # Django Admin eligibility (is_staff/is_superuser) and the pending
        # challenge must agree; a role-label mismatch must not strand them.
        su = CustomUser.objects.create_superuser(
            username='mfa_super_customer', password=PASSWORD, role='customer')
        _device, secret, _codes = self._enroll(su)

        first = self._admin_login('mfa_super_customer')
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, self.CHALLENGE_URL)
        self.assertNotIn('_auth_user_id', self.client.session)

        second = self._challenge(code=self._code(secret))
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second.url, reverse('admin:index'))
        self.assertEqual(self.client.get('/admin/').status_code, 200)

    def test_ordinary_customer_cannot_use_staff_mfa(self):
        response = self._admin_login('mfa_customer')
        self.assertEqual(response.status_code, 200)   # rejected by admin form
        self.assertNotIn('mfa_pending', self.client.session)
        self.assertNotIn('_auth_user_id', self.client.session)

        # Customer login stays password-only.
        response = self._login('mfa_customer')
        self.assertEqual(response.url, reverse('menu:index'))
        self.assertIn('_auth_user_id', self.client.session)

    # ── 16-20: recovery codes ────────────────────────────────────────────────

    def test_recovery_code_works_once_and_reuse_is_rejected(self):
        _device, _secret, codes = self._enroll()
        self._login('mfa_admin')
        response = self._challenge(recovery=codes[0])
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)
        self.assertEqual(
            MFARecoveryCode.objects.filter(
                user=self.admin, used_at__isnull=False).count(), 1)

        self.client.logout()
        self._login('mfa_admin')
        reuse = self._challenge(recovery=codes[0])
        self.assertEqual(reuse.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_invalid_recovery_code_is_rejected(self):
        _device, _secret, _codes = self._enroll()
        self._login('mfa_admin')
        response = self._challenge(recovery='AAAA-BBBB-CCCC-DDDD')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_recovery_attempts_are_rate_limited(self):
        _device, secret, codes = self._enroll()
        self._login('mfa_admin')
        for _ in range(5):
            self._challenge(recovery='AAAA-BBBB-CCCC-DDDD')
        response = self._challenge(recovery=codes[0])
        self.assertEqual(response.status_code, 429)   # even the valid code waits
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_recovery_codes_are_hashed_at_rest(self):
        _device, _secret, codes = self._enroll()
        stored = list(MFARecoveryCode.objects.filter(user=self.admin))
        self.assertEqual(len(stored), mfa.unused_recovery_code_count(self.admin))
        for row in stored:
            normalized = mfa.normalize_recovery_code(codes[0])
            self.assertNotIn(normalized, row.code_hash)
            self.assertNotEqual(row.code_hash, normalized)
            identify_hasher(row.code_hash)          # a real password hash
        # None of the checks above consumed a code.
        self.assertEqual(
            MFARecoveryCode.objects.filter(
                user=self.admin, used_at__isnull=False).count(), 0)
        # A different user cannot use these codes.
        self.assertFalse(mfa.consume_recovery_code(self.cashier, codes[0]))

    # ── 21-23: disable / regenerate / audit ──────────────────────────────────

    def test_disable_requires_recent_verification_and_valid_code(self):
        _device, secret, _codes = self._enroll()
        self.client.force_login(self.admin)

        # No recent verification at all -> redirected to re-verify.
        response = self.client.post(
            reverse('accounts:mfa_disable'), {'code': self._code(secret)})
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.CHALLENGE_URL, response.url)
        self.assertTrue(StaffMFADevice.objects.filter(user=self.admin).exists())

        # Verified, but a wrong code -> still enabled.
        self._mark_session_verified()
        self.client.post(reverse('accounts:mfa_disable'),
                         {'code': self._code(secret, drift=5)})
        self.assertTrue(StaffMFADevice.objects.filter(user=self.admin).exists())

    def test_valid_code_disables_mfa_and_audits(self):
        _device, secret, _codes = self._enroll()
        self.client.force_login(self.admin)
        self._mark_session_verified()

        response = self.client.post(
            reverse('accounts:mfa_disable'), {'code': self._code(secret)})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(StaffMFADevice.objects.filter(user=self.admin).exists())
        self.assertFalse(MFARecoveryCode.objects.filter(user=self.admin).exists())
        self.assertNotIn('mfa_verified', self.client.session)
        self.assertTrue(AuditLog.objects.filter(action='mfa.disable').exists())
        # After disabling, login is password-only again.
        self.client.logout()
        response = self._login('mfa_admin')
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('mfa_pending', self.client.session)

    def test_regenerate_recovery_codes_requires_valid_code(self):
        _device, secret, old_codes = self._enroll()
        self.client.force_login(self.admin)
        self._mark_session_verified()

        self.client.post(
            reverse('accounts:mfa_regenerate_recovery_codes'),
            {'code': self._code(secret, drift=5)},
        )
        self.assertTrue(mfa.consume_recovery_code(self.admin, old_codes[0]))

        response = self.client.post(
            reverse('accounts:mfa_regenerate_recovery_codes'),
            {'code': self._code(secret, drift=0)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Recovery Codes')
        self.assertFalse(mfa.consume_recovery_code(self.admin, old_codes[1]))
        self.assertTrue(AuditLog.objects.filter(action='mfa.recovery.regenerate').exists())

    def test_unenrolled_staff_cannot_touch_other_users_mfa(self):
        _device, _secret, codes = self._enroll(self.admin)
        self.client.force_login(self.cashier)
        self._mark_session_verified(self.cashier)

        self.client.post(reverse('accounts:mfa_disable'), {'recovery_code': codes[0]})
        self.assertTrue(StaffMFADevice.objects.filter(user=self.admin).exists())
        self.assertFalse(
            MFARecoveryCode.objects.filter(user=self.admin, used_at__isnull=False).exists())

    def test_management_command_resets_mfa_and_audits(self):
        self._enroll(self.admin)
        call_command('mfa_reset', 'mfa_admin', '--yes')
        self.assertFalse(StaffMFADevice.objects.filter(user=self.admin).exists())
        self.assertFalse(MFARecoveryCode.objects.filter(user=self.admin).exists())
        log = AuditLog.objects.get(action='mfa.reset')
        self.assertIn('mfa_admin', log.detail)

    def test_audit_entries_never_contain_secrets_or_codes(self):
        _device, secret, codes = self._enroll()
        self.client.force_login(self.admin)
        self._mark_session_verified()
        self.client.post(reverse('accounts:mfa_disable'), {'code': self._code(secret)})

        entries = AuditLog.objects.filter(action__startswith='mfa.')
        self.assertTrue(entries.exists())
        blob = ' '.join(
            f'{e.detail} {e.object_repr}' for e in entries)
        self.assertNotIn(secret, blob)
        for code in codes:
            self.assertNotIn(code, blob)

    def test_password_reset_does_not_disable_mfa(self):
        self._enroll()
        uid = urlsafe_base64_encode(force_bytes(self.admin.pk))
        token = default_token_generator.make_token(self.admin)
        response = self.client.post(
            reverse('accounts:password_reset_confirm',
                    kwargs={'uidb64': uid, 'token': token}),
            {'new_password1': 'NewStrongPass!2026',
             'new_password2': 'NewStrongPass!2026'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(StaffMFADevice.objects.filter(user=self.admin).exists())
        # And the new password still requires MFA at login.
        response = self._login('mfa_admin', password='NewStrongPass!2026')
        self.assertEqual(response.url, self.CHALLENGE_URL)
        self.assertNotIn('_auth_user_id', self.client.session)

    # ── replay protection ────────────────────────────────────────────────────

    def test_totp_code_cannot_be_replayed(self):
        _device, secret, _codes = self._enroll()
        code = self._code(secret)
        self._login('mfa_admin')
        self.assertEqual(self._challenge(code=code).status_code, 302)
        self.client.logout()

        self._login('mfa_admin')
        replay = self._challenge(code=code)
        self.assertEqual(replay.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_totp_newer_step_accepted_and_older_step_rejected(self):
        device, secret, _codes = self._enroll()
        self.assertIsNotNone(
            mfa.verify_device_code(device, self._code(secret)))
        # A previous-step code is now below the consumed timestep.
        self.assertIsNone(
            mfa.verify_device_code(device, self._code(secret, drift=-1)))
        # The next step is still accepted.
        self.assertIsNotNone(
            mfa.verify_device_code(device, self._code(secret, drift=1)))

    def test_stale_device_cannot_consume_the_same_timestep(self):
        """The conditional UPDATE is the replay gate, not in-memory state.

        SQLite cannot express two real PostgreSQL transactions here, so this
        reproduces the race directly: two device objects are loaded before
        either consumes the step. Only the first conditional UPDATE matches;
        the stale second object must fail even though its in-memory
        ``last_used_timestep`` still allows the code.
        """
        _device, secret, _codes = self._enroll()
        code = self._code(secret)
        # Two independent loads of the same row, both before either consumes
        # the step (a reverse OneToOne access would return the same cached
        # object, so query the model directly).
        first = StaffMFADevice.objects.get(user=self.admin)
        second = StaffMFADevice.objects.get(user=self.admin)

        self.assertIsNotNone(mfa.verify_device_code(first, code))
        self.assertIsNone(mfa.verify_device_code(second, code))
        self.assertEqual(
            StaffMFADevice.objects.get(pk=first.pk).last_used_timestep,
            first.last_used_timestep,
        )

    def test_regeneration_rolls_back_when_creation_fails(self):
        _device, _secret, old_codes = self._enroll()
        with mock.patch.object(
            mfa, 'make_password', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                mfa.regenerate_recovery_codes(self.admin)

        self.assertEqual(
            MFARecoveryCode.objects.filter(user=self.admin).count(),
            len(old_codes),
        )
        self.assertTrue(mfa.consume_recovery_code(self.admin, old_codes[0]))

    def test_regenerated_codes_work_once(self):
        _device, _secret, old_codes = self._enroll()
        new_codes = mfa.regenerate_recovery_codes(self.admin)

        self.assertFalse(mfa.consume_recovery_code(self.admin, old_codes[0]))
        self.assertTrue(mfa.consume_recovery_code(self.admin, new_codes[0]))
        self.assertFalse(mfa.consume_recovery_code(self.admin, new_codes[0]))

    def test_management_command_revokes_only_the_target_sessions(self):
        self._enroll(self.admin)
        self.client.force_login(self.admin)
        target_key = self.client.session.session_key
        other_client = Client()
        other_client.force_login(self.cashier)
        other_key = other_client.session.session_key
        self.assertTrue(Session.objects.filter(session_key=target_key).exists())

        call_command('mfa_reset', 'mfa_admin', '--yes')

        self.assertFalse(Session.objects.filter(session_key=target_key).exists())
        self.assertTrue(Session.objects.filter(session_key=other_key).exists())

    @override_settings(
        SESSION_ENGINE='django.contrib.sessions.backends.signed_cookies')
    def test_session_revocation_reports_unsupported_backend(self):
        from apps.accounts.management.commands.mfa_reset import (
            revoke_user_sessions,
        )
        # Must not claim success for a backend it cannot enumerate.
        self.assertIsNone(revoke_user_sessions(self.admin))

    @mock.patch('builtins.input', return_value='n')
    def test_management_command_requires_confirmation(self, _input):
        self._enroll(self.admin)
        out = io.StringIO()
        call_command('mfa_reset', 'mfa_admin', stdout=out)
        self.assertIn('Aborted', out.getvalue())
        self.assertTrue(StaffMFADevice.objects.filter(user=self.admin).exists())
        self.assertTrue(MFARecoveryCode.objects.filter(user=self.admin).exists())

    def test_management_command_unknown_user_fails_safely(self):
        with self.assertRaises(CommandError):
            call_command('mfa_reset', 'no-such-user', '--yes')

    def test_management_command_preserves_password_role_and_active(self):
        self._enroll(self.admin)
        before = (self.admin.password, self.admin.role, self.admin.is_active)
        call_command('mfa_reset', 'mfa_admin', '--yes')
        self.admin.refresh_from_db()
        self.assertEqual(
            (self.admin.password, self.admin.role, self.admin.is_active), before)

    def test_management_command_never_prints_secrets_or_codes(self):
        _device, secret, codes = self._enroll(self.admin)
        out = io.StringIO()
        call_command('mfa_reset', 'mfa_admin', '--yes', stdout=out)
        printed = out.getvalue()
        self.assertNotIn(secret, printed)
        for code in codes:
            self.assertNotIn(code, printed)

    # ── test-only request adapter for session helpers ────────────────────────

    def _request_with_session(self):
        class _Request:
            pass
        request = _Request()
        request.session = self.client.session
        request.user = self.admin if '_auth_user_id' in self.client.session else None
        return request


class MFAEncryptionKeyTests(SimpleTestCase):
    """Finding A: production must configure MFA_ENCRYPTION_KEY explicitly.

    The test runner forces DEBUG=False, so these tests set DEBUG and the
    fallback switch explicitly rather than inheriting them.
    """

    SECRET = 'JBSWY3DPEHPK3PXP'
    VALID_KEY = Fernet.generate_key().decode()

    @staticmethod
    def _check():
        return checks.mfa_encryption_key_check(None)

    @override_settings(
        DEBUG=False, MFA_ENCRYPTION_KEY='', MFA_ALLOW_SECRET_KEY_FALLBACK=False)
    def test_missing_key_in_production_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            mfa.encrypt_secret(self.SECRET)
        errors = self._check()
        self.assertEqual([error.id for error in errors], ['accounts.E001'])

    @override_settings(
        DEBUG=False, MFA_ENCRYPTION_KEY=VALID_KEY,
        MFA_ALLOW_SECRET_KEY_FALLBACK=False)
    def test_configured_valid_key_works(self):
        token = mfa.encrypt_secret(self.SECRET)
        self.assertEqual(mfa.decrypt_secret(token), self.SECRET)
        self.assertEqual(self._check(), [])

    @override_settings(
        DEBUG=True, MFA_ENCRYPTION_KEY='', MFA_ALLOW_SECRET_KEY_FALLBACK=True)
    def test_development_fallback_still_works(self):
        token = mfa.encrypt_secret(self.SECRET)
        self.assertEqual(mfa.decrypt_secret(token), self.SECRET)
        self.assertEqual(self._check(), [])

    @override_settings(
        DEBUG=False, MFA_ENCRYPTION_KEY='', MFA_ALLOW_SECRET_KEY_FALLBACK=True)
    def test_fallback_outside_debug_is_a_warning(self):
        token = mfa.encrypt_secret(self.SECRET)
        self.assertEqual(mfa.decrypt_secret(token), self.SECRET)
        self.assertEqual([w.id for w in self._check()], ['accounts.W001'])

    @override_settings(
        DEBUG=False, MFA_ENCRYPTION_KEY='not-a-fernet-key',
        MFA_ALLOW_SECRET_KEY_FALLBACK=False)
    def test_invalid_key_is_rejected_clearly(self):
        with self.assertRaises(ImproperlyConfigured):
            mfa.encrypt_secret(self.SECRET)
        self.assertEqual([e.id for e in self._check()], ['accounts.E002'])
