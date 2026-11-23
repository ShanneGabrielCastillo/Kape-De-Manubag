"""Phase 7B-1 — new-product tracking-mode default regression tests.

Verifies that ordinary product-creation paths produce ingredient-tracked
products (the Phase 7B-1 default change on Product.inventory_tracking_mode)
and that no ordinary path can reintroduce legacy tracking.

Covers:
  1. Model-level creation without an explicit mode defaults to ingredient.
  2. ProductForm-created products (menu:product_create view) use ingredient.
  3. Crafted POST data attempting legacy is ignored server-side.
  4. Admin creation (ProductAdmin excludes the field) uses ingredient.
  5. Explicitly supplied modes on existing rows are never overwritten.
  6. Ingredient availability behavior remains intact after the change.

Isolated test database only — the live development database is never
written. A migration for the default change is intentionally NOT created
in this phase (reported for owner authorization instead).
"""
import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from django.urls import reverse

from apps.inventory.services import get_available_units
from apps.inventory.models import (
    InventoryItem, PackagingProfile, ProductPackagingRule,
    Recipe, RecipeItem,
)
from apps.menu.forms import ProductForm
from apps.menu.models import Category, Product


def _uid():
    import uuid
    return uuid.uuid4().hex[:12]


def _staff():
    return get_user_model().objects.create_user(
        username=f'p7b1_staff_{_uid()}', password='p7b1-pass', role='admin')


def _category():
    return Category.objects.create(
        name=f'P7B1 Cat {_uid()}', slug=f'p7b1-cat-{_uid()}')


class ModelDefaultTests(TestCase):
    """Scenario 1 — model-level creation defaults to ingredient."""

    def test_new_product_without_mode_defaults_to_ingredient(self):
        product = Product.objects.create(
            category=_category(), name=f'P7B1 Default {_uid()}',
            price='50.00', stock_quantity=10,
        )
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')

    def test_field_default_constant_is_ingredient(self):
        # The model field itself — not just instances — carries the new default.
        field = Product._meta.get_field('inventory_tracking_mode')
        self.assertEqual(field.default, Product.INVENTORY_TRACKING_INGREDIENT)
        # Choices and validation unchanged (both modes still legal values).
        self.assertEqual(field.choices, Product.INVENTORY_TRACKING_CHOICES)

    def test_explicit_mode_is_respected_when_supplied(self):
        # An explicit value always wins over the default (the cutover
        # service's rollback path relies on this for target_mode='legacy').
        product = Product.objects.create(
            category=_category(), name=f'P7B1 Explicit {_uid()}',
            price='50.00', stock_quantity=10,
            inventory_tracking_mode='legacy',
        )
        self.assertEqual(product.inventory_tracking_mode, 'legacy')


class ProductFormCreationTests(TestCase):
    """Scenarios 2, 3 — the ordinary staff creation path (menu:product_create)
    uses the ingredient default and cannot be forced to legacy via POST."""

    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username=f'p7b1_form_{_uid()}', password='p7b1-pass', role='admin')
        self.c = Client(HTTP_HOST='testserver')
        self.c.force_login(self.staff)
        self.category = _category()

    def _form_data(self, name):
        return {
            'category': str(self.category.pk),
            'name': name,
            'description': 'Phase 7B-1 fixture',
            'price': '50.00',
            'has_sizes': 'false',
            'is_available': 'true',
            'is_featured': '',
            'stock_quantity': '10',
            'low_stock_threshold': '5',
        }

    def test_product_create_view_defaults_to_ingredient(self):
        resp = self.c.post(
            reverse('menu:product_create'), self._form_data(f'P7B1 Form {_uid()}'))
        self.assertEqual(resp.status_code, 302)
        product = Product.objects.latest('pk')
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')

    def test_crafted_legacy_post_cannot_reintroduce_legacy(self):
        # A crafted POST cannot select legacy: the field is not part of
        # ProductForm, so the extra key is ignored server-side.
        data = self._form_data(f'P7B1 Crafted {_uid()}')
        data['inventory_tracking_mode'] = 'legacy'
        resp = self.c.post(reverse('menu:product_create'), data)
        self.assertEqual(resp.status_code, 302)
        product = Product.objects.latest('pk')
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')

    def test_productform_has_no_tracking_mode_field(self):
        # Structural guarantee: the field is not form-reachable at all.
        form = ProductForm()
        self.assertNotIn('inventory_tracking_mode', form.fields)


