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
            'email': 'new@example.com', 'phone': '', 'role': 'cashier',
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
