"""Phase 7B-3A — legacy finished-product inventory UI retirement.

Covers the ten required scenarios from the phase spec:

  1. The legacy finished-product inventory page no longer exposes
     stock-management controls (/inventory/ is a compatibility redirect).
  2. Legacy restock requests cannot mutate finished-product stock.
  3. The retired POST endpoint does not return misleading success.
  4. Product Management no longer displays legacy Stock columns/actions.
  5. Product creation and editing remain functional.
  6. Ingredient Inventory remains accessible.
  7. Packaging Inventory remains accessible.
  8. Stock Movements and historical InventoryLog viewing remain accessible.
  9. Existing permission protections remain intact.
 10. Legacy historical order and cancellation compatibility remains intact.

All tests run on isolated test databases (never the live dev DB).
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse

from apps.inventory.models import IngredientMovementLog, InventoryItem, InventoryLog
from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderCancellationDisposition, OrderItem
from apps.orders.services import place_order_inventory

PASSWORD = 'phase7b3a-pass'


def _uid():
    import time
    return str(time.monotonic_ns())


def _user(role='admin'):
    return get_user_model().objects.create_user(
        username=f'p7b3a_{role}_{_uid()}', password=PASSWORD, role=role)


def _client(user):
    c = Client(HTTP_HOST='testserver')
    c.force_login(user)
    return c


def _category(name=f'P7B3A Cat {_uid()}'):
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _legacy_product(category, name=f'P7B3A Legacy {_uid()}', stock=10):
    return Product.objects.create(
        category=category, name=name, price='50.00',
        stock_quantity=stock, low_stock_threshold=10,
        inventory_tracking_mode='legacy',
    )


class RetiredStockPageTests(TestCase):
    """Scenarios 1 — /inventory/ serves no stock-management controls."""

    def setUp(self):
        self.admin = _user('admin')
        self.c = _client(self.admin)
        self.category = _category()
        self.legacy = _legacy_product(self.category)

    def test_retired_page_redirects_and_exposes_no_controls(self):
        resp = self.c.get(reverse('inventory:list'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'],
                         reverse('inventory:ingredient_list'))

        followed = self.c.get(reverse('inventory:list'), follow=True)
        self.assertEqual(followed.status_code, 200)
        html = followed.content.decode()
        # Retired page identity + controls absent from the followed target.
        self.assertNotIn('Legacy Finished-Product Stock', html)
        self.assertNotIn('restock-modal', html)
        self.assertNotIn('openRestockModal', html)
        self.assertNotIn('id="inv-stock-status"', html)
        self.assertNotIn(self.legacy.name, html)


class RetiredRestockEndpointTests(TestCase):
    """Scenarios 2, 3 — restock requests cannot mutate anything and never
    report success."""

    def setUp(self):
        self.admin = _user('admin')
        self.c = _client(self.admin)
        self.category = _category()
        self.legacy = _legacy_product(self.category, stock=10)

    def test_post_cannot_mutate_legacy_stock(self):
        resp = self.c.post(
            reverse('inventory:restock', args=[self.legacy.pk]),
            {'quantity': '25', 'notes': 'must be ignored'},
        )
        self.assertEqual(resp.status_code, 410)
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.stock_quantity, 10)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_post_does_not_return_misleading_success(self):
        resp = self.c.post(
            reverse('inventory:restock', args=[self.legacy.pk]),
            {'quantity': '5'},
        )
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertEqual(resp.status_code, 410)
        self.assertIn('retired', data['error'].lower())
        # The response must not pretend a new quantity exists.
        self.assertNotIn('new_qty', data)

    def test_post_does_not_write_audit_log(self):
        from apps.audit.models import AuditLog
        before = AuditLog.objects.count()
        self.c.post(
            reverse('inventory:restock', args=[self.legacy.pk]),
            {'quantity': '5'},
        )
        self.assertEqual(AuditLog.objects.count(), before)

    def test_get_redirects_without_mutating(self):
        resp = self.c.get(reverse('inventory:restock', args=[self.legacy.pk]))
        self.assertEqual(resp.status_code, 302)
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.stock_quantity, 10)


class ProductManagementStockRemovalTests(TestCase):
    """Scenario 4 — Product Management shows no legacy Stock column/action."""

    def setUp(self):
        self.admin = _user('admin')
        self.c = _client(self.admin)
        self.category = _category()

    def _page(self):
        resp = self.c.get(reverse('menu:product_list'))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_no_stock_column_or_action(self):
        legacy = _legacy_product(self.category, stock=42)
        html = self._page()
        self.assertNotIn('>Stock</th>', html)
        self.assertNotIn('>Stock</a>', html)
        self.assertNotIn('data-stock-url', html)
        self.assertNotIn('stock-indicator', html)
        self.assertNotIn(f'/inventory/{legacy.pk}/restock/', html)

    def test_edit_and_activation_controls_preserved(self):
        legacy = _legacy_product(self.category)
        html = self._page()
        self.assertIn(f'href="/manage/products/{legacy.pk}/edit/"', html)
        self.assertIn(f'data-url="/manage/products/{legacy.pk}/toggle/"', html)


class ProductCreateEditStillWorksTests(TestCase):
    """Scenario 5 — product creation and editing remain functional."""

    def setUp(self):
        self.admin = _user('admin')
        self.c = _client(self.admin)
        self.category = _category()

    def test_create_then_edit_product(self):
        resp = self.c.post(reverse('menu:product_create'), {
            'category': str(self.category.pk),
            'name': f'P7B3A Created {_uid()}',
            'description': 'created by 7B-3A test',
            'price': '75.00',
            'stock_quantity': '5',
            'low_stock_threshold': '2',
        })
        self.assertEqual(resp.status_code, 302)
        product = Product.objects.get(name__startswith='P7B3A Created')
        self.assertEqual(product.price, Decimal('75.00'))
        # Phase 7C-1: new products are ingredient-tracked and the legacy
        # stock fields are no longer part of the creation form, so a posted
        # stock value must not be written (the model default is kept).
        self.assertEqual(
            product.inventory_tracking_mode,
            Product.INVENTORY_TRACKING_INGREDIENT,
        )
        default_stock = Product._meta.get_field('stock_quantity').default
        self.assertEqual(product.stock_quantity, default_stock)
        self.assertEqual(InventoryLog.objects.count(), 0)

        # Editing still works (price change persists).
        resp = self.c.post(
            reverse('menu:product_edit', args=[product.pk]), {
                'category': str(self.category.pk),
                'name': product.name,
                'description': 'created by 7B-3A test',
                'price': '80.00',
                'stock_quantity': '5',
                'low_stock_threshold': '2',
            })
        self.assertEqual(resp.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal('80.00'))

    def test_edit_form_does_not_link_legacy_restock(self):
        product = _legacy_product(self.category)
        resp = self.c.get(reverse('menu:product_edit', args=[product.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('/restock/', resp.content.decode())


class IngredientPackagingLogAccessibleTests(TestCase):
    """Scenarios 6, 7, 8 — the surviving inventory surfaces stay up."""

    def setUp(self):
        self.admin = _user('admin')
        self.c = _client(self.admin)
        self.category = _category()

    def test_ingredient_inventory_accessible(self):
        resp = self.c.get(reverse('inventory:ingredient_list'))
        self.assertEqual(resp.status_code, 200)

    def test_packaging_inventory_accessible(self):
        resp = self.c.get(reverse('inventory:packaging_list'))
        self.assertEqual(resp.status_code, 200)

    def test_stock_movements_page_accessible(self):
        resp = self.c.get(reverse('inventory:log'))
        self.assertEqual(resp.status_code, 200)

    def test_historical_inventory_log_records_still_viewable(self):
        legacy = _legacy_product(self.category)
        InventoryLog.record(
            product=legacy, action='restock', source='manual_adjustment',
            reason='Phase 7B-3A history probe', quantity_change=5,
            quantity_before=legacy.stock_quantity,
            quantity_after=legacy.stock_quantity + 5,
            performed_by=self.admin,
        )
        # Phase 1.1: legacy InventoryLog history moved to its own read-only
        # route (/inventory/log/legacy/); /inventory/log/ now shows current
        # ingredient/packaging movements.
        resp = self.c.get(reverse('inventory:legacy_log'), {'q': 'history probe'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'history probe')

    def test_nav_link_points_to_stock_movements_not_legacy_page(self):
        resp = self.c.get(reverse('inventory:ingredient_list'))
        html = resp.content.decode()
        self.assertIn('aria-label="Stock Movements"', html)
        self.assertNotIn('aria-label="Finished-Product Stock"', html)


class RetiredSurfacePermissionsTests(TestCase):
    """Scenario 9 — permission protections remain intact."""

    def setUp(self):
        self.admin = _user('admin')
        self.cashier = _user('cashier')
        self.kitchen = _user('kitchen_staff')
        self.category = _category()
        self.legacy = _legacy_product(self.category)
        self.restock_url = reverse('inventory:restock', args=[self.legacy.pk])

    def test_anon_redirected_on_all_inventory_surfaces(self):
        anon = Client(HTTP_HOST='testserver')
        for url in (reverse('inventory:list'), self.restock_url,
                    reverse('inventory:log'),
                    reverse('inventory:ingredient_list'),
                    reverse('inventory:packaging_list')):
            resp = anon.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn('/login', resp['Location'])

    def test_kitchen_staff_denied_on_retired_and_surviving_surfaces(self):
        c = _client(self.kitchen)
        for url in (reverse('inventory:list'), self.restock_url,
                    reverse('inventory:log')):
            resp = c.get(url)
            self.assertIn(resp.status_code, (302, 403), url)

    def test_cashier_still_authorized_on_surviving_surfaces(self):
        c = _client(self.cashier)
        self.assertEqual(c.get(reverse('inventory:ingredient_list')).status_code, 200)
        self.assertEqual(c.get(reverse('inventory:packaging_list')).status_code, 200)
        self.assertEqual(c.get(reverse('inventory:log')).status_code, 200)
        # Retired restock: authorized role still gets the explicit 410.
        resp = c.post(self.restock_url, {'quantity': '5'})
        self.assertEqual(resp.status_code, 410)

    def test_anon_post_cannot_reach_restock_mutation(self):
        anon = Client(HTTP_HOST='testserver')
        resp = anon.post(self.restock_url, {'quantity': '99'})
        self.assertIn(resp.status_code, (302, 403))
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.stock_quantity, 10)


class LegacyOrderCancellationCompatibilityTests(TestCase):
    """Scenario 10 — legacy historical order + cancellation compatibility
    is untouched (real placement + real cancellation endpoints)."""

    def setUp(self):
        self.staff = _user('admin')
        self.c = _client(self.staff)
        self.category = _category()
        self.legacy = _legacy_product(self.category, stock=10)

    def _place_legacy_order(self, quantity=2):
        order = Order.objects.create(
            customer_name=f'P7B3A Legacy Order {_uid()}',
            order_type='dine_in', payment_method='cash',
        )
        OrderItem.objects.create(
            order=order, product=self.legacy,
            product_name=self.legacy.name,
            category_name=self.category.name,
            packaging_eligible=False, size='none', quantity=quantity,
            unit_price=Decimal('50.00'),
            subtotal=Decimal('50.00') * quantity,
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        with transaction.atomic():
            place_order_inventory(order.pk)
        return order

    def test_legacy_order_placement_and_cancellation_still_work(self):
        order = self._place_legacy_order(quantity=2)
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.stock_quantity, 8)   # 10 - 2
        item = OrderItem.objects.get(order=order)
        self.assertEqual(item.inventory_mode, OrderItem.INVENTORY_MODE_LEGACY)

        # Cancel through the real staff endpoint (same as production flow).
        resp = self.c.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.stock_quantity, 10)  # restored
        # Historical compatibility: disposition carries the ledger marker.
        disposition = OrderCancellationDisposition.objects.get(order=order)
        self.assertEqual(disposition.ledger, 'legacy')

    def test_no_reservation_rows_for_pure_legacy_orders(self):
        order = self._place_legacy_order(quantity=1)
        self.assertFalse(
            IngredientMovementLog.objects.filter(order=order).exists())
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.stock_quantity, 9)
