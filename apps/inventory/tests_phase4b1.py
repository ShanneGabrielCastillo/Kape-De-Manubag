"""
tests_phase4b1.py
=================
Phase 4B-1 — Recipe and RecipeItem model tests.

Covers (Rev 3 §D.4–D.5):

  Recipe model
  ────────────
  1. Creation / field defaults
  2. All 5 valid SIZE_CHOICES values accepted
  3. All 3 valid SCOPE_CHOICES values accepted
  4. All 3 valid STATUS_CHOICES values accepted
  5. DB constraint: recipe_one_approved_per_scope   (partial unique on approved)
  6. DB constraint: recipe_revision_unique          (no duplicate revisions)
  7. DB constraint: recipe_approved_has_approval    (approved → approved_at not null)
  8. DB constraint: recipe_status_valid             (unknown status rejected)
  9. DB constraint: recipe_size_valid               (unknown size rejected)
  10. DB constraint: recipe_scope_valid             (unknown scope rejected)
  11. DB constraint: recipe_revision_positive       (revision 0 rejected)
  12. Draft + retired coexist freely (no unique constraint fires)
  13. Partial unique allows two approved rows for different scopes
  14. __str__ smoke-test

  RecipeItem model
  ─────────────────
  15. Creation / fields
  16. Positive quantity accepted
  17. Zero quantity rejected by DB (recipeitem_quantity_positive)
  18. Negative quantity rejected by DB
  19. Duplicate InventoryItem in same Recipe rejected (recipeitem_unique_item)
  20. Different InventoryItems in same Recipe accepted
  21. ForeignKey Recipe CASCADE: deleting recipe removes its items
  22. ForeignKey InventoryItem PROTECT: deleting item with recipe items blocked

All tests use synthetic DB objects only. No real client data.
All constraint tests use transaction.atomic() savepoints to recover from
IntegrityError without rolling back the test transaction.
All tests are SQLite-safe (CHECK constraints, partial unique indices, CASCADE,
PROTECT are all supported by SQLite 3.25+).
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.inventory.models import InventoryItem, Recipe, RecipeItem
from apps.menu.models import Category, Product

User = get_user_model()

# ── Helpers ───────────────────────────────────────────────────────────────────

_PASSWORD = 'test-4b1-pass'

# Counter for unique name generation within a test run.
_counter = 0


def _uid():
    """Return a short monotonically increasing suffix for unique names."""
    global _counter
    _counter += 1
    return str(_counter)


def _user(username=None, role='admin'):
    uid = username or f'user_{_uid()}'
    return User.objects.create_user(username=uid, password=_PASSWORD, role=role)


def _category(name=None):
    n = name or f'Cat_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower().replace(' ', '-'))


def _product(name=None, category=None, price='65.00'):
    cat = category or _category()
    n = name or f'Product_{_uid()}'
    return Product.objects.create(
        category=cat,
        name=n,
        price=Decimal(price),
        stock_quantity=100,
    )


def _inventory_item(name=None, item_type='ingredient', base_unit='g',
                    on_hand=Decimal('1000.000')):
    n = name or f'Item_{_uid()}'
    return InventoryItem.objects.create(
        name=n,
        item_type=item_type,
        base_unit=base_unit,
        on_hand_quantity=on_hand,
    )


def _recipe(product=None, size='none', scope='all', status='draft',
            revision=1, approved_by=None, approved_at=None, approval_note=''):
    """
    Create and return a Recipe.  Caller is responsible for providing a valid
    combination (e.g. approved_at when status='approved').
    """
    prod = product or _product()
    return Recipe.objects.create(
        product=prod,
        size=size,
        order_type_scope=scope,
        status=status,
        revision=revision,
        approved_by=approved_by,
        approved_at=approved_at,
        approval_note=approval_note,
    )


def _approved_recipe(**kwargs):
    """Shortcut: recipe with status='approved' and approved_at set."""
    kwargs.setdefault('status', 'approved')
    kwargs.setdefault('approved_at', timezone.now())
    kwargs.setdefault('approval_note', 'Approved by test')
    return _recipe(**kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Recipe — creation and field defaults
# ═════════════════════════════════════════════════════════════════════════════

class RecipeCreationTests(TestCase):
    """Recipe can be created; defaults are correct."""

    def test_create_draft_recipe(self):
        prod = _product()
        r = Recipe.objects.create(
            product=prod,
            size=Recipe.SIZE_NONE,
            order_type_scope=Recipe.SCOPE_ALL,
            status=Recipe.STATUS_DRAFT,
            revision=1,
        )
        self.assertIsNotNone(r.pk)

    def test_default_status_is_draft(self):
        """status defaults to 'draft' if not supplied."""
        prod = _product()
        r = Recipe.objects.create(
            product=prod,
            size=Recipe.SIZE_NONE,
            order_type_scope=Recipe.SCOPE_ALL,
            revision=1,
        )
        self.assertEqual(r.status, Recipe.STATUS_DRAFT)

    def test_approval_fields_default_null_blank(self):
        """approved_by, approved_at default to None; approval_note to ''."""
        r = _recipe()
        self.assertIsNone(r.approved_by)
        self.assertIsNone(r.approved_at)
        self.assertEqual(r.approval_note, '')

    def test_created_at_and_updated_at_set(self):
        r = _recipe()
        self.assertIsNotNone(r.created_at)
        self.assertIsNotNone(r.updated_at)

    def test_str_smoke(self):
        prod = _product(name='Espresso')
        r = _recipe(product=prod, size='none', scope='all',
                    status='draft', revision=1)
        s = str(r)
        self.assertIn('none', s)
        self.assertIn('all', s)
        self.assertIn('draft', s)
        self.assertIn('1', s)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Recipe — all valid SIZE_CHOICES accepted
# ═════════════════════════════════════════════════════════════════════════════

class RecipeSizeChoicesTests(TestCase):
    """All five SIZE_CHOICES values pass the DB constraint."""

    def _assert_size_accepted(self, size_value):
        r = _recipe(size=size_value)
        r.refresh_from_db()
        self.assertEqual(r.size, size_value)

    def test_size_none_accepted(self):
        self._assert_size_accepted(Recipe.SIZE_NONE)

    def test_size_small_accepted(self):
        self._assert_size_accepted(Recipe.SIZE_SMALL)

    def test_size_medium_accepted(self):
        self._assert_size_accepted(Recipe.SIZE_MEDIUM)

    def test_size_large_accepted(self):
        self._assert_size_accepted(Recipe.SIZE_LARGE)

    def test_size_hot_accepted(self):
        """'hot' must be accepted — this is the 5th size only in
        OrderItem.SIZE_CHOICES, not in Product.SIZE_CHOICES."""
        self._assert_size_accepted(Recipe.SIZE_HOT)

    def test_size_choices_has_five_entries(self):
        self.assertEqual(len(Recipe.SIZE_CHOICES), 5)

    def test_size_choices_contains_hot(self):
        values = [c[0] for c in Recipe.SIZE_CHOICES]
        self.assertIn('hot', values)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Recipe — all valid SCOPE_CHOICES accepted
# ═════════════════════════════════════════════════════════════════════════════

class RecipeScopeChoicesTests(TestCase):
    """All three SCOPE_CHOICES values pass the DB constraint."""

    def _assert_scope_accepted(self, scope_value):
        r = _recipe(scope=scope_value)
        r.refresh_from_db()
        self.assertEqual(r.order_type_scope, scope_value)

    def test_scope_all_accepted(self):
        self._assert_scope_accepted(Recipe.SCOPE_ALL)

    def test_scope_dine_in_accepted(self):
        self._assert_scope_accepted(Recipe.SCOPE_DINE_IN)

    def test_scope_takeout_accepted(self):
        self._assert_scope_accepted(Recipe.SCOPE_TAKEOUT)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Recipe — all valid STATUS_CHOICES accepted
# ═════════════════════════════════════════════════════════════════════════════

class RecipeStatusChoicesTests(TestCase):
    """All three STATUS_CHOICES values pass the DB constraint."""

    def test_status_draft_accepted(self):
        r = _recipe(status=Recipe.STATUS_DRAFT)
        r.refresh_from_db()
        self.assertEqual(r.status, Recipe.STATUS_DRAFT)

    def test_status_approved_accepted(self):
        r = _approved_recipe()
        r.refresh_from_db()
        self.assertEqual(r.status, Recipe.STATUS_APPROVED)

    def test_status_retired_accepted(self):
        r = _recipe(status=Recipe.STATUS_RETIRED)
        r.refresh_from_db()
        self.assertEqual(r.status, Recipe.STATUS_RETIRED)


# ═════════════════════════════════════════════════════════════════════════════
# 5. DB constraint: recipe_one_approved_per_scope
# ═════════════════════════════════════════════════════════════════════════════

class RecipeOneApprovedPerScopeConstraintTests(TestCase):
    """
    recipe_one_approved_per_scope: at most one approved recipe per
    (product, size, order_type_scope).

    This is a partial UniqueConstraint with condition=Q(status='approved').
    It fires only when status='approved'.
    """

    def setUp(self):
        self.prod = _product()
        # First approved recipe — should succeed.
        self.r1 = _approved_recipe(product=self.prod, size='none', scope='all',
                                   revision=1)

    def test_second_approved_same_scope_rejected(self):
        """A second approved recipe for the same (product, size, scope)
        must be rejected with IntegrityError."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _approved_recipe(product=self.prod, size='none', scope='all',
                                 revision=2)

    def test_draft_does_not_trigger_partial_unique(self):
        """A draft for the same (product, size, scope) must be allowed —
        the partial unique is scoped to approved only."""
        r = _recipe(product=self.prod, size='none', scope='all',
                    status='draft', revision=2)
        self.assertIsNotNone(r.pk)

    def test_retired_does_not_trigger_partial_unique(self):
        """A retired recipe for the same (product, size, scope) must be
        allowed."""
        r = _recipe(product=self.prod, size='none', scope='all',
                    status='retired', revision=3)
        self.assertIsNotNone(r.pk)

    def test_approved_different_size_allowed(self):
        """An approved recipe for a different size is independent."""
        r = _approved_recipe(product=self.prod, size='small', scope='all',
                             revision=1)
        self.assertIsNotNone(r.pk)

    def test_approved_different_scope_allowed(self):
        """An approved recipe for a different scope is independent."""
        r = _approved_recipe(product=self.prod, size='none', scope='dine_in',
                             revision=1)
        self.assertIsNotNone(r.pk)

    def test_constraint_name_is_correct(self):
        names = [c.name for c in Recipe._meta.constraints]
        self.assertIn('recipe_one_approved_per_scope', names)


