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


# ── Order Audit Tests ─────────────────────────────────────────────────────────


class OrderAuditTests(TestCase):
    """Order payment, cancellation and status changes are logged."""

    def setUp(self):
        self.admin = _create_user('order_audit_admin', 'admin')
        self.cashier = _create_user('order_audit_cashier', 'cashier')
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Drinks', slug='drinks-oa')
        self.product = Product.objects.create(
            category=self.category, name='Latte', price='60.00',
            stock_quantity=50,
        )

    def _create_order(self):
        """Create a minimal pending POS order for the cashier."""
        response = self.client.post(
            '/orders/pos/create/',
            json.dumps({
                'customer_name': 'Walk-in Customer',
                'items': [{'product_id': self.product.pk, 'quantity': 1, 'size': 'none'}],
                'order_type': 'dine_in', 'table_number': '', 'notes': '',
            }),
            content_type='application/json',
        )
        self.assertTrue(response.json()['success'])
        from apps.orders.models import Order
        return Order.objects.get(pk=response.json()['order_id'])

    def test_payment_creates_exactly_one_audit_entry(self):
        order = self._create_order()
        AuditLog.objects.all().delete()  # clear order-creation entries
        response = self.client.post(
            f'/orders/manage/{order.pk}/payment/',
            {'amount_paid': '100.00', 'payment_method': 'cash'},
        )
        self.assertTrue(response.json()['success'])
        self.assertEqual(AuditLog.objects.filter(action='order.payment').count(), 1)
        # No duplicate entries from signals or other paths.
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_payment_audit_detail_contains_method_and_amounts(self):
        order = self._create_order()
        AuditLog.objects.all().delete()
        self.client.post(
            f'/orders/manage/{order.pk}/payment/',
            {'amount_paid': '100.00', 'payment_method': 'cash'},
        )
        entry = AuditLog.objects.get(action='order.payment')
        self.assertIn('Cash', entry.detail)
        self.assertIn('₱', entry.detail)

    def test_payment_audit_detail_contains_no_sensitive_data(self):
        order = self._create_order()
        AuditLog.objects.all().delete()
        self.client.post(
            f'/orders/manage/{order.pk}/payment/',
            {'amount_paid': '100.00', 'payment_method': 'cash'},
        )
        entry = AuditLog.objects.get(action='order.payment')
        # Passwords and tokens must never appear in audit entries.
        self.assertNotIn('password', entry.detail.lower())
        self.assertNotIn('token', entry.detail.lower())
        self.assertNotIn('secret', entry.detail.lower())

    def test_failed_payment_insufficient_amount_creates_no_audit_entry(self):
        order = self._create_order()
        AuditLog.objects.all().delete()
        response = self.client.post(
            f'/orders/manage/{order.pk}/payment/',
            {'amount_paid': '1.00', 'payment_method': 'cash'},
        )
        self.assertFalse(response.json()['success'])
        self.assertEqual(AuditLog.objects.filter(action='order.payment').count(), 0)

    def test_status_change_creates_exactly_one_audit_entry(self):
        order = self._create_order()
        AuditLog.objects.all().delete()
        response = self.client.post(
            f'/orders/manage/{order.pk}/status/',
            {'status': 'preparing'},
        )
        self.assertTrue(response.json()['success'])
        self.assertEqual(
            AuditLog.objects.filter(action='order.status_changed').count(), 1,
        )
        entry = AuditLog.objects.get(action='order.status_changed')
        self.assertIn('pending', entry.detail)
        self.assertIn('preparing', entry.detail)

    def test_cancel_creates_order_cancel_not_order_status_changed(self):
        order = self._create_order()
        AuditLog.objects.all().delete()
        response = self.client.post(
            f'/orders/manage/{order.pk}/status/',
            {'status': 'cancelled'},
        )
        self.assertTrue(response.json()['success'])
        self.assertEqual(AuditLog.objects.filter(action='order.cancel').count(), 1)
        self.assertEqual(
            AuditLog.objects.filter(action='order.status_changed').count(), 0,
        )

    def test_invalid_status_transition_creates_no_audit_entry(self):
        order = self._create_order()
        AuditLog.objects.all().delete()
        # pending → completed is not a valid transition.
        response = self.client.post(
            f'/orders/manage/{order.pk}/status/',
            {'status': 'completed'},
        )
        self.assertFalse(response.json()['success'])
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_quick_advance_creates_one_status_changed_entry(self):
        order = self._create_order()
        AuditLog.objects.all().delete()
        response = self.client.post(f'/orders/manage/{order.pk}/advance/')
        self.assertTrue(response.json()['success'])
        self.assertEqual(
            AuditLog.objects.filter(action='order.status_changed').count(), 1,
        )
        entry = AuditLog.objects.get(action='order.status_changed')
        self.assertIn('pending', entry.detail)
        self.assertIn('preparing', entry.detail)


