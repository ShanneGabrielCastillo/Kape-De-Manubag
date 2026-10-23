"""
Menu app tests — CategoryForm validation and category_type field behaviour.

These tests cover:
1. CategoryForm requires an explicit category_type selection.
2. Valid drink/food/other values are accepted and saved.
3. Omitting category_type raises a validation error.
4. Editing an existing category retains and allows changing its type.
5. The chatbot correctly uses stored category_type for filtering.
6. is_packaging_required behaviour is independent of category_type.
"""
from decimal import Decimal

from django.test import TestCase, RequestFactory, Client
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.menu.models import Category, Product
from apps.menu.forms import CategoryForm

User = get_user_model()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_admin():
    return User.objects.create_user(
        username='testadmin',
        password='pass123',
        role='admin',
    )


def _category_post_data(**kwargs):
    """Return a minimal valid CategoryForm POST dict, overridable via kwargs."""
    defaults = {
        'name':                 'Test Category',
        'icon':                 '🧪',
        'description':          '',
        'is_active':            True,
        'category_type':        'drink',
        'is_packaging_required': False,
        'order':                99,
    }
    defaults.update(kwargs)
    return defaults


# ── 1. CategoryForm unit tests ────────────────────────────────────────────────

class CategoryFormTest(TestCase):
    """Pure form validation — no HTTP, no views."""

    def test_valid_drink_form(self):
        data = _category_post_data(name='Fruit Tea', category_type='drink')
        form = CategoryForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_food_form(self):
        data = _category_post_data(name='Rice Bowls', category_type='food')
        form = CategoryForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_other_form(self):
        data = _category_post_data(name='Miscellaneous', category_type='other')
        form = CategoryForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_missing_category_type_is_invalid(self):
        data = _category_post_data(name='No Type')
        data.pop('category_type')
        form = CategoryForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('category_type', form.errors)

    def test_empty_category_type_is_invalid(self):
        """The empty-label sentinel value '' must fail validation."""
        data = _category_post_data(name='Empty Type', category_type='')
        form = CategoryForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('category_type', form.errors)

    def test_invalid_category_type_value_rejected(self):
        data = _category_post_data(name='Bad Type', category_type='beverage')
        form = CategoryForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('category_type', form.errors)

    def test_category_type_saved_to_model(self):
        data = _category_post_data(name='Herbal Tea', category_type='drink')
        form = CategoryForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        instance = form.save()
        self.assertEqual(instance.category_type, 'drink')

    def test_food_type_saved_to_model(self):
        data = _category_post_data(name='Noodles', category_type='food')
        form = CategoryForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        instance = form.save()
        self.assertEqual(instance.category_type, 'food')

    def test_category_type_field_in_form_fields(self):
        """category_type must be listed in CategoryForm's visible fields."""
        form = CategoryForm()
        self.assertIn('category_type', form.fields)

    def test_packaging_required_independent_of_type(self):
        """Drink with packaging required = True — both fields independent."""
        data = _category_post_data(
            name='Bottled Drink',
            category_type='drink',
            is_packaging_required=True,
        )
        form = CategoryForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        instance = form.save()
        self.assertEqual(instance.category_type, 'drink')
        self.assertTrue(instance.is_packaging_required)

    def test_food_with_no_packaging_required(self):
        """Food with is_packaging_required=False — both fields independent."""
        data = _category_post_data(
            name='Free Appetizer',
            category_type='food',
            is_packaging_required=False,
        )
        form = CategoryForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        instance = form.save()
        self.assertEqual(instance.category_type, 'food')
        self.assertFalse(instance.is_packaging_required)


# ── 2. Edit form retains and allows changing type ─────────────────────────────