# ═════════════════════════════════════════════════════════════════════════════
# 6. DB constraint: recipe_revision_unique
# ═════════════════════════════════════════════════════════════════════════════

class RecipeRevisionUniqueConstraintTests(TestCase):
    """
    recipe_revision_unique: (product, size, order_type_scope, revision) must
    be unique regardless of status.
    """

    def setUp(self):
        self.prod = _product()
        self.r1 = _recipe(product=self.prod, size='none', scope='all',
                          status='draft', revision=1)

    def test_same_revision_same_scope_rejected(self):
        """Creating a second recipe with the same (product, size, scope,
        revision) — even with a different status — must raise IntegrityError."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _recipe(product=self.prod, size='none', scope='all',
                        status='retired', revision=1)

    def test_different_revision_same_scope_accepted(self):
        r = _recipe(product=self.prod, size='none', scope='all',
                    status='draft', revision=2)
        self.assertIsNotNone(r.pk)

    def test_same_revision_different_scope_accepted(self):
        r = _recipe(product=self.prod, size='none', scope='dine_in',
                    status='draft', revision=1)
        self.assertIsNotNone(r.pk)

    def test_same_revision_different_size_accepted(self):
        r = _recipe(product=self.prod, size='large', scope='all',
                    status='draft', revision=1)
        self.assertIsNotNone(r.pk)

    def test_constraint_name_is_correct(self):
        names = [c.name for c in Recipe._meta.constraints]
        self.assertIn('recipe_revision_unique', names)


# ═════════════════════════════════════════════════════════════════════════════
# 7. DB constraint: recipe_approved_has_approval
# ═════════════════════════════════════════════════════════════════════════════

class RecipeApprovedHasApprovalConstraintTests(TestCase):
    """
    recipe_approved_has_approval: a recipe with status='approved' must have
    approved_at IS NOT NULL.
    """

    def test_approved_with_approved_at_accepted(self):
        """The normal happy path: approved_at is set when approving."""
        r = _approved_recipe()
        r.refresh_from_db()
        self.assertIsNotNone(r.approved_at)

    def test_approved_without_approved_at_rejected(self):
        """Approving without a timestamp must fail the CHECK constraint."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                _recipe(status='approved', approved_at=None)

    def test_draft_without_approved_at_accepted(self):
        """Draft recipes need no approval timestamp."""
        r = _recipe(status='draft', approved_at=None)
        self.assertIsNotNone(r.pk)

    def test_retired_without_approved_at_accepted(self):
        """Retired recipes (superseded before approval) need no timestamp."""
        r = _recipe(status='retired', approved_at=None)
        self.assertIsNotNone(r.pk)

    def test_constraint_name_is_correct(self):
        names = [c.name for c in Recipe._meta.constraints]
        self.assertIn('recipe_approved_has_approval', names)


