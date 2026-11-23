"""
Regression tests for the legacy finished-product stock exception in C-3.

`Product.stock_quantity` is the legacy finished-product balance. The authorized
write path for it is the staff product form (`menu:product_edit`), which records
an `InventoryLog` row through `_log_inventory_update()`. The Django Admin has no
equivalent logging path, so `ProductAdmin` keeps `stock_quantity` and
`low_stock_threshold` out of the bound form for every product.

These tests exercise the admin over HTTP with crafted POSTs and assert persisted
values; they also prove the staff workflow still updates stock and still writes
the expected ledger row.

All tests use Django's isolated test database.
"""

from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.inventory.models import InventoryLog
from apps.menu.admin import ProductAdmin
from apps.menu.models import Category, Product

PASSWORD = 'legacy-stock-guard-pass'


# ── Helpers ───────────────────────────────────────────────────────────────────

def _superuser(username='legacy_guard_root'):
    return get_user_model().objects.create_superuser(
        username=username, password=PASSWORD, role='admin',
    )


def _staff(username='legacy_guard_staff'):
    return get_user_model().objects.create_user(
        username=username, password=PASSWORD, role='admin',
    )


def _category(name='Legacy Guard Drinks'):
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _product(category, *, mode, name, stock=50, threshold=10):
    return Product.objects.create(
        category=category,
        name=name,
        price=Decimal('55.00'),
        stock_quantity=stock,
        low_stock_threshold=threshold,
        inventory_tracking_mode=mode,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. ProductAdmin — stock fields are server-side read-only
# ═════════════════════════════════════════════════════════════════════════════

class LegacyProductAdminStockTests(TestCase):
    def setUp(self):
        self.root = _superuser()
        self.client.force_login(self.root)
        self.category = _category()
        self.product = _product(
            self.category,
            mode=Product.INVENTORY_TRACKING_LEGACY,
            name='Legacy Mocha',
            stock=92,
            threshold=10,
        )
        self.url = reverse(
            'admin:menu_product_change', args=[self.product.pk])

    def _admin_request(self):
        request = RequestFactory().get('/admin/menu/product/')
        request.user = self.root
        return request

    def _payload(self, **overrides):
        data = {
            'category': self.category.pk,
            'name': self.product.name,
            'slug': self.product.slug,
            'description': '',
            'price': '55.00',
            'price_medium': '',
            'price_large': '',
            'price_hot': '',
            'has_sizes': '',
            'is_available': 'on',
            'is_active': 'on',
            'is_featured': '',
            'stock_quantity': str(self.product.stock_quantity),
            'low_stock_threshold': str(self.product.low_stock_threshold),
        }
        data.update(overrides)
        return data

    # ── Form shape ────────────────────────────────────────────────────────────

    def test_legacy_stock_quantity_is_read_only_in_admin_form(self):
        model_admin = ProductAdmin(Product, AdminSite())

        readonly = model_admin.get_readonly_fields(
            self._admin_request(), self.product)
        self.assertIn('stock_quantity', readonly)

        form_class = model_admin.get_form(
            self._admin_request(), obj=self.product)
        self.assertNotIn('stock_quantity', form_class.base_fields)

    def test_legacy_low_stock_threshold_is_read_only_in_admin_form(self):
        model_admin = ProductAdmin(Product, AdminSite())

        readonly = model_admin.get_readonly_fields(
            self._admin_request(), self.product)
        self.assertIn('low_stock_threshold', readonly)

        form_class = model_admin.get_form(
            self._admin_request(), obj=self.product)
        self.assertNotIn('low_stock_threshold', form_class.base_fields)

    # ── Crafted POSTs ─────────────────────────────────────────────────────────

    def test_crafted_post_cannot_change_legacy_stock_quantity(self):
        logs_before = InventoryLog.objects.count()

        response = self.client.post(self.url, self._payload(
            stock_quantity='1',
            name='Legacy Mocha Renamed',
        ))

        # 302 proves the form validated and saved; the stock field was ignored.
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 92)
        self.assertEqual(self.product.name, 'Legacy Mocha Renamed')
        self.assertEqual(InventoryLog.objects.count(), logs_before)

    def test_crafted_post_cannot_change_legacy_low_stock_threshold(self):
        response = self.client.post(self.url, self._payload(
            low_stock_threshold='99',
            price='61.00',
        ))

        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.low_stock_threshold, 10)
        self.assertEqual(self.product.price, Decimal('61.00'))

    def test_permitted_metadata_still_saves(self):
        response = self.client.post(self.url, self._payload(
            name='Legacy Mocha Grande',
            description='Now with more cocoa.',
            price='70.00',
            is_featured='on',
        ))

        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Legacy Mocha Grande')
        self.assertEqual(self.product.description, 'Now with more cocoa.')
        self.assertEqual(self.product.price, Decimal('70.00'))
        self.assertTrue(self.product.is_featured)
        # Stock and mode are untouched.
        self.assertEqual(self.product.stock_quantity, 92)
        self.assertEqual(self.product.low_stock_threshold, 10)
        self.assertEqual(
            self.product.inventory_tracking_mode,
            Product.INVENTORY_TRACKING_LEGACY,
        )

    def test_ingredient_tracked_admin_behavior_unchanged(self):
        ingredient = _product(
            self.category,
            mode=Product.INVENTORY_TRACKING_INGREDIENT,
            name='Ingredient Latte',
            stock=7,
            threshold=5,
        )
        url = reverse('admin:menu_product_change', args=[ingredient.pk])

        response = self.client.post(url, {
            'category': self.category.pk,
            'name': 'Ingredient Latte Renamed',
            'slug': ingredient.slug,
            'description': '',
            'price': '55.00',
            'price_medium': '',
            'price_large': '',
            'price_hot': '',
            'has_sizes': '',
            'is_available': 'on',
            'is_active': 'on',
            'is_featured': '',
            'stock_quantity': '999',
            'low_stock_threshold': '99',
        })

        self.assertEqual(response.status_code, 302)
        ingredient.refresh_from_db()
        self.assertEqual(ingredient.name, 'Ingredient Latte Renamed')
        self.assertEqual(ingredient.stock_quantity, 7)
        self.assertEqual(ingredient.low_stock_threshold, 5)
        self.assertEqual(
            ingredient.inventory_tracking_mode,
            Product.INVENTORY_TRACKING_INGREDIENT,
        )


