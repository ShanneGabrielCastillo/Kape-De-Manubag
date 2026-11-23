"""Phase 7C-1 — legacy InventoryLog writes from product forms are stopped.

The Phase 7B-3B audit found ``_log_inventory_update()`` writing an
``InventoryLog`` row for *every* product create/edit, including products that
default to ingredient tracking (whose ``Product.stock_quantity`` is frozen and
non-authoritative). That fabricated obsolete finished-product history.

These tests pin the corrected behaviour:

  * creating an ingredient-tracked product writes no InventoryLog row,
  * editing one writes no InventoryLog row,
  * an explicitly legacy-tracked product still logs its stock movements,
  * new products still default to ingredient tracking,
  * ingredient product forms hide/disable the legacy stock fields and ignore
    crafted values for them,
  * the Django admin makes those fields read-only for every product,
  * ordinary create/edit still works,
  * historical InventoryLog rows are untouched.

All tests run on isolated test databases.
"""
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.inventory.models import InventoryLog
from apps.menu.admin import ProductAdmin
from apps.menu.forms import ProductForm
from apps.menu.models import Category, Product

PASSWORD = 'phase7c1-pass'


def _uid():
    import time
    return str(time.monotonic_ns())


def _category():
    name = f'P7C1 Cat {_uid()}'
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _admin_user():
    return get_user_model().objects.create_user(
        username=f'p7c1_admin_{_uid()}', password=PASSWORD, role='admin')


def _legacy_product(category, stock=10, name=None):
    return Product.objects.create(
        category=category,
        name=name or f'P7C1 Legacy {_uid()}',
        price='50.00',
        stock_quantity=stock,
        low_stock_threshold=10,
        inventory_tracking_mode=Product.INVENTORY_TRACKING_LEGACY,
    )


def _ingredient_product(category, stock=7, name=None):
    return Product.objects.create(
        category=category,
        name=name or f'P7C1 Ingredient {_uid()}',
        price='40.00',
        stock_quantity=stock,
        low_stock_threshold=5,
        inventory_tracking_mode=Product.INVENTORY_TRACKING_INGREDIENT,
    )


