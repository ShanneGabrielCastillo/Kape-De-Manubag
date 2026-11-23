"""
Phase 7D-4 — legacy product reactivation guard.

Legacy-tracked products are historical. They may be deactivated and edited,
but ordinary application workflows must not bring them back into service.
This module covers all four activation channels:

1. ``Product.activate()`` (central model guard),
2. ``Product.clean()`` (Django admin change form, which bypasses ``activate()``),
3. ``product_toggle_active()`` (staff product-list Re-activate button),
4. ``ProductAdmin.activate_products`` (Django admin bulk action).

It also pins the protected contracts: ingredient activation/deactivation is
unchanged, legacy deactivation still works, and the public stock API is
untouched.

All tests use Django's isolated test database; the development SQLite file is
never touched.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.inventory.models import (
    IngredientMovementLog,
    InventoryLog,
)
from apps.menu.models import (
    LEGACY_PRODUCT_REACTIVATION_ERROR,
    Category,
    Product,
)
from apps.orders.models import (
    OrderIngredientReservation,
    OrderIngredientReservationItem,
)

PASSWORD = 'phase7d4-pass'


def _uid():
    import time
    return str(time.monotonic_ns())


def _category():
    name = f'P7D4 Cat {_uid()}'
    return Category.objects.create(
        name=name, slug=name.lower().replace(' ', '-'))


def _admin_user():
    return get_user_model().objects.create_user(
        username=f'p7d4_admin_{_uid()}', password=PASSWORD, role='admin')


def _superuser():
    return get_user_model().objects.create_superuser(
        username=f'p7d4_root_{_uid()}', password=PASSWORD, role='admin')


def _product(category, *, mode='legacy', name=None, stock=25):
    return Product.objects.create(
        category=category,
        name=name or f'P7D4 {mode.title()} {_uid()}',
        price=Decimal('55.00'),
        stock_quantity=stock,
        low_stock_threshold=10,
        inventory_tracking_mode=mode,
    )


def _inactive(product):
    """Deactivate a product through the supported model workflow."""
    product.deactivate()
    product.refresh_from_db()
    return product


def _counts():
    """Snapshot of every table a rejected activation must not touch."""
    return {
        'audit': AuditLog.objects.count(),
        'inventory_log': InventoryLog.objects.count(),
        'movements': IngredientMovementLog.objects.count(),
        'reservations': OrderIngredientReservation.objects.count(),
        'reservation_items': OrderIngredientReservationItem.objects.count(),
    }


# ═════════════════════════════════════════════════════════════════════════════
# 1. Central model guard
# ═════════════════════════════════════════════════════════════════════════════

class LegacyActivationModelTests(TestCase):
    def setUp(self):
        self.category = _category()

    def test_ingredient_product_activation_succeeds(self):
        product = _inactive(_product(self.category, mode='ingredient'))
        self.assertIsNotNone(product.deactivated_at)

        product.activate()
        product.refresh_from_db()

        self.assertTrue(product.is_active)
        self.assertIsNone(product.deactivated_at)

    def test_legacy_product_activation_is_rejected(self):
        product = _inactive(_product(self.category, mode='legacy'))

        with self.assertRaises(ValidationError) as ctx:
            product.activate()

        message = ' '.join(ctx.exception.messages)
        self.assertIn('legacy', message)
        self.assertIn('cannot be reactivated', message)
        self.assertIn(LEGACY_PRODUCT_REACTIVATION_ERROR, ctx.exception.messages)

    def test_legacy_product_stays_inactive_after_rejection(self):
        product = _inactive(_product(self.category, mode='legacy'))
        original_deactivated_at = product.deactivated_at

        with self.assertRaises(ValidationError):
            product.activate()

        product.refresh_from_db()
        self.assertFalse(product.is_active)
        self.assertEqual(product.deactivated_at, original_deactivated_at)

    def test_rejection_does_not_change_stock_or_tracking_mode(self):
        product = _inactive(_product(
            self.category, mode='legacy', stock=37))
        original = (
            product.stock_quantity,
            product.low_stock_threshold,
            product.inventory_tracking_mode,
        )

        with self.assertRaises(ValidationError):
            product.activate()

        product.refresh_from_db()
        self.assertEqual(
            (product.stock_quantity,
             product.low_stock_threshold,
             product.inventory_tracking_mode),
            original,
        )

    def test_rejection_creates_no_audit_or_inventory_rows(self):
        product = _inactive(_product(self.category, mode='legacy'))
        before = _counts()

        with self.assertRaises(ValidationError):
            product.activate()

        self.assertEqual(_counts(), before)
        self.assertFalse(AuditLog.objects.filter(
            action='product.reactivate', object_id=str(product.pk)).exists())

    def test_clean_rejects_direct_admin_style_reactivation(self):
        """The admin change form saves directly; ``clean()`` must refuse it."""
        product = _inactive(_product(self.category, mode='legacy'))
        product.is_active = True

        with self.assertRaises(ValidationError) as ctx:
            product.clean()

        self.assertIn('cannot be reactivated', ' '.join(ctx.exception.messages))

    def test_clean_allows_already_active_legacy_product(self):
        """Already-active legacy products remain editable."""
        product = _product(self.category, mode='legacy')
        product.name = f'{product.name} renamed'

        product.full_clean()  # must not raise

    def test_clean_allows_ingredient_product(self):
        product = _inactive(_product(self.category, mode='ingredient'))
        product.name = f'{product.name} renamed'

        product.full_clean()  # must not raise

    def test_duplicate_name_validation_still_works(self):
        """The new guard must not displace unrelated validation."""
        first = _product(self.category, mode='ingredient', name='Guard Probe')
        duplicate = Product(
            category=self.category, name='guard probe', price=Decimal('10.00'),
            inventory_tracking_mode='ingredient',
        )

        with self.assertRaises(ValidationError) as ctx:
            duplicate.full_clean()

        self.assertIn('name', ctx.exception.message_dict)
        self.assertTrue(first.pk)

    def test_activate_on_already_active_legacy_product_is_noop(self):
        product = _product(self.category, mode='legacy')

        product.activate()
        product.refresh_from_db()

        self.assertTrue(product.is_active)
        self.assertIsNone(product.deactivated_at)

    def test_legacy_product_deactivation_still_works(self):
        product = _product(self.category, mode='legacy')

        product.deactivate()
        product.refresh_from_db()

        self.assertFalse(product.is_active)
        self.assertIsNotNone(product.deactivated_at)

    def test_ingredient_deactivate_reactivate_round_trip_preserved(self):
        product = _product(self.category, mode='ingredient')

        product.deactivate()
        product.refresh_from_db()
        self.assertFalse(product.is_active)
        self.assertIsNotNone(product.deactivated_at)

        product.activate()
        product.refresh_from_db()
        self.assertTrue(product.is_active)
        self.assertIsNone(product.deactivated_at)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Staff product-list toggle view
# ═════════════════════════════════════════════════════════════════════════════

class ProductToggleActiveViewTests(TestCase):
    def setUp(self):
        self.admin = _admin_user()
        self.client.force_login(self.admin)
        self.category = _category()
        self.url = 'menu:product_toggle_active'

    def test_legacy_reactivation_returns_400_with_error(self):
        product = _inactive(_product(self.category, mode='legacy'))

        response = self.client.post(reverse(self.url, args=[product.pk]))

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('cannot be reactivated', payload['error'])

    def test_legacy_product_remains_inactive_after_view_rejection(self):
        product = _inactive(_product(self.category, mode='legacy'))
        original_deactivated_at = product.deactivated_at
        before = _counts()

        self.client.post(reverse(self.url, args=[product.pk]))

        product.refresh_from_db()
        self.assertFalse(product.is_active)
        self.assertEqual(product.deactivated_at, original_deactivated_at)
        self.assertEqual(_counts(), before)
        self.assertFalse(AuditLog.objects.filter(
            action='product.reactivate', object_id=str(product.pk)).exists())

    def test_legacy_rejection_leaves_stock_and_mode_unchanged(self):
        product = _inactive(_product(
            self.category, mode='legacy', stock=41))

        self.client.post(reverse(self.url, args=[product.pk]))

        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 41)
        self.assertEqual(product.inventory_tracking_mode, 'legacy')

    def test_ingredient_reactivation_returns_200_and_audits(self):
        product = _inactive(_product(self.category, mode='ingredient'))

        response = self.client.post(reverse(self.url, args=[product.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        product.refresh_from_db()
        self.assertTrue(product.is_active)
        self.assertTrue(AuditLog.objects.filter(
            action='product.reactivate', object_id=str(product.pk)).exists())

    def test_legacy_deactivation_still_returns_200(self):
        product = _product(self.category, mode='legacy')

        response = self.client.post(reverse(self.url, args=[product.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['status'], 'deactivated')
        product.refresh_from_db()
        self.assertFalse(product.is_active)
        self.assertTrue(AuditLog.objects.filter(
            action='product.deactivate', object_id=str(product.pk)).exists())


# ═════════════════════════════════════════════════════════════════════════════
# 3. Django admin bulk action
# ═════════════════════════════════════════════════════════════════════════════

class ProductAdminActionTests(TestCase):
    def setUp(self):
        self.root = _superuser()
        self.client.force_login(self.root)
        self.category = _category()
        self.url = reverse('admin:menu_product_changelist')

    def _run_action(self, products, follow=False):
        return self.client.post(self.url, {
            'action': 'activate_products',
            '_selected_action': [str(p.pk) for p in products],
        }, follow=follow)

    def test_activate_action_activates_ingredient_products(self):
        product = _inactive(_product(self.category, mode='ingredient'))

        response = self._run_action([product])

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertTrue(product.is_active)
        self.assertTrue(AuditLog.objects.filter(
            action='product.reactivate', object_id=str(product.pk)).exists())

    def test_activate_action_leaves_legacy_products_inactive(self):
        product = _inactive(_product(self.category, mode='legacy'))

        response = self._run_action([product])

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertFalse(product.is_active)

    def test_activate_action_reports_blocked_legacy_products(self):
        product = _inactive(_product(
            self.category, mode='legacy', name=f'P7D4 Blocked {_uid()}'))

        response = self._run_action([product], follow=True)

        self.assertContains(response, 'legacy-tracked product(s) could not be')
        self.assertContains(response, product.name)

    def test_activate_action_writes_audit_only_for_activated_products(self):
        ingredient = _inactive(_product(self.category, mode='ingredient'))
        legacy = _inactive(_product(self.category, mode='legacy'))

        self._run_action([ingredient, legacy])

        self.assertEqual(
            AuditLog.objects.filter(action='product.reactivate').count(), 1)
        self.assertTrue(AuditLog.objects.filter(
            action='product.reactivate',
            object_id=str(ingredient.pk)).exists())
        self.assertFalse(AuditLog.objects.filter(
            action='product.reactivate',
            object_id=str(legacy.pk)).exists())

    def test_activate_action_mixed_selection_partial_success(self):
        """One refused legacy row must not block an eligible ingredient row."""
        ingredient = _inactive(_product(self.category, mode='ingredient'))
        legacy_one = _inactive(_product(self.category, mode='legacy'))
        legacy_two = _inactive(_product(self.category, mode='legacy'))

        response = self._run_action([legacy_one, ingredient, legacy_two])

        self.assertEqual(response.status_code, 302)
        ingredient.refresh_from_db()
        legacy_one.refresh_from_db()
        legacy_two.refresh_from_db()
        self.assertTrue(ingredient.is_active)
        self.assertFalse(legacy_one.is_active)
        self.assertFalse(legacy_two.is_active)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Django admin change form (direct is_active editing)
# ═════════════════════════════════════════════════════════════════════════════

class ProductAdminChangeFormTests(TestCase):
    def setUp(self):
        self.root = _superuser()
        self.client.force_login(self.root)
        self.category = _category()

    def _payload(self, product, **overrides):
        data = {
            'category': self.category.pk,
            'name': product.name,
            'slug': product.slug,
            'description': '',
            'price': '55.00',
            'price_medium': '',
            'price_large': '',
            'price_hot': '',
            'has_sizes': '',
            'is_available': 'on',
            'is_active': 'on',
            'is_featured': '',
            'stock_quantity': str(product.stock_quantity),
            'low_stock_threshold': str(product.low_stock_threshold),
        }
        data.update(overrides)
        return data

    def _url(self, product):
        return reverse('admin:menu_product_change', args=[product.pk])

    def test_crafted_active_post_on_inactive_legacy_is_rejected(self):
        product = _inactive(_product(self.category, mode='legacy'))

        response = self.client.post(self._url(product), self._payload(product))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot be reactivated')
        product.refresh_from_db()
        self.assertFalse(product.is_active)

    def test_rejected_admin_activation_keeps_timestamp_and_audit_clean(self):
        product = _inactive(_product(self.category, mode='legacy'))
        original_deactivated_at = product.deactivated_at

        self.client.post(self._url(product), self._payload(product))

        product.refresh_from_db()
        self.assertEqual(product.deactivated_at, original_deactivated_at)
        self.assertFalse(AuditLog.objects.filter(
            action='product.reactivate', object_id=str(product.pk)).exists())

    def test_editing_inactive_legacy_without_activation_still_saves(self):
        product = _inactive(_product(self.category, mode='legacy'))
        new_name = f'P7D4 Edited {_uid()}'
        payload = self._payload(
            product, name=new_name, slug=new_name.lower().replace(' ', '-'),
        )
        payload.pop('is_active')  # unchecked box is omitted by browsers

        response = self.client.post(self._url(product), payload)

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.name, new_name)
        self.assertFalse(product.is_active)

    def test_active_legacy_product_admin_edit_unchanged(self):
        product = _product(self.category, mode='legacy')
        new_price = '61.00'

        response = self.client.post(
            self._url(product), self._payload(product, price=new_price))

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal(new_price))
        self.assertTrue(product.is_active)
        self.assertIsNone(product.deactivated_at)

    def test_ingredient_reactivation_via_admin_form_still_works(self):
        product = _inactive(_product(self.category, mode='ingredient'))
        payload = self._payload(product)
        # Ingredient stock fields are read-only in the admin form, so they are
        # not part of the bound form; including them is harmless.
        response = self.client.post(self._url(product), payload)

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertTrue(product.is_active)
        # The admin form saves ``is_active`` directly. Clearing
        # ``deactivated_at`` is ``activate()``'s job and was already not done
        # on this pre-existing path; Phase 7D-4 does not change it.
        self.assertIsNotNone(product.deactivated_at)


# ═════════════════════════════════════════════════════════════════════════════
# 5. Protected contracts
# ═════════════════════════════════════════════════════════════════════════════

class ProtectedContractRegressionTests(TestCase):
    def setUp(self):
        self.category = _category()

    def test_stock_api_excludes_inactive_legacy_and_shape_unchanged(self):
        legacy = _inactive(_product(self.category, mode='legacy'))
        ingredient = _product(self.category, mode='ingredient')

        response = self.client.get(reverse('menu:product_stock'))

        self.assertEqual(response.status_code, 200)
        products = response.json()['products']
        self.assertNotIn(str(legacy.pk), products)
        entry = products[str(ingredient.pk)]
        for field in (
            'stock_quantity', 'is_available', 'is_active',
            'inventory_tracking_mode', 'available_units',
            'configuration_complete', 'sellable', 'out_of_stock', 'sizes',
        ):
            self.assertIn(field, entry)
        self.assertEqual(entry['inventory_tracking_mode'], 'ingredient')

    def test_legacy_deactivation_does_not_alter_existing_inventory_log(self):
        product = _product(self.category, mode='legacy')
        log = InventoryLog.record(
            product=product,
            action='restock',
            source='manual_adjustment',
            reason='Phase 7D-4 history probe',
            quantity_change=5,
            quantity_before=0,
            quantity_after=5,
        )
        snapshot = (
            log.product_id, log.product_name, log.action, log.source,
            log.reason, log.quantity_change, log.quantity_before,
            log.quantity_after,
        )

        product.deactivate()

        log.refresh_from_db()
        self.assertEqual(
            (log.product_id, log.product_name, log.action, log.source,
             log.reason, log.quantity_change, log.quantity_before,
             log.quantity_after),
            snapshot,
        )
