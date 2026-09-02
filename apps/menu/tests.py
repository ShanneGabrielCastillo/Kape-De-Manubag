"""
Tests for the menu module's soft-delete (deactivation) workflow.

Products are never permanently deleted: deactivating one hides it from the
customer menu and the POS while preserving its row, ID and every historical
reference (order items, reports, finance records, analytics). This suite
covers the model-level guards, the admin toggle view, the ordering gates,
and the visibility rules.
"""

import json

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import CustomUser
from apps.inventory.models import InventoryLog
from apps.menu.models import Category, Product

PASSWORD = 'kdm-menu-pass-123'


class SoftDeleteModelTests(TestCase):
    """Product soft-deactivation lives on the model layer."""

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
        )

    def test_delete_soft_deactivates_instead_of_removing(self):
        self.product.delete()
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)
        self.assertIsNotNone(self.product.deactivated_at)

    def test_deactivate_preserves_id_and_category_relationship(self):
        product_pk = self.product.pk
        self.product.deactivate()
        self.product.refresh_from_db()
        self.assertEqual(self.product.pk, product_pk)
        self.assertEqual(self.product.category, self.category)

    def test_reactivate_clears_deactivated_at(self):
        self.product.deactivate()
        self.product.activate()
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)
        self.assertIsNone(self.product.deactivated_at)

    def test_bulk_delete_is_blocked(self):
        # QuerySet.delete() runs inside an internal atomic block; the
        # pre_delete guard raises there, so wrap the call in a savepoint to
        # keep the connection usable for the assertion below.
        from django.db import transaction
        with transaction.atomic():
            with self.assertRaises(ValidationError):
                Product.objects.filter(pk=self.product.pk).delete()
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())

    def test_category_delete_with_products_is_blocked(self):
        with self.assertRaises(ValidationError):
            self.category.delete()
        # The category and its products are untouched and still active.
        self.assertTrue(Category.objects.filter(pk=self.category.pk).exists())
        self.category.refresh_from_db()
        self.assertTrue(self.category.is_active)
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)
        self.assertEqual(self.product.category, self.category)

    def test_bulk_category_delete_with_products_is_blocked(self):
        # QuerySet.delete() runs inside an internal atomic block; the
        # pre_delete guard raises there, so wrap the call in a savepoint to
        # keep the connection usable for the assertions below.
        from django.db import transaction
        with transaction.atomic():
            with self.assertRaises(ValidationError):
                Category.objects.filter(pk=self.category.pk).delete()
        self.assertTrue(Category.objects.filter(pk=self.category.pk).exists())
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())

    def test_empty_category_still_deletes(self):
        empty = Category.objects.create(name='Empty', slug='empty')
        empty.delete()
        self.assertFalse(Category.objects.filter(pk=empty.pk).exists())


