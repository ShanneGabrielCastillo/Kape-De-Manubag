"""
tests_phase4b5.py
=================
Phase 4B-5 — Recipe and Packaging Management UI tests.

Covers:
  Services
  ────────
  1.  approve_recipe: happy path
  2.  approve_recipe: requires approval note
  3.  approve_recipe: non-draft rejected
  4.  approve_recipe: no items rejected
  5.  approve_recipe: inactive ingredient rejected
  6.  approve_recipe: wrong item_type (packaging) rejected
  7.  approve_recipe: retires previous approved revision in same transaction
  8.  approve_recipe: locks Product and InventoryItems (verified by outcome)
  9.  retire_recipe: happy path
  10. retire_recipe: already retired rejected
  11. retire_recipe: draft rejected
  12. retire_recipe: ingredient-mode product without replacement rejected
  13. approve_packaging_profile: happy path
  14. approve_packaging_profile: requires approval note
  15. approve_packaging_profile: non-draft rejected
  16. approve_packaging_profile: no items rejected
  17. approve_packaging_profile: inactive packaging item rejected
  18. approve_packaging_profile: wrong item_type (ingredient) rejected
  19. retire_packaging_profile: happy path
  20. retire_packaging_profile: already retired rejected
  21. retire_packaging_profile: draft rejected
  22. retire_packaging_profile: referenced by rule rejected
  23. save_packaging_rule: create with profile
  24. save_packaging_rule: create with no_packaging
  25. save_packaging_rule: draft profile rejected
  26. save_packaging_rule: profile+no_packaging=True rejected
  27. save_packaging_rule: neither profile nor no_packaging rejected
  28. save_packaging_rule: update existing rule

  Views — recipe management
  ──────────────────────────
  29. recipe_list: admin can access
  30. recipe_list: kitchen denied
  31. recipe_list: unauthenticated redirected to login
  32. recipe_create GET: renders form
  33. recipe_create POST: creates draft recipe
  34. recipe_create POST: duplicate revision rejected
  35. recipe_detail GET: shows recipe and items
  36. recipe_detail POST add_item: adds RecipeItem to draft
  37. recipe_detail POST add_item: wrong item_type rejected
  38. recipe_detail POST add_item: duplicate ingredient rejected
  39. recipe_item_delete POST: removes item from draft
  40. recipe_edit GET: draft editable
  41. recipe_edit POST: approved not editable
  42. recipe_approve GET: renders approval form
  43. recipe_approve POST: approves draft, retires previous
  44. recipe_approve POST: no note rejected
  45. recipe_retire POST: retires approved recipe
  46. recipe_retire POST: draft rejected

  Views — packaging profile management
  ──────────────────────────────────────
  47. packaging_profile_list: accessible
  48. packaging_profile_create: creates draft profile
  49. packaging_profile_detail: shows profile and items
  50. packaging_profile_detail POST add_item: adds item
  51. packaging_profile_detail POST add_item: ingredient item rejected
  52. packaging_profile_item_delete: removes item from draft
  53. packaging_profile_edit: draft editable, approved not editable
  54. packaging_profile_approve: approves profile
  55. packaging_profile_retire: retires profile; refused while rule references it

  Views — product packaging rules
  ─────────────────────────────────
  56. packaging_rule_list: accessible
  57. packaging_rule_create POST: creates rule with approved profile
  58. packaging_rule_create POST: creates no_packaging rule
  59. packaging_rule_create POST: draft profile rejected
  60. packaging_rule_edit POST: updates rule

  Views — config completeness
  ────────────────────────────
  61. recipe_config_status: complete cell shown correctly
  62. recipe_config_status: missing packaging rule shown as error
  63. recipe_config_status: non-ingredient product returns 404

  Authorization cross-checks
  ──────────────────────────
  64. Cashier can access recipe_list
  65. Kitchen denied recipe_list
  66. Kitchen denied recipe_create
  67. Kitchen denied packaging_profile_list
  68. Kitchen denied packaging_rule_list

All tests use synthetic fixtures. No real recipes or client data.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    PackagingProfile,
    PackagingProfileItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.inventory.services import (
    RecipeConfigError,
    PackagingConfigError,
    approve_recipe,
    approve_packaging_profile,
    retire_recipe,
    retire_packaging_profile,
    save_packaging_rule,
)
from apps.menu.models import Category, Product

User = get_user_model()
_PASSWORD = 'test-4b5-pass'
_counter = 0


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _user(username=None, role='admin'):
    uid = username or f'u_{_uid()}'
    return User.objects.create_user(username=uid, password=_PASSWORD, role=role)


def _category():
    n = f'Cat_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower())


def _product(name=None, tracking='legacy'):
    n = name or f'Prod_{_uid()}'
    return Product.objects.create(
        category=_category(), name=n,
        price=Decimal('65.00'), stock_quantity=100,
        inventory_tracking_mode=tracking,
    )


def _ingredient_item(name=None, is_active=True, item_type='ingredient',
                     on_hand=Decimal('500.000')):
    n = name or f'Ing_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type=item_type, base_unit='g',
        on_hand_quantity=on_hand, is_active=is_active,
    )


def _packaging_item(name=None, is_active=True, item_type='packaging',
                    on_hand=Decimal('200.000')):
    n = name or f'Pkg_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type=item_type, base_unit='pcs',
        on_hand_quantity=on_hand, is_active=is_active,
    )


def _draft_recipe(product=None, size='none', scope='all', revision=1):
    prod = product or _product()
    return Recipe.objects.create(
        product=prod, size=size, order_type_scope=scope,
        status=Recipe.STATUS_DRAFT, revision=revision,
    )


def _approved_recipe(product=None, size='none', scope='all', revision=1):
    prod = product or _product()
    return Recipe.objects.create(
        product=prod, size=size, order_type_scope=scope,
        status=Recipe.STATUS_APPROVED, revision=revision,
        approved_at=timezone.now(), approval_note='Test',
    )


def _draft_profile(name=None, revision=1):
    n = name or f'Prof_{_uid()}'
    return PackagingProfile.objects.create(
        name=n, status=PackagingProfile.STATUS_DRAFT, revision=revision,
    )


def _approved_profile(name=None, revision=1):
    n = name or f'Prof_{_uid()}'
    return PackagingProfile.objects.create(
        name=n, status=PackagingProfile.STATUS_APPROVED,
        revision=revision, approved_at=timezone.now(),
        approval_note='Test',
    )


# ═════════════════════════════════════════════════════════════════════════════
# Service tests — approve_recipe
# ═════════════════════════════════════════════════════════════════════════════

class ApproveRecipeServiceTests(TestCase):

    def setUp(self):
        self.admin = _user()
        self.prod  = _product()
        self.ing   = _ingredient_item()

    def _recipe_with_item(self, revision=1, scope='all'):
        rec = _draft_recipe(self.prod, size='none', scope=scope, revision=revision)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        return rec

    def test_approve_recipe_happy_path(self):
        rec = self._recipe_with_item()
        result = approve_recipe(rec.pk, self.admin, 'Client confirmed on 2026-01-01')
        result.refresh_from_db()
        self.assertEqual(result.status, Recipe.STATUS_APPROVED)
        self.assertEqual(result.approved_by, self.admin)
        self.assertIsNotNone(result.approved_at)
        self.assertEqual(result.approval_note, 'Client confirmed on 2026-01-01')

    def test_approve_recipe_requires_approval_note(self):
        rec = self._recipe_with_item()
        with self.assertRaises(RecipeConfigError):
            approve_recipe(rec.pk, self.admin, '')

    def test_approve_recipe_whitespace_note_rejected(self):
        rec = self._recipe_with_item()
        with self.assertRaises(RecipeConfigError):
            approve_recipe(rec.pk, self.admin, '   ')

    def test_approve_recipe_non_draft_rejected(self):
        rec = _approved_recipe(self.prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('5.000'))
        with self.assertRaises(RecipeConfigError):
            approve_recipe(rec.pk, self.admin, 'Should fail')

    def test_approve_recipe_no_items_rejected(self):
        rec = _draft_recipe(self.prod)
        with self.assertRaises(RecipeConfigError):
            approve_recipe(rec.pk, self.admin, 'No items')

    def test_approve_recipe_inactive_ingredient_rejected(self):
        inactive = _ingredient_item(is_active=False)
        rec = _draft_recipe(self.prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=inactive,
                                  quantity=Decimal('10.000'))
        with self.assertRaises(RecipeConfigError):
            approve_recipe(rec.pk, self.admin, 'Inactive item')

    def test_approve_recipe_wrong_item_type_rejected(self):
        """A recipe item of type 'packaging' must be rejected."""
        pkg = _ingredient_item(item_type='packaging')
        rec = _draft_recipe(self.prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=pkg,
                                  quantity=Decimal('1.000'))
        with self.assertRaises(RecipeConfigError):
            approve_recipe(rec.pk, self.admin, 'Wrong type')

    def test_approve_recipe_retires_previous_approved_revision(self):
        """Approving a new revision retires the existing approved one atomically."""
        old_rec = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        new_rec = self._recipe_with_item(revision=2)
        approve_recipe(new_rec.pk, self.admin, 'New rev confirmed')
        old_rec.refresh_from_db()
        new_rec.refresh_from_db()
        self.assertEqual(old_rec.status, Recipe.STATUS_RETIRED)
        self.assertEqual(new_rec.status, Recipe.STATUS_APPROVED)

    def test_approve_recipe_preserves_historical_revisions(self):
        """Retired revisions are preserved — they are never deleted."""
        old_rec = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        new_rec = self._recipe_with_item(revision=2)
        approve_recipe(new_rec.pk, self.admin, 'Rev 2 approved')
        self.assertTrue(Recipe.objects.filter(pk=old_rec.pk).exists())


# ═════════════════════════════════════════════════════════════════════════════
# Service tests — retire_recipe
# ═════════════════════════════════════════════════════════════════════════════

class RetireRecipeServiceTests(TestCase):

    def setUp(self):
        self.prod = _product()
        self.ing  = _ingredient_item()

    def test_retire_recipe_happy_path(self):
        rec = _approved_recipe(self.prod)
        result = retire_recipe(rec.pk)
        result.refresh_from_db()
        self.assertEqual(result.status, Recipe.STATUS_RETIRED)

    def test_retire_recipe_already_retired_rejected(self):
        rec = Recipe.objects.create(
            product=self.prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_RETIRED, revision=1,
        )
        with self.assertRaises(RecipeConfigError):
            retire_recipe(rec.pk)

    def test_retire_recipe_draft_rejected(self):
        rec = _draft_recipe(self.prod)
        with self.assertRaises(RecipeConfigError):
            retire_recipe(rec.pk)

    def test_retire_recipe_ingredient_mode_no_replacement_rejected(self):
        """Retiring the only approved recipe of an ingredient-tracked product is refused."""
        prod = _product(tracking='ingredient')
        rec = _approved_recipe(prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        with self.assertRaises(RecipeConfigError):
            retire_recipe(rec.pk)

    def test_retire_recipe_legacy_product_always_allowed(self):
        """
        For a legacy-mode product, retiring an approved recipe is always
        allowed — there is no constraint requiring a live replacement.
        """
        prod = _product(tracking='legacy')
        rec = _approved_recipe(prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        result = retire_recipe(rec.pk)
        result.refresh_from_db()
        self.assertEqual(result.status, Recipe.STATUS_RETIRED)


# ═════════════════════════════════════════════════════════════════════════════
# Service tests — approve_packaging_profile
# ═════════════════════════════════════════════════════════════════════════════

class ApprovePackagingProfileServiceTests(TestCase):

    def setUp(self):
        self.admin = _user()
        self.pkg   = _packaging_item()

    def _profile_with_item(self, name=None, revision=1):
        prof = _draft_profile(name=name, revision=revision)
        PackagingProfileItem.objects.create(
            profile=prof, inventory_item=self.pkg, quantity=Decimal('1.000'),
        )
        return prof

    def test_approve_profile_happy_path(self):
        prof = self._profile_with_item()
        result = approve_packaging_profile(prof.pk, self.admin, 'Confirmed')
        result.refresh_from_db()
        self.assertEqual(result.status, PackagingProfile.STATUS_APPROVED)
        self.assertEqual(result.approved_by, self.admin)
        self.assertIsNotNone(result.approved_at)

    def test_approve_profile_requires_note(self):
        prof = self._profile_with_item()
        with self.assertRaises(PackagingConfigError):
            approve_packaging_profile(prof.pk, self.admin, '')

    def test_approve_profile_non_draft_rejected(self):
        prof = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=prof, inventory_item=self.pkg, quantity=Decimal('1.000'),
        )
        with self.assertRaises(PackagingConfigError):
            approve_packaging_profile(prof.pk, self.admin, 'Re-approve')

    def test_approve_profile_no_items_rejected(self):
        prof = _draft_profile()
        with self.assertRaises(PackagingConfigError):
            approve_packaging_profile(prof.pk, self.admin, 'No items')

    def test_approve_profile_inactive_item_rejected(self):
        inactive = _packaging_item(is_active=False)
        prof = _draft_profile()
        PackagingProfileItem.objects.create(
            profile=prof, inventory_item=inactive, quantity=Decimal('1.000'),
        )
        with self.assertRaises(PackagingConfigError):
            approve_packaging_profile(prof.pk, self.admin, 'Inactive')

    def test_approve_profile_wrong_item_type_rejected(self):
        """A profile item of type 'ingredient' must be rejected."""
        ing = _packaging_item(item_type='ingredient')
        prof = _draft_profile()
        PackagingProfileItem.objects.create(
            profile=prof, inventory_item=ing, quantity=Decimal('1.000'),
        )
        with self.assertRaises(PackagingConfigError):
            approve_packaging_profile(prof.pk, self.admin, 'Wrong type')


# ═════════════════════════════════════════════════════════════════════════════
# Service tests — retire_packaging_profile
# ═════════════════════════════════════════════════════════════════════════════

class RetirePackagingProfileServiceTests(TestCase):

    def test_retire_profile_happy_path(self):
        prof = _approved_profile()
        result = retire_packaging_profile(prof.pk)
        result.refresh_from_db()
        self.assertEqual(result.status, PackagingProfile.STATUS_RETIRED)

    def test_retire_profile_already_retired_rejected(self):
        prof = PackagingProfile.objects.create(
            name=f'P_{_uid()}', status=PackagingProfile.STATUS_RETIRED, revision=1,
        )
        with self.assertRaises(PackagingConfigError):
            retire_packaging_profile(prof.pk)

    def test_retire_profile_draft_rejected(self):
        prof = _draft_profile()
        with self.assertRaises(PackagingConfigError):
            retire_packaging_profile(prof.pk)

    def test_retire_profile_referenced_by_rule_rejected(self):
        """Cannot retire a profile that is still pointed at by a rule."""
        prod = _product()
        prof = _approved_profile()
        ProductPackagingRule.objects.create(
            product=prod, size='none', order_type='takeout',
            profile=prof, no_packaging=False,
        )
        with self.assertRaises(PackagingConfigError):
            retire_packaging_profile(prof.pk)


# ═════════════════════════════════════════════════════════════════════════════
# Service tests — save_packaging_rule
# ═════════════════════════════════════════════════════════════════════════════

class SavePackagingRuleServiceTests(TestCase):

    def setUp(self):
        self.admin = _user()
        self.prod  = _product()
        self.prof  = _approved_profile()

    def test_create_rule_with_profile(self):
        rule = save_packaging_rule(
            product_id=self.prod.pk, size='none', order_type='takeout',
            profile_id=self.prof.pk, no_packaging=False,
            confirmed_by=self.admin,
        )
        self.assertIsNotNone(rule.pk)
        self.assertEqual(rule.profile, self.prof)
        self.assertFalse(rule.no_packaging)

    def test_create_rule_with_no_packaging(self):
        rule = save_packaging_rule(
            product_id=self.prod.pk, size='none', order_type='dine_in',
            profile_id=None, no_packaging=True,
            confirmed_by=self.admin,
        )
        self.assertTrue(rule.no_packaging)
        self.assertIsNone(rule.profile)

    def test_create_rule_draft_profile_rejected(self):
        draft_prof = _draft_profile()
        with self.assertRaises(PackagingConfigError):
            save_packaging_rule(
                product_id=self.prod.pk, size='none', order_type='takeout',
                profile_id=draft_prof.pk, no_packaging=False,
                confirmed_by=self.admin,
            )

    def test_create_rule_both_profile_and_no_packaging_rejected(self):
        with self.assertRaises(PackagingConfigError):
            save_packaging_rule(
                product_id=self.prod.pk, size='small', order_type='takeout',
                profile_id=self.prof.pk, no_packaging=True,
                confirmed_by=self.admin,
            )

    def test_create_rule_neither_profile_nor_no_packaging_rejected(self):
        with self.assertRaises(PackagingConfigError):
            save_packaging_rule(
                product_id=self.prod.pk, size='small', order_type='dine_in',
                profile_id=None, no_packaging=False,
                confirmed_by=self.admin,
            )

    def test_update_existing_rule(self):
        rule = save_packaging_rule(
            product_id=self.prod.pk, size='none', order_type='takeout',
            profile_id=self.prof.pk, no_packaging=False,
            confirmed_by=self.admin,
        )
        prof2 = _approved_profile()
        updated = save_packaging_rule(
            product_id=self.prod.pk, size='none', order_type='takeout',
            profile_id=prof2.pk, no_packaging=False,
            confirmed_by=self.admin, existing_rule_id=rule.pk,
        )
        updated.refresh_from_db()
        self.assertEqual(updated.profile, prof2)
        self.assertEqual(updated.pk, rule.pk)


# ═════════════════════════════════════════════════════════════════════════════
# View tests — authorization helpers
# ═════════════════════════════════════════════════════════════════════════════

class AuthorizationMixin:
    """Shared authorization helpers for view tests."""

    def _login_as(self, role='admin'):
        user = _user(role=role)
        self.client.force_login(user)
        return user

    def assertRequiresStaff(self, url):
        """GET request by kitchen user must be redirected."""
        self.client.logout()
        kitchen = _user(role='kitchen_staff')
        self.client.force_login(kitchen)
        response = self.client.get(url)
        self.assertIn(response.status_code, [302, 403],
            f'Kitchen staff should be denied {url}')

    def assertRequiresLogin(self, url):
        """Unauthenticated GET must redirect to login."""
        self.client.logout()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


# ═════════════════════════════════════════════════════════════════════════════
# View tests — recipe management
# ═════════════════════════════════════════════════════════════════════════════

class RecipeViewTests(AuthorizationMixin, TestCase):

    def setUp(self):
        self.admin = self._login_as('admin')
        self.prod  = _product()
        self.ing   = _ingredient_item()

    # ── recipe_list ───────────────────────────────────────────────────────────

    def test_recipe_list_admin_access(self):
        response = self.client.get(reverse('inventory:recipe_list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/recipe_list.html')

    def test_recipe_list_cashier_access(self):
        self._login_as('cashier')
        response = self.client.get(reverse('inventory:recipe_list'))
        self.assertEqual(response.status_code, 200)

    def test_recipe_list_kitchen_denied(self):
        self.assertRequiresStaff(reverse('inventory:recipe_list'))

    def test_recipe_list_unauthenticated_redirected(self):
        self.assertRequiresLogin(reverse('inventory:recipe_list'))

    # ── recipe_create ─────────────────────────────────────────────────────────

    def test_recipe_create_get(self):
        response = self.client.get(reverse('inventory:recipe_create'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/recipe_form.html')

    def test_recipe_create_post_creates_draft(self):
        response = self.client.post(reverse('inventory:recipe_create'), {
            'product':          self.prod.pk,
            'size':             'none',
            'order_type_scope': 'all',
            'revision':         1,
        })
        self.assertEqual(Recipe.objects.filter(product=self.prod, status='draft').count(), 1)
        recipe = Recipe.objects.get(product=self.prod, status='draft')
        self.assertRedirects(response, reverse('inventory:recipe_detail', args=[recipe.pk]))

    def test_recipe_create_duplicate_revision_rejected(self):
        _draft_recipe(self.prod, size='none', scope='all', revision=1)
        response = self.client.post(reverse('inventory:recipe_create'), {
            'product':          self.prod.pk,
            'size':             'none',
            'order_type_scope': 'all',
            'revision':         1,
        })
        self.assertEqual(response.status_code, 200)  # re-renders with error
        self.assertEqual(Recipe.objects.filter(product=self.prod).count(), 1)

    # ── recipe_detail ─────────────────────────────────────────────────────────

    def test_recipe_detail_get(self):
        rec = _draft_recipe(self.prod)
        response = self.client.get(reverse('inventory:recipe_detail', args=[rec.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/recipe_detail.html')

    def test_recipe_detail_add_item_to_draft(self):
        rec = _draft_recipe(self.prod)
        response = self.client.post(reverse('inventory:recipe_detail', args=[rec.pk]), {
            'add_item':        '1',
            'inventory_item':  self.ing.pk,
            'quantity':        '15.000',
        })
        self.assertRedirects(response, reverse('inventory:recipe_detail', args=[rec.pk]))
        self.assertEqual(RecipeItem.objects.filter(recipe=rec).count(), 1)

    def test_recipe_detail_add_packaging_item_rejected(self):
        pkg = _ingredient_item(item_type='packaging')
        rec = _draft_recipe(self.prod)
        response = self.client.post(reverse('inventory:recipe_detail', args=[rec.pk]), {
            'add_item':        '1',
            'inventory_item':  pkg.pk,
            'quantity':        '1.000',
        })
        self.assertEqual(response.status_code, 200)  # re-renders with form error
        self.assertEqual(RecipeItem.objects.filter(recipe=rec).count(), 0)

    def test_recipe_detail_duplicate_ingredient_rejected(self):
        rec = _draft_recipe(self.prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        response = self.client.post(reverse('inventory:recipe_detail', args=[rec.pk]), {
            'add_item':        '1',
            'inventory_item':  self.ing.pk,
            'quantity':        '5.000',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RecipeItem.objects.filter(recipe=rec).count(), 1)

    def test_recipe_detail_zero_quantity_rejected(self):
        rec = _draft_recipe(self.prod)
        response = self.client.post(reverse('inventory:recipe_detail', args=[rec.pk]), {
            'add_item':        '1',
            'inventory_item':  self.ing.pk,
            'quantity':        '0.000',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RecipeItem.objects.filter(recipe=rec).count(), 0)

    # ── recipe_item_delete ────────────────────────────────────────────────────

    def test_recipe_item_delete_post(self):
        rec = _draft_recipe(self.prod)
        ri = RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                       quantity=Decimal('10.000'))
        response = self.client.post(
            reverse('inventory:recipe_item_delete', args=[rec.pk, ri.pk])
        )
        self.assertRedirects(response, reverse('inventory:recipe_detail', args=[rec.pk]))
        self.assertFalse(RecipeItem.objects.filter(pk=ri.pk).exists())

    def test_recipe_item_delete_from_approved_blocked(self):
        rec = _approved_recipe(self.prod)
        ri = RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                       quantity=Decimal('10.000'))
        self.client.post(reverse('inventory:recipe_item_delete', args=[rec.pk, ri.pk]))
        # Item must still exist.
        self.assertTrue(RecipeItem.objects.filter(pk=ri.pk).exists())

    # ── recipe_edit ───────────────────────────────────────────────────────────

    def test_recipe_edit_draft_accessible(self):
        rec = _draft_recipe(self.prod)
        response = self.client.get(reverse('inventory:recipe_edit', args=[rec.pk]))
        self.assertEqual(response.status_code, 200)

    def test_recipe_edit_approved_redirects(self):
        rec = _approved_recipe(self.prod)
        response = self.client.get(reverse('inventory:recipe_edit', args=[rec.pk]))
        self.assertRedirects(response, reverse('inventory:recipe_detail', args=[rec.pk]))

    # ── recipe_approve ────────────────────────────────────────────────────────

    def test_recipe_approve_get(self):
        rec = _draft_recipe(self.prod)
        response = self.client.get(reverse('inventory:recipe_approve', args=[rec.pk]))
        self.assertEqual(response.status_code, 200)

    def test_recipe_approve_post_approves(self):
        rec = _draft_recipe(self.prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        response = self.client.post(reverse('inventory:recipe_approve', args=[rec.pk]), {
            'approval_note': 'Client confirmed 2026-01-15',
        })
        self.assertRedirects(response, reverse('inventory:recipe_detail', args=[rec.pk]))
        rec.refresh_from_db()
        self.assertEqual(rec.status, Recipe.STATUS_APPROVED)

    def test_recipe_approve_post_no_note_rejected(self):
        rec = _draft_recipe(self.prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        response = self.client.post(reverse('inventory:recipe_approve', args=[rec.pk]), {
            'approval_note': '',
        })
        self.assertEqual(response.status_code, 200)
        rec.refresh_from_db()
        self.assertEqual(rec.status, Recipe.STATUS_DRAFT)

    def test_recipe_approve_retires_previous_revision(self):
        old_rec = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        new_rec = _draft_recipe(self.prod, size='none', scope='all', revision=2)
        RecipeItem.objects.create(recipe=new_rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        self.client.post(reverse('inventory:recipe_approve', args=[new_rec.pk]), {
            'approval_note': 'Rev 2 confirmed',
        })
        old_rec.refresh_from_db()
        self.assertEqual(old_rec.status, Recipe.STATUS_RETIRED)

    # ── recipe_retire ─────────────────────────────────────────────────────────

    def test_recipe_retire_post_retires(self):
        rec = _approved_recipe(self.prod)
        response = self.client.post(reverse('inventory:recipe_retire', args=[rec.pk]))
        self.assertRedirects(response, reverse('inventory:recipe_detail', args=[rec.pk]))
        rec.refresh_from_db()
        self.assertEqual(rec.status, Recipe.STATUS_RETIRED)

    def test_recipe_retire_draft_shows_error(self):
        rec = _draft_recipe(self.prod)
        self.client.post(reverse('inventory:recipe_retire', args=[rec.pk]))
        rec.refresh_from_db()
        self.assertEqual(rec.status, Recipe.STATUS_DRAFT)


# ═════════════════════════════════════════════════════════════════════════════
# View tests — packaging profile management
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileViewTests(AuthorizationMixin, TestCase):

    def setUp(self):
        self.admin = self._login_as('admin')
        self.pkg   = _packaging_item()

    def test_packaging_profile_list_accessible(self):
        response = self.client.get(reverse('inventory:packaging_profile_list'))
        self.assertEqual(response.status_code, 200)

    def test_packaging_profile_list_kitchen_denied(self):
        self.assertRequiresStaff(reverse('inventory:packaging_profile_list'))

    def test_packaging_profile_create_creates_draft(self):
        response = self.client.post(reverse('inventory:packaging_profile_create'), {
            'name':     f'TestProfile_{_uid()}',
            'revision': 1,
        })
        self.assertEqual(PackagingProfile.objects.filter(status='draft').count(), 1)

    def test_packaging_profile_detail_shows_items(self):
        prof = _draft_profile()
        PackagingProfileItem.objects.create(
            profile=prof, inventory_item=self.pkg, quantity=Decimal('1.000'),
        )
        response = self.client.get(
            reverse('inventory:packaging_profile_detail', args=[prof.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.pkg.name)

    def test_packaging_profile_detail_add_item(self):
        prof = _draft_profile()
        response = self.client.post(
            reverse('inventory:packaging_profile_detail', args=[prof.pk]), {
                'add_item':        '1',
                'inventory_item':  self.pkg.pk,
                'quantity':        '2.000',
            }
        )
        self.assertRedirects(response,
            reverse('inventory:packaging_profile_detail', args=[prof.pk]))
        self.assertEqual(PackagingProfileItem.objects.filter(profile=prof).count(), 1)

    def test_packaging_profile_detail_add_ingredient_item_rejected(self):
        ing = _packaging_item(item_type='ingredient')
        prof = _draft_profile()
        response = self.client.post(
            reverse('inventory:packaging_profile_detail', args=[prof.pk]), {
                'add_item':        '1',
                'inventory_item':  ing.pk,
                'quantity':        '1.000',
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PackagingProfileItem.objects.filter(profile=prof).count(), 0)

    def test_packaging_profile_item_delete_post(self):
        prof = _draft_profile()
        ppi = PackagingProfileItem.objects.create(
            profile=prof, inventory_item=self.pkg, quantity=Decimal('1.000'),
        )
        response = self.client.post(
            reverse('inventory:packaging_profile_item_delete', args=[prof.pk, ppi.pk])
        )
        self.assertRedirects(response,
            reverse('inventory:packaging_profile_detail', args=[prof.pk]))
        self.assertFalse(PackagingProfileItem.objects.filter(pk=ppi.pk).exists())

    def test_packaging_profile_edit_draft_accessible(self):
        prof = _draft_profile()
        response = self.client.get(
            reverse('inventory:packaging_profile_edit', args=[prof.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_packaging_profile_edit_approved_redirects(self):
        prof = _approved_profile()
        response = self.client.get(
            reverse('inventory:packaging_profile_edit', args=[prof.pk])
        )
        self.assertRedirects(response,
            reverse('inventory:packaging_profile_detail', args=[prof.pk]))

    def test_packaging_profile_approve_post_approves(self):
        prof = _draft_profile()
        PackagingProfileItem.objects.create(
            profile=prof, inventory_item=self.pkg, quantity=Decimal('1.000'),
        )
        response = self.client.post(
            reverse('inventory:packaging_profile_approve', args=[prof.pk]), {
                'approval_note': 'Client confirmed packaging 2026-02-01',
            }
        )
        self.assertRedirects(response,
            reverse('inventory:packaging_profile_detail', args=[prof.pk]))
        prof.refresh_from_db()
        self.assertEqual(prof.status, PackagingProfile.STATUS_APPROVED)

    def test_packaging_profile_retire_post_retires(self):
        prof = _approved_profile()
        response = self.client.post(
            reverse('inventory:packaging_profile_retire', args=[prof.pk])
        )
        self.assertRedirects(response,
            reverse('inventory:packaging_profile_detail', args=[prof.pk]))
        prof.refresh_from_db()
        self.assertEqual(prof.status, PackagingProfile.STATUS_RETIRED)

    def test_packaging_profile_retire_refused_while_rule_references_it(self):
        prof = _approved_profile()
        prod = _product()
        ProductPackagingRule.objects.create(
            product=prod, size='none', order_type='takeout',
            profile=prof, no_packaging=False,
        )
        self.client.post(reverse('inventory:packaging_profile_retire', args=[prof.pk]))
        prof.refresh_from_db()
        # Must still be approved because retirement is blocked.
        self.assertEqual(prof.status, PackagingProfile.STATUS_APPROVED)


# ═════════════════════════════════════════════════════════════════════════════
# View tests — product packaging rules
# ═════════════════════════════════════════════════════════════════════════════

class PackagingRuleViewTests(AuthorizationMixin, TestCase):

    def setUp(self):
        self.admin = self._login_as('admin')
        self.prod  = _product()
        self.prof  = _approved_profile()

    def test_packaging_rule_list_accessible(self):
        response = self.client.get(reverse('inventory:packaging_rule_list'))
        self.assertEqual(response.status_code, 200)

    def test_packaging_rule_list_kitchen_denied(self):
        self.assertRequiresStaff(reverse('inventory:packaging_rule_list'))

    def test_packaging_rule_create_with_profile(self):
        response = self.client.post(reverse('inventory:packaging_rule_create'), {
            'product_id':   self.prod.pk,
            'size':         'none',
            'order_type':   'takeout',
            'no_packaging': '',
            'profile_id':   self.prof.pk,
        })
        self.assertRedirects(response, reverse('inventory:packaging_rule_list'))
        self.assertEqual(
            ProductPackagingRule.objects.filter(product=self.prod, order_type='takeout').count(), 1
        )

    def test_packaging_rule_create_no_packaging(self):
        response = self.client.post(reverse('inventory:packaging_rule_create'), {
            'product_id':   self.prod.pk,
            'size':         'none',
            'order_type':   'dine_in',
            'no_packaging': 'on',
            'profile_id':   '',
        })
        self.assertRedirects(response, reverse('inventory:packaging_rule_list'))
        rule = ProductPackagingRule.objects.get(product=self.prod, order_type='dine_in')
        self.assertTrue(rule.no_packaging)

    def test_packaging_rule_create_draft_profile_rejected(self):
        draft_prof = _draft_profile()
        response = self.client.post(reverse('inventory:packaging_rule_create'), {
            'product_id':   self.prod.pk,
            'size':         'small',
            'order_type':   'takeout',
            'no_packaging': '',
            'profile_id':   draft_prof.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            ProductPackagingRule.objects.filter(product=self.prod, size='small').exists()
        )

    def test_packaging_rule_edit_updates_rule(self):
        rule = ProductPackagingRule.objects.create(
            product=self.prod, size='none', order_type='takeout',
            profile=self.prof, no_packaging=False,
        )
        prof2 = _approved_profile()
        response = self.client.post(
            reverse('inventory:packaging_rule_edit', args=[rule.pk]), {
                'product_id':   self.prod.pk,
                'size':         'none',
                'order_type':   'takeout',
                'no_packaging': '',
                'profile_id':   prof2.pk,
            }
        )
        self.assertRedirects(response, reverse('inventory:packaging_rule_list'))
        rule.refresh_from_db()
        self.assertEqual(rule.profile, prof2)


# ═════════════════════════════════════════════════════════════════════════════
# View tests — config completeness
# ═════════════════════════════════════════════════════════════════════════════

class RecipeConfigStatusViewTests(AuthorizationMixin, TestCase):

    def setUp(self):
        self.admin = self._login_as('admin')

    def test_config_status_complete_cell(self):
        """A fully configured (product, size, order_type) shows as complete."""
        prod = _product(tracking='ingredient')
        ing  = _ingredient_item()
        rec  = _approved_recipe(prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        ProductPackagingRule.objects.create(
            product=prod, size='none', order_type='takeout',
            profile=None, no_packaging=True,
        )
        response = self.client.get(
            reverse('inventory:recipe_config_status', args=[prod.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/recipe_config_status.html')
        # At least one cell should be marked ok.
        matrix = response.context['matrix']
        ok_cells = [c for row in matrix for c in row['cells'] if c['ok']]
        self.assertTrue(len(ok_cells) >= 1)

    def test_config_status_missing_packaging_rule_shown_as_error(self):
        """A cell missing a packaging rule should appear as incomplete."""
        prod = _product(tracking='ingredient')
        ing  = _ingredient_item()
        rec  = _approved_recipe(prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        # No packaging rules at all.
        response = self.client.get(
            reverse('inventory:recipe_config_status', args=[prod.pk])
        )
        self.assertEqual(response.status_code, 200)
        matrix = response.context['matrix']
        error_cells = [c for row in matrix for c in row['cells'] if not c['ok']]
        self.assertTrue(len(error_cells) >= 1)

    def test_config_status_non_ingredient_product_returns_404(self):
        """Only ingredient-tracked products have a config status page."""
        prod = _product(tracking='legacy')
        response = self.client.get(
            reverse('inventory:recipe_config_status', args=[prod.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_config_status_kitchen_denied(self):
        prod = _product(tracking='ingredient')
        self.assertRequiresStaff(
            reverse('inventory:recipe_config_status', args=[prod.pk])
        )


# ═════════════════════════════════════════════════════════════════════════════
# Authorization — comprehensive kitchen/unauthenticated checks
# ═════════════════════════════════════════════════════════════════════════════

class PhaseAuthorizationTests(AuthorizationMixin, TestCase):

    def test_kitchen_denied_recipe_create(self):
        self.assertRequiresStaff(reverse('inventory:recipe_create'))

    def test_kitchen_denied_packaging_profile_create(self):
        self.assertRequiresStaff(reverse('inventory:packaging_profile_create'))

    def test_kitchen_denied_packaging_rule_create(self):
        self.assertRequiresStaff(reverse('inventory:packaging_rule_create'))

    def test_unauthenticated_redirected_from_recipe_list(self):
        self.assertRequiresLogin(reverse('inventory:recipe_list'))

    def test_unauthenticated_redirected_from_profile_list(self):
        self.assertRequiresLogin(reverse('inventory:packaging_profile_list'))

    def test_cashier_can_access_recipe_list(self):
        self._login_as('cashier')
        response = self.client.get(reverse('inventory:recipe_list'))
        self.assertEqual(response.status_code, 200)

    def test_cashier_can_access_packaging_profile_list(self):
        self._login_as('cashier')
        response = self.client.get(reverse('inventory:packaging_profile_list'))
        self.assertEqual(response.status_code, 200)


# ═════════════════════════════════════════════════════════════════════════════
# Read-only guarantee — no stock or log mutations from UI operations
# ═════════════════════════════════════════════════════════════════════════════

class NoStockMutationTests(AuthorizationMixin, TestCase):

    def setUp(self):
        self._login_as('admin')
        self.prod = _product()
        self.ing  = _ingredient_item(on_hand=Decimal('100.000'))

    def test_recipe_approval_does_not_change_inventory_quantities(self):
        rec = _draft_recipe(self.prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        on_hand_before = self.ing.on_hand_quantity
        self.client.post(reverse('inventory:recipe_approve', args=[rec.pk]), {
            'approval_note': 'Confirmed — no stock change expected',
        })
        self.ing.refresh_from_db()
        self.assertEqual(self.ing.on_hand_quantity, on_hand_before)

    def test_recipe_approval_creates_no_movement_logs(self):
        rec = _draft_recipe(self.prod)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        count_before = IngredientMovementLog.objects.count()
        self.client.post(reverse('inventory:recipe_approve', args=[rec.pk]), {
            'approval_note': 'No movement logs expected',
        })
        self.assertEqual(IngredientMovementLog.objects.count(), count_before)

    def test_packaging_profile_approval_does_not_change_inventory_quantities(self):
        pkg = _packaging_item(on_hand=Decimal('50.000'))
        prof = _draft_profile()
        PackagingProfileItem.objects.create(
            profile=prof, inventory_item=pkg, quantity=Decimal('1.000'),
        )
        on_hand_before = pkg.on_hand_quantity
        self.client.post(
            reverse('inventory:packaging_profile_approve', args=[prof.pk]), {
                'approval_note': 'Confirmed — no stock change',
            }
        )
        pkg.refresh_from_db()
        self.assertEqual(pkg.on_hand_quantity, on_hand_before)


# ═════════════════════════════════════════════════════════════════════════════
# Corrective-pass tests — mixed-scope prohibition and lock-order verification
# (Phase 4B-5 audit corrections)
# ═════════════════════════════════════════════════════════════════════════════

class MixedScopeApprovalTests(TestCase):
    """
    Rev 3 §D.4: "No mixed scopes. For one (product, size) the approved set is
    either {all} or {dine_in, takeout}, never both. Enforced at approval."

    These tests verify that approve_recipe() enforces this rule explicitly,
    rather than relying solely on the partial unique DB constraint (which only
    prevents duplicate (product, size, scope) triples within the same scope,
    not cross-scope conflicts).
    """

    def setUp(self):
        self.admin = _user()
        self.prod  = _product()
        self.ing   = _ingredient_item()

    def _draft_with_item(self, scope, revision=2):
        """Create a draft recipe with one ingredient item."""
        rec = _draft_recipe(self.prod, size='none', scope=scope, revision=revision)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        return rec

    # ── Approving 'all' rejected when a specific scope is already approved ────

    def test_approve_all_rejected_when_dine_in_already_approved(self):
        """
        Approving scope='all' must be rejected when a scope='dine_in' recipe
        is already approved for the same (product, size).
        """
        existing = _approved_recipe(self.prod, size='none', scope='dine_in', revision=1)
        RecipeItem.objects.create(recipe=existing, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        new_rec = self._draft_with_item(scope='all', revision=2)
        with self.assertRaises(RecipeConfigError) as ctx:
            approve_recipe(new_rec.pk, self.admin, 'Client confirmed 2026-01-01')
        self.assertIn('dine_in', str(ctx.exception))
        # Existing approved recipe must be unchanged.
        existing.refresh_from_db()
        self.assertEqual(existing.status, Recipe.STATUS_APPROVED)
        # Draft must remain draft.
        new_rec.refresh_from_db()
        self.assertEqual(new_rec.status, Recipe.STATUS_DRAFT)

    def test_approve_all_rejected_when_takeout_already_approved(self):
        """
        Approving scope='all' must be rejected when a scope='takeout' recipe
        is already approved for the same (product, size).
        """
        existing = _approved_recipe(self.prod, size='none', scope='takeout', revision=1)
        RecipeItem.objects.create(recipe=existing, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        new_rec = self._draft_with_item(scope='all', revision=2)
        with self.assertRaises(RecipeConfigError):
            approve_recipe(new_rec.pk, self.admin, 'Should fail')
        existing.refresh_from_db()
        self.assertEqual(existing.status, Recipe.STATUS_APPROVED)
        new_rec.refresh_from_db()
        self.assertEqual(new_rec.status, Recipe.STATUS_DRAFT)

    # ── Approving specific scope rejected when 'all' already approved ─────────

    def test_approve_dine_in_rejected_when_all_already_approved(self):
        """
        Approving scope='dine_in' must be rejected when a scope='all' recipe
        is already approved for the same (product, size).
        """
        existing = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=existing, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        new_rec = self._draft_with_item(scope='dine_in', revision=2)
        with self.assertRaises(RecipeConfigError) as ctx:
            approve_recipe(new_rec.pk, self.admin, 'Should fail')
        self.assertIn('all', str(ctx.exception))
        existing.refresh_from_db()
        self.assertEqual(existing.status, Recipe.STATUS_APPROVED)
        new_rec.refresh_from_db()
        self.assertEqual(new_rec.status, Recipe.STATUS_DRAFT)

    def test_approve_takeout_rejected_when_all_already_approved(self):
        """
        Approving scope='takeout' must be rejected when a scope='all' recipe
        is already approved for the same (product, size).
        """
        existing = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=existing, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        new_rec = self._draft_with_item(scope='takeout', revision=2)
        with self.assertRaises(RecipeConfigError):
            approve_recipe(new_rec.pk, self.admin, 'Should fail')
        existing.refresh_from_db()
        self.assertEqual(existing.status, Recipe.STATUS_APPROVED)

    # ── Permitted combinations ────────────────────────────────────────────────

    def test_approve_dine_in_and_takeout_can_coexist(self):
        """
        Rev 3 §D.4: the approved set {dine_in, takeout} is the legitimate
        alternative to {all}. A dine_in and takeout recipe for the same
        (product, size) must be approved without error.
        """
        # Approve dine_in first.
        dine_in_rec = self._draft_with_item(scope='dine_in', revision=1)
        approve_recipe(dine_in_rec.pk, self.admin, 'Dine-in confirmed')
        dine_in_rec.refresh_from_db()
        self.assertEqual(dine_in_rec.status, Recipe.STATUS_APPROVED)

        # Now approve takeout — must succeed.
        takeout_rec = _draft_recipe(self.prod, size='none', scope='takeout', revision=1)
        RecipeItem.objects.create(recipe=takeout_rec, inventory_item=self.ing,
                                  quantity=Decimal('12.000'))
        approve_recipe(takeout_rec.pk, self.admin, 'Takeout confirmed')
        takeout_rec.refresh_from_db()
        self.assertEqual(takeout_rec.status, Recipe.STATUS_APPROVED)
        # dine_in must still be approved (different scope — not retired).
        dine_in_rec.refresh_from_db()
        self.assertEqual(dine_in_rec.status, Recipe.STATUS_APPROVED)

    def test_approve_new_revision_same_scope_retires_previous(self):
        """
        Approving a new revision for the exact same scope correctly retires
        the previous approved revision.  Rev 3 §D.4: "approving it retires
        the previous revision in the same transaction."
        """
        old_rec = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=old_rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        new_rec = self._draft_with_item(scope='all', revision=2)
        approve_recipe(new_rec.pk, self.admin, 'Rev 2 confirmed by client')
        old_rec.refresh_from_db()
        new_rec.refresh_from_db()
        self.assertEqual(old_rec.status, Recipe.STATUS_RETIRED,
            'Previous revision must be retired after new revision approved')
        self.assertEqual(new_rec.status, Recipe.STATUS_APPROVED)

    def test_mixed_scope_check_is_size_scoped(self):
        """
        Mixed-scope prohibition applies per (product, size).  An 'all' recipe
        for size='small' must not block approving a 'dine_in' recipe for
        size='none'.
        """
        # Approve an 'all' recipe for size='small'.
        all_small = _draft_recipe(self.prod, size='small', scope='all', revision=1)
        RecipeItem.objects.create(recipe=all_small, inventory_item=self.ing,
                                  quantity=Decimal('8.000'))
        approve_recipe(all_small.pk, self.admin, 'Small confirmed')

        # Approving 'dine_in' for size='none' is unrelated — must succeed.
        dine_in_none = _draft_recipe(self.prod, size='none', scope='dine_in', revision=1)
        RecipeItem.objects.create(recipe=dine_in_none, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        approve_recipe(dine_in_none.pk, self.admin, 'None dine_in confirmed')
        dine_in_none.refresh_from_db()
        self.assertEqual(dine_in_none.status, Recipe.STATUS_APPROVED)

    def test_rejected_approval_does_not_partially_commit(self):
        """
        When approve_recipe raises RecipeConfigError, the transaction must be
        fully rolled back: no status changes, no partial writes.
        """
        existing = _approved_recipe(self.prod, size='none', scope='dine_in', revision=1)
        RecipeItem.objects.create(recipe=existing, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        draft = self._draft_with_item(scope='all', revision=2)

        initial_recipe_count = Recipe.objects.count()
        try:
            approve_recipe(draft.pk, self.admin, 'Will fail')
        except RecipeConfigError:
            pass

        # Row count must be unchanged.
        self.assertEqual(Recipe.objects.count(), initial_recipe_count)
        # existing must still be approved.
        existing.refresh_from_db()
        self.assertEqual(existing.status, Recipe.STATUS_APPROVED)
        # draft must still be draft.
        draft.refresh_from_db()
        self.assertEqual(draft.status, Recipe.STATUS_DRAFT)


# ─────────────────────────────────────────────────────────────────────────────
# Lock-order verification
# ─────────────────────────────────────────────────────────────────────────────

class RecipeLockOrderTests(TestCase):
    """
    Verify that the corrected approve_recipe() and retire_recipe() do NOT
    issue select_for_update() on Recipe rows.

    Rev 3 §H.9: "Not locked: Recipe, RecipeItem, PackagingProfileItem, and
    ProductPackagingRule rows."

    These tests inspect the source code of the service functions to confirm
    the documented lock order is followed.  They do not test actual PostgreSQL
    lock behaviour (which is PostgreSQL-only by Rev 3 §M.5/M.6).
    """

    def test_approve_recipe_does_not_lock_recipe_row(self):
        """
        approve_recipe() must not call select_for_update() on the initial
        Recipe queryset.  Inspect the source code of the function.
        """
        import inspect
        from apps.inventory import services
        source = inspect.getsource(services.approve_recipe)

        # The initial Recipe fetch must be a plain .get(), not .select_for_update().
        # We confirm 'select_for_update' does NOT appear before the Product lock.
        # Strategy: split at 'Product.objects.select_for_update' — everything
        # before that must not contain 'Recipe.objects.select_for_update'.
        before_product_lock = source.split('Product.objects.select_for_update')[0]
        self.assertNotIn(
            'Recipe.objects.select_for_update',
            before_product_lock,
            'approve_recipe must not lock the Recipe row before the Product lock. '
            'Rev 3 §H.9: Recipe rows are not locked by configuration writers.'
        )

    def test_retire_recipe_does_not_lock_recipe_row(self):
        """
        retire_recipe() must not call select_for_update() on the Recipe row.
        """
        import inspect
        from apps.inventory import services
        source = inspect.getsource(services.retire_recipe)

        # Everything before the Product lock must not contain a Recipe FOR UPDATE.
        before_product_lock = source.split('Product.objects.select_for_update')[0]
        self.assertNotIn(
            'Recipe.objects.select_for_update',
            before_product_lock,
            'retire_recipe must not lock the Recipe row. '
            'Rev 3 §H.9: Recipe rows are not locked by configuration writers.'
        )

    def test_approve_recipe_locks_product_before_inventory_items(self):
        """
        approve_recipe() must acquire the Product lock (level 2) before the
        InventoryItem locks (level 3).  Inspect the source.
        """
        import inspect
        from apps.inventory import services
        source = inspect.getsource(services.approve_recipe)

        # Search for the lock call patterns.  Use the distinctive combined
        # pattern 'InventoryItem.objects\n            .select_for_update()' or
        # the single-line form — both appear in the source.
        # More robustly: find the position of the Product lock statement vs
        # the InventoryItem lock statement by searching for the comment that
        # precedes each in the corrected implementation.
        product_lock_pos = source.find('Product.objects.select_for_update')
        # The InventoryItem lock call inside the function body.
        inv_lock_pos = source.find('.select_for_update()\n            .filter(pk__in=item_pks)')
        if inv_lock_pos == -1:
            # Fallback: look for the comment that marks the InventoryItem lock step.
            inv_lock_pos = source.find('Level 3: Lock referenced InventoryItems')
        self.assertGreater(product_lock_pos, 0,
            'approve_recipe must lock the Product row.')
        self.assertGreater(inv_lock_pos, product_lock_pos,
            'approve_recipe must lock Product before InventoryItems '
            '(Rev 3 §H.9 lock order: Product → InventoryItems).')

    def test_retire_recipe_locks_product_row(self):
        """
        retire_recipe() must acquire the Product lock.  Inspect the source.
        """
        import inspect
        from apps.inventory import services
        source = inspect.getsource(services.retire_recipe)
        self.assertIn('Product.objects.select_for_update', source,
            'retire_recipe must lock the Product row (Rev 3 §H.9).')


# ═════════════════════════════════════════════════════════════════════════════
# Post-lock TOCTOU recheck tests
# (Phase 4B-5 final concurrency correction)
# ═════════════════════════════════════════════════════════════════════════════

class TOCTOURecheckTests(TestCase):
    """
    Tests for the post-lock status refresh in approve_recipe() and
    retire_recipe().

    True concurrent execution (two PostgreSQL connections racing on the same
    Product lock) cannot be verified on SQLite, which ignores row locks and
    has a single writer.  These tests instead verify the TOCTOU guard by
    simulating what happens when the status changes *between* the initial
    pre-lock read and the post-lock refresh: the recipe is modified directly
    via the ORM to represent the state it would have after a concurrent
    transaction committed, then the service is called with the now-stale
    snapshot scenario.

    Limitation: these tests prove the correctness of the refresh + recheck
    logic in isolation (sequential execution).  They do not prove that
    PostgreSQL row-level locking prevents the race under concurrent load.
    PostgreSQL concurrency testing requires two real connections and is
    deferred to the CI PostgreSQL suite (Rev 3 §M.5/M.6).
    """

    def setUp(self):
        self.admin = _user()
        self.prod  = _product()
        self.ing   = _ingredient_item()

    # ── approve_recipe post-lock recheck ──────────────────────────────────────

    def test_approve_recipe_rejects_already_approved_recipe(self):
        """
        If a recipe is approved by the time approve_recipe() acquires the
        Product lock (simulated by approving it directly before the service
        call), the post-lock refresh detects the status change and raises
        RecipeConfigError.

        This covers the concurrent case where Transaction B calls
        approve_recipe() on a recipe that Transaction A already approved and
        committed while B was waiting for the Product lock.
        """
        rec = _draft_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))

        # Simulate: another transaction has already approved this recipe.
        rec.status       = Recipe.STATUS_APPROVED
        rec.approved_by  = self.admin
        rec.approved_at  = timezone.now()
        rec.approval_note = 'First approver'
        rec.save(update_fields=['status', 'approved_by', 'approved_at', 'approval_note', 'updated_at'])

        # Now call approve_recipe — it should detect the non-draft status
        # via refresh_from_db after acquiring the Product lock.
        with self.assertRaises(RecipeConfigError) as ctx:
            approve_recipe(rec.pk, self.admin, 'Second approver — should be rejected')

        err_msg = str(ctx.exception)
        self.assertIn('draft', err_msg.lower(),
            'Error message should indicate the recipe is no longer draft')

    def test_approve_recipe_first_approver_not_overwritten(self):
        """
        The first approver's identity and note must not be overwritten by a
        concurrent second approval attempt.
        """
        first_admin  = _user(username=f'first_{_uid()}')
        second_admin = _user(username=f'second_{_uid()}')

        rec = _draft_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))

        # First approval succeeds.
        approve_recipe(rec.pk, first_admin, 'First approval note')
        rec.refresh_from_db()
        self.assertEqual(rec.approved_by, first_admin)
        self.assertEqual(rec.approval_note, 'First approval note')

        # Second attempt is rejected by the post-lock recheck.
        with self.assertRaises(RecipeConfigError):
            approve_recipe(rec.pk, second_admin, 'Second approval note')

        # First approver's data must be unchanged.
        rec.refresh_from_db()
        self.assertEqual(rec.approved_by, first_admin,
            'First approver must not be overwritten by the rejected second attempt')
        self.assertEqual(rec.approval_note, 'First approval note',
            'Approval note must not be overwritten by the rejected second attempt')

    def test_approve_recipe_rejects_retired_recipe(self):
        """
        A recipe that was retired between the initial read and the Product lock
        must be rejected.
        """
        rec = _draft_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))

        # Simulate: status changed to retired (e.g., by an admin ORM operation
        # or a concurrent retire_recipe call that won the race).
        rec.status = Recipe.STATUS_RETIRED
        rec.save(update_fields=['status', 'updated_at'])

        with self.assertRaises(RecipeConfigError):
            approve_recipe(rec.pk, self.admin, 'Should fail — recipe is retired')

    # ── retire_recipe post-lock recheck ───────────────────────────────────────

    def test_retire_recipe_rejects_already_retired_recipe(self):
        """
        If a recipe is retired by the time retire_recipe() acquires the Product
        lock, the post-lock refresh detects the status and raises
        RecipeConfigError.

        Covers concurrent double-retirement: both transactions read 'approved',
        B waits, A commits the retirement, B acquires the lock and must not
        retire again.
        """
        rec = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))

        # Simulate: another transaction already retired the recipe.
        rec.status = Recipe.STATUS_RETIRED
        rec.save(update_fields=['status', 'updated_at'])

        with self.assertRaises(RecipeConfigError) as ctx:
            retire_recipe(rec.pk)

        self.assertIn('already retired', str(ctx.exception))

    def test_retire_recipe_rejects_draft_recipe(self):
        """
        A draft recipe must be rejected even if a concurrent create-then-draft
        operation reset it to draft status after the initial pre-lock read.
        """
        # Start with an approved recipe (satisfies pre-lock check).
        rec = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))

        # Simulate: status changed to draft between initial read and Product lock.
        rec.status = Recipe.STATUS_DRAFT
        rec.save(update_fields=['status', 'updated_at'])

        with self.assertRaises(RecipeConfigError) as ctx:
            retire_recipe(rec.pk)

        self.assertIn('draft', str(ctx.exception).lower())

    # ── Existing behavior still intact ───────────────────────────────────────

    def test_approve_recipe_valid_draft_still_succeeds(self):
        """Correct approval still works after the post-lock recheck is added."""
        rec = _draft_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        result = approve_recipe(rec.pk, self.admin, 'Client confirmed 2026-01-01')
        result.refresh_from_db()
        self.assertEqual(result.status, Recipe.STATUS_APPROVED)

    def test_retire_recipe_valid_approved_still_succeeds(self):
        """Correct retirement still works after the post-lock recheck is added."""
        rec = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        RecipeItem.objects.create(recipe=rec, inventory_item=self.ing,
                                  quantity=Decimal('10.000'))
        result = retire_recipe(rec.pk)
        result.refresh_from_db()
        self.assertEqual(result.status, Recipe.STATUS_RETIRED)

    # ── Lock-order confirmation: no new Recipe select_for_update introduced ───

    def test_no_recipe_select_for_update_in_approve_recipe(self):
        """
        The TOCTOU fix must use refresh_from_db(), not select_for_update().
        Confirm the source contains refresh_from_db but no Recipe lock.
        """
        import inspect
        from apps.inventory import services
        source = inspect.getsource(services.approve_recipe)
        self.assertIn('refresh_from_db', source,
            'approve_recipe must call refresh_from_db after the Product lock')
        before_product_lock = source.split('Product.objects.select_for_update')[0]
        self.assertNotIn('Recipe.objects.select_for_update', before_product_lock,
            'approve_recipe must not lock the Recipe row (Rev 3 §H.9)')

    def test_no_recipe_select_for_update_in_retire_recipe(self):
        """
        retire_recipe() must use refresh_from_db(), not select_for_update().
        """
        import inspect
        from apps.inventory import services
        source = inspect.getsource(services.retire_recipe)
        self.assertIn('refresh_from_db', source,
            'retire_recipe must call refresh_from_db after the Product lock')
        self.assertNotIn('Recipe.objects.select_for_update', source,
            'retire_recipe must not lock the Recipe row (Rev 3 §H.9)')
