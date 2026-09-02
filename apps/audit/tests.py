"""
Tests for the audit trail (apps.audit).

Verifies that every significant administrative action is recorded with the
authenticated user, timestamp, action and affected object, and that:

* normal operational flows (placing orders, browsing the menu, cart
  updates) are NOT logged -- only the chosen core admin actions are,
* sensitive data (passwords) is never stored in the audit trail,
* logging failures can never break the business action (best-effort).
"""

import json
from unittest import mock

from django.db import DatabaseError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.accounts.models import CustomUser
from apps.audit.admin import AuditLogAdmin
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.dashboard.models import SystemSetting
from apps.menu.models import Category, Product

PASSWORD = 'kdm-audit-pass-123'


def _create_user(username, role='admin'):
    return CustomUser.objects.create_user(
        username=username, password=PASSWORD, role=role,
    )


class AuditServiceTests(TestCase):
    """The log_action() service itself."""

    def test_log_action_records_user_action_timestamp_and_object(self):
        admin = _create_user('audit_svc')
        category = Category.objects.create(name='Coffee', slug='coffee')
        log_action(admin, 'category.create', category)
        entry = AuditLog.objects.get()
        self.assertEqual(entry.user, admin)
        self.assertEqual(entry.action, 'category.create')
        self.assertEqual(entry.object_type, 'category')
        self.assertEqual(entry.object_id, str(category.pk))
        self.assertIn('Coffee', entry.object_repr)
        self.assertIsNotNone(entry.created_at)

    def test_log_action_without_object_uses_explicit_values(self):
        admin = _create_user('audit_svc')
        log_action(admin, 'settings.update',
                   object_type='SystemSetting', object_repr='System Settings')
        entry = AuditLog.objects.get()
        self.assertEqual(entry.object_type, 'SystemSetting')
        self.assertEqual(entry.object_id, '')
        self.assertEqual(entry.object_repr, 'System Settings')

    def test_log_action_with_anonymous_user_stores_null_user(self):
        log_action(None, 'test.action', object_repr='x')
        entry = AuditLog.objects.get()
        self.assertIsNone(entry.user)

    def test_log_action_never_raises_when_database_fails(self):
        with mock.patch.object(
            AuditLog.objects, 'create', side_effect=DatabaseError('db down'),
        ), self.assertLogs('apps.audit', level='ERROR'):
            log_action(None, 'test.action')  # must not raise


class AuditAdminReadOnlyTests(TestCase):
    """The audit trail is append-only: the admin site cannot alter it."""

    def test_admin_permissions_are_all_read_only(self):
        from django.contrib.admin.sites import AdminSite

        admin_user = _create_user('audit_admin', 'admin')
        request = RequestFactory().get('/admin/apps/audit/auditlog/')
        request.user = admin_user
        model_admin = AuditLogAdmin(AuditLog, AdminSite())

        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(
            model_admin.has_change_permission(request, obj=AuditLog())
        )
        self.assertFalse(
            model_admin.has_delete_permission(request, obj=AuditLog())
        )


class StaffAuditTests(TestCase):
    """Staff account management is logged."""

    def setUp(self):
        self.admin = _create_user('audit_admin', 'admin')
        self.client.force_login(self.admin)

    def test_staff_create_is_logged_and_never_stores_password(self):
        response = self.client.post('/accounts/staff/create/', {
            'username': 'new_cashier', 'first_name': 'New', 'last_name': 'Cashier',
            'email': 'cashier@example.com', 'phone': '', 'role': 'cashier',
            'password1': 'cashier-secret-123', 'password2': 'cashier-secret-123',
        })
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action='staff.create')
        self.assertEqual(entry.user, self.admin)
        self.assertEqual(entry.object_type, 'customuser')
        self.assertIn('cashier', entry.detail)
        # Sensitive data must never end up in the trail.
        self.assertNotIn('cashier-secret-123', entry.object_repr)
        self.assertNotIn('cashier-secret-123', entry.detail)

    def test_staff_toggle_is_logged(self):
        cashier = _create_user('toggle_cashier', 'cashier')
        self.client.post(f'/accounts/staff/{cashier.pk}/toggle/')
        deactivate = AuditLog.objects.get(action='staff.deactivate')
        self.assertEqual(deactivate.user, self.admin)
        self.assertEqual(deactivate.object_id, str(cashier.pk))
        self.client.post(f'/accounts/staff/{cashier.pk}/toggle/')
        self.assertTrue(
            AuditLog.objects.filter(
                action='staff.activate', object_id=str(cashier.pk),
            ).exists()
        )


