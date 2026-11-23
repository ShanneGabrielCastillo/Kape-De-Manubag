"""
Regression tests for C-1A — environment-driven superuser provisioning.

`manage.py create_superuser_env` must skip safely when required variables are
missing, reject passwords that fail Django's configured validators, create an
explicit administrator-role superuser, never overwrite an existing user, and
never print the password.

All tests run against Django's isolated test database.
"""

import os
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.accounts.models import CustomUser

_ENV_KEYS = (
    'DJANGO_SUPERUSER_USERNAME',
    'DJANGO_SUPERUSER_EMAIL',
    'DJANGO_SUPERUSER_PASSWORD',
)


def _env(**values):
    """Patch the superuser env vars, defaulting every unspecified key to ''."""
    data = {key: '' for key in _ENV_KEYS}
    data.update(values)
    return mock.patch.dict(os.environ, data, clear=False)


class CreateSuperuserEnvTests(TestCase):
    def _run(self):
        output = StringIO()
        call_command('create_superuser_env', stdout=output)
        return output.getvalue()

    def test_skips_when_username_and_password_are_missing(self):
        with _env():
            output = self._run()

        self.assertEqual(CustomUser.objects.count(), 0)
        self.assertIn('skipping', output.lower())

    def test_skips_when_only_username_is_set(self):
        with _env(DJANGO_SUPERUSER_USERNAME='no_password_admin'):
            output = self._run()

        self.assertEqual(CustomUser.objects.count(), 0)
        self.assertIn('skipping', output.lower())

    def test_rejects_weak_password(self):
        weak_password = '12345678'

        with _env(
            DJANGO_SUPERUSER_USERNAME='weak_env_admin',
            DJANGO_SUPERUSER_PASSWORD=weak_password,
        ):
            with self.assertRaises(CommandError) as ctx:
                self._run()

        self.assertEqual(CustomUser.objects.count(), 0)
        self.assertNotIn(weak_password, str(ctx.exception))

    def test_valid_credentials_create_administrator_role_superuser(self):
        password = 'Env-Guard-Str0ng!2026'

        with _env(
            DJANGO_SUPERUSER_USERNAME='env_admin',
            DJANGO_SUPERUSER_EMAIL='env-admin@example.com',
            DJANGO_SUPERUSER_PASSWORD=password,
        ):
            output = self._run()

        user = CustomUser.objects.get(username='env_admin')
        self.assertEqual(user.role, 'admin')
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password(password))
        self.assertIn('env_admin', output)
        self.assertNotIn(password, output)

    def test_email_is_optional(self):
        password = 'Env-Guard-Str0ng!2026'

        with _env(
            DJANGO_SUPERUSER_USERNAME='env_admin_no_email',
            DJANGO_SUPERUSER_PASSWORD=password,
        ):
            self._run()

        user = CustomUser.objects.get(username='env_admin_no_email')
        self.assertEqual(user.email, '')

    def test_existing_user_is_not_overwritten_or_reset(self):
        existing = CustomUser.objects.create_user(
            username='existing_env_admin',
            password='Existing-Guard!2026',
            role='cashier',
        )
        original_password_hash = existing.password
        attempted_password = 'Different-Guard!2026'

        with _env(
            DJANGO_SUPERUSER_USERNAME='existing_env_admin',
            DJANGO_SUPERUSER_PASSWORD=attempted_password,
        ):
            output = self._run()

        existing.refresh_from_db()
        self.assertEqual(CustomUser.objects.count(), 1)
        self.assertEqual(existing.role, 'cashier')
        self.assertFalse(existing.is_superuser)
        self.assertFalse(existing.is_staff)
        self.assertEqual(existing.password, original_password_hash)
        self.assertTrue(existing.check_password('Existing-Guard!2026'))
        self.assertFalse(existing.check_password(attempted_password))
        self.assertIn('already exists', output.lower())
        self.assertNotIn(attempted_password, output)