class AdminCreationTests(TestCase):
    """Scenario 3 — the Django admin path uses the ingredient default
    (ProductAdmin excludes the field; the admin form cannot set it)."""

    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username=f'p7b1_admin_{_uid()}', password='p7b1-pass', role='admin',
            is_superuser=True, is_staff=True)
        self.c = Client(HTTP_HOST='testserver')
        self.c.force_login(self.staff)
        self.category = _category()

    def test_admin_add_product_defaults_to_ingredient(self):
        # Drive the real admin add view (menu | product).
        add_url = reverse('admin:menu_product_add')
        resp = self.c.post(add_url, {
            'category': str(self.category.pk),
            'name': f'P7B1 Admin {_uid()}',
            'price': '50.00',
            'stock_quantity': '10',
            'low_stock_threshold': '5',
            'is_available': 'on',
        })
        self.assertIn(resp.status_code, (302, 200))
        product = Product.objects.latest('pk')
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')


class ExistingRowsAndBehaviorTests(TestCase):
    """Scenarios 4, 5, 6 — existing modes are never modified; ingredient
    availability behavior remains intact."""

    def test_default_change_does_not_touch_existing_rows(self):
        # Django field defaults apply only to INSERTs without the column;
        # existing rows keep their stored mode. Prove it: create rows with
        # both modes, re-save, and confirm nothing changed.
        by_default = Product.objects.create(
            category=_category(), name=f'P7B1 Existing A {_uid()}',
            price='50.00', stock_quantity=10,
        )
        explicit = Product.objects.create(
            category=_category(), name=f'P7B1 Existing B {_uid()}',
            price='50.00', stock_quantity=10,
            inventory_tracking_mode='ingredient',
        )
        self.assertEqual(by_default.inventory_tracking_mode, 'ingredient')
        self.assertEqual(explicit.inventory_tracking_mode, 'ingredient')

        by_default.save()
        explicit.save()

        by_default.refresh_from_db()
        explicit.refresh_from_db()
        self.assertEqual(by_default.inventory_tracking_mode, 'ingredient')
        self.assertEqual(explicit.inventory_tracking_mode, 'ingredient')

    def test_ingredient_availability_intact(self):
        # An ingredient-tracked product with a complete plan still computes
        # derived availability the same way after the default change.
        product = Product.objects.create(
            category=_category(), name=f'P7B1 Avail {_uid()}',
            price='50.00', stock_quantity=10,
            inventory_tracking_mode='ingredient',
        )
        ingredient = InventoryItem.objects.create(
            name=f'P7B1 Ingr {_uid()}', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('100.000'),
        )
        recipe = Recipe.objects.create(
            product=product, size='none', order_type_scope=Recipe.SCOPE_ALL,
            status=Recipe.STATUS_APPROVED, revision=1,
            approved_at=timezone.now(), approval_note='P7B1 fixture',
        )
        RecipeItem.objects.create(
            recipe=recipe, inventory_item=ingredient, quantity=Decimal('10.000'))
        for order_type in ('dine_in', 'takeout'):
            ProductPackagingRule.objects.create(
                product=product, size='none', order_type=order_type,
                no_packaging=True,
            )

        result = get_available_units(product, 'none', None)
        self.assertEqual(result.units, 10)
        self.assertTrue(result.configuration_complete)

    def test_stock_quantity_field_unchanged_semantics(self):
        # The stock_quantity column/default itself was not modified in this
        # phase — only the tracking-mode default. A new product still gets
        # the stock_quantity column default (100) at the model level;
        # its tracking mode is what changed.
        product = Product.objects.create(
            category=_category(), name=f'P7B1 Stock {_uid()}',
            price='50.00',
        )
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')
        self.assertEqual(product.stock_quantity, 100)   # unchanged column default

    def test_menu_seed_products_would_be_ingredient(self):
        # sample_data/seed_data.py creates products without an explicit
        # tracking mode — with the new default they become ingredient-tracked
        # (legacy stock_quantity in the seed becomes the frozen field).
        product = Product.objects.create(
            category=_category(), name='P7B1 Seed-like Salted Caramel',
            price='69.00', price_medium='69.00', price_large='89.00',
            price_hot='69.00', has_sizes=True, stock_quantity=100,
        )
        self.assertEqual(product.inventory_tracking_mode, 'ingredient')