class LegacyWriteGuardTests(TestCase):
    def setUp(self):
        self.admin = _admin_user()
        self.client.force_login(self.admin)
        self.category = _category()

    def _payload(self, name, **overrides):
        payload = {
            'category': self.category.pk,
            'name': name,
            'description': '',
            'price': '45.00',
            'stock_quantity': '10',
            'low_stock_threshold': '10',
            'has_sizes': '',
            'is_available': 'on',
            'is_featured': '',
        }
        payload.update(overrides)
        return payload

    # ── Ingredient products ───────────────────────────────────────────────────

    def test_creating_ingredient_product_writes_no_inventory_log(self):
        before = InventoryLog.objects.count()
        response = self.client.post(
            reverse('menu:product_create'),
            self._payload(f'P7C1 New {_uid()}',
                          stock_quantity='999', low_stock_threshold='5'),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventoryLog.objects.count(), before)

        product = Product.objects.latest('pk')
        self.assertEqual(
            product.inventory_tracking_mode,
            Product.INVENTORY_TRACKING_INGREDIENT,
        )
        # The crafted legacy stock value was ignored (disabled field).
        self.assertNotEqual(product.stock_quantity, 999)

    def test_editing_ingredient_product_writes_no_inventory_log(self):
        product = _ingredient_product(self.category, stock=7)
        before = InventoryLog.objects.count()
        response = self.client.post(
            reverse('menu:product_edit', args=[product.pk]),
            self._payload(product.name,
                          stock_quantity='999', low_stock_threshold='1'),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventoryLog.objects.count(), before)

        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 7)
        self.assertNotEqual(product.low_stock_threshold, 1)

    def test_create_defaults_to_ingredient_tracking(self):
        self.client.post(
            reverse('menu:product_create'), self._payload(f'P7C1 Default {_uid()}'))
        product = Product.objects.latest('pk')
        self.assertEqual(
            product.inventory_tracking_mode,
            Product.INVENTORY_TRACKING_INGREDIENT,
        )

    # ── Legacy products keep their behaviour ──────────────────────────────────

    def test_editing_legacy_product_still_logs_stock_movement(self):
        product = _legacy_product(self.category, stock=10)
        before = InventoryLog.objects.count()
        response = self.client.post(
            reverse('menu:product_edit', args=[product.pk]),
            self._payload(product.name, stock_quantity='25'),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventoryLog.objects.count(), before + 1)

        log = InventoryLog.objects.latest('pk')
        self.assertEqual(log.product_id, product.pk)
        self.assertEqual(log.action, 'adjustment')
        self.assertEqual(log.source, 'inventory_update')
        self.assertEqual(log.reason, 'Product stock update')
        self.assertEqual(log.quantity_change, 15)
        self.assertEqual(log.quantity_before, 10)
        self.assertEqual(log.quantity_after, 25)

        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 25)

    # ── Form surface ──────────────────────────────────────────────────────────

    def test_ingredient_form_hides_and_disables_legacy_fields(self):
        form = ProductForm()
        self.assertFalse(form.show_legacy_stock_fields)
        self.assertTrue(form.fields['stock_quantity'].disabled)
        self.assertTrue(form.fields['low_stock_threshold'].disabled)
        # Mode is never editable through the product form.
        self.assertNotIn('inventory_tracking_mode', form.fields)

        page = self.client.get(reverse('menu:product_create'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id_stock_quantity')
        self.assertNotContains(page, 'id_low_stock_threshold')
        self.assertContains(page, 'ingredient-tracked')

    def test_legacy_form_shows_and_allows_stock_fields(self):
        product = _legacy_product(self.category)
        form = ProductForm(instance=product)
        self.assertTrue(form.show_legacy_stock_fields)
        self.assertFalse(form.fields['stock_quantity'].disabled)
        self.assertFalse(form.fields['low_stock_threshold'].disabled)

        page = self.client.get(reverse('menu:product_edit', args=[product.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id_stock_quantity')

    def test_admin_stock_fields_readonly_for_all_products(self):
        model_admin = ProductAdmin(Product, AdminSite())
        ingredient = _ingredient_product(self.category)
        legacy = _legacy_product(self.category)

        ingredient_fields = model_admin.get_readonly_fields(None, ingredient)
        self.assertIn('stock_quantity', ingredient_fields)
        self.assertIn('low_stock_threshold', ingredient_fields)

        # Creation screen (obj=None) defaults to ingredient tracking.
        create_fields = model_admin.get_readonly_fields(None, None)
        self.assertIn('stock_quantity', create_fields)

        # Legacy products use the staff form for stock, which logs the change.
        legacy_fields = model_admin.get_readonly_fields(None, legacy)
        self.assertIn('stock_quantity', legacy_fields)
        self.assertIn('low_stock_threshold', legacy_fields)

    # ── Existing behaviour and history ────────────────────────────────────────

    def test_create_and_edit_flow_still_works(self):
        name = f'P7C1 Flow {_uid()}'
        response = self.client.post(reverse('menu:product_create'), self._payload(name))
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(name=name)
        self.assertEqual(product.price, Decimal('45.00'))

        renamed = f'{name} renamed'
        response = self.client.post(
            reverse('menu:product_edit', args=[product.pk]),
            self._payload(renamed, price='52.00'),
        )
        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.name, renamed)

    def test_historical_inventory_log_records_unchanged(self):
        legacy = _legacy_product(self.category)
        historical = InventoryLog.objects.create(
            product=legacy,
            product_name=legacy.name,
            action='restock',
            source='manual_adjustment',
            reason='Phase 7C-1 history probe',
            quantity_change=5,
            quantity_before=5,
            quantity_after=10,
            performed_by=self.admin,
        )
        before = InventoryLog.objects.count()

        self.client.post(
            reverse('menu:product_create'), self._payload(f'P7C1 History {_uid()}'))
        self.client.post(
            reverse('menu:product_edit', args=[legacy.pk]),
            self._payload(legacy.name),
        )

        self.assertEqual(InventoryLog.objects.count(), before)
        historical.refresh_from_db()
        self.assertEqual(historical.reason, 'Phase 7C-1 history probe')
        self.assertEqual(historical.quantity_change, 5)
        self.assertEqual(historical.quantity_after, 10)
        self.assertEqual(historical.product_name, legacy.name)
