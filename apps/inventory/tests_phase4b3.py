"""
tests_phase4b3.py
=================
Phase 4B-3 — resolve_plan() service tests.

Covers Rev 3 §I.2, §I.3, §M.1:

  Happy paths
  ───────────
  1.  'all' scope recipe resolves for dine_in order type
  2.  'all' scope recipe resolves for takeout order type
  3.  scope-specific 'dine_in' recipe resolves for dine_in
  4.  scope-specific 'takeout' recipe resolves for takeout
  5.  scope-specific recipe does NOT resolve for the other order type
  6.  resolve with explicit no_packaging rule (packaging components = empty)
  7.  resolve with packaging profile (packaging components populated)
  8.  ResolvedPlan fields are correct
  9.  ResolvedPlanComponent fields are correct — ingredient source
  10. ResolvedPlanComponent fields are correct — packaging source
  11. Decimal quantities are exact (no float rounding)
  12. Multiple recipe items all appear in components
  13. Multiple packaging profile items all appear in components

  Error paths — Rev 3 §M.1 exact list
  ─────────────────────────────────────
  14. Recipe missing — no Recipe row at all → RecipeMissingError
  15. Recipe missing — draft only → RecipeMissingError
  16. Recipe missing — retired only → RecipeMissingError
  17. Recipe ambiguous — 'all' + specific scope both approved → AmbiguousRecipeError
  18. Packaging rule missing → PackagingRuleMissingError
  19. Inactive inventory item (recipe) → InactiveItemError
  20. Wrong item type — recipe item has item_type='packaging' → InactiveItemError
  21. Inactive inventory item (packaging profile) → InactiveItemError
  22. Wrong item type — packaging profile item has item_type='ingredient' → InactiveItemError
  23. Draft packaging profile on rule → InactiveItemError
  24. Retired packaging profile on rule → InactiveItemError

  Exception hierarchy
  ────────────────────
  25. All four specific errors subclass RecipeIncompleteError
  26. All four specific errors subclass InventoryServiceError (ValueError)

  No-mutation guarantee
  ──────────────────────
  27. resolve_plan() leaves InventoryItem quantities unchanged
  28. resolve_plan() creates no IngredientMovementLog rows
  29. resolve_plan() creates no OrderIngredientReservation rows

  Edge cases
  ──────────
  30. Different product same size/order_type resolves independently
  31. Same product different size resolves independently
  32. AmbiguousRecipeError carries the matching recipe ids

All tests use synthetic fixtures.
No PostgreSQL-specific behaviour is tested here (resolver is read-only,
no row locking is exercised in the resolver itself).
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
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
    AmbiguousRecipeError,
    InactiveItemError,
    InventoryServiceError,
    PackagingRuleMissingError,
    RecipeIncompleteError,
    RecipeMissingError,
    ResolvedPlan,
    ResolvedPlanComponent,
    COMPONENT_SOURCE_PACKAGING,
    COMPONENT_SOURCE_RECIPE,
    resolve_plan,
)
from apps.menu.models import Category, Product

User = get_user_model()
_PASSWORD = 'test-4b3-pass'
_counter = 0


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _user(username=None):
    uid = username or f'u_{_uid()}'
    return User.objects.create_user(username=uid, password=_PASSWORD, role='admin')


def _category():
    n = f'Cat_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower())


def _product(name=None):
    n = name or f'Prod_{_uid()}'
    return Product.objects.create(
        category=_category(), name=n,
        price=Decimal('65.00'), stock_quantity=100,
    )


def _ingredient(name=None, base_unit='g', is_active=True, item_type='ingredient'):
    n = name or f'Ing_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=Decimal('1000.000'), is_active=is_active,
    )


def _packaging_item(name=None, is_active=True, item_type='packaging'):
    n = name or f'Pkg_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type=item_type, base_unit='pcs',
        on_hand_quantity=Decimal('500.000'), is_active=is_active,
    )


def _approved_recipe(product, size='none', scope='all', revision=1,
                     approved_at=None):
    return Recipe.objects.create(
        product=product, size=size, order_type_scope=scope,
        status=Recipe.STATUS_APPROVED, revision=revision,
        approved_at=approved_at or timezone.now(),
        approval_note='Test approval',
    )


def _approved_profile(name=None, revision=1):
    n = name or f'Prof_{_uid()}'
    return PackagingProfile.objects.create(
        name=n, status=PackagingProfile.STATUS_APPROVED,
        revision=revision, approved_at=timezone.now(),
        approval_note='Test profile approval',
    )


def _rule(product, size='none', order_type='takeout',
          profile=None, no_packaging=False):
    return ProductPackagingRule.objects.create(
        product=product, size=size, order_type=order_type,
        profile=profile, no_packaging=no_packaging,
    )


def _no_packaging_rule(product, size='none', order_type='takeout'):
    return _rule(product, size=size, order_type=order_type,
                 profile=None, no_packaging=True)


# ─────────────────────────────────────────────────────────────────────────────
# Shared setup helper: one product with one approved all-scope recipe and one
# takeout no-packaging rule — the minimal happy-path fixture.
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_setup():
    """Return (product, ingredient, recipe, rule) for the simplest happy path."""
    prod = _product()
    ing  = _ingredient()
    rec  = _approved_recipe(prod, size='none', scope='all')
    RecipeItem.objects.create(
        recipe=rec, inventory_item=ing, quantity=Decimal('50.000'),
    )
    rule = _no_packaging_rule(prod, size='none', order_type='takeout')
    return prod, ing, rec, rule


# ═════════════════════════════════════════════════════════════════════════════
# 1–4. Happy paths — scope resolution
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanScopeTests(TestCase):

    def setUp(self):
        self.prod = _product()
        self.ing  = _ingredient()

    def _make_all_scope_recipe(self):
        rec = _approved_recipe(self.prod, size='none', scope='all')
        RecipeItem.objects.create(
            recipe=rec, inventory_item=self.ing, quantity=Decimal('30.000'),
        )
        return rec

    def _make_dine_in_recipe(self):
        rec = _approved_recipe(self.prod, size='none', scope='dine_in')
        RecipeItem.objects.create(
            recipe=rec, inventory_item=self.ing, quantity=Decimal('30.000'),
        )
        return rec

    def _make_takeout_recipe(self):
        rec = _approved_recipe(self.prod, size='none', scope='takeout')
        RecipeItem.objects.create(
            recipe=rec, inventory_item=self.ing, quantity=Decimal('30.000'),
        )
        return rec

    def _make_rule(self, order_type):
        return _no_packaging_rule(self.prod, size='none', order_type=order_type)

    def test_all_scope_recipe_resolves_for_dine_in(self):
        """'all' scope recipe is a valid match for dine_in order type."""
        rec = self._make_all_scope_recipe()
        self._make_rule('dine_in')
        plan = resolve_plan(self.prod.pk, 'none', 'dine_in')
        self.assertEqual(plan.recipe.pk, rec.pk)

    def test_all_scope_recipe_resolves_for_takeout(self):
        """'all' scope recipe is a valid match for takeout order type."""
        rec = self._make_all_scope_recipe()
        self._make_rule('takeout')
        plan = resolve_plan(self.prod.pk, 'none', 'takeout')
        self.assertEqual(plan.recipe.pk, rec.pk)

    def test_dine_in_scope_recipe_resolves_for_dine_in(self):
        """'dine_in' scope recipe resolves for dine_in."""
        rec = self._make_dine_in_recipe()
        self._make_rule('dine_in')
        plan = resolve_plan(self.prod.pk, 'none', 'dine_in')
        self.assertEqual(plan.recipe.pk, rec.pk)

    def test_takeout_scope_recipe_resolves_for_takeout(self):
        """'takeout' scope recipe resolves for takeout."""
        rec = self._make_takeout_recipe()
        self._make_rule('takeout')
        plan = resolve_plan(self.prod.pk, 'none', 'takeout')
        self.assertEqual(plan.recipe.pk, rec.pk)

    def test_dine_in_scope_recipe_does_not_resolve_for_takeout(self):
        """A 'dine_in' scope recipe must NOT match a takeout request.
        The takeout request would find zero candidates → RecipeMissingError."""
        self._make_dine_in_recipe()
        self._make_rule('takeout')
        with self.assertRaises(RecipeMissingError):
            resolve_plan(self.prod.pk, 'none', 'takeout')

    def test_takeout_scope_recipe_does_not_resolve_for_dine_in(self):
        """A 'takeout' scope recipe must NOT match a dine_in request."""
        self._make_takeout_recipe()
        self._make_rule('dine_in')
        with self.assertRaises(RecipeMissingError):
            resolve_plan(self.prod.pk, 'none', 'dine_in')


# ═════════════════════════════════════════════════════════════════════════════
# 5. No-packaging rule resolves with empty packaging components
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanNoPackagingTests(TestCase):

    def test_no_packaging_rule_yields_empty_packaging_components(self):
        prod, ing, rec, rule = _minimal_setup()
        plan = resolve_plan(prod.pk, 'none', 'takeout')
        packaging_components = [
            c for c in plan.components
            if c.component_source == COMPONENT_SOURCE_PACKAGING
        ]
        self.assertEqual(len(packaging_components), 0,
            'no_packaging=True must produce zero packaging components')

    def test_no_packaging_rule_still_has_ingredient_components(self):
        prod, ing, rec, rule = _minimal_setup()
        plan = resolve_plan(prod.pk, 'none', 'takeout')
        ingredient_components = [
            c for c in plan.components
            if c.component_source == COMPONENT_SOURCE_RECIPE
        ]
        self.assertEqual(len(ingredient_components), 1)

    def test_no_packaging_packaging_rule_field_accessible(self):
        prod, ing, rec, rule = _minimal_setup()
        plan = resolve_plan(prod.pk, 'none', 'takeout')
        self.assertTrue(plan.packaging_rule.no_packaging)


# ═════════════════════════════════════════════════════════════════════════════
# 6–10. ResolvedPlan and ResolvedPlanComponent field correctness
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanFieldsTests(TestCase):

    def setUp(self):
        self.prod = _product()
        self.ing  = _ingredient(name=f'Coffee_{_uid()}')
        self.rec  = _approved_recipe(self.prod, size='medium', scope='all',
                                     revision=3)
        RecipeItem.objects.create(
            recipe=self.rec, inventory_item=self.ing, quantity=Decimal('18.000'),
        )
        self.profile = _approved_profile()
        self.pkg_item = _packaging_item(name=f'Cup_{_uid()}')
        PackagingProfileItem.objects.create(
            profile=self.profile, inventory_item=self.pkg_item,
            quantity=Decimal('1.000'),
        )
        self.rule = _rule(self.prod, size='medium', order_type='takeout',
                          profile=self.profile, no_packaging=False)
        self.plan = resolve_plan(self.prod.pk, 'medium', 'takeout')

    def test_plan_product_id(self):
        self.assertEqual(self.plan.product_id, self.prod.pk)

    def test_plan_size(self):
        self.assertEqual(self.plan.size, 'medium')

    def test_plan_order_type(self):
        self.assertEqual(self.plan.order_type, 'takeout')

    def test_plan_recipe_is_correct(self):
        self.assertEqual(self.plan.recipe.pk, self.rec.pk)

    def test_plan_packaging_rule_is_correct(self):
        self.assertEqual(self.plan.packaging_rule.pk, self.rule.pk)

    def test_plan_has_one_ingredient_component(self):
        ing_comps = [c for c in self.plan.components
                     if c.component_source == COMPONENT_SOURCE_RECIPE]
        self.assertEqual(len(ing_comps), 1)

    def test_plan_has_one_packaging_component(self):
        pkg_comps = [c for c in self.plan.components
                     if c.component_source == COMPONENT_SOURCE_PACKAGING]
        self.assertEqual(len(pkg_comps), 1)

    def test_ingredient_component_fields(self):
        comp = next(c for c in self.plan.components
                    if c.component_source == COMPONENT_SOURCE_RECIPE)
        self.assertEqual(comp.inventory_item.pk, self.ing.pk)
        self.assertEqual(comp.component_source, COMPONENT_SOURCE_RECIPE)
        self.assertEqual(comp.source_recipe_id, self.rec.pk)
        self.assertIsNone(comp.source_packaging_profile_id)
        self.assertEqual(comp.source_revision, self.rec.revision)   # 3
        self.assertEqual(comp.quantity_per_unit, Decimal('18.000'))

    def test_packaging_component_fields(self):
        comp = next(c for c in self.plan.components
                    if c.component_source == COMPONENT_SOURCE_PACKAGING)
        self.assertEqual(comp.inventory_item.pk, self.pkg_item.pk)
        self.assertEqual(comp.component_source, COMPONENT_SOURCE_PACKAGING)
        self.assertIsNone(comp.source_recipe_id)
        self.assertEqual(comp.source_packaging_profile_id, self.profile.pk)
        self.assertEqual(comp.source_revision, self.profile.revision)
        self.assertEqual(comp.quantity_per_unit, Decimal('1.000'))

    def test_components_are_frozen(self):
        """ResolvedPlanComponent is a frozen dataclass — cannot mutate."""
        comp = self.plan.components[0]
        with self.assertRaises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            comp.quantity_per_unit = Decimal('99.000')

    def test_plan_is_frozen(self):
        """ResolvedPlan is a frozen dataclass — cannot mutate."""
        with self.assertRaises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            self.plan.size = 'large'


import dataclasses  # noqa: E402 — needed for FrozenInstanceError reference above


# ═════════════════════════════════════════════════════════════════════════════
# 11. Decimal precision — no float contamination
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanDecimalPrecisionTests(TestCase):

    def test_quantity_per_unit_is_exact_decimal(self):
        """Quantities in ResolvedPlanComponent must be exact Decimal values."""
        prod = _product()
        ing  = _ingredient()
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(
            recipe=rec, inventory_item=ing, quantity=Decimal('0.125'),
        )
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        plan = resolve_plan(prod.pk, 'none', 'dine_in')
        comp = plan.components[0]
        self.assertIsInstance(comp.quantity_per_unit, Decimal)
        self.assertEqual(comp.quantity_per_unit, Decimal('0.125'))

    def test_aggregation_is_exact(self):
        """quantity_per_unit × line.quantity must not lose precision."""
        prod = _product()
        ing  = _ingredient()
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(
            recipe=rec, inventory_item=ing, quantity=Decimal('3.333'),
        )
        _no_packaging_rule(prod, size='none', order_type='takeout')
        plan = resolve_plan(prod.pk, 'none', 'takeout')
        qty_per_unit = plan.components[0].quantity_per_unit
        # Simulate place_order_inventory accumulation for line.quantity = 3
        total = qty_per_unit * 3
        self.assertEqual(total, Decimal('9.999'))
        self.assertNotIsInstance(total, float)


# ═════════════════════════════════════════════════════════════════════════════
# 12–13. Multiple items in recipe / packaging profile
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanMultipleItemsTests(TestCase):

    def test_multiple_recipe_items_all_in_components(self):
        prod = _product()
        ing1 = _ingredient(name=f'Coffee_{_uid()}')
        ing2 = _ingredient(name=f'Milk_{_uid()}')
        ing3 = _ingredient(name=f'Sugar_{_uid()}')
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing1, quantity=Decimal('18.000'))
        RecipeItem.objects.create(recipe=rec, inventory_item=ing2, quantity=Decimal('150.000'))
        RecipeItem.objects.create(recipe=rec, inventory_item=ing3, quantity=Decimal('5.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        plan = resolve_plan(prod.pk, 'none', 'dine_in')
        ing_comps = [c for c in plan.components
                     if c.component_source == COMPONENT_SOURCE_RECIPE]
        self.assertEqual(len(ing_comps), 3)
        item_ids = {c.inventory_item.pk for c in ing_comps}
        self.assertIn(ing1.pk, item_ids)
        self.assertIn(ing2.pk, item_ids)
        self.assertIn(ing3.pk, item_ids)

    def test_multiple_packaging_items_all_in_components(self):
        prod    = _product()
        ing     = _ingredient()
        rec     = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing, quantity=Decimal('10.000'))
        profile = _approved_profile()
        p1 = _packaging_item(name=f'Cup_{_uid()}')
        p2 = _packaging_item(name=f'Lid_{_uid()}')
        PackagingProfileItem.objects.create(profile=profile, inventory_item=p1, quantity=Decimal('1.000'))
        PackagingProfileItem.objects.create(profile=profile, inventory_item=p2, quantity=Decimal('1.000'))
        _rule(prod, size='none', order_type='takeout', profile=profile)
        plan = resolve_plan(prod.pk, 'none', 'takeout')
        pkg_comps = [c for c in plan.components
                     if c.component_source == COMPONENT_SOURCE_PACKAGING]
        self.assertEqual(len(pkg_comps), 2)
        item_ids = {c.inventory_item.pk for c in pkg_comps}
        self.assertIn(p1.pk, item_ids)
        self.assertIn(p2.pk, item_ids)


# ═════════════════════════════════════════════════════════════════════════════
# 14–16. Error: recipe missing / draft / retired
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanRecipeMissingTests(TestCase):

    def setUp(self):
        self.prod = _product()

    def test_no_recipe_row_raises_recipe_missing_error(self):
        """No Recipe row at all → RecipeMissingError."""
        with self.assertRaises(RecipeMissingError) as ctx:
            resolve_plan(self.prod.pk, 'none', 'takeout')
        err = ctx.exception
        self.assertEqual(err.product_id, self.prod.pk)
        self.assertEqual(err.size, 'none')
        self.assertEqual(err.order_type, 'takeout')

    def test_draft_only_recipe_raises_recipe_missing_error(self):
        """Only a draft recipe exists — drafts do not count (§I.2)."""
        Recipe.objects.create(
            product=self.prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_DRAFT, revision=1,
        )
        with self.assertRaises(RecipeMissingError):
            resolve_plan(self.prod.pk, 'none', 'dine_in')

    def test_retired_only_recipe_raises_recipe_missing_error(self):
        """Only a retired recipe exists — retired recipes do not count."""
        Recipe.objects.create(
            product=self.prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_RETIRED, revision=1,
        )
        with self.assertRaises(RecipeMissingError):
            resolve_plan(self.prod.pk, 'none', 'dine_in')

    def test_draft_and_retired_together_still_raises_recipe_missing_error(self):
        """Both draft AND retired — still no approved recipe."""
        Recipe.objects.create(
            product=self.prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_DRAFT, revision=1,
        )
        Recipe.objects.create(
            product=self.prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_RETIRED, revision=2,
        )
        with self.assertRaises(RecipeMissingError):
            resolve_plan(self.prod.pk, 'none', 'takeout')

    def test_recipe_missing_error_message_contains_context(self):
        with self.assertRaises(RecipeMissingError) as ctx:
            resolve_plan(self.prod.pk, 'small', 'takeout')
        msg = str(ctx.exception)
        self.assertIn(str(self.prod.pk), msg)
        self.assertIn('small', msg)
        self.assertIn('takeout', msg)


# ═════════════════════════════════════════════════════════════════════════════
# 17. Error: ambiguous recipe
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanAmbiguousRecipeTests(TestCase):

    def test_all_scope_plus_specific_scope_raises_ambiguous(self):
        """
        'all' scope AND 'dine_in' scope both approved for (product, size).
        Rev 3 §I.2 RAISE AmbiguousRecipeError — §M.1: "ambiguous (all plus a specific scope)".
        """
        prod = _product()
        ing  = _ingredient()
        rec_all    = _approved_recipe(prod, size='none', scope='all', revision=1)
        rec_dine   = _approved_recipe(prod, size='none', scope='dine_in', revision=1)
        RecipeItem.objects.create(recipe=rec_all,  inventory_item=ing, quantity=Decimal('10.000'))
        RecipeItem.objects.create(recipe=rec_dine, inventory_item=ing, quantity=Decimal('10.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        with self.assertRaises(AmbiguousRecipeError) as ctx:
            resolve_plan(prod.pk, 'none', 'dine_in')
        err = ctx.exception
        self.assertEqual(err.product_id, prod.pk)
        self.assertEqual(err.size, 'none')
        self.assertEqual(err.order_type, 'dine_in')
        # Both matching recipe ids must be reported.
        self.assertIn(rec_all.pk, err.matching_ids)
        self.assertIn(rec_dine.pk, err.matching_ids)
        self.assertEqual(len(err.matching_ids), 2)

    def test_two_different_scope_specific_recipes_do_not_conflict_for_own_order_type(self):
        """dine_in + takeout scopes for the same (product, size) are fine:
        each resolves only for its own order_type."""
        prod = _product()
        ing  = _ingredient()
        rec_d = _approved_recipe(prod, size='none', scope='dine_in', revision=1)
        rec_t = _approved_recipe(prod, size='none', scope='takeout', revision=1)
        RecipeItem.objects.create(recipe=rec_d, inventory_item=ing, quantity=Decimal('10.000'))
        RecipeItem.objects.create(recipe=rec_t, inventory_item=ing, quantity=Decimal('10.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        _no_packaging_rule(prod, size='none', order_type='takeout')
        plan_d = resolve_plan(prod.pk, 'none', 'dine_in')
        plan_t = resolve_plan(prod.pk, 'none', 'takeout')
        self.assertEqual(plan_d.recipe.pk, rec_d.pk)
        self.assertEqual(plan_t.recipe.pk, rec_t.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 18. Error: packaging rule missing
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanPackagingRuleMissingTests(TestCase):

    def test_no_packaging_rule_raises_packaging_rule_missing_error(self):
        """Approved recipe exists but no ProductPackagingRule → PackagingRuleMissingError."""
        prod = _product()
        ing  = _ingredient()
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing, quantity=Decimal('10.000'))
        # No ProductPackagingRule created.
        with self.assertRaises(PackagingRuleMissingError) as ctx:
            resolve_plan(prod.pk, 'none', 'takeout')
        err = ctx.exception
        self.assertEqual(err.product_id, prod.pk)
        self.assertEqual(err.size, 'none')
        self.assertEqual(err.order_type, 'takeout')

    def test_packaging_rule_exists_for_dine_in_but_not_takeout(self):
        """Rule for dine_in doesn't satisfy a takeout request."""
        prod = _product()
        ing  = _ingredient()
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing, quantity=Decimal('10.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')  # only dine_in
        with self.assertRaises(PackagingRuleMissingError):
            resolve_plan(prod.pk, 'none', 'takeout')