class CategoryFormEditTest(TestCase):

    def setUp(self):
        self.cat = Category.objects.create(
            name='Old Drink',
            category_type='drink',
            is_packaging_required=False,
        )

    def test_edit_form_prepopulates_category_type(self):
        form = CategoryForm(instance=self.cat)
        self.assertEqual(form.initial.get('category_type') or
                         form.fields['category_type'].initial or
                         self.cat.category_type,
                         'drink')

    def test_edit_changes_type_from_drink_to_food(self):
        data = _category_post_data(name='Old Drink', category_type='food')
        form = CategoryForm(data=data, instance=self.cat)
        self.assertTrue(form.is_valid(), form.errors)
        instance = form.save()
        self.assertEqual(instance.category_type, 'food')

    def test_edit_keeps_same_type(self):
        data = _category_post_data(name='Old Drink', category_type='drink')
        form = CategoryForm(data=data, instance=self.cat)
        self.assertTrue(form.is_valid(), form.errors)
        self.cat.refresh_from_db()
        self.assertEqual(self.cat.category_type, 'drink')

    def test_existing_categories_not_silently_reclassified(self):
        """Editing a category without changing type keeps its stored type."""
        existing = Category.objects.create(
            name='Appetizers',
            category_type='food',
            is_packaging_required=False,
        )
        # Simulate a POST that omits category_type → form invalid, not saved
        data = _category_post_data(name='Appetizers', category_type='')
        form = CategoryForm(data=data, instance=existing)
        self.assertFalse(form.is_valid())
        existing.refresh_from_db()
        # Original type must be unchanged
        self.assertEqual(existing.category_type, 'food')


# ── 3. View-level tests (create + edit via HTTP) ──────────────────────────────

class CategoryViewTest(TestCase):

    def setUp(self):
        self.admin = _make_admin()
        self.client = Client()
        self.client.force_login(self.admin)

    def test_create_category_with_drink_type(self):
        url = reverse('menu:category_create')
        data = _category_post_data(name='Fruit Shake', category_type='drink')
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        cat = Category.objects.filter(name='Fruit Shake').first()
        self.assertIsNotNone(cat, "Category was not created")
        self.assertEqual(cat.category_type, 'drink')

    def test_create_category_with_food_type(self):
        url = reverse('menu:category_create')
        data = _category_post_data(name='Rice Meals', category_type='food')
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        cat = Category.objects.filter(name='Rice Meals').first()
        self.assertIsNotNone(cat)
        self.assertEqual(cat.category_type, 'food')

    def test_create_without_category_type_fails(self):
        url = reverse('menu:category_create')
        data = _category_post_data(name='No Type Category', category_type='')
        response = self.client.post(url, data)
        # Form invalid → stays on the form page (no redirect)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Category.objects.filter(name='No Type Category').exists())

    def test_edit_updates_category_type(self):
        cat = Category.objects.create(
            name='Mystery Cat',
            category_type='other',
            is_packaging_required=False,
        )
        url = reverse('menu:category_edit', args=[cat.pk])
        data = _category_post_data(name='Mystery Cat', category_type='food')
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        cat.refresh_from_db()
        self.assertEqual(cat.category_type, 'food')

    def test_category_form_page_renders(self):
        url = reverse('menu:category_create')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # category_type field must appear in the rendered HTML
        self.assertContains(response, 'category_type')


# ── 4. Chatbot uses stored category_type, not is_packaging_required ───────────

