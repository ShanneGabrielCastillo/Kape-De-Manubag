"""
tests_recipe_management.py
==========================
Recipe management improvements — revisioning, draft deletion, archiving and
ingredient-line editing.

Covers the approved implementation:

  Services
  ────────
  1.  create_recipe_revision copies the header and all lines
  2.  revision number increases across draft/approved/retired history
  3.  the source approved recipe is unchanged by revision creation
  4.  draft deletion removes the recipe and its lines
  5.  approved and retired recipes cannot be deleted
  6.  a recipe referenced by a reservation item cannot be deleted
  7.  archive succeeds for a legacy-mode product
  8.  archive succeeds for an inactive ingredient-tracked product
  9.  archive is refused for an active ingredient-tracked product
  10. archived recipes stay in history but are excluded from resolve_plan()
  11. ingredient-line editing works on drafts
  12. ingredient-line editing is rejected for approved/retired recipes

  Views / permissions
  ───────────────────
  13. recipe list/detail expose the new actions
  14. new revision requires approval and creates a draft
  15. delete and archive are admin-only
  16. cashiers can still create revisions, edit drafts and edit lines
  17. recipe_edit rejects identity changes once lines exist
  18. destructive actions are POST-only

All tests use synthetic fixtures.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import (
    InventoryItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.inventory.services import (
    RecipeConfigError,
    RecipeMissingError,
    archive_recipe,
    create_recipe_revision,
    delete_draft_recipe,
    resolve_plan,
    update_recipe_item,
)
from apps.menu.models import Category, Product

User = get_user_model()
_PASSWORD = 'test-recipe-mgmt-pass'
_counter = 0


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _user(role='admin'):
    return User.objects.create_user(
        username=f'u_{_uid()}', password=_PASSWORD, role=role,
    )


def _category():
    n = f'Cat_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower())


def _product(tracking='legacy', is_active=True):
    prod = Product.objects.create(
        category=_category(),
        name=f'Prod_{_uid()}',
        price=Decimal('65.00'),
        stock_quantity=100,
        inventory_tracking_mode=tracking,
        is_active=is_active,
    )
    return prod


def _ingredient(name=None, is_active=True, item_type='ingredient'):
    return InventoryItem.objects.create(
        name=name or f'Ing_{_uid()}',
        item_type=item_type,
        base_unit='g',
        on_hand_quantity=Decimal('500.000'),
        is_active=is_active,
    )


def _recipe(product, status=Recipe.STATUS_DRAFT, revision=1,
            size='none', scope='all', approved_by=None):
    kwargs = {}
    if status == Recipe.STATUS_APPROVED:
        kwargs = {
            'approved_at': timezone.now(),
            'approval_note': 'Client confirmed',
            'approved_by': approved_by,
        }
    return Recipe.objects.create(
        product=product, size=size, order_type_scope=scope,
        status=status, revision=revision, **kwargs,
    )


def _line(recipe, ingredient, quantity='25.000'):
    return RecipeItem.objects.create(
        recipe=recipe, inventory_item=ingredient,
        quantity=Decimal(quantity),
    )


# ═════════════════════════════════════════════════════════════════════════════
# create_recipe_revision
# ═════════════════════════════════════════════════════════════════════════════

class CreateRecipeRevisionTests(TestCase):

    def setUp(self):
        self.prod = _product()
        self.ing_a = _ingredient()
        self.ing_b = _ingredient()
        self.approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED)
        _line(self.approved, self.ing_a, '12.500')
        _line(self.approved, self.ing_b, '7.250')

    def test_revision_copies_header_and_lines(self):
        draft = create_recipe_revision(self.approved.pk)

        self.assertEqual(draft.status, Recipe.STATUS_DRAFT)
        self.assertEqual(draft.revision, 2)
        self.assertEqual(draft.product_id, self.approved.product_id)
        self.assertEqual(draft.size, self.approved.size)
        self.assertEqual(draft.order_type_scope, self.approved.order_type_scope)

        source_items = {
            ri.inventory_item_id: ri.quantity
            for ri in self.approved.items.all()
        }
        copied_items = {
            ri.inventory_item_id: ri.quantity
            for ri in draft.items.all()
        }
        self.assertEqual(copied_items, source_items)

    def test_new_draft_has_no_approval_metadata(self):
        draft = create_recipe_revision(self.approved.pk)
        self.assertIsNone(draft.approved_by)
        self.assertIsNone(draft.approved_at)
        self.assertEqual(draft.approval_note, '')

    def test_source_recipe_is_unchanged(self):
        original_items = self.approved.items.count()
        create_recipe_revision(self.approved.pk)

        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, Recipe.STATUS_APPROVED)
        self.assertEqual(self.approved.revision, 1)
        self.assertEqual(self.approved.items.count(), original_items)
        self.assertEqual(
            Recipe.objects.get(
                product=self.prod, size='none', order_type_scope='all',
                status=Recipe.STATUS_APPROVED,
            ).pk,
            self.approved.pk,
        )

    def test_revision_number_spans_draft_approved_and_retired(self):
        _recipe(self.prod, status=Recipe.STATUS_RETIRED, revision=2)
        _recipe(self.prod, status=Recipe.STATUS_DRAFT, revision=5)

        draft = create_recipe_revision(self.approved.pk)
        self.assertEqual(draft.revision, 6)

    def test_revision_number_is_scoped_to_product_size_and_scope(self):
        _recipe(self.prod, status=Recipe.STATUS_APPROVED, revision=9,
                size='small')
        draft = create_recipe_revision(self.approved.pk)
        self.assertEqual(draft.revision, 2)

    def test_draft_cannot_be_revised(self):
        draft = _recipe(self.prod, revision=10)
        with self.assertRaises(RecipeConfigError):
            create_recipe_revision(draft.pk)

    def test_retired_cannot_be_revised(self):
        retired = _recipe(self.prod, status=Recipe.STATUS_RETIRED, revision=10)
        with self.assertRaises(RecipeConfigError):
            create_recipe_revision(retired.pk)


# ═════════════════════════════════════════════════════════════════════════════
# delete_draft_recipe
# ═════════════════════════════════════════════════════════════════════════════

class DeleteDraftRecipeTests(TestCase):

    def setUp(self):
        self.prod = _product()
        self.ing = _ingredient()

    def test_draft_deletion_removes_recipe_and_lines(self):
        draft = _recipe(self.prod)
        _line(draft, self.ing)

        info = delete_draft_recipe(draft.pk)

        self.assertFalse(Recipe.objects.filter(pk=draft.pk).exists())
        self.assertFalse(RecipeItem.objects.filter(recipe_id=draft.pk).exists())
        self.assertEqual(info['pk'], draft.pk)
        self.assertEqual(info['revision'], 1)
        self.assertEqual(info['product_name'], self.prod.name)

    def test_approved_recipe_cannot_be_deleted(self):
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED)
        with self.assertRaises(RecipeConfigError):
            delete_draft_recipe(approved.pk)
        self.assertTrue(Recipe.objects.filter(pk=approved.pk).exists())

    def test_retired_recipe_cannot_be_deleted(self):
        retired = _recipe(self.prod, status=Recipe.STATUS_RETIRED)
        with self.assertRaises(RecipeConfigError):
            delete_draft_recipe(retired.pk)
        self.assertTrue(Recipe.objects.filter(pk=retired.pk).exists())

    def test_referenced_recipe_cannot_be_deleted(self):
        draft = _recipe(self.prod)
        with mock.patch(
            'apps.orders.models.OrderIngredientReservationItem.objects.filter'
        ) as mock_filter:
            mock_filter.return_value.exists.return_value = True
            with self.assertRaises(RecipeConfigError):
                delete_draft_recipe(draft.pk)

        self.assertTrue(Recipe.objects.filter(pk=draft.pk).exists())


# ═════════════════════════════════════════════════════════════════════════════
# archive_recipe
# ═════════════════════════════════════════════════════════════════════════════

class ArchiveRecipeTests(TestCase):

    def test_archive_legacy_product(self):
        prod = _product(tracking=Product.INVENTORY_TRACKING_LEGACY)
        ing = _ingredient()
        approved = _recipe(prod, status=Recipe.STATUS_APPROVED, revision=3)
        _line(approved, ing)

        archived = archive_recipe(approved.pk)

        self.assertEqual(archived.status, Recipe.STATUS_RETIRED)
        self.assertEqual(archived.revision, 3)
        self.assertEqual(archived.approval_note, 'Client confirmed')
        self.assertIsNotNone(archived.approved_at)
        self.assertEqual(archived.items.count(), 1)

    def test_archive_inactive_ingredient_product(self):
        prod = _product(
            tracking=Product.INVENTORY_TRACKING_INGREDIENT, is_active=False,
        )
        approved = _recipe(prod, status=Recipe.STATUS_APPROVED)
        _line(approved, _ingredient())

        archived = archive_recipe(approved.pk)
        self.assertEqual(archived.status, Recipe.STATUS_RETIRED)

    def test_archive_active_ingredient_product_without_replacement_refused(self):
        prod = _product(tracking=Product.INVENTORY_TRACKING_INGREDIENT)
        approved = _recipe(prod, status=Recipe.STATUS_APPROVED)

        with self.assertRaises(RecipeConfigError) as ctx:
            archive_recipe(approved.pk)
        self.assertIn('Cannot archive', str(ctx.exception))

        approved.refresh_from_db()
        self.assertEqual(approved.status, Recipe.STATUS_APPROVED)

    def test_already_retired_refused(self):
        prod = _product(tracking=Product.INVENTORY_TRACKING_LEGACY)
        retired = _recipe(prod, status=Recipe.STATUS_RETIRED)
        with self.assertRaises(RecipeConfigError):
            archive_recipe(retired.pk)

    def test_draft_refused(self):
        prod = _product(tracking=Product.INVENTORY_TRACKING_LEGACY)
        draft = _recipe(prod)
        with self.assertRaises(RecipeConfigError):
            archive_recipe(draft.pk)

    def test_archived_recipe_is_excluded_from_resolve_plan(self):
        prod = _product(tracking=Product.INVENTORY_TRACKING_LEGACY)
        ing = _ingredient()
        approved = _recipe(prod, status=Recipe.STATUS_APPROVED)
        _line(approved, ing)
        ProductPackagingRule.objects.create(
            product=prod, size='none', order_type='takeout',
            profile=None, no_packaging=True,
        )

        plan = resolve_plan(prod.pk, 'none', 'takeout')
        self.assertEqual(plan.recipe.pk, approved.pk)

        archive_recipe(approved.pk)

        # The row is retained as history ...
        self.assertTrue(Recipe.objects.filter(pk=approved.pk).exists())
        # ... but no longer resolvable.
        with self.assertRaises(RecipeMissingError):
            resolve_plan(prod.pk, 'none', 'takeout')


# ═════════════════════════════════════════════════════════════════════════════
# update_recipe_item
# ═════════════════════════════════════════════════════════════════════════════

class UpdateRecipeItemTests(TestCase):

    def setUp(self):
        self.prod = _product()
        self.ing = _ingredient()
        self.other_ing = _ingredient()
        self.draft = _recipe(self.prod)
        self.line = _line(self.draft, self.ing, '10.000')

    def test_quantity_can_be_edited_on_draft(self):
        updated = update_recipe_item(
            self.draft.pk, self.line.pk, self.ing, Decimal('22.500'),
        )
        self.assertEqual(updated.quantity, Decimal('22.500'))

    def test_ingredient_can_be_replaced_on_draft(self):
        updated = update_recipe_item(
            self.draft.pk, self.line.pk, self.other_ing, Decimal('8.000'),
        )
        self.assertEqual(updated.inventory_item_id, self.other_ing.pk)

    def test_duplicate_ingredient_rejected(self):
        _line(self.draft, self.other_ing, '1.000')
        with self.assertRaises(RecipeConfigError):
            update_recipe_item(
                self.draft.pk, self.line.pk, self.other_ing, Decimal('2.000'),
            )
        self.line.refresh_from_db()
        self.assertEqual(self.line.inventory_item_id, self.ing.pk)

    def test_zero_quantity_rejected(self):
        with self.assertRaises(RecipeConfigError):
            update_recipe_item(
                self.draft.pk, self.line.pk, self.ing, Decimal('0.000'),
            )

    def test_packaging_item_rejected(self):
        packaging = _ingredient(item_type='packaging')
        with self.assertRaises(RecipeConfigError):
            update_recipe_item(
                self.draft.pk, self.line.pk, packaging, Decimal('5.000'),
            )

    def test_approved_recipe_line_cannot_be_edited(self):
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED, revision=2)
        line = _line(approved, self.ing)
        with self.assertRaises(RecipeConfigError):
            update_recipe_item(
                approved.pk, line.pk, self.ing, Decimal('99.000'),
            )
        line.refresh_from_db()
        self.assertEqual(line.quantity, Decimal('25.000'))

    def test_retired_recipe_line_cannot_be_edited(self):
        retired = _recipe(self.prod, status=Recipe.STATUS_RETIRED, revision=3)
        line = _line(retired, self.ing)
        with self.assertRaises(RecipeConfigError):
            update_recipe_item(
                retired.pk, line.pk, self.ing, Decimal('99.000'),
            )


# ═════════════════════════════════════════════════════════════════════════════
# Views and permissions
# ═════════════════════════════════════════════════════════════════════════════

class RecipeManagementViewTests(TestCase):

    def setUp(self):
        self.admin = _user('admin')
        self.cashier = _user('cashier')
        self.kitchen = _user('kitchen')
        self.prod = _product()
        self.ing = _ingredient()

    # ── Revision ─────────────────────────────────────────────────────────────

    def test_cashier_can_create_revision(self):
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED)
        _line(approved, self.ing)
        self.client.force_login(self.cashier)

        response = self.client.post(
            reverse('inventory:recipe_revision_create', args=[approved.pk])
        )
        draft = Recipe.objects.get(
            product=self.prod, status=Recipe.STATUS_DRAFT, revision=2,
        )
        self.assertRedirects(
            response, reverse('inventory:recipe_detail', args=[draft.pk])
        )
        self.assertEqual(draft.items.count(), 1)

    def test_revision_get_requires_approved_recipe(self):
        draft = _recipe(self.prod)
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse('inventory:recipe_revision_create', args=[draft.pk])
        )
        self.assertRedirects(
            response, reverse('inventory:recipe_detail', args=[draft.pk])
        )

    # ── Delete ───────────────────────────────────────────────────────────────

    def test_admin_can_delete_draft_via_post(self):
        draft = _recipe(self.prod)
        _line(draft, self.ing)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('inventory:recipe_delete', args=[draft.pk])
        )
        self.assertRedirects(response, reverse('inventory:recipe_list'))
        self.assertFalse(Recipe.objects.filter(pk=draft.pk).exists())

    def test_delete_get_only_confirms(self):
        draft = _recipe(self.prod)
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse('inventory:recipe_delete', args=[draft.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Recipe.objects.filter(pk=draft.pk).exists())

    def test_cashier_cannot_delete_draft(self):
        draft = _recipe(self.prod)
        self.client.force_login(self.cashier)

        response = self.client.post(
            reverse('inventory:recipe_delete', args=[draft.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Recipe.objects.filter(pk=draft.pk).exists())

    # ── Archive ──────────────────────────────────────────────────────────────

    def test_admin_can_archive_legacy_recipe_via_post(self):
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('inventory:recipe_archive', args=[approved.pk])
        )
        self.assertRedirects(
            response, reverse('inventory:recipe_detail', args=[approved.pk])
        )
        approved.refresh_from_db()
        self.assertEqual(approved.status, Recipe.STATUS_RETIRED)

    def test_cashier_cannot_archive(self):
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED)
        self.client.force_login(self.cashier)

        self.client.post(reverse('inventory:recipe_archive', args=[approved.pk]))
        approved.refresh_from_db()
        self.assertEqual(approved.status, Recipe.STATUS_APPROVED)

    def test_archive_get_only_confirms(self):
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED)
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse('inventory:recipe_archive', args=[approved.pk])
        )
        self.assertEqual(response.status_code, 200)
        approved.refresh_from_db()
        self.assertEqual(approved.status, Recipe.STATUS_APPROVED)

    # ── Ingredient-line edit ─────────────────────────────────────────────────

    def test_cashier_can_edit_ingredient_line(self):
        draft = _recipe(self.prod)
        line = _line(draft, self.ing, '5.000')
        self.client.force_login(self.cashier)

        response = self.client.post(
            reverse('inventory:recipe_item_edit', args=[draft.pk, line.pk]),
            {'inventory_item': self.ing.pk, 'quantity': '9.250'},
        )
        self.assertRedirects(
            response, reverse('inventory:recipe_detail', args=[draft.pk])
        )
        line.refresh_from_db()
        self.assertEqual(line.quantity, Decimal('9.250'))

    def test_item_edit_blocked_for_approved_recipe(self):
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED)
        line = _line(approved, self.ing)
        self.client.force_login(self.cashier)

        response = self.client.get(
            reverse('inventory:recipe_item_edit', args=[approved.pk, line.pk])
        )
        self.assertRedirects(
            response, reverse('inventory:recipe_detail', args=[approved.pk])
        )

    # ── recipe_edit identity guard ───────────────────────────────────────────

    def test_recipe_edit_rejects_identity_change_with_lines(self):
        draft = _recipe(self.prod)
        _line(draft, self.ing)
        other_prod = _product()
        self.client.force_login(self.cashier)

        response = self.client.post(
            reverse('inventory:recipe_edit', args=[draft.pk]),
            {
                'product': other_prod.pk,
                'size': 'none',
                'order_type_scope': 'all',
                'revision': 1,
            },
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.product_id, self.prod.pk)

    def test_recipe_edit_allows_identity_change_without_lines(self):
        draft = _recipe(self.prod)
        other_prod = _product()
        self.client.force_login(self.cashier)

        response = self.client.post(
            reverse('inventory:recipe_edit', args=[draft.pk]),
            {
                'product': other_prod.pk,
                'size': 'none',
                'order_type_scope': 'all',
                'revision': 1,
            },
        )
        self.assertRedirects(
            response, reverse('inventory:recipe_detail', args=[draft.pk])
        )
        draft.refresh_from_db()
        self.assertEqual(draft.product_id, other_prod.pk)

    # ── List/detail actions ──────────────────────────────────────────────────

    def test_list_shows_new_actions(self):
        draft = _recipe(self.prod)
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED, revision=2,
                           size='small')
        self.client.force_login(self.admin)

        response = self.client.get(reverse('inventory:recipe_list'))
        body = response.content.decode()
        self.assertContains(response, reverse('inventory:recipe_edit', args=[draft.pk]))
        self.assertContains(response, reverse('inventory:recipe_delete', args=[draft.pk]))
        self.assertContains(response, reverse('inventory:recipe_archive', args=[approved.pk]))
        self.assertIn(
            reverse('inventory:recipe_revision_create', args=[approved.pk]),
            body,
        )

    def test_cashier_sees_no_delete_or_archive_links(self):
        draft = _recipe(self.prod)
        approved = _recipe(self.prod, status=Recipe.STATUS_APPROVED, revision=2,
                           size='small')
        self.client.force_login(self.cashier)

        response = self.client.get(reverse('inventory:recipe_list'))
        self.assertNotContains(
            response, reverse('inventory:recipe_delete', args=[draft.pk]),
        )
        self.assertNotContains(
            response, reverse('inventory:recipe_archive', args=[approved.pk]),
        )

    def test_kitchen_denied_recipe_list(self):
        self.client.force_login(self.kitchen)
        response = self.client.get(reverse('inventory:recipe_list'))
        self.assertEqual(response.status_code, 302)