# ═════════════════════════════════════════════════════════════════════════════
# 8. DB constraint: recipe_status_valid
# ═════════════════════════════════════════════════════════════════════════════

class RecipeStatusValidConstraintTests(TestCase):
    """recipe_status_valid rejects any value not in {draft, approved, retired}."""

    def test_unknown_status_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                Recipe.objects.create(
                    product=_product(),
                    size='none',
                    order_type_scope='all',
                    status='pending',   # invalid
                    revision=1,
                )

    def test_empty_status_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                Recipe.objects.create(
                    product=_product(),
                    size='none',
                    order_type_scope='all',
                    status='',
                    revision=1,
                )

    def test_constraint_name_is_correct(self):
        names = [c.name for c in Recipe._meta.constraints]
        self.assertIn('recipe_status_valid', names)


# ═════════════════════════════════════════════════════════════════════════════
# 9. DB constraint: recipe_size_valid
# ═════════════════════════════════════════════════════════════════════════════

class RecipeSizeValidConstraintTests(TestCase):
    """recipe_size_valid rejects any size value not in the OrderItem vocabulary."""

    def test_unknown_size_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                Recipe.objects.create(
                    product=_product(),
                    size='jumbo',       # invalid
                    order_type_scope='all',
                    status='draft',
                    revision=1,
                )

    def test_empty_size_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                Recipe.objects.create(
                    product=_product(),
                    size='',
                    order_type_scope='all',
                    status='draft',
                    revision=1,
                )

    def test_constraint_name_is_correct(self):
        names = [c.name for c in Recipe._meta.constraints]
        self.assertIn('recipe_size_valid', names)