class MenuAuditTests(TestCase):
    """Product and category management is logged."""

    def setUp(self):
        self.admin = _create_user('audit_admin', 'admin')
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')

    def _product_data(self, **overrides):
        data = {
            'category': self.category.pk, 'name': 'Iced Coffee', 'description': '',
            'price': '60.00', 'price_medium': '', 'price_large': '', 'price_hot': '',
            'has_sizes': '', 'is_available': 'on', 'is_featured': '',
            'stock_quantity': '50', 'low_stock_threshold': '10',
        }
        data.update(overrides)
        return data

    def test_product_create_is_logged(self):
        response = self.client.post('/manage/products/create/', self._product_data())
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(name='Iced Coffee')
        entry = AuditLog.objects.get(action='product.create')
        self.assertEqual(entry.user, self.admin)
        self.assertEqual(entry.object_type, 'product')
        self.assertEqual(entry.object_id, str(product.pk))
        self.assertIn('Iced Coffee', entry.object_repr)

    def test_product_update_is_logged(self):
        product = Product.objects.create(
            category=self.category, name='Hot Coffee', price='50.00',
        )
        response = self.client.post(
            f'/manage/products/{product.pk}/edit/',
            self._product_data(name='Hot Coffee', price='55.00'),
        )
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action='product.update')
        self.assertEqual(entry.object_id, str(product.pk))
        self.assertEqual(entry.user, self.admin)

    def test_product_deactivate_reactivate_are_logged(self):
        product = Product.objects.create(
            category=self.category, name='To Deactivate', price='10.00',
        )
        response = self.client.post(
            f'/manage/products/{product.pk}/toggle-active/',
        )
        self.assertEqual(response.status_code, 200)
        entry = AuditLog.objects.get(action='product.deactivate')
        self.assertEqual(entry.object_id, str(product.pk))
        self.assertEqual(entry.user, self.admin)
        product.refresh_from_db()
        self.assertFalse(product.is_active)

        response = self.client.post(
            f'/manage/products/{product.pk}/toggle-active/',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(
            action='product.reactivate', object_id=str(product.pk),
        ).exists())
        product.refresh_from_db()
        self.assertTrue(product.is_active)

    def test_product_availability_toggle_is_logged(self):
        product = Product.objects.create(
            category=self.category, name='To Toggle', price='10.00',
        )
        response = self.client.post(f'/manage/products/{product.pk}/toggle/')
        self.assertEqual(response.status_code, 200)
        entry = AuditLog.objects.get(action='product.availability')
        self.assertEqual(entry.object_id, str(product.pk))

    def test_category_create_edit_delete_are_logged(self):
        # Create
        response = self.client.post('/manage/categories/create/', {
            'name': 'Snacks', 'icon': 'fries', 'description': '',
            'is_active': 'on', 'is_packaging_required': 'on', 'order': '3',
        })
        self.assertEqual(response.status_code, 302)
        category = Category.objects.get(name='Snacks')
        self.assertTrue(AuditLog.objects.filter(
            action='category.create', object_id=str(category.pk),
        ).exists())

        # Edit
        response = self.client.post(f'/manage/categories/{category.pk}/edit/', {
            'name': 'Snacks', 'icon': 'fries', 'description': 'Crispy!',
            'is_active': 'on', 'is_packaging_required': 'on', 'order': '3',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(
            action='category.update', object_id=str(category.pk),
        ).exists())

        # Delete
        response = self.client.post(f'/manage/categories/{category.pk}/delete/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(
            action='category.delete', object_id=str(category.pk),
        ).exists())