class CategoryDeleteViewTests(TestCase):
    """Deleting a category is blocked while it still contains products."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
        )

    def test_delete_empty_category_succeeds(self):
        empty = Category.objects.create(name='Empty', slug='empty')
        response = self.client.post(reverse('menu:category_delete', args=[empty.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertFalse(Category.objects.filter(pk=empty.pk).exists())

    def test_delete_category_with_products_is_blocked(self):
        response = self.client.post(
            reverse('menu:category_delete', args=[self.category.pk]),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertEqual(data['product_count'], 1)
        # Clear reason + guidance for the admin.
        self.assertIn('cannot be deleted', data['message'])
        self.assertIn('still has 1 product', data['message'])
        self.assertIn('another category', data['message'])

    def test_blocked_delete_changes_nothing(self):
        response = self.client.post(
            reverse('menu:category_delete', args=[self.category.pk]),
        )
        self.assertFalse(response.json()['success'])
        # Category still exists, is still active, and keeps its product.
        self.assertTrue(Category.objects.filter(pk=self.category.pk).exists())
        self.category.refresh_from_db()
        self.assertTrue(self.category.is_active)
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)
        self.assertEqual(self.product.category, self.category)

    def test_blocked_delete_requires_post(self):
        response = self.client.get(
            reverse('menu:category_delete', args=[self.category.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertTrue(Category.objects.filter(pk=self.category.pk).exists())


class CategoryValidationTests(TestCase):
    """Category names are trimmed and unique case-insensitively."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)

    def _category_data(self, **overrides):
        data = {
            'name': 'Beverages', 'icon': '☕', 'description': '',
            'is_active': 'on', 'is_packaging_required': '', 'order': '1',
        }
        data.update(overrides)
        return data

    def test_create_category_succeeds_and_trims_name(self):
        response = self.client.post(
            reverse('menu:category_create'),
            self._category_data(name='  Beverages  '),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Category.objects.filter(name='Beverages').exists())

    def test_create_with_duplicate_name_is_blocked(self):
        Category.objects.create(name='Beverages', slug='beverages')
        response = self.client.post(
            reverse('menu:category_create'), self._category_data(name='Beverages'),
        )
        self.assertEqual(response.status_code, 200)  # form re-rendered with errors
        self.assertContains(response, 'already exists')
        self.assertEqual(Category.objects.filter(name__iexact='Beverages').count(), 1)

    def test_duplicate_check_is_case_insensitive(self):
        Category.objects.create(name='Beverages', slug='beverages')
        response = self.client.post(
            reverse('menu:category_create'), self._category_data(name='BEVERAGES'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        self.assertEqual(Category.objects.count(), 1)

    def test_duplicate_check_trims_whitespace(self):
        Category.objects.create(name='Beverages', slug='beverages')
        response = self.client.post(
            reverse('menu:category_create'), self._category_data(name='  beverages  '),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        self.assertEqual(Category.objects.count(), 1)

    def test_duplicate_detects_whitespace_padded_legacy_name(self):
        # A legacy row with surrounding whitespace still blocks a new name.
        Category.objects.create(name='Beverages ', slug='beverages-padded')
        response = self.client.post(
            reverse('menu:category_create'), self._category_data(name='Beverages'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')

    def test_whitespace_only_name_is_rejected(self):
        # CharField strips whitespace, so a spaces-only name is rejected as
        # required before the model clean() runs; no category is created.
        response = self.client.post(
            reverse('menu:category_create'), self._category_data(name='   '),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This field is required')
        self.assertEqual(Category.objects.count(), 0)

    def test_model_clean_rejects_whitespace_only_name(self):
        # Direct full_clean() (bypassing the form's strip) gets a clear error.
        category = Category(name='   ')
        with self.assertRaises(ValidationError):
            category.full_clean()

    def test_edit_keeping_own_name_is_allowed(self):
        cat = Category.objects.create(name='Beverages', slug='beverages')
        response = self.client.post(
            reverse('menu:category_edit', args=[cat.pk]),
            self._category_data(name='Beverages', icon='🧋'),
        )
        self.assertEqual(response.status_code, 302)
        cat.refresh_from_db()
        self.assertEqual(cat.name, 'Beverages')

    def test_edit_renaming_to_existing_name_is_blocked(self):
        Category.objects.create(name='Beverages', slug='beverages')
        other = Category.objects.create(name='Snacks', slug='snacks')
        response = self.client.post(
            reverse('menu:category_edit', args=[other.pk]),
            self._category_data(name='beverages', icon='🍔'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        other.refresh_from_db()
        self.assertEqual(other.name, 'Snacks')

    def test_edit_legacy_duplicate_without_renaming_is_allowed(self):
        # Pre-existing case-variant duplicates (created via ORM, bypassing
        # clean) can still be edited as long as the name is unchanged -- this
        # preserves the existing editing workflow.
        Category.objects.create(name='Beverages', slug='beverages')
        twin = Category.objects.create(name='beverages', slug='beverages-2')
        response = self.client.post(
            reverse('menu:category_edit', args=[twin.pk]),
            self._category_data(name='beverages', icon='🥤'),
        )
        self.assertEqual(response.status_code, 302)
        twin.refresh_from_db()
        self.assertEqual(twin.name, 'beverages')

    def test_model_clean_blocks_duplicate_case_insensitively(self):
        Category.objects.create(name='Beverages', slug='beverages')
        duplicate = Category(name='BEVERAGES')
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_model_clean_trims_name(self):
        category = Category(name='  Beverages  ')
        category.full_clean()
        self.assertEqual(category.name, 'Beverages')


class CategoryStatusTests(TestCase):
    """Category Active/Inactive (soft-delete) workflow."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Beverages', slug='beverages')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
        )

    def _product_data(self, **overrides):
        data = {
            'category': self.category.pk, 'name': 'Latte', 'description': '',
            'price': '60.00', 'price_medium': '', 'price_large': '', 'price_hot': '',
            'has_sizes': '', 'is_available': 'on', 'is_featured': '',
            'stock_quantity': '50', 'low_stock_threshold': '10',
        }
        data.update(overrides)
        return data

    # ── Toggle endpoint ─────────────────────────────────────────────────────

    def test_toggle_active_deactivates_then_reactivates(self):
        response = self.client.post(
            reverse('menu:category_toggle_active', args=[self.category.pk]),
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], 'deactivated')
        self.category.refresh_from_db()
        self.assertFalse(self.category.is_active)

        response = self.client.post(
            reverse('menu:category_toggle_active', args=[self.category.pk]),
        )
        data = response.json()
        self.assertEqual(data['status'], 'reactivated')
        self.category.refresh_from_db()
        self.assertTrue(self.category.is_active)

    def test_toggle_active_requires_admin(self):
        cashier = CustomUser.objects.create_user(
            username='cashier1', password=PASSWORD, role='cashier',
        )
        self.client.force_login(cashier)
        response = self.client.post(
            reverse('menu:category_toggle_active', args=[self.category.pk]),
        )
        self.assertEqual(response.status_code, 302)
        self.category.refresh_from_db()
        self.assertTrue(self.category.is_active)

    # ── Visibility on selling surfaces ─────────────────────────────────────

    def test_inactive_category_hidden_from_customer_menu(self):
        self.category.deactivate()
        response = self.client.get(reverse('menu:index'))
        self.assertNotContains(response, 'Beverages')
        self.assertNotContains(response, 'Iced Coffee')

    def test_inactive_category_hidden_from_pos(self):
        self.category.deactivate()
        response = self.client.get(reverse('orders:pos'))
        self.assertNotContains(response, 'Beverages')
        self.assertNotContains(response, 'Iced Coffee')

    def test_reactivated_category_returns_to_menu(self):
        self.category.deactivate()
        self.category.activate()
        response = self.client.get(reverse('menu:index'))
        self.assertContains(response, 'Beverages')
        self.assertContains(response, 'Iced Coffee')

    # ── Product dropdowns ──────────────────────────────────────────────────

    def test_inactive_category_not_selectable_on_product_create(self):
        self.category.deactivate()
        response = self.client.post(
            reverse('menu:product_create'), self._product_data(),
        )
        self.assertEqual(response.status_code, 200)  # form re-rendered
        self.assertContains(response, 'Select a valid choice')
        self.assertFalse(Product.objects.filter(name='Latte').exists())

    def test_editing_product_in_inactive_category_keeps_category(self):
        # The product's own (inactive) category stays selectable so the edit
        # does not silently move the product.
        self.category.deactivate()
        response = self.client.post(
            reverse('menu:product_edit', args=[self.product.pk]),
            self._product_data(name='Iced Coffee', price='65.00'),
        )
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.category, self.category)
        self.assertEqual(self.product.price, 65)

    def test_inactive_category_can_still_be_chosen_explicitly_on_edit(self):
        # A product in an inactive category may be edited to another active
        # category in the same dropdown.
        active = Category.objects.create(name='Snacks', slug='snacks')
        self.category.deactivate()
        response = self.client.post(
            reverse('menu:product_edit', args=[self.product.pk]),
            self._product_data(name='Iced Coffee', category=active.pk),
        )
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.category, active)

    # ── Historical data integrity ──────────────────────────────────────────

    def test_deactivation_preserves_product_relationship(self):
        self.category.deactivate()
        self.product.refresh_from_db()
        self.assertEqual(self.product.category, self.category)
        self.assertTrue(self.product.is_active)
        # Product is still sellable (its own flags are untouched).
        self.assertTrue(self.product.is_available)


class ProductToggleViewTests(TestCase):
    """Deactivate/reactivate through the admin product toggle view."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
        )

    def test_toggle_active_deactivates_then_reactivates(self):
        response = self.client.post(
            reverse('menu:product_toggle_active', args=[self.product.pk]),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], 'deactivated')
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)

        response = self.client.post(
            reverse('menu:product_toggle_active', args=[self.product.pk]),
        )
        data = response.json()
        self.assertEqual(data['status'], 'reactivated')
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)

    def test_toggle_active_requires_admin(self):
        cashier = CustomUser.objects.create_user(
            username='cashier1', password=PASSWORD, role='cashier',
        )
        self.client.force_login(cashier)
        response = self.client.post(
            reverse('menu:product_toggle_active', args=[self.product.pk]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('dashboard:index'))
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)


class ProductVisibilityTests(TestCase):
    """Inactive products disappear from every selling surface."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
        )

    def test_active_product_is_on_customer_menu(self):
        response = self.client.get(reverse('menu:index'))
        self.assertContains(response, 'Iced Coffee')

    def test_deactivated_product_hidden_from_customer_menu(self):
        self.product.deactivate()
        response = self.client.get(reverse('menu:index'))
        self.assertNotContains(response, 'Iced Coffee')

    def test_deactivated_product_hidden_from_pos(self):
        self.product.deactivate()
        response = self.client.get(reverse('orders:pos'))
        self.assertNotContains(response, 'Iced Coffee')

    def test_reactivated_product_returns_to_menu(self):
        self.product.deactivate()
        self.product.activate()
        response = self.client.get(reverse('menu:index'))
        self.assertContains(response, 'Iced Coffee')


class OrderingGateTests(TestCase):
    """Inactive products can never be ordered."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
        )

    def test_inactive_product_cannot_be_added_to_cart(self):
        self.product.deactivate()
        response = self.client.post(
            reverse('orders:add_to_cart', args=[self.product.pk]),
            {'quantity': '1', 'size': 'none'},
        )
        self.assertEqual(response.status_code, 404)

    def test_inactive_product_cannot_be_ordered_via_pos(self):
        self.product.deactivate()
        response = self.client.post(
            reverse('orders:create_pos_order'),
            json.dumps({
                'customer_name': 'Walk-in Customer',
                'items': [{'product_id': self.product.pk, 'quantity': 1,
                           'size': 'none'}],
                'order_type': 'dine_in',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('no longer available', data['error'])
        # No order was persisted for the rejected item.
        from apps.orders.models import Order
        self.assertFalse(Order.objects.exists())

    def test_checkout_removes_deactivated_cart_items(self):
        from apps.orders.models import Cart, CartItem, Order
        # Attach a cart to the test client's session.
        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(
            cart=cart, product=self.product, size='none',
            quantity=1, unit_price=60.00,
        )
        self.product.deactivate()

        response = self.client.post(reverse('orders:checkout'), {
            'customer_name': 'Test Customer', 'order_type': 'dine_in',
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('orders:cart'))
        self.assertFalse(CartItem.objects.filter(product=self.product).exists())
        self.assertFalse(Order.objects.exists())


class ProductValidationTests(TestCase):
    """Product names are trimmed and unique within their category."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.drinks = Category.objects.create(name='Drinks', slug='drinks')
        self.snacks = Category.objects.create(name='Snacks', slug='snacks')
        self.product = Product.objects.create(
            category=self.drinks, name='Iced Coffee', price='60.00',
        )

    def _product_data(self, **overrides):
        data = {
            'category': self.drinks.pk, 'name': 'Latte', 'description': '',
            'price': '60.00', 'price_medium': '', 'price_large': '', 'price_hot': '',
            'has_sizes': '', 'is_available': 'on', 'is_featured': '',
            'stock_quantity': '50', 'low_stock_threshold': '10',
        }
        data.update(overrides)
        return data

    def test_create_with_duplicate_name_in_same_category_is_blocked(self):
        response = self.client.post(
            reverse('menu:product_create'), self._product_data(name='Iced Coffee'),
        )
        self.assertEqual(response.status_code, 200)  # form re-rendered with errors
        self.assertContains(response, 'already exists')
        self.assertEqual(
            Product.objects.filter(category=self.drinks, name='Iced Coffee').count(),
            1,
        )

    def test_duplicate_check_is_case_insensitive(self):
        response = self.client.post(
            reverse('menu:product_create'), self._product_data(name='iced coffee'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        self.assertEqual(Product.objects.count(), 1)

    def test_duplicate_check_trims_whitespace(self):
        response = self.client.post(
            reverse('menu:product_create'), self._product_data(name='  Iced Coffee  '),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        self.assertEqual(Product.objects.count(), 1)

    def test_same_name_in_different_category_is_allowed(self):
        response = self.client.post(
            reverse('menu:product_create'),
            self._product_data(category=self.snacks.pk, name='Iced Coffee'),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product.objects.filter(
            category=self.snacks, name='Iced Coffee',
        ).exists())

    def test_whitespace_is_trimmed_before_save(self):
        response = self.client.post(
            reverse('menu:product_create'), self._product_data(name='  Hot Coffee  '),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product.objects.filter(name='Hot Coffee').exists())

    def test_edit_keeping_own_name_is_allowed(self):
        response = self.client.post(
            reverse('menu:product_edit', args=[self.product.pk]),
            self._product_data(name='Iced Coffee', price='65.00'),
        )
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, 65)

    def test_edit_renaming_to_existing_name_is_blocked(self):
        other = Product.objects.create(
            category=self.drinks, name='Latte', price='50.00',
        )
        response = self.client.post(
            reverse('menu:product_edit', args=[other.pk]),
            self._product_data(name='Iced Coffee', price='55.00'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        other.refresh_from_db()
        self.assertEqual(other.name, 'Latte')

    def test_model_clean_blocks_duplicate_case_insensitively(self):
        duplicate = Product(
            category=self.drinks, name='ICED COFFEE', price='70.00',
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_model_clean_allows_same_name_in_different_category(self):
        # Must not raise: 'Iced Coffee' already exists in Drinks, but this
        # one lives in Snacks.
        Product(category=self.snacks, name='Iced Coffee', price='70.00').full_clean()

    def test_edit_legacy_duplicate_without_renaming_is_allowed(self):
        # A pre-existing duplicate (created via ORM, bypassing clean) can
        # still be edited as long as the name is not changed -- preserving
        # the editing workflow without forcing a rename.
        twin = Product.objects.create(
            category=self.drinks, name='Iced Coffee', price='55.00',
        )
        response = self.client.post(
            reverse('menu:product_edit', args=[twin.pk]),
            self._product_data(name='Iced Coffee', price='58.00'),
        )
        self.assertEqual(response.status_code, 302)
        twin.refresh_from_db()
        self.assertEqual(twin.price, 58)


class ProductImageTests(TestCase):
    """Product images fall back to a shared placeholder when unavailable."""

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Plain Coffee', price='50.00',
        )

    def _placeholder(self):
        from django.templatetags.static import static
        return static('images/placeholder.svg')

    def test_no_image_uses_placeholder(self):
        from apps.menu.templatetags.product_images import resolve_product_image_url
        self.assertEqual(
            resolve_product_image_url(self.product), self._placeholder(),
        )

    def test_missing_image_file_falls_back_to_placeholder(self):
        from apps.menu.templatetags.product_images import resolve_product_image_url
        # Field is set but the file does not exist on disk.
        self.product.image = 'products/ghost.png'
        self.product.save(update_fields=['image'])
        self.assertEqual(
            resolve_product_image_url(self.product), self._placeholder(),
        )

    def test_with_image_uses_its_own_url(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from apps.menu.templatetags.product_images import resolve_product_image_url
        png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                img = SimpleUploadedFile('coffee.png', png, content_type='image/png')
                product = Product.objects.create(
                    category=self.category, name='With Image', price='60.00',
                    image=img,
                )
                self.assertEqual(
                    resolve_product_image_url(product), product.image.url,
                )

    def test_product_image_tag_renders_img_with_fallback(self):
        from django.template import Context, Template
        html = Template(
            '{% load product_images %}{% product_image product %}'
        ).render(Context({'product': self.product}))
        # static() resolves the placeholder with its storage hash.
        self.assertIn(self._placeholder(), html)
        self.assertIn('class="product-image', html)
        self.assertIn('onerror=', html)

    def test_customer_menu_renders_placeholder_for_imageless_products(self):
        response = self.client.get(reverse('menu:index'))
        self.assertContains(response, self._placeholder())
        # No product uploads exist in the test DB, so no /media/ URLs appear.
        self.assertNotContains(response, '/media/')


class HistoricalDataPreservationTests(TestCase):
    """Deactivating a product never touches its historical records."""

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
        )

    def test_order_items_survive_deactivation(self):
        from apps.orders.models import Order, OrderItem
        order = Order.objects.create(customer_name='Test Customer')
        OrderItem.objects.create(
            order=order, product=self.product, product_name=self.product.name,
            size='none', quantity=2, unit_price=60.00, subtotal=120.00,
        )
        self.product.deactivate()
        order_item = OrderItem.objects.get(order=order)
        self.assertEqual(order_item.product.pk, self.product.pk)
        self.assertEqual(order_item.product_name, 'Iced Coffee')


class ActiveOrderLifecycleGuardTests(TestCase):
    """Deactivation/availability changes are gated on live-order references.

    A product referenced by active (pending/preparing/ready) orders needs an
    explicit confirmation before it can be deactivated or marked unavailable.
    Historical (completed/cancelled) orders never trigger the guard, and the
    action itself never touches saved line items.
    """

    ACTIVE = ['pending', 'preparing', 'ready']
    FINAL = ['completed', 'cancelled']

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
        )

    def _order_with_product(self, status):
        from apps.orders.models import Order, OrderItem
        order = Order.objects.create(customer_name='Test Customer', status=status)
        OrderItem.objects.create(
            order=order, product=self.product, product_name=self.product.name,
            size='none', quantity=1, unit_price=60.00, subtotal=60.00,
        )
        return order

    # ── Deactivation guard ──────────────────────────────────────────────────

    def test_deactivation_requires_confirmation_when_in_active_order(self):
        self._order_with_product('pending')
        response = self.client.post(
            reverse('menu:product_toggle_active', args=[self.product.pk]),
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertTrue(data['requires_confirmation'])
        self.assertEqual(data['active_order_count'], 1)
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)  # nothing changed without confirm

    def test_deactivation_proceeds_with_confirmation(self):
        self._order_with_product('pending')
        response = self.client.post(
            reverse('menu:product_toggle_active', args=[self.product.pk]),
            {'confirm': '1'},
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], 'deactivated')
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)

    def test_guard_applies_to_all_active_statuses(self):
        for status in self.ACTIVE:
            order = self._order_with_product(status)
            response = self.client.post(
                reverse('menu:product_toggle_active', args=[self.product.pk]),
            )
            data = response.json()
            self.assertFalse(data['success'], f'guard failed for {status}')
            self.assertTrue(data['requires_confirmation'])
            order.delete()  # (soft path irrelevant here; Order has no guard)

    def test_historical_orders_never_trigger_confirmation(self):
        self._order_with_product('completed')
        self._order_with_product('cancelled')
        response = self.client.post(
            reverse('menu:product_toggle_active', args=[self.product.pk]),
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)

    def test_reactivation_never_requires_confirmation(self):
        self._order_with_product('preparing')
        self.product.deactivate()
        response = self.client.post(
            reverse('menu:product_toggle_active', args=[self.product.pk]),
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], 'reactivated')
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)

    def test_confirmed_deactivation_keeps_active_order_items_intact(self):
        from apps.orders.models import OrderItem
        order = self._order_with_product('preparing')
        self.client.post(
            reverse('menu:product_toggle_active', args=[self.product.pk]),
            {'confirm': '1'},
        )
        order_item = OrderItem.objects.get(order=order)
        self.assertEqual(order_item.product.pk, self.product.pk)
        self.assertEqual(order_item.product_name, 'Iced Coffee')
        self.assertEqual(order_item.unit_price, 60)

    # ── Availability guard ──────────────────────────────────────────────────

    def test_marking_unavailable_requires_confirmation_in_active_order(self):
        self._order_with_product('ready')
        response = self.client.post(
            reverse('menu:product_toggle', args=[self.product.pk]),
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertTrue(data['requires_confirmation'])
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_available)

    def test_marking_unavailable_proceeds_with_confirmation(self):
        self._order_with_product('preparing')
        response = self.client.post(
            reverse('menu:product_toggle', args=[self.product.pk]),
            {'confirm': '1'},
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['is_available'])
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_available)

    def test_marking_available_never_requires_confirmation(self):
        self._order_with_product('pending')
        self.product.is_available = False
        self.product.save(update_fields=['is_available'])
        response = self.client.post(
            reverse('menu:product_toggle', args=[self.product.pk]),
        )
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_available'])

    # ── Visibility of references ────────────────────────────────────────────

    def test_product_list_reports_active_order_references(self):
        self._order_with_product('pending')
        self._order_with_product('completed')
        response = self.client.get(reverse('menu:product_list'))
        self.assertEqual(response.status_code, 200)
        # Row badge for the pending-order reference and the page-level alert.
        self.assertContains(response, 'in 1 active order')
        self.assertContains(response, 'currently referenced by active orders')

    def test_edit_page_warns_when_product_is_in_active_orders(self):
        self._order_with_product('preparing')
        response = self.client.get(reverse('menu:product_edit', args=[self.product.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'active order')

    def test_edit_page_no_warning_for_historical_only_references(self):
        self._order_with_product('completed')
        response = self.client.get(reverse('menu:product_edit', args=[self.product.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'active order')


class NegativeStockSafeguardTests(TestCase):
    """Stock can never drop below zero, even under simultaneous deductions.

    ``reduce_stock`` decrements with a single conditional UPDATE, so the
    availability check and the subtraction are one atomic statement -- two
    orders that both passed a stale pre-check cannot both deduct. The
    database CHECK constraint is the final backstop for any code path that
    writes stock directly.
    """

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=5,
        )

    def test_reduce_stock_succeeds_with_enough_stock(self):
        self.product.reduce_stock(3)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 2)

    def test_reduce_stock_rejects_more_than_available(self):
        with self.assertRaisesMessage(
            ValueError,
            "Insufficient stock for 'Iced Coffee'. Available: 5, Requested: 6",
        ):
            self.product.reduce_stock(6)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 5)  # untouched

    def test_simultaneous_deductions_never_go_negative(self):
        # Simulate two concurrent orders that both read stock=5 before either
        # deducted (the classic check-then-write race). The second deduction
        # must fail atomically -- never drive stock below zero.
        first = Product.objects.get(pk=self.product.pk)
        second = Product.objects.get(pk=self.product.pk)
        first.reduce_stock(4)  # 5 -> 1
        with self.assertRaisesMessage(
            ValueError,
            "Insufficient stock for 'Iced Coffee'. Available: 1, Requested: 4",
        ):
            second.reduce_stock(4)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)
        self.assertGreaterEqual(self.product.stock_quantity, 0)

    def test_repeated_partial_deductions_never_go_negative(self):
        # Deduct 2 at a time until exhausted: every deduction succeeds while
        # enough stock remains, then the next one fails -- never negative.
        for _ in range(10):
            try:
                self.product.reduce_stock(2)
            except ValueError:
                break
        self.product.refresh_from_db()
        self.assertGreaterEqual(self.product.stock_quantity, 0)
        # 5 is not divisible by 2, so exactly 1 unit must remain.
        self.assertEqual(self.product.stock_quantity, 1)

    def test_database_rejects_negative_stock_directly(self):
        # A raw queryset update bypasses model validation AND signals, so the
        # CHECK constraint is the only thing standing between this and a
        # negative stock value.
        from django.db import IntegrityError, transaction
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Product.objects.filter(pk=self.product.pk).update(
                    stock_quantity=-3,
                )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 5)

    def test_create_with_negative_stock_is_rejected(self):
        from django.db import IntegrityError, transaction
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Product.objects.create(
                    category=self.category, name='Ghost Item', price='1.00',
                    stock_quantity=-1,
                )
        self.assertFalse(Product.objects.filter(name='Ghost Item').exists())

    def test_restore_stock_never_goes_negative(self):
        # Restoring is additive and cannot underflow, but it must keep
        # working normally after deductions.
        self.product.reduce_stock(5)  # 5 -> 0
        self.product.restore_stock(3)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 3)

    def test_reduce_stock_still_publishes_low_stock_realtime_event(self):
        # Deductions now use QuerySet.update() (which bypasses save()), so the
        # post_save signal is replayed manually. This proves the realtime
        # low-stock alert still fires when a sale crosses the threshold.
        import queue
        from apps.realtime.broker import subscribe, unsubscribe

        self.product.low_stock_threshold = 4
        self.product.stock_quantity = 6
        self.product.save(update_fields=['stock_quantity', 'low_stock_threshold'])

        q = subscribe()
        try:
            self.product.reduce_stock(3)  # 6 -> 3, now below the threshold
            events = []
            while True:
                try:
                    events.append(q.get_nowait())
                except queue.Empty:
                    break
            low_stock = [e for e in events if e['event'] == 'inventory_low']
            self.assertEqual(len(low_stock), 1)
            self.assertEqual(low_stock[0]['data']['stock_quantity'], 3)
        finally:
            unsubscribe(q)


class InventoryUpdateLogTests(TestCase):
    """Stock changes made through the product create/edit forms are audited.

    The stock itself behaves exactly as before -- these tests only verify the
    inventory audit trail records the movement with source ``inventory_update``.
    """

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=20,
        )

    def _product_data(self, **overrides):
        data = {
            'category': self.category.pk, 'name': 'Latte', 'description': '',
            'price': '60.00', 'price_medium': '', 'price_large': '', 'price_hot': '',
            'has_sizes': '', 'is_available': 'on', 'is_featured': '',
            'stock_quantity': '50', 'low_stock_threshold': '10',
        }
        data.update(overrides)
        return data

    def test_create_logs_initial_stock(self):
        response = self.client.post(
            reverse('menu:product_create'), self._product_data(name='Latte'),
        )
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(name='Latte')
        log = InventoryLog.objects.get(product=product)
        self.assertEqual(log.source, 'inventory_update')
        self.assertEqual(log.reason, 'Initial stock')
        self.assertEqual(log.quantity_before, 0)
        self.assertEqual(log.quantity_after, 50)
        self.assertEqual(log.quantity_change, 50)
        self.assertEqual(log.performed_by, self.admin)

    def test_create_with_zero_stock_logs_nothing(self):
        response = self.client.post(
            reverse('menu:product_create'),
            self._product_data(name='Latte', stock_quantity='0'),
        )
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(name='Latte')
        self.assertFalse(InventoryLog.objects.filter(product=product).exists())

    def test_edit_changed_stock_logs_movement(self):
        response = self.client.post(
            reverse('menu:product_edit', args=[self.product.pk]),
            self._product_data(name='Iced Coffee', stock_quantity='30'),
        )
        self.assertEqual(response.status_code, 302)
        log = InventoryLog.objects.get(product=self.product)
        self.assertEqual(log.source, 'inventory_update')
        self.assertEqual(log.reason, 'Product stock update')
        self.assertEqual(log.quantity_before, 20)
        self.assertEqual(log.quantity_after, 30)
        self.assertEqual(log.quantity_change, 10)

    def test_edit_unchanged_stock_logs_nothing(self):
        response = self.client.post(
            reverse('menu:product_edit', args=[self.product.pk]),
            self._product_data(name='Iced Coffee', stock_quantity='20'),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(InventoryLog.objects.filter(product=self.product).exists())


class StockStatusTests(TestCase):
    """The shared stock-status definitions stay consistent at every level.

    ``stock_status`` (and its ``is_critical_stock`` / ``is_low_stock``
    halves) are the single source of truth used by the dashboard, inventory,
    product management and POS templates.
    """

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')

    def _product(self, stock, threshold=10):
        return Product.objects.create(
            category=self.category, name=f'Item {stock}/{threshold}',
            price='60.00', stock_quantity=stock,
            low_stock_threshold=threshold,
        )

    def test_default_threshold_levels(self):
        # 0 and below are critical too (out of stock is the worst case).
        for stock in (0, 3, 5):
            product = self._product(stock)
            self.assertEqual(product.stock_status, 'critical', f'stock={stock}')
            self.assertTrue(product.is_critical_stock, f'stock={stock}')
        # 6..10 are low (default threshold is 10).
        for stock in (6, 10):
            product = self._product(stock)
            self.assertEqual(product.stock_status, 'low', f'stock={stock}')
            self.assertFalse(product.is_critical_stock, f'stock={stock}')
            self.assertTrue(product.is_low_stock, f'stock={stock}')
        # Above the threshold is OK.
        product = self._product(11)
        self.assertEqual(product.stock_status, 'ok')
        self.assertFalse(product.is_low_stock)
        self.assertFalse(product.is_critical_stock)

    def test_custom_threshold_respected(self):
        # A product with a custom threshold is low relative to ITS OWN
        # threshold, not a hard-coded number.
        low = self._product(stock=15, threshold=20)
        self.assertEqual(low.stock_status, 'low')
        self.assertTrue(low.is_low_stock)
        # Critical is still the shared threshold regardless of the custom one.
        critical = self._product(stock=4, threshold=20)
        self.assertEqual(critical.stock_status, 'critical')
        self.assertTrue(critical.is_critical_stock)
        ok = self._product(stock=21, threshold=20)
        self.assertEqual(ok.stock_status, 'ok')


class StockStatusRenderingTests(TestCase):
    """Every low-stock surface renders the SAME stock status for the same
    product: dashboard, inventory, product management and POS."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='menu_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')

    def _product(self, name, stock, threshold=10):
        return Product.objects.create(
            category=self.category, name=name, price='60.00',
            stock_quantity=stock, low_stock_threshold=threshold,
        )

    def test_surfaces_agree_on_status(self):
        self._product('Crit', stock=3)
        self._product('Lowish', stock=8)
        self._product('CustomLow', stock=15, threshold=20)
        self._product('Fine', stock=50)
        self._product('Gone', stock=0)

        # Inventory: banner count + per-row badges.
        inv = self.client.get(reverse('inventory:list'))
        self.assertContains(inv, '4 products')
        self.assertContains(inv, 'with low stock')
        self.assertContains(inv, '2 critical')
        self.assertContains(inv, '>Critical<')
        self.assertContains(inv, '>Low<')
        self.assertContains(inv, '>OK<')

        # Product management: indicator classes match the same status.
        prods = self.client.get(reverse('menu:product_list'))
        self.assertContains(prods, 'stock-critical')
        self.assertContains(prods, 'stock-low')
        self.assertContains(prods, 'stock-ok')

        # POS: out-of-stock vs critical vs low badges; OK products get none.
        pos = self.client.get(reverse('orders:pos'))
        self.assertContains(pos, 'Out of Stock')
        self.assertContains(pos, 'stock-badge--out')
        self.assertContains(pos, 'stock-badge--critical')
        self.assertContains(pos, 'stock-badge--low')
        self.assertContains(pos, '>3 left<')
        self.assertContains(pos, '>8 left<')
        self.assertContains(pos, '>15 left<')
        self.assertNotContains(pos, '>50 left<')

        # Dashboard: the low-stock card labels critical vs low with the
        # remaining count.
        dash = self.client.get(reverse('dashboard:index'))
        self.assertContains(dash, 'Critical: 3 left')
        self.assertContains(dash, 'Low: 8 left')
        self.assertContains(dash, 'Low: 15 left')
        self.assertContains(dash, 'Critical: 0 left')


class ProductStockEndpointTests(TestCase):
    """The public menu's live availability poll reports stock correctly."""

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )

    def test_reports_stock_for_active_products(self):
        response = self.client.get(reverse('menu:product_stock'))
        self.assertEqual(response.status_code, 200)
        products = response.json()['products']
        self.assertIn(str(self.product.pk), products)
        info = products[str(self.product.pk)]
        self.assertEqual(info['stock_quantity'], 10)
        self.assertTrue(info['is_available'])
        self.assertTrue(info['is_active'])

    def test_reports_unavailable_and_out_of_stock_states(self):
        self.product.is_available = False
        self.product.save(update_fields=['is_available'])
        response = self.client.get(reverse('menu:product_stock'))
        info = response.json()['products'][str(self.product.pk)]
        self.assertFalse(info['is_available'])

        self.product.is_available = True
        self.product.stock_quantity = 0
        self.product.save(update_fields=['is_available', 'stock_quantity'])
        response = self.client.get(reverse('menu:product_stock'))
        info = response.json()['products'][str(self.product.pk)]
        self.assertEqual(info['stock_quantity'], 0)

    def test_inactive_products_are_not_reported(self):
        self.product.deactivate()
        response = self.client.get(reverse('menu:product_stock'))
        self.assertNotIn(str(self.product.pk), response.json()['products'])

    def test_menu_page_marks_out_of_stock_products(self):
        # The customer menu must clearly label (and disable) a product that
        # has run out of stock instead of hiding the problem.
        self.product.stock_quantity = 0
        self.product.save(update_fields=['stock_quantity'])
        response = self.client.get(reverse('menu:index'))
        self.assertContains(response, 'out-of-stock-label')
        self.assertContains(response, 'Out of Stock')
        # No add-to-cart form (its hidden quantity input) is rendered for the
        # out-of-stock card, so it cannot be added.
        self.assertNotContains(response, 'name="quantity"')


class InventoryChangedRealtimeEventTests(TestCase):
    """Stock movements publish the inventory_changed event that keeps the
    POS cards and current order in sync."""

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=5, low_stock_threshold=10,
        )

    def _drain(self, q):
        import queue
        events = []
        while True:
            try:
                events.append(q.get_nowait())
            except queue.Empty:
                return events

    def test_reduce_stock_publishes_inventory_changed(self):
        import queue
        from apps.realtime.broker import subscribe, unsubscribe

        q = subscribe()
        try:
            self.product.reduce_stock(2)  # 5 -> 3
            events = self._drain(q)
        finally:
            unsubscribe(q)

        changed = [e for e in events if e['event'] == 'inventory_changed']
        self.assertEqual(len(changed), 1)
        data = changed[0]['data']
        self.assertEqual(data['product_id'], self.product.pk)
        self.assertEqual(data['product_name'], 'Iced Coffee')
        self.assertEqual(data['stock_quantity'], 3)
        self.assertEqual(data['low_stock_threshold'], 10)
        self.assertTrue(data['is_available'])
        self.assertTrue(data['is_active'])

    def test_availability_change_publishes_inventory_changed(self):
        import queue
        from apps.realtime.broker import subscribe, unsubscribe

        q = subscribe()
        try:
            self.product.is_available = False
            self.product.save(update_fields=['is_available'])
            events = self._drain(q)
        finally:
            unsubscribe(q)

        changed = [e for e in events if e['event'] == 'inventory_changed']
        self.assertEqual(len(changed), 1)
        self.assertFalse(changed[0]['data']['is_available'])

    def test_restore_stock_publishes_inventory_changed(self):
        import queue
        from apps.realtime.broker import subscribe, unsubscribe

        self.product.stock_quantity = 0
        self.product.save(update_fields=['stock_quantity'])

        q = subscribe()
        try:
            self.product.restore_stock(5)  # 0 -> 5
            events = self._drain(q)
        finally:
            unsubscribe(q)

        changed = [e for e in events if e['event'] == 'inventory_changed']
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]['data']['stock_quantity'], 5)