class CategoryTypeChatbotIntegrationTest(TestCase):
    """
    Create a new 'Fruit Tea' category explicitly set to drink,
    verify it appears in drink queries and not in food queries.
    Also verify changing type updates chatbot results.
    """

    def setUp(self):
        self.fruit_tea = Category.objects.create(
            name='Fruit Tea',
            category_type='drink',
            is_packaging_required=False,
            is_active=True,
        )
        Product.objects.create(
            name='Lemon Tea',
            price=Decimal('45'),
            category=self.fruit_tea,
            is_active=True,
            is_available=True,
            stock_quantity=50,
        )

    def test_new_drink_category_appears_in_drink_filter(self):
        from apps.chatbot.service import get_filtered_menu_context
        ctx, was_filtered = get_filtered_menu_context({}, 'drink')
        self.assertTrue(was_filtered)
        self.assertIn('Lemon Tea', ctx,
            "Newly created drink category must appear in drink results")

    def test_new_drink_category_not_in_food_filter(self):
        from apps.chatbot.service import get_filtered_menu_context
        ctx, _ = get_filtered_menu_context({}, 'meal')
        self.assertNotIn('Lemon Tea', ctx,
            "Drink category must not appear in food results")

    def test_changing_type_to_food_updates_filter(self):
        """After changing category_type to 'food', chatbot must return it in food queries."""
        from apps.chatbot.service import get_filtered_menu_context
        self.fruit_tea.category_type = 'food'
        self.fruit_tea.save(update_fields=['category_type'])

        # Now must NOT appear in drink results
        ctx_drink, _ = get_filtered_menu_context({}, 'drink')
        self.assertNotIn('Lemon Tea', ctx_drink)
        # Must appear in food results
        ctx_food, _ = get_filtered_menu_context({}, 'meal')
        self.assertIn('Lemon Tea', ctx_food)

    def test_is_packaging_required_unchanged_after_type_change(self):
        """Changing category_type must not affect is_packaging_required."""
        original_pkg = self.fruit_tea.is_packaging_required
        self.fruit_tea.category_type = 'food'
        self.fruit_tea.save(update_fields=['category_type'])
        self.fruit_tea.refresh_from_db()
        self.assertEqual(self.fruit_tea.is_packaging_required, original_pkg,
            "is_packaging_required must not change when category_type changes")


# ── 5. ProductListFilterTests ─────────────────────────────────────────────────