# ── Activity Log View Tests ───────────────────────────────────────────────────


class ActivityLogViewTests(TestCase):
    """The /audit/ Activity Log page — access control and UI behavior."""

    def setUp(self):
        self.admin = _create_user('view_admin', 'admin')
        self.cashier = _create_user('view_cashier', 'cashier')
        self.category = Category.objects.create(name='Coffee', slug='coffee-vt')

    def test_admin_can_access_activity_log(self):
        self.client.force_login(self.admin)
        response = self.client.get('/audit/')
        self.assertEqual(response.status_code, 200)

    def test_cashier_cannot_access_activity_log(self):
        self.client.force_login(self.cashier)
        response = self.client.get('/audit/')
        # @admin_required redirects non-admin to dashboard.
        self.assertIn(response.status_code, [302, 403])

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get('/audit/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_page_shows_entries_newest_first(self):
        self.client.force_login(self.admin)
        cat = Category.objects.get(slug='coffee-vt')
        log_action(self.admin, 'category.create', cat, detail='first')
        log_action(self.admin, 'category.update', cat, detail='second')
        response = self.client.get('/audit/')
        self.assertEqual(response.status_code, 200)
        logs = list(response.context['logs'])
        self.assertEqual(logs[0].detail, 'second')
        self.assertEqual(logs[1].detail, 'first')

    def test_search_filters_by_username(self):
        self.client.force_login(self.admin)
        cat = Category.objects.get(slug='coffee-vt')
        log_action(self.admin, 'category.create', cat)
        log_action(self.cashier, 'category.update', cat)
        response = self.client.get('/audit/', {'q': 'view_admin'})
        self.assertEqual(response.status_code, 200)
        logs = list(response.context['logs'])
        self.assertTrue(all(e.user == self.admin for e in logs))

    def test_search_filters_by_detail(self):
        self.client.force_login(self.admin)
        log_action(self.admin, 'settings.update', detail='unique_detail_xyz')
        log_action(self.admin, 'settings.update', detail='other_detail')
        response = self.client.get('/audit/', {'q': 'unique_detail_xyz'})
        logs = list(response.context['logs'])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].detail, 'unique_detail_xyz')

    def test_category_filter_shows_only_matching_actions(self):
        self.client.force_login(self.admin)
        cat = Category.objects.get(slug='coffee-vt')
        log_action(self.admin, 'order.payment', object_repr='Order #001')
        log_action(self.admin, 'category.create', cat)
        response = self.client.get('/audit/', {'category': 'orders'})
        logs = list(response.context['logs'])
        self.assertTrue(all(e.action.startswith('order.') for e in logs))

    def test_user_filter_works(self):
        self.client.force_login(self.admin)
        cat = Category.objects.get(slug='coffee-vt')
        log_action(self.admin, 'category.create', cat)
        log_action(self.cashier, 'category.update', cat)
        response = self.client.get('/audit/', {'user': str(self.cashier.pk)})
        logs = list(response.context['logs'])
        self.assertTrue(all(e.user == self.cashier for e in logs))

    def test_date_filter_works(self):
        self.client.force_login(self.admin)
        from django.utils import timezone as tz
        today_str = tz.localdate().isoformat()
        log_action(self.admin, 'settings.update', detail='today_entry')
        response = self.client.get('/audit/', {
            'date_from': today_str, 'date_to': today_str,
        })
        self.assertEqual(response.status_code, 200)
        logs = list(response.context['logs'])
        self.assertTrue(len(logs) >= 1)

    def test_pagination_50_per_page(self):
        self.client.force_login(self.admin)
        # Create 55 entries.
        for i in range(55):
            log_action(self.admin, 'settings.update', detail=f'entry {i}')
        response = self.client.get('/audit/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 50)
        response2 = self.client.get('/audit/', {'page': '2'})
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(len(response2.context['logs']), 5)

    def test_empty_state_renders(self):
        self.client.force_login(self.admin)
        AuditLog.objects.all().delete()
        response = self.client.get('/audit/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No activity logs')

    def test_category_label_attached_to_entries(self):
        """Each log entry should have category_label set by the view."""
        self.client.force_login(self.admin)
        log_action(self.admin, 'order.payment', object_repr='Order #001')
        response = self.client.get('/audit/')
        logs = list(response.context['logs'])
        payment_entry = next(e for e in logs if e.action == 'order.payment')
        self.assertEqual(payment_entry.category_label, 'orders')


# ── Regression Tests ──────────────────────────────────────────────────────────


class RegressionTests(TestCase):
    """Existing functionality is not broken by the Activity Log feature."""

    def setUp(self):
        self.admin = _create_user('regression_admin', 'admin')
        self.cashier = _create_user('regression_cashier', 'cashier')
        self.category = Category.objects.create(name='Drinks', slug='drinks-rt')
        self.product = Product.objects.create(
            category=self.category, name='Americano', price='55.00',
            stock_quantity=100,
        )

    def test_existing_inventory_log_still_works_after_restock(self):
        from apps.inventory.models import InventoryLog
        self.client.force_login(self.admin)
        response = self.client.post(
            f'/inventory/{self.product.pk}/restock/',
            {'quantity': '10', 'notes': 'test restock'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        # Both InventoryLog and AuditLog entries must exist.
        self.assertTrue(
            InventoryLog.objects.filter(
                product=self.product, action='restock',
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action='inventory.restock').exists()
        )

    def test_customer_order_stock_deduction_creates_no_order_audit_entries(self):
        """POS order creation is intentionally not logged per scope."""
        self.client.force_login(self.admin)
        AuditLog.objects.all().delete()
        response = self.client.post(
            '/orders/pos/create/',
            json.dumps({
                'customer_name': 'Walk-in',
                'items': [{'product_id': self.product.pk, 'quantity': 1, 'size': 'none'}],
                'order_type': 'dine_in', 'table_number': '', 'notes': '',
            }),
            content_type='application/json',
        )
        self.assertTrue(response.json()['success'])
        self.assertEqual(
            AuditLog.objects.filter(action__startswith='order.').count(), 0,
        )

    def test_payment_processing_still_works_functionally(self):
        self.client.force_login(self.cashier)
        r = self.client.post(
            '/orders/pos/create/',
            json.dumps({
                'customer_name': 'Walk-in',
                'items': [{'product_id': self.product.pk, 'quantity': 1, 'size': 'none'}],
                'order_type': 'dine_in', 'table_number': '', 'notes': '',
            }),
            content_type='application/json',
        )
        from apps.orders.models import Order
        order = Order.objects.get(pk=r.json()['order_id'])
        response = self.client.post(
            f'/orders/manage/{order.pk}/payment/',
            {'amount_paid': '100.00', 'payment_method': 'cash'},
        )
        self.assertTrue(response.json()['success'])
        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.status, 'completed')

    def test_cancel_order_restores_inventory_and_creates_cancel_audit(self):
        self.client.force_login(self.cashier)
        original_stock = self.product.stock_quantity
        r = self.client.post(
            '/orders/pos/create/',
            json.dumps({
                'customer_name': 'Walk-in',
                'items': [{'product_id': self.product.pk, 'quantity': 2, 'size': 'none'}],
                'order_type': 'dine_in', 'table_number': '', 'notes': '',
            }),
            content_type='application/json',
        )
        from apps.orders.models import Order
        order = Order.objects.get(pk=r.json()['order_id'])
        AuditLog.objects.all().delete()
        self.client.post(
            f'/orders/manage/{order.pk}/status/',
            {'status': 'cancelled'},
        )
        # Stock must be restored.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, original_stock)
        # Exactly one order.cancel audit entry.
        self.assertEqual(AuditLog.objects.filter(action='order.cancel').count(), 1)

    def test_inventory_stock_movements_not_duplicated_in_audit_log(self):
        """InventoryLog records stock movements; AuditLog only records restock actions."""
        from apps.inventory.models import InventoryLog
        self.client.force_login(self.cashier)
        initial_stock = self.product.stock_quantity
        # Place an order — deducts stock.
        r = self.client.post(
            '/orders/pos/create/',
            json.dumps({
                'customer_name': 'Walk-in',
                'items': [{'product_id': self.product.pk, 'quantity': 3, 'size': 'none'}],
                'order_type': 'dine_in', 'table_number': '', 'notes': '',
            }),
            content_type='application/json',
        )
        self.assertTrue(r.json()['success'])
        # InventoryLog has the sale deduction — AuditLog does NOT.
        self.assertTrue(
            InventoryLog.objects.filter(action='sale').exists()
        )
        self.assertEqual(
            AuditLog.objects.filter(action__startswith='order.').count(), 0,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, initial_stock - 3)
