"""
Regression tests for C-1A — demo seeding must never create accounts.

`sample_data/seed_data.py` (and the `seed_data` management command that runs
it) seeds menu categories, products and system settings only. No user account
is created, no credential is printed, and repeated runs remain idempotent.

All tests run against Django's isolated test database.
"""

from contextlib import redirect_stdout
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import CustomUser
from apps.dashboard.models import SystemSetting
from apps.menu.models import Category, Product


class SeedDataSafetyTests(TestCase):
    def _run_seed(self):
        output = StringIO()
        with redirect_stdout(output):
            call_command('seed_data', verbosity=0)
        return output.getvalue()

    def test_seed_creates_no_user_accounts(self):
        self._run_seed()

        self.assertEqual(CustomUser.objects.count(), 0)
        self.assertFalse(CustomUser.objects.filter(username='admin').exists())
        self.assertFalse(CustomUser.objects.filter(username='cashier1').exists())

    def test_seed_still_creates_demo_menu_data(self):
        self._run_seed()

        self.assertTrue(Category.objects.filter(name='Coffee').exists())
        self.assertTrue(Product.objects.filter(name='Mocha').exists())
        self.assertTrue(
            SystemSetting.objects.filter(key='PACKAGING_FEE_PER_ITEM').exists()
        )

    def test_seed_is_safe_to_repeat(self):
        self._run_seed()
        counts = (
            Category.objects.count(),
            Product.objects.count(),
            SystemSetting.objects.count(),
        )

        self._run_seed()

        self.assertEqual(
            (
                Category.objects.count(),
                Product.objects.count(),
                SystemSetting.objects.count(),
            ),
            counts,
        )

    def test_seed_output_contains_no_credentials(self):
        output = self._run_seed().lower()

        self.assertNotIn('login credentials', output)
        self.assertNotIn('password', output)
        self.assertNotIn('cashier', output)
        self.assertNotIn('admin /', output)
