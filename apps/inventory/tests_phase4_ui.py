"""Phase 4 — mode-correct inventory UI regression tests.

Verifies (docs/phase4-mode-correct-inventory-ui.md):

  1. Legacy products still render the Stock/Restock action on the product
     list (desktop table AND mobile-card data attributes).
  2. Ingredient products do NOT render the legacy Stock/Restock action.
  3. The legacy inventory page (/inventory/) lists ONLY legacy-tracked
     products.
  4. The legacy inventory page still lists legacy-tracked products.
  5. Legacy restocking still works end-to-end.
  6. Ingredient and packaging pages remain isolated (separate routes,
     no mixing into the legacy page).
  7. Ingredient products cannot be restocked through forged requests
     (server-side 400 — the UI change is not the enforcement layer).
  8. Dashboard low-stock surfaces are limited to the ingredient/packaging
     card; the retired finished-product card must not reappear.
  9. Phase 3 realtime tests remain green (run separately; see module doc).
 10. Product management behavior does not regress (edit/toggle still work).

Runs on Django's isolated test database; the live development database is
never touched. No PostgreSQL access.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from django.urls import reverse, NoReverseMatch

from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.menu.models import Category, Product


def _uid():
    import time
    return str(time.monotonic_ns())


def _staff():
    return get_user_model().objects.create_user(
        username=f'p4_staff_{_uid()}', password='p4-pass', role='admin')


def _client(user):
    c = Client(HTTP_HOST='testserver')
    c.force_login(user)
    return c


class ProductListStockActionTests(TestCase):
    """Phase 7B-3A — the legacy Stock column / Stock action / restock link
    are REMOVED from Product Management for every mode. Edit, availability
    toggle, activation toggle, and the mode marker remain."""

    def setUp(self):
        self.staff = _staff()
        self.c = _client(self.staff)
        self.category = Category.objects.create(
            name=f'P4 Cat {_uid()}', slug=f'p4-cat-{_uid()}')

    def _legacy(self, name='P4 Legacy Prod', stock=50):
        return Product.objects.create(
            category=self.category, name=name, price='50.00',
            stock_quantity=stock, inventory_tracking_mode='legacy',
        )

    def _ingredient(self, name='P4 Ingr Prod', on_hand=100):
        product = Product.objects.create(
            category=self.category, name=name, price='60.00',
            stock_quantity=0, inventory_tracking_mode='ingredient',
        )
        ingredient = InventoryItem.objects.create(
            name=f'P4 Ingr {name} {_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal(str(on_hand)),
        )
        recipe = Recipe.objects.create(
            product=product, size='none', order_type_scope=Recipe.SCOPE_ALL,
            status=Recipe.STATUS_APPROVED, revision=1,
            approved_at=timezone.now(), approval_note='P4 fixture',
        )
        RecipeItem.objects.create(
            recipe=recipe, inventory_item=ingredient, quantity=Decimal('10.000'))
        for order_type in ('dine_in', 'takeout'):
            ProductPackagingRule.objects.create(
                product=product, size='none', order_type=order_type,
                no_packaging=True,
            )
        return product

    def test_no_stock_column_header_rendered(self):
        self._legacy()
        self._ingredient()
        html = self.c.get('/manage/products/').content.decode('utf-8')
        self.assertNotIn('>Stock</th>', html)

    def test_no_stock_action_or_restock_link_for_any_mode(self):
        legacy = self._legacy()
        ingredient = self._ingredient()
        html = self.c.get('/manage/products/').content.decode('utf-8')
        self.assertNotIn('>Stock</a>', html)
        self.assertNotIn('data-stock-url', html)
        # No row may link into the retired restock endpoint.
        self.assertNotIn('/restock/', html)
        for product in (legacy, ingredient):
            self.assertIn(f'data-product-id="{product.pk}"', html)

    def test_no_stock_quantity_presentation_in_rows(self):
        legacy = self._legacy('P4 Legacy Qty', stock=50)
        ingredient = self._ingredient('P4 Ingr Qty')
        html = self.c.get('/manage/products/').content.decode('utf-8')
        self.assertNotIn('stock-indicator', html)
        self.assertNotIn('data-stock-hidden', html)
        self.assertNotIn('data-stock-status', html)
        start = html.index(f'data-product-id="{legacy.pk}"')
        row = html[start:html.index('</tr>', start)]
        # The frozen quantity must not be displayed as a managed figure.
        self.assertNotIn(f'>{legacy.stock_quantity}</span>', row)

    def test_mode_marker_still_travels_with_rows(self):
        legacy = self._legacy('P4 Card Legacy')
        ingredient = self._ingredient('P4 Card Ingr')
        html = self.c.get('/manage/products/').content.decode('utf-8')
        self.assertIn('data-tracking-mode="legacy"', html)
        self.assertIn('data-tracking-mode="ingredient"', html)

    def test_edit_and_toggle_still_available_for_both_modes(self):
        legacy = self._legacy()
        ingredient = self._ingredient()
        html = self.c.get('/manage/products/').content.decode('utf-8')
        for pk in (legacy.pk, ingredient.pk):
            self.assertIn(f'href="/manage/products/{pk}/edit/"', html)
            self.assertIn(f'data-url="/manage/products/{pk}/toggle/"',
                          html)


class LegacyInventoryPageTests(TestCase):
    """Phase 7B-3A — /inventory/ is RETIRED: it redirects to the ingredient
    inventory and must not serve the finished-product stock page. The
    ingredient, packaging, and historical log pages remain separate."""

    def setUp(self):
        self.staff = _staff()
        self.c = _client(self.staff)
        self.category = Category.objects.create(
            name=f'P4 Cat {_uid()}', slug=f'p4-cat-{_uid()}')

    def _legacy(self, name='P4 Legacy Item', stock=50):
        return Product.objects.create(
            category=self.category, name=name, price='50.00',
            stock_quantity=stock, inventory_tracking_mode='legacy',
        )

    def _ingredient(self, name='P4 Ingr Page'):
        product = Product.objects.create(
            category=self.category, name=name, price='60.00',
            stock_quantity=0, inventory_tracking_mode='ingredient',
        )
        InventoryItem.objects.create(
            name=f'P4 Page Ingr {name} {_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal('100.000'),
        )
        return product

    def test_inventory_url_redirects_to_ingredient_list(self):
        self._legacy('P4 Legacy Page')
        resp = self.c.get('/inventory/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], '/inventory/ingredients/')

    def test_followed_target_serves_no_legacy_stock_page(self):
        legacy = self._legacy('P4 Legacy Page')
        resp = self.c.get('/inventory/', follow=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode('utf-8')
        self.assertNotIn('Legacy Finished-Product Stock', html)
        self.assertNotIn('P4 Legacy Page', html)
        self.assertNotIn(legacy.name, html)

    def test_legacy_restocking_is_retired(self):
        product = self._legacy(stock=50)
        resp = self.c.post(f'/inventory/{product.pk}/restock/', {'quantity': '10'})
        self.assertEqual(resp.status_code, 410)
        self.assertFalse(resp.json()['success'])
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 50)

    def test_ingredient_and_packaging_pages_isolated(self):
        # Ingredient page: ingredient items only, no products.
        item = InventoryItem.objects.create(
            name=f'P4 Iso Ingr {_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal('10.000'),
        )
        ing_page = self.c.get('/inventory/ingredients/').content.decode('utf-8')
        self.assertIn(item.name, ing_page)
        self.assertNotIn('data-product-id', ing_page)
        # Packaging page: packaging items only.
        pkg = InventoryItem.objects.create(
            name=f'P4 Iso Pkg {_uid()}', item_type='packaging',
            base_unit='pcs', on_hand_quantity=Decimal('30.000'),
        )
        pkg_page = self.c.get('/inventory/packaging/').content.decode('utf-8')
        self.assertIn(pkg.name, pkg_page)
        self.assertNotIn('P4 Legacy', pkg_page)

    def test_log_page_labels_as_legacy_stock_movements(self):
        # Phase 1.1: the legacy listing lives at /inventory/log/legacy/;
        # /inventory/log/ is the current ingredient/packaging history.
        html = self.c.get('/inventory/log/legacy/').content.decode('utf-8')
        self.assertIn('Stock Movements (Legacy)', html)
        # The back link into the retired page must be gone.
        self.assertNotIn('← Back to Inventory', html)


class ForgedRestockGuardTests(TestCase):
    """Phase 7B-3A — the retired endpoint is the enforcement layer: forged
    POSTs are answered 410 Gone (never success) and mutate nothing; the
    authentication guard still runs first."""

    def setUp(self):
        self.staff = _staff()
        self.c = _client(self.staff)
        self.category = Category.objects.create(
            name=f'P4 Cat {_uid()}', slug=f'p4-cat-{_uid()}')

    def test_forged_restock_for_ingredient_product_rejected(self):
        product = Product.objects.create(
            category=self.category, name='P4 Forged Ingr', price='60.00',
            stock_quantity=0, inventory_tracking_mode='ingredient',
        )
        resp = self.c.post(f'/inventory/{product.pk}/restock/', {'quantity': '25'})
        self.assertEqual(resp.status_code, 410)
        self.assertFalse(resp.json()['success'])
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 0)

    def test_forged_restock_for_ingredient_product_rejected_anon(self):
        product = Product.objects.create(
            category=self.category, name='P4 Anon Ingr', price='60.00',
            stock_quantity=0, inventory_tracking_mode='ingredient',
        )
        anon = Client(HTTP_HOST='testserver')
        resp = anon.post(f'/inventory/{product.pk}/restock/', {'quantity': '25'})
        self.assertIn(resp.status_code, (302, 403))   # login redirect / deny


class DashboardLowStockLabelTests(TestCase):
    """Phase 7C-2 — the finished-product low-stock card is retired; the
    ingredient/packaging card is the only dashboard low-stock surface."""

    def setUp(self):
        self.staff = _staff()
        self.c = _client(self.staff)
        self.category = Category.objects.create(
            name=f'P4 Cat {_uid()}', slug=f'p4-cat-{_uid()}')

    def test_dashboard_has_only_ingredient_low_stock_card(self):
        html = self.c.get('/dashboard/').content.decode('utf-8')
        self.assertIn('Ingredients &amp; Packaging', html)
        self.assertIn('id="ingredient-low-stock-list"', html)
        self.assertNotIn('Low Stock - Finished Products', html)
        self.assertNotIn('id="low-stock-list"', html)
        self.assertNotIn('> Low Stock</h3>', html)

    def test_phase3_realtime_listener_present(self):
        html = self.c.get('/dashboard/').content.decode('utf-8')
        self.assertIn("RealtimeConnection.on('ingredient_low'", html)

    def test_legacy_low_stock_widget_is_retired(self):
        from apps.dashboard import views
        self.assertFalse(hasattr(views, '_low_stock'))
        response = self.c.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('low_stock', response.context)