class InventoryAuditTests(TestCase):
    """Inventory restocking is logged."""

    def setUp(self):
        self.admin = _create_user('audit_admin', 'admin')
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Milk Tea', price='70.00',
            stock_quantity=10,
        )

    def test_restock_is_logged_with_before_after_quantities(self):
        response = self.client.post(
            f'/inventory/{self.product.pk}/restock/',
            {'quantity': '15', 'notes': 'delivery'},
        )
        self.assertEqual(response.status_code, 200)
        entry = AuditLog.objects.get(action='inventory.restock')
        self.assertEqual(entry.user, self.admin)
        self.assertEqual(entry.object_id, str(self.product.pk))
        self.assertIn('+15', entry.detail)
        self.assertIn('10 -> 25', entry.detail)


class FinanceAuditTests(TestCase):
    """Daily finance record creation and updates are logged."""

    def setUp(self):
        self.admin = _create_user('audit_admin', 'admin')
        self.client.force_login(self.admin)

    def _finance_data(self, **overrides):
        data = {
            'date': timezone.now().date().isoformat(),
            'previous_coh': '1000.00',
            'expenses': '0.00', 'expenses_notes': '',
            'gcash_payments': '0.00', 'coins': '0.00',
            'cash_advance': '0.00', 'floating_cash': '0.00',
        }
        data.update(overrides)
        return data

    def test_finance_create_is_logged(self):
        response = self.client.post('/finance/', self._finance_data())
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action='finance.create')
        self.assertEqual(entry.user, self.admin)
        self.assertEqual(entry.object_type, 'dailyfinance')

    def test_finance_update_is_logged(self):
        self.client.post('/finance/', self._finance_data())
        response = self.client.post('/finance/', self._finance_data(expenses='50.00'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(action='finance.update').exists())
        # One create + one update -- no duplicates.
        self.assertEqual(
            AuditLog.objects.filter(action='finance.create').count(), 1,
        )


class SettingsAuditTests(TestCase):
    """System settings changes are logged."""

    def setUp(self):
        self.admin = _create_user('audit_admin', 'admin')
        self.client.force_login(self.admin)

    def test_settings_update_is_logged(self):
        response = self.client.post(
            '/dashboard/settings/', {'value_PACKAGING_FEE_PER_ITEM': '7.50'},
        )
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action='settings.update')
        self.assertEqual(entry.user, self.admin)
        self.assertIn('PACKAGING_FEE_PER_ITEM', entry.detail)
        self.assertEqual(
            SystemSetting.objects.get(key='PACKAGING_FEE_PER_ITEM').value, '7.50',
        )

    def test_submitting_unchanged_values_is_not_logged(self):
        SystemSetting.objects.get_or_create(
            key='PACKAGING_FEE_PER_ITEM', defaults={'value': '6.00'},
        )
        response = self.client.post(
            '/dashboard/settings/', {'value_PACKAGING_FEE_PER_ITEM': '6.00'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            AuditLog.objects.filter(action='settings.update').exists()
        )


class ScopeBoundaryTests(TestCase):
    """Normal operational flows must not pollute the audit trail."""

    def setUp(self):
        self.admin = _create_user('audit_admin', 'admin')
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Americano', price='50.00',
            stock_quantity=100,
        )

    def test_pos_order_creation_is_not_logged(self):
        response = self.client.post(
            '/orders/pos/create/',
            json.dumps({
                'customer_name': 'Walk-in Customer',
                'items': [{'product_id': self.product.pk, 'quantity': 1, 'size': 'none'}],
                'order_type': 'dine_in', 'table_number': '', 'notes': '',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_public_menu_and_cart_flows_are_not_logged(self):
        self.client.get('/')
        self.client.post(f'/orders/cart/add/{self.product.pk}/', {
            'quantity': '1', 'size': 'none',
        })
        self.assertEqual(AuditLog.objects.count(), 0)