# ═════════════════════════════════════════════════════════════════════════════
# 2. Staff product form — the authorized legacy stock workflow
# ═════════════════════════════════════════════════════════════════════════════

class LegacyStaffStockWorkflowTests(TestCase):
    def setUp(self):
        self.staff = _staff()
        self.client.force_login(self.staff)
        self.category = _category('Legacy Staff Drinks')
        self.product = _product(
            self.category,
            mode=Product.INVENTORY_TRACKING_LEGACY,
            name='Staff Legacy Mocha',
            stock=10,
            threshold=10,
        )
        self.url = reverse('menu:product_edit', args=[self.product.pk])

    def _payload(self, **overrides):
        data = {
            'category': self.category.pk,
            'name': self.product.name,
            'description': '',
            'price': '45.00',
            'stock_quantity': str(self.product.stock_quantity),
            'low_stock_threshold': str(self.product.low_stock_threshold),
            'has_sizes': '',
            'is_available': 'on',
            'is_featured': '',
        }
        data.update(overrides)
        return data

    def test_staff_form_still_updates_legacy_stock(self):
        response = self.client.post(self.url, self._payload(
            stock_quantity='25'))

        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 25)
        self.assertEqual(
            self.product.inventory_tracking_mode,
            Product.INVENTORY_TRACKING_LEGACY,
        )

    def test_staff_stock_update_still_writes_inventory_log(self):
        before = InventoryLog.objects.count()

        response = self.client.post(self.url, self._payload(
            stock_quantity='25'))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventoryLog.objects.count(), before + 1)

        log = InventoryLog.objects.latest('pk')
        self.assertEqual(log.product_id, self.product.pk)
        self.assertEqual(log.action, 'adjustment')
        self.assertEqual(log.source, 'inventory_update')
        self.assertEqual(log.reason, 'Product stock update')
        self.assertEqual(log.quantity_change, 15)
        self.assertEqual(log.quantity_before, 10)
        self.assertEqual(log.quantity_after, 25)
        self.assertEqual(log.performed_by_id, self.staff.pk)