# ═════════════════════════════════════════════════════════════════════════════
# 10. DB constraint: recipe_scope_valid
# ═════════════════════════════════════════════════════════════════════════════

class RecipeScopeValidConstraintTests(TestCase):
    """recipe_scope_valid rejects any scope value not in {all, dine_in, takeout}."""

    def test_unknown_scope_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                Recipe.objects.create(
                    product=_product(),
                    size='none',
                    order_type_scope='delivery',  # invalid
                    status='draft',
                    revision=1,
                )

    def test_empty_scope_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                Recipe.objects.create(
                    product=_product(),
                    size='none',
                    order_type_scope='',
                    status='draft',
                    revision=1,
                )

    def test_constraint_name_is_correct(self):
        names = [c.name for c in Recipe._meta.constraints]
        self.assertIn('recipe_scope_valid', names)


# ═════════════════════════════════════════════════════════════════════════════
# 11. DB constraint: recipe_revision_positive
# ═════════════════════════════════════════════════════════════════════════════

class RecipeRevisionPositiveConstraintTests(TestCase):
    """recipe_revision_positive: revision must be > 0."""

    def test_revision_one_accepted(self):
        r = _recipe(revision=1)
        self.assertIsNotNone(r.pk)

    def test_revision_zero_rejected(self):
        """revision=0 must be rejected. PositiveIntegerField rejects 0 in
        Python, and the DB CHECK provides the same guarantee at the DB level."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                Recipe.objects.create(
                    product=_product(),
                    size='none',
                    order_type_scope='all',
                    status='draft',
                    revision=0,
                )

    def test_constraint_name_is_correct(self):
        names = [c.name for c in Recipe._meta.constraints]
        self.assertIn('recipe_revision_positive', names)


# ═════════════════════════════════════════════════════════════════════════════
# 12. Draft + retired coexist freely
# ═════════════════════════════════════════════════════════════════════════════

class RecipeDraftRetiredCoexistenceTests(TestCase):
    """Multiple draft and retired recipes for the same (product, size, scope)
    can coexist, as they are not subject to the partial unique constraint."""

    def setUp(self):
        self.prod = _product()

    def test_multiple_drafts_same_scope_allowed(self):
        """Multiple drafts (revision 1, 2, 3) must all save successfully."""
        r1 = _recipe(product=self.prod, size='none', scope='all',
                     status='draft', revision=1)
        r2 = _recipe(product=self.prod, size='none', scope='all',
                     status='draft', revision=2)
        r3 = _recipe(product=self.prod, size='none', scope='all',
                     status='draft', revision=3)
        self.assertIsNotNone(r1.pk)
        self.assertIsNotNone(r2.pk)
        self.assertIsNotNone(r3.pk)

    def test_draft_and_retired_same_scope_allowed(self):
        r1 = _recipe(product=self.prod, size='small', scope='all',
                     status='retired', revision=1)
        r2 = _recipe(product=self.prod, size='small', scope='all',
                     status='draft', revision=2)
        self.assertIsNotNone(r1.pk)
        self.assertIsNotNone(r2.pk)

    def test_multiple_retired_same_scope_allowed(self):
        for rev in range(1, 5):
            r = _recipe(product=self.prod, size='medium', scope='all',
                        status='retired', revision=rev)
            self.assertIsNotNone(r.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 13. Partial unique: two approved rows for different scopes allowed
# ═════════════════════════════════════════════════════════════════════════════

class RecipeApprovedDifferentScopeTests(TestCase):
    """
    Two approved recipes for the same (product, size) but different
    order_type_scope values must both be accepted.
    """

    def test_approved_dine_in_and_takeout_coexist(self):
        prod = _product()
        r1 = _approved_recipe(product=prod, size='none', scope='dine_in',
                              revision=1)
        r2 = _approved_recipe(product=prod, size='none', scope='takeout',
                              revision=1)
        self.assertIsNotNone(r1.pk)
        self.assertIsNotNone(r2.pk)
        self.assertNotEqual(r1.pk, r2.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 14. Recipe constraint names — all seven present
# ═════════════════════════════════════════════════════════════════════════════

class RecipeAllConstraintNamesTests(TestCase):
    """All seven constraint names required by Rev3 must exist on Recipe."""

    _REQUIRED = {
        'recipe_one_approved_per_scope',
        'recipe_revision_unique',
        'recipe_approved_has_approval',
        'recipe_status_valid',
        'recipe_size_valid',
        'recipe_scope_valid',
        'recipe_revision_positive',
    }

    def test_all_constraint_names_present(self):
        actual = {c.name for c in Recipe._meta.constraints}
        for name in self._REQUIRED:
            with self.subTest(constraint=name):
                self.assertIn(name, actual,
                              f'Expected constraint {name!r} is missing from Recipe._meta.constraints')


# ═════════════════════════════════════════════════════════════════════════════
# 15. RecipeItem — creation and fields
# ═════════════════════════════════════════════════════════════════════════════

class RecipeItemCreationTests(TestCase):
    """RecipeItem can be created; fields round-trip correctly."""

    def setUp(self):
        self.recipe = _recipe()
        self.item = _inventory_item()

    def test_create_recipe_item(self):
        ri = RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=self.item,
            quantity=Decimal('50.000'),
        )
        self.assertIsNotNone(ri.pk)

    def test_quantity_stored_correctly(self):
        ri = RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=self.item,
            quantity=Decimal('123.456'),
        )
        ri.refresh_from_db()
        self.assertEqual(ri.quantity, Decimal('123.456'))

    def test_str_smoke(self):
        ri = RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=self.item,
            quantity=Decimal('10.000'),
        )
        s = str(ri)
        self.assertIn(str(self.recipe.pk), s)
        self.assertIn(str(self.item.pk), s)

    def test_two_constraint_names_present(self):
        names = {c.name for c in RecipeItem._meta.constraints}
        self.assertIn('recipeitem_quantity_positive', names)
        self.assertIn('recipeitem_unique_item', names)


# ═════════════════════════════════════════════════════════════════════════════
# 16–18. RecipeItem — recipeitem_quantity_positive constraint
# ═════════════════════════════════════════════════════════════════════════════

class RecipeItemQuantityPositiveConstraintTests(TestCase):
    """recipeitem_quantity_positive: quantity must be > 0."""

    def setUp(self):
        self.recipe = _recipe()

    def test_positive_quantity_accepted(self):
        item = _inventory_item()
        ri = RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=item,
            quantity=Decimal('0.001'),
        )
        self.assertIsNotNone(ri.pk)

    def test_large_positive_quantity_accepted(self):
        item = _inventory_item()
        ri = RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=item,
            quantity=Decimal('99999.999'),
        )
        self.assertIsNotNone(ri.pk)

    def test_zero_quantity_rejected(self):
        """quantity=0 must fail the CHECK constraint."""
        item = _inventory_item()
        with self.assertRaises(Exception):
            with transaction.atomic():
                RecipeItem.objects.create(
                    recipe=self.recipe,
                    inventory_item=item,
                    quantity=Decimal('0.000'),
                )

    def test_negative_quantity_rejected(self):
        """quantity < 0 must fail the CHECK constraint."""
        item = _inventory_item()
        with self.assertRaises(Exception):
            with transaction.atomic():
                RecipeItem.objects.create(
                    recipe=self.recipe,
                    inventory_item=item,
                    quantity=Decimal('-1.000'),
                )

    def test_constraint_name_is_correct(self):
        names = [c.name for c in RecipeItem._meta.constraints]
        self.assertIn('recipeitem_quantity_positive', names)


# ═════════════════════════════════════════════════════════════════════════════
# 19–20. RecipeItem — recipeitem_unique_item constraint
# ═════════════════════════════════════════════════════════════════════════════

class RecipeItemUniqueItemConstraintTests(TestCase):
    """recipeitem_unique_item: each InventoryItem appears at most once per Recipe."""

    def setUp(self):
        self.recipe = _recipe()
        self.item = _inventory_item(name='Coffee Powder')

    def test_same_item_twice_in_same_recipe_rejected(self):
        RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=self.item,
            quantity=Decimal('50.000'),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RecipeItem.objects.create(
                    recipe=self.recipe,
                    inventory_item=self.item,
                    quantity=Decimal('25.000'),
                )

    def test_different_items_in_same_recipe_accepted(self):
        item2 = _inventory_item(name='Milk')
        ri1 = RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=self.item,
            quantity=Decimal('50.000'),
        )
        ri2 = RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=item2,
            quantity=Decimal('200.000'),
        )
        self.assertIsNotNone(ri1.pk)
        self.assertIsNotNone(ri2.pk)

    def test_same_item_in_different_recipes_accepted(self):
        """The same InventoryItem CAN appear in different recipes."""
        recipe2 = _recipe()
        ri1 = RecipeItem.objects.create(
            recipe=self.recipe,
            inventory_item=self.item,
            quantity=Decimal('50.000'),
        )
        ri2 = RecipeItem.objects.create(
            recipe=recipe2,
            inventory_item=self.item,
            quantity=Decimal('30.000'),
        )
        self.assertIsNotNone(ri1.pk)
        self.assertIsNotNone(ri2.pk)

    def test_constraint_name_is_correct(self):
        names = [c.name for c in RecipeItem._meta.constraints]
        self.assertIn('recipeitem_unique_item', names)


# ═════════════════════════════════════════════════════════════════════════════
# 21. RecipeItem — CASCADE on Recipe delete
# ═════════════════════════════════════════════════════════════════════════════

class RecipeItemCascadeTests(TestCase):
    """Deleting a Recipe must cascade to its RecipeItem rows."""

    def test_recipe_delete_removes_items(self):
        recipe = _recipe()
        item1 = _inventory_item(name='Cascaded Powder')
        item2 = _inventory_item(name='Cascaded Milk')
        ri1 = RecipeItem.objects.create(
            recipe=recipe,
            inventory_item=item1,
            quantity=Decimal('50.000'),
        )
        ri2 = RecipeItem.objects.create(
            recipe=recipe,
            inventory_item=item2,
            quantity=Decimal('200.000'),
        )
        recipe_pk = recipe.pk
        recipe.delete()
        self.assertFalse(
            RecipeItem.objects.filter(pk__in=[ri1.pk, ri2.pk]).exists(),
            'RecipeItem rows must be deleted when their Recipe is deleted (CASCADE)',
        )
        self.assertFalse(
            Recipe.objects.filter(pk=recipe_pk).exists(),
        )


# ═════════════════════════════════════════════════════════════════════════════
# 22. RecipeItem — PROTECT on InventoryItem delete
# ═════════════════════════════════════════════════════════════════════════════

class RecipeItemProtectTests(TestCase):
    """Deleting an InventoryItem that is referenced by a RecipeItem must be
    blocked (PROTECT)."""

    def test_delete_referenced_inventory_item_blocked(self):
        recipe = _recipe()
        item = _inventory_item(name='Protected Ingredient')
        RecipeItem.objects.create(
            recipe=recipe,
            inventory_item=item,
            quantity=Decimal('100.000'),
        )
        with self.assertRaises(Exception):
            with transaction.atomic():
                item.delete()

    def test_delete_unreferenced_inventory_item_allowed(self):
        """An InventoryItem with no RecipeItem references can be deleted freely."""
        item = _inventory_item(name='Orphan Ingredient')
        pk = item.pk
        item.delete()
        self.assertFalse(InventoryItem.objects.filter(pk=pk).exists())