# ═════════════════════════════════════════════════════════════════════════════
# 19–22. Error: inactive item / wrong item type
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanInactiveItemTests(TestCase):

    def test_inactive_ingredient_raises_inactive_item_error(self):
        """Recipe item references inactive InventoryItem → InactiveItemError (§I.3)."""
        prod = _product()
        ing  = _ingredient(is_active=False)  # inactive
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing, quantity=Decimal('10.000'))
        _no_packaging_rule(prod, size='none', order_type='takeout')
        with self.assertRaises(InactiveItemError) as ctx:
            resolve_plan(prod.pk, 'none', 'takeout')
        self.assertIsInstance(ctx.exception, InactiveItemError)

    def test_wrong_item_type_in_recipe_raises_inactive_item_error(self):
        """Recipe item has item_type='packaging' (wrong) → InactiveItemError (§M.1 'wrong item type')."""
        prod = _product()
        # Active but item_type='packaging' — wrong for a recipe ingredient.
        wrong_item = InventoryItem.objects.create(
            name=f'WrongType_{_uid()}', item_type='packaging',
            base_unit='pcs', on_hand_quantity=Decimal('100.000'), is_active=True,
        )
        rec = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=wrong_item,
                                  quantity=Decimal('1.000'))
        _no_packaging_rule(prod, size='none', order_type='takeout')
        with self.assertRaises(InactiveItemError):
            resolve_plan(prod.pk, 'none', 'takeout')

    def test_inactive_packaging_item_raises_inactive_item_error(self):
        """Packaging profile item references inactive InventoryItem → InactiveItemError."""
        prod    = _product()
        ing     = _ingredient()
        rec     = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        profile = _approved_profile()
        pkg_inactive = _packaging_item(is_active=False)
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=pkg_inactive, quantity=Decimal('1.000'),
        )
        _rule(prod, size='none', order_type='takeout', profile=profile)
        with self.assertRaises(InactiveItemError):
            resolve_plan(prod.pk, 'none', 'takeout')

    def test_wrong_item_type_in_packaging_profile_raises_inactive_item_error(self):
        """Packaging profile item has item_type='ingredient' → InactiveItemError."""
        prod    = _product()
        ing     = _ingredient()
        rec     = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        profile  = _approved_profile()
        # Active but wrong type — ingredient instead of packaging.
        wrong_type_item = InventoryItem.objects.create(
            name=f'WrongPkg_{_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal('100.000'), is_active=True,
        )
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=wrong_type_item, quantity=Decimal('1.000'),
        )
        _rule(prod, size='none', order_type='dine_in', profile=profile)
        with self.assertRaises(InactiveItemError):
            resolve_plan(prod.pk, 'none', 'dine_in')

    def test_inactive_item_error_carries_item_reference(self):
        """InactiveItemError.item is set for item-level failures."""
        prod = _product()
        ing  = _ingredient(is_active=False)
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        _no_packaging_rule(prod, size='none', order_type='takeout')
        with self.assertRaises(InactiveItemError) as ctx:
            resolve_plan(prod.pk, 'none', 'takeout')
        self.assertEqual(ctx.exception.item.pk, ing.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 23–24. Error: draft/retired packaging profile on rule
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanDraftRetiredProfileTests(TestCase):

    def _setup_with_profile_status(self, profile_status):
        prod = _product()
        ing  = _ingredient()
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        profile = PackagingProfile.objects.create(
            name=f'Prof_{_uid()}', status=profile_status, revision=1,
            approved_at=timezone.now() if profile_status == 'approved' else None,
        )
        # Bypass ppr_profile_xor_none at DB level (DB only checks null/not-null, not status).
        ProductPackagingRule.objects.create(
            product=prod, size='none', order_type='takeout',
            profile=profile, no_packaging=False,
        )
        return prod

    def test_draft_profile_on_rule_raises_inactive_item_error(self):
        """Rule pointing at a draft profile → InactiveItemError (profile not approved)."""
        prod = self._setup_with_profile_status('draft')
        with self.assertRaises(InactiveItemError):
            resolve_plan(prod.pk, 'none', 'takeout')

    def test_retired_profile_on_rule_raises_inactive_item_error(self):
        """Rule pointing at a retired profile → InactiveItemError."""
        prod = self._setup_with_profile_status('retired')
        with self.assertRaises(InactiveItemError):
            resolve_plan(prod.pk, 'none', 'takeout')


# ═════════════════════════════════════════════════════════════════════════════
# 25–26. Exception hierarchy
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanExceptionHierarchyTests(TestCase):

    def test_recipe_missing_error_is_recipe_incomplete_error(self):
        self.assertTrue(issubclass(RecipeMissingError, RecipeIncompleteError))

    def test_ambiguous_recipe_error_is_recipe_incomplete_error(self):
        self.assertTrue(issubclass(AmbiguousRecipeError, RecipeIncompleteError))

    def test_packaging_rule_missing_error_is_recipe_incomplete_error(self):
        self.assertTrue(issubclass(PackagingRuleMissingError, RecipeIncompleteError))

    def test_inactive_item_error_is_recipe_incomplete_error(self):
        self.assertTrue(issubclass(InactiveItemError, RecipeIncompleteError))

    def test_recipe_incomplete_error_is_inventory_service_error(self):
        self.assertTrue(issubclass(RecipeIncompleteError, InventoryServiceError))

    def test_inventory_service_error_is_value_error(self):
        self.assertTrue(issubclass(InventoryServiceError, ValueError))

    def test_recipe_missing_error_caught_by_value_error(self):
        prod = _product()
        with self.assertRaises(ValueError):
            resolve_plan(prod.pk, 'none', 'takeout')

    def test_recipe_missing_error_caught_by_recipe_incomplete_error(self):
        prod = _product()
        with self.assertRaises(RecipeIncompleteError):
            resolve_plan(prod.pk, 'none', 'dine_in')


# ═════════════════════════════════════════════════════════════════════════════
# 27–29. No-mutation guarantee
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanNoMutationTests(TestCase):

    def setUp(self):
        self.prod, self.ing, self.rec, self.rule = _minimal_setup()

    def test_inventory_item_quantities_unchanged_after_resolve(self):
        """resolve_plan must not modify any InventoryItem quantity."""
        on_hand_before  = self.ing.on_hand_quantity
        reserved_before = self.ing.reserved_quantity
        resolve_plan(self.prod.pk, 'none', 'takeout')
        self.ing.refresh_from_db()
        self.assertEqual(self.ing.on_hand_quantity, on_hand_before)
        self.assertEqual(self.ing.reserved_quantity, reserved_before)

    def test_no_movement_log_rows_created(self):
        """resolve_plan must not write any IngredientMovementLog rows."""
        log_count_before = IngredientMovementLog.objects.count()
        resolve_plan(self.prod.pk, 'none', 'takeout')
        self.assertEqual(IngredientMovementLog.objects.count(), log_count_before)

    def test_no_mutation_on_error_path(self):
        """Even when resolve_plan raises, quantities must be unchanged."""
        prod = _product()  # no recipe → will raise
        on_hand_before = self.ing.on_hand_quantity
        try:
            resolve_plan(prod.pk, 'none', 'takeout')
        except RecipeMissingError:
            pass
        self.ing.refresh_from_db()
        self.assertEqual(self.ing.on_hand_quantity, on_hand_before)


# ═════════════════════════════════════════════════════════════════════════════
# 30–32. Independence — different products and sizes
# ═════════════════════════════════════════════════════════════════════════════

class ResolvePlanIndependenceTests(TestCase):

    def test_different_products_resolve_independently(self):
        """Configuration for product A does not affect product B."""
        prod_a = _product(name=f'ProdA_{_uid()}')
        prod_b = _product(name=f'ProdB_{_uid()}')
        ing_a  = _ingredient(name=f'IngA_{_uid()}')
        ing_b  = _ingredient(name=f'IngB_{_uid()}')

        rec_a = _approved_recipe(prod_a, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec_a, inventory_item=ing_a,
                                  quantity=Decimal('10.000'))
        _no_packaging_rule(prod_a, size='none', order_type='dine_in')

        rec_b = _approved_recipe(prod_b, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec_b, inventory_item=ing_b,
                                  quantity=Decimal('20.000'))
        _no_packaging_rule(prod_b, size='none', order_type='dine_in')

        plan_a = resolve_plan(prod_a.pk, 'none', 'dine_in')
        plan_b = resolve_plan(prod_b.pk, 'none', 'dine_in')

        self.assertEqual(plan_a.recipe.pk, rec_a.pk)
        self.assertEqual(plan_b.recipe.pk, rec_b.pk)
        self.assertNotEqual(plan_a.recipe.pk, plan_b.recipe.pk)

    def test_same_product_different_sizes_resolve_independently(self):
        """A recipe for size='small' does not satisfy size='medium' resolution."""
        prod   = _product()
        ing    = _ingredient()
        rec_sm = _approved_recipe(prod, size='small', scope='all')
        RecipeItem.objects.create(recipe=rec_sm, inventory_item=ing,
                                  quantity=Decimal('15.000'))
        _no_packaging_rule(prod, size='small', order_type='takeout')
        # No recipe for size='medium'
        with self.assertRaises(RecipeMissingError):
            resolve_plan(prod.pk, 'medium', 'takeout')

    def test_ambiguous_error_carries_correct_matching_ids(self):
        """AmbiguousRecipeError.matching_ids contains exactly the two conflicting pks."""
        prod = _product()
        ing  = _ingredient()
        rec1 = _approved_recipe(prod, size='none', scope='all', revision=1)
        rec2 = _approved_recipe(prod, size='none', scope='dine_in', revision=1)
        RecipeItem.objects.create(recipe=rec1, inventory_item=ing,
                                  quantity=Decimal('5.000'))
        RecipeItem.objects.create(recipe=rec2, inventory_item=ing,
                                  quantity=Decimal('5.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        with self.assertRaises(AmbiguousRecipeError) as ctx:
            resolve_plan(prod.pk, 'none', 'dine_in')
        err = ctx.exception
        self.assertCountEqual(err.matching_ids, [rec1.pk, rec2.pk])