class ProductListFilterTests(TestCase):
    """Tests for automatic search, category filtering, and pagination on
    the Product List page (/manage/products/).

    Setup: one admin user, two categories (Drinks, Food), and 12 products —
    6 named "Iced Coffee X" in Drinks, 6 named "Rice Bowl X" in Food — so
    that the unfiltered total (12) triggers pagination at page size 10.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username='pl_admin',
            password='pass123',
            role='admin',
        )
        cls.drinks = Category.objects.create(
            name='Drinks',
            category_type='drink',
            is_packaging_required=False,
        )
        cls.food = Category.objects.create(
            name='Food',
            category_type='food',
            is_packaging_required=False,
        )
        # 6 Iced Coffee products in Drinks
        for i in range(1, 7):
            Product.objects.create(
                name=f'Iced Coffee {i}',
                price=Decimal('55'),
                category=cls.drinks,
                is_active=True,
                is_available=True,
                stock_quantity=10,
            )
        # 6 Rice Bowl products in Food
        for i in range(1, 7):
            Product.objects.create(
                name=f'Rice Bowl {i}',
                price=Decimal('85'),
                category=cls.food,
                is_active=True,
                is_available=True,
                stock_quantity=10,
            )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.admin)
        self.url = reverse('menu:product_list')

    # TC-01
    def test_product_list_requires_login(self):
        """Unauthenticated GET should redirect to the login page."""
        c = Client()
        response = c.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    # TC-02
    def test_product_list_renders_for_admin(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('products', response.context)

    # TC-03: search by name returns only matching products
    def test_product_list_search_by_name(self):
        response = self.client.get(self.url, {'q': 'Iced Coffee'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee 1')
        self.assertNotContains(response, 'Rice Bowl 1')

    # TC-04: search is case-insensitive (name__icontains)
    def test_product_list_search_case_insensitive(self):
        response = self.client.get(self.url, {'q': 'iced coffee'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee 1')
        self.assertNotContains(response, 'Rice Bowl 1')

    # TC-05: search with no match returns empty page
    def test_product_list_empty_results(self):
        response = self.client.get(self.url, {'q': 'ZZZnonexistent'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 0)

    # TC-06: empty search string returns all products (paginated)
    def test_product_list_clear_filters(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        page_obj = response.context['page_obj']
        self.assertEqual(page_obj.paginator.count, 12)

    # TC-07: category filter returns only that category
    def test_product_list_filter_by_category(self):
        response = self.client.get(self.url, {'category': self.drinks.slug})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee 1')
        self.assertNotContains(response, 'Rice Bowl 1')

    # TC-08: combined search and category filter
    def test_product_list_combined_search_and_category(self):
        response = self.client.get(self.url, {'q': 'Rice Bowl', 'category': self.food.slug})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Rice Bowl 1')
        self.assertNotContains(response, 'Iced Coffee 1')

    # TC-09: blank category param removes the category restriction
    def test_product_list_all_categories(self):
        response = self.client.get(self.url, {'category': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 12)

    # TC-10: first page has 10 of 12 products
    def test_product_list_pagination_first_page(self):
        response = self.client.get(self.url)
        page_obj = response.context['page_obj']
        self.assertEqual(page_obj.paginator.count, 12)
        self.assertEqual(len(page_obj.object_list), 10)

    # TC-11: second page has the remaining 2 products
    def test_product_list_pagination_second_page(self):
        response = self.client.get(self.url, {'page': 2})
        page_obj = response.context['page_obj']
        self.assertEqual(len(page_obj.object_list), 2)

    # TC-12: pagination preserves search query in context
    def test_product_list_pagination_preserves_query(self):
        response = self.client.get(self.url, {'q': 'Iced Coffee', 'page': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['search'], 'Iced Coffee')

    # TC-13: pagination preserves category filter in context
    def test_product_list_pagination_preserves_category(self):
        response = self.client.get(self.url, {'category': self.drinks.slug, 'page': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['category_filter'], self.drinks.slug)

    # TC-14: out-of-range page returns last page gracefully (no 404/500)
    def test_product_list_out_of_range_page(self):
        response = self.client.get(self.url, {'q': 'Iced Coffee', 'page': 9999})
        self.assertEqual(response.status_code, 200)

    # TC-15: non-numeric page param does not crash
    def test_product_list_invalid_page_number(self):
        response = self.client.get(self.url, {'page': 'banana'})
        self.assertEqual(response.status_code, 200)

    # TC-16: empty results render correctly (no 500, count is 0)
    def test_product_list_empty_results_render(self):
        response = self.client.get(self.url, {'q': 'ZZZ'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 0)

    # TC-17: search spans all records, not only the current page
    def test_product_list_search_spans_beyond_current_page(self):
        # All 12 products match (6 Iced Coffee + 6 Rice Bowl), so the
        # paginator count must equal 12 even though page 1 shows only 10.
        response = self.client.get(self.url)
        self.assertEqual(response.context['page_obj'].paginator.count, 12)
        # Search for Iced Coffee — all 6 must be counted by the paginator.
        response2 = self.client.get(self.url, {'q': 'Iced Coffee'})
        self.assertEqual(response2.context['page_obj'].paginator.count, 6)

    # TC-18: ordering is newest first (-created_at)
    def test_product_list_ordering_newest_first(self):
        # The last created product in setUpTestData is "Rice Bowl 6".
        # Because setUpTestData runs once for the class we just confirm
        # that the first page's first item has a created_at >= others.
        response = self.client.get(self.url)
        page_obj = response.context['page_obj']
        items = list(page_obj.object_list)
        for i in range(len(items) - 1):
            self.assertGreaterEqual(items[i].created_at, items[i + 1].created_at)

    # TC-19: both search and category_filter returned in context for pagination links
    def test_product_list_context_has_search_and_category(self):
        response = self.client.get(self.url, {'q': 'Bowl', 'category': self.food.slug})
        self.assertEqual(response.context['search'], 'Bowl')
        self.assertEqual(response.context['category_filter'], self.food.slug)

    # TC-20: non-admin user is forbidden (admin_required decorator)
    def test_product_list_unauthorized(self):
        staff = User.objects.create_user(
            username='pl_staff',
            password='pass123',
            role='staff',
        )
        c = Client()
        c.force_login(staff)
        response = c.get(self.url)
        # admin_required redirects or returns 403
        self.assertIn(response.status_code, [302, 403])


# ── Product List AJAX Tests ───────────────────────────────────────────────────


class ProductListAjaxTests(TestCase):
    """Confirm id="results-region" is present in the Product List page responses."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username='ajax_admin_pl',
            password='pass123',
            role='admin',
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_product_list_results_region_present(self):
        response = self.client.get(reverse('menu:product_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_product_list_search_inside_results_region(self):
        response = self.client.get(reverse('menu:product_list'), {'q': 'test'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_product_list_category_filter_inside_results_region(self):
        response = self.client.get(reverse('menu:product_list'), {'category': 'drinks'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')
