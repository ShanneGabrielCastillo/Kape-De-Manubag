"""Phase 1.1 — Stock Movements page regression tests.

Phase 1 browser verification found that /inventory/log/ showed only the
legacy finished-product InventoryLog (0 records) while the real ingredient
and packaging movement history (IngredientMovementLog, 9 records) had no
browsable page.

These tests pin the corrected behaviour:

  * /inventory/log/ renders the current IngredientMovementLog history,
  * ingredients and packaging are distinguishable per row,
  * movement fields (date, item, kind, type, quantity, unit, reference,
    reason, actor) render,
  * filtering, pagination and the empty state work,
  * the permission guard is unchanged,
  * legacy InventoryLog history stays available read-only at
    /inventory/log/legacy/ and is never merged into the current list,
  * the route and sidebar navigation remain compatible.

All tests run on isolated test databases.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.inventory.models import IngredientMovementLog, InventoryItem, InventoryLog
from apps.inventory.services import record_adjustment
from apps.menu.models import Category, Product

PASSWORD = 'phase1-1-pass'


def _uid():
    import time
    return str(time.monotonic_ns())


def _user(role='admin'):
    return get_user_model().objects.create_user(
        username=f'p11_{role}_{_uid()}', password=PASSWORD, role=role)


def _item(name, item_type, base_unit, on_hand):
    return InventoryItem.objects.create(
        name=name, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=Decimal(on_hand),
    )


class StockMovementsPageTests(TestCase):
    def setUp(self):
        self.admin = _user('admin')
        self.cashier = _user('cashier')
        self.kitchen = _user('kitchen_staff')
        self.client.force_login(self.admin)

        self.ingredient = _item(f'P11 Coffee {_uid()}', 'ingredient', 'g', '100.000')
        self.packaging = _item(f'P11 Cup {_uid()}', 'packaging', 'pcs', '50.000')

        # Ingredient adjustment through the real service (records an
        # adjustment movement with arithmetic validated by the DB).
        record_adjustment(
            inventory_item_id=self.ingredient.pk,
            new_on_hand=Decimal('110.000'),
            expected_on_hand=Decimal('100.000'),
            reason_code='stock_count_correction',
            note='Phase 1.1 adjustment probe',
            performed_by=self.admin,
        )
        # Packaging purchase movement through the model's own recorder.
        IngredientMovementLog.record(
            inventory_item=self.packaging,
            movement_type=IngredientMovementLog.MOVEMENT_PURCHASE,
            on_hand_delta='5.000',
            reserved_delta='0.000',
            on_hand_before='50.000',
            reserved_before='0.000',
            reason_code='supplier_delivery',
            note='Phase 1.1 purchase probe',
            performed_by=self.admin,
        )

    # ── Access ────────────────────────────────────────────────────────────────

    def test_admin_can_access_current_movements(self):
        response = self.client.get(reverse('inventory:log'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/movement_log.html')

    def test_cashier_can_access_current_movements(self):
        self.client.force_login(self.cashier)
        self.assertEqual(
            self.client.get(reverse('inventory:log')).status_code, 200)

    def test_anonymous_redirected_to_login(self):
        response = Client().get(reverse('inventory:log'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_kitchen_staff_denied(self):
        self.client.force_login(self.kitchen)
        response = self.client.get(reverse('inventory:log'))
        self.assertIn(response.status_code, (302, 403))

    # ── Content ───────────────────────────────────────────────────────────────

    def test_ingredient_and_packaging_movements_visible(self):
        response = self.client.get(reverse('inventory:log'))
        self.assertContains(response, self.ingredient.name)
        self.assertContains(response, self.packaging.name)
        self.assertContains(response, 'Ingredient')
        self.assertContains(response, 'Packaging')

    def test_movement_fields_render(self):
        response = self.client.get(reverse('inventory:log'))
        content = response.content.decode()
        # Movement types (display labels) for both rows.
        self.assertIn('Manual Adjustment', content)
        self.assertIn('Purchase / Restock', content)
        # Quantity, unit, reason code/note and actor.
        self.assertIn('+10.000', content)
        self.assertIn('+5.000', content)
        self.assertIn(InventoryItem.BASE_UNIT_G, content)
        self.assertIn(InventoryItem.BASE_UNIT_PCS, content)
        self.assertIn('stock_count_correction', content)
        self.assertIn('Phase 1.1 adjustment probe', content)
        self.assertIn(self.admin.username, content)

    def test_newest_movement_first(self):
        response = self.client.get(reverse('inventory:log'))
        movements = list(response.context['movements'])
        self.assertEqual(len(movements), 2)
        self.assertGreaterEqual(
            movements[0].created_at, movements[-1].created_at)

    # ── Filtering / pagination / empty state ──────────────────────────────────

    def test_type_filter(self):
        response = self.client.get(reverse('inventory:log'), {'type': 'purchase'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.packaging.name)
        self.assertNotContains(response, self.ingredient.name)

    def test_kind_filter(self):
        response = self.client.get(reverse('inventory:log'), {'kind': 'ingredient'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.ingredient.name)
        self.assertNotContains(response, self.packaging.name)

    def test_invalid_filters_are_ignored(self):
        response = self.client.get(
            reverse('inventory:log'), {'type': 'not-a-type', 'kind': 'nope'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['movements']), 2)

    def test_search_matches_note(self):
        response = self.client.get(
            reverse('inventory:log'), {'q': 'purchase probe'})
        self.assertContains(response, self.packaging.name)
        self.assertNotContains(response, self.ingredient.name)

    def test_pagination(self):
        for index in range(21):
            IngredientMovementLog.record(
                inventory_item=self.ingredient,
                movement_type=IngredientMovementLog.MOVEMENT_ADJUSTMENT,
                on_hand_delta='1.000',
                reserved_delta='0.000',
                on_hand_before='110.000',
                reserved_before='0.000',
                reason_code='stock_count_correction',
                performed_by=self.admin,
            )
        response = self.client.get(reverse('inventory:log'), {'page': 2})
        self.assertEqual(response.status_code, 200)
        movements = response.context['movements']
        self.assertEqual(movements.number, 2)
        self.assertEqual(movements.paginator.num_pages, 2)
        self.assertEqual(movements.paginator.count, 23)

    # ── Legacy history remains separate and intact ────────────────────────────

    def test_legacy_history_separate_and_unchanged(self):
        category = Category.objects.create(
            name=f'P11 Cat {_uid()}', slug=f'p11-cat-{_uid()}')
        legacy_product = Product.objects.create(
            category=category, name=f'P11 Legacy {_uid()}', price='50.00',
            stock_quantity=10, low_stock_threshold=10,
            inventory_tracking_mode='legacy',
        )
        InventoryLog.objects.create(
            product=legacy_product, action='restock', source='manual_adjustment',
            reason='Phase 1.1 legacy probe', quantity_change=5,
            quantity_before=10, quantity_after=15, performed_by=self.admin,
        )
        before = InventoryLog.objects.count()

        current = self.client.get(reverse('inventory:log'))
        self.assertNotContains(current, legacy_product.name)

        legacy = self.client.get(reverse('inventory:legacy_log'))
        self.assertEqual(legacy.status_code, 200)
        self.assertTemplateUsed(legacy, 'inventory/log.html')
        self.assertContains(legacy, legacy_product.name)
        self.assertContains(legacy, 'Stock Movements (Legacy)')
        self.assertNotContains(legacy, self.ingredient.name)

        self.assertEqual(InventoryLog.objects.count(), before)

    # ── Route / navigation compatibility ──────────────────────────────────────

    def test_routes_unchanged(self):
        self.assertEqual(reverse('inventory:log'), '/inventory/log/')
        self.assertEqual(reverse('inventory:legacy_log'), '/inventory/log/legacy/')

    def test_sidebar_link_targets_current_movements(self):
        response = self.client.get(reverse('inventory:ingredient_list'))
        content = response.content.decode()
        self.assertIn('aria-label="Stock Movements"', content)
        self.assertIn('href="/inventory/log/"', content)
        self.assertNotIn('aria-label="Stock Movements (Legacy)"', content)


class EmptyStockMovementsTests(TestCase):
    def setUp(self):
        self.admin = _user('admin')
        self.client.force_login(self.admin)

    def test_empty_history_renders_safely(self):
        response = self.client.get(reverse('inventory:log'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No stock movements have been recorded yet.')
        self.assertEqual(len(response.context['movements']), 0)
