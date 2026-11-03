"""
tests_phase4b4.py
=================
Phase 4B-4 — get_available_units() service tests.

Covers Rev 3 §C.2 and §E.3b:

  Legacy product routing
  ──────────────────────
  1.  Legacy product returns stock_quantity regardless of size/order_type
  2.  Legacy product: size and order_type parameters are irrelevant
  3.  Legacy product: configuration_complete=True

  Ingredient product — happy paths
  ─────────────────────────────────
  4.  Ingredient product with complete config returns correct units (single component)
  5.  Units = floor(available / qty_per_unit)
  6.  Min over multiple components (bottleneck is the limiting component)
  7.  Packaging components also count in the minimum
  8.  no_packaging=True rule: only recipe components count
  9.  complete=True on happy path
  10. reason='' on happy path
  11. Returns integer (not Decimal)

  Ingredient product — order_type=None (most restrictive)
  ────────────────────────────────────────────────────────
  12. order_type=None: both complete, returns minimum of the two
  13. order_type=None: dine_in complete, takeout missing — returns dine_in result
  14. order_type=None: takeout complete, dine_in missing — returns takeout result
  15. order_type=None: both incomplete — returns 0 with complete=False

  Ingredient product — zero stock / zero units
  ─────────────────────────────────────────────
  16. Zero available stock → units=0, configuration_complete=True
  17. Available stock less than qty_per_unit → units=0

  Ingredient product — configuration errors → 0 + complete=False
  ────────────────────────────────────────────────────────────────
  18. Missing recipe → units=0, configuration_complete=False
  19. Draft-only recipe → units=0, configuration_complete=False
  20. Retired-only recipe → units=0, configuration_complete=False
  21. Ambiguous recipe → units=0, configuration_complete=False
  22. Missing packaging rule → units=0, configuration_complete=False
  23. Inactive ingredient item → units=0, configuration_complete=False
  24. Wrong item_type in recipe → units=0, configuration_complete=False
  25. Inactive packaging item → units=0, configuration_complete=False
  26. Draft packaging profile on rule → units=0, configuration_complete=False

  Return structure
  ─────────────────
  27. AvailabilityResult is a NamedTuple with correct fields
  28. reason is a non-empty string when configuration_complete=False
  29. reason is '' when configuration_complete=True

  Read-only guarantee
  ────────────────────
  30. No InventoryItem quantities changed
  31. No IngredientMovementLog rows created
  32. Call on error path leaves quantities unchanged

  Precision
  ──────────
  33. Exact floor calculation with fractional availability

All tests use synthetic fixtures. All tests are SQLite-safe (no locks).
"""

import math
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
    AvailabilityResult,
    get_available_units,
    RecipeIncompleteError,
)
from apps.menu.models import Category, Product

User = get_user_model()
_PASSWORD = 'test-4b4-pass'
_counter = 0


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _category():
    n = f'Cat_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower())


def _product(name=None, tracking_mode='legacy', stock=100):
    n = name or f'Prod_{_uid()}'
    return Product.objects.create(
        category=_category(), name=n,
        price=Decimal('65.00'),
        stock_quantity=stock,
        inventory_tracking_mode=tracking_mode,
    )


def _ingredient_product(name=None, stock=100):
    return _product(name=name, tracking_mode='ingredient', stock=stock)


def _legacy_product(name=None, stock=100):
    return _product(name=name, tracking_mode='legacy', stock=stock)


def _inventory_item(name=None, item_type='ingredient', base_unit='g',
                    on_hand=Decimal('1000.000'), reserved=Decimal('0.000'),
                    is_active=True):
    n = name or f'Item_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=on_hand,
        reserved_quantity=reserved,
        is_active=is_active,
    )


def _ingredient(name=None, on_hand=Decimal('1000.000'),
                reserved=Decimal('0.000'), is_active=True):
    return _inventory_item(name=name, item_type='ingredient', base_unit='g',
                           on_hand=on_hand, reserved=reserved,
                           is_active=is_active)


def _packaging_inv_item(name=None, on_hand=Decimal('500.000'),
                        reserved=Decimal('0.000'), is_active=True):
    return _inventory_item(name=name, item_type='packaging', base_unit='pcs',
                           on_hand=on_hand, reserved=reserved,
                           is_active=is_active)


def _approved_recipe(product, size='none', scope='all', revision=1):
    return Recipe.objects.create(
        product=product, size=size, order_type_scope=scope,
        status=Recipe.STATUS_APPROVED, revision=revision,
        approved_at=timezone.now(), approval_note='Test',
    )


def _approved_profile(name=None, revision=1):
    n = name or f'Prof_{_uid()}'
    return PackagingProfile.objects.create(
        name=n, status=PackagingProfile.STATUS_APPROVED,
        revision=revision, approved_at=timezone.now(),
        approval_note='Test',
    )


def _no_packaging_rule(product, size='none', order_type='takeout'):
    return ProductPackagingRule.objects.create(
        product=product, size=size, order_type=order_type,
        profile=None, no_packaging=True,
    )


def _profile_rule(product, profile, size='none', order_type='takeout'):
    return ProductPackagingRule.objects.create(
        product=product, size=size, order_type=order_type,
        profile=profile, no_packaging=False,
    )


def _complete_setup(size='none', order_type='takeout',
                    ing_on_hand=Decimal('1000.000'),
                    ing_reserved=Decimal('0.000'),
                    qty_per_unit=Decimal('50.000')):
    """
    Return (product, ingredient, recipe, rule) with a fully configured
    ingredient-tracked product ready for get_available_units() calls.
    """
    prod = _ingredient_product()
    ing  = _ingredient(on_hand=ing_on_hand, reserved=ing_reserved)
    rec  = _approved_recipe(prod, size=size, scope='all')
    RecipeItem.objects.create(
        recipe=rec, inventory_item=ing, quantity=qty_per_unit,
    )
    rule = _no_packaging_rule(prod, size=size, order_type=order_type)
    return prod, ing, rec, rule


# ═════════════════════════════════════════════════════════════════════════════
# 1–3. Legacy product routing
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsLegacyTests(TestCase):
    """Legacy products return stock_quantity without touching resolve_plan."""

    def test_legacy_returns_stock_quantity(self):
        """Rev 3 §C.2: legacy → returns stock_quantity."""
        prod = _legacy_product(stock=42)
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 42)

    def test_legacy_stock_quantity_zero(self):
        prod = _legacy_product(stock=0)
        result = get_available_units(prod, 'none', 'dine_in')
        self.assertEqual(result.units, 0)

    def test_legacy_size_parameter_irrelevant(self):
        """For legacy products all sizes return the same stock_quantity."""
        prod = _legacy_product(stock=10)
        for size in ['none', 'small', 'medium', 'large', 'hot']:
            with self.subTest(size=size):
                result = get_available_units(prod, size, 'takeout')
                self.assertEqual(result.units, 10)

    def test_legacy_order_type_irrelevant(self):
        prod = _legacy_product(stock=7)
        self.assertEqual(get_available_units(prod, 'none', 'dine_in').units, 7)
        self.assertEqual(get_available_units(prod, 'none', 'takeout').units, 7)
        self.assertEqual(get_available_units(prod, 'none', None).units, 7)

    def test_legacy_configuration_complete_true(self):
        """Legacy products always have configuration_complete=True."""
        prod = _legacy_product(stock=5)
        result = get_available_units(prod, 'none', 'takeout')
        self.assertTrue(result.configuration_complete)
        self.assertEqual(result.reason, '')

    def test_legacy_returns_int(self):
        prod = _legacy_product(stock=15)
        result = get_available_units(prod, 'none', 'takeout')
        self.assertIsInstance(result.units, int)


# ═════════════════════════════════════════════════════════════════════════════
# 4–11. Ingredient product — happy paths
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsIngredientHappyTests(TestCase):
    """Ingredient products with complete config return correct unit counts."""

    def test_basic_units_calculation(self):
        """1000g available / 50g per unit = 20 units. Rev 3 §E.3b."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('1000.000'),
            ing_reserved=Decimal('0.000'),
            qty_per_unit=Decimal('50.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 20)

    def test_floor_applied(self):
        """Rev 3 §E.3b: floor(available / qty_per_unit). 100g / 30g = 3.33 → 3."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('100.000'),
            qty_per_unit=Decimal('30.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 3)

    def test_reserved_stock_reduces_available(self):
        """Available = on_hand − reserved. 1000g − 900g reserved = 100g → 2 units of 50g."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('1000.000'),
            ing_reserved=Decimal('900.000'),
            qty_per_unit=Decimal('50.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 2)

    def test_minimum_over_multiple_recipe_components(self):
        """Rev 3 §E.3b: minimum over ALL components. Bottleneck item limits result."""
        prod = _ingredient_product()
        coffee = _ingredient(name=f'Coffee_{_uid()}', on_hand=Decimal('500.000'))
        milk   = _ingredient(name=f'Milk_{_uid()}',   on_hand=Decimal('150.000'))
        rec = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=coffee, quantity=Decimal('18.000'))
        RecipeItem.objects.create(recipe=rec, inventory_item=milk,   quantity=Decimal('120.000'))
        _no_packaging_rule(prod, size='none', order_type='takeout')
        # coffee: floor(500 / 18) = 27
        # milk:   floor(150 / 120) = 1  ← bottleneck
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 1)

    def test_packaging_components_count_in_minimum(self):
        """Packaging items also constrain availability. Rev 3 §E.3b."""
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('1000.000'))
        cup  = _packaging_inv_item(name=f'Cup_{_uid()}', on_hand=Decimal('5.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing, quantity=Decimal('10.000'))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        _profile_rule(prod, profile=profile, size='none', order_type='takeout')
        # ingredient: floor(1000 / 10) = 100
        # cup:        floor(5 / 1) = 5  ← bottleneck
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 5)

    def test_no_packaging_rule_only_recipe_components_count(self):
        """no_packaging=True means no packaging components; only recipe items count."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('300.000'),
            qty_per_unit=Decimal('100.000'),
        )
        # No packaging profile → floor(300 / 100) = 3
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 3)
        self.assertTrue(result.configuration_complete)

    def test_configuration_complete_true_on_happy_path(self):
        prod, ing, rec, rule = _complete_setup()
        result = get_available_units(prod, 'none', 'takeout')
        self.assertTrue(result.configuration_complete)

    def test_reason_empty_on_happy_path(self):
        prod, ing, rec, rule = _complete_setup()
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.reason, '')

    def test_returns_int(self):
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('750.000'), qty_per_unit=Decimal('25.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        self.assertIsInstance(result.units, int)
        self.assertEqual(result.units, 30)

    def test_single_component_exact_multiple(self):
        """Exact multiples (no fractional remainder) give a clean result."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('100.000'), qty_per_unit=Decimal('25.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 4)


# ═════════════════════════════════════════════════════════════════════════════
# 12–15. order_type=None — most restrictive
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsNoOrderTypeTests(TestCase):
    """
    Rev 3 §E.3b: when order_type is None, use the most restrictive applicable
    order type — compute for both and return the minimum.
    """

    def test_none_order_type_returns_minimum_when_both_complete(self):
        """Both dine_in and takeout complete: returns the smaller of the two."""
        prod = _ingredient_product()
        # Single ingredient used by both order types (same recipe via 'all' scope)
        ing = _ingredient(on_hand=Decimal('1000.000'))
        rec = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('50.000'))

        # Dine-in: no packaging (floor 1000/50 = 20)
        _no_packaging_rule(prod, size='none', order_type='dine_in')

        # Takeout: packaging with only 5 cups
        cup     = _packaging_inv_item(on_hand=Decimal('5.000'))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        _profile_rule(prod, profile, size='none', order_type='takeout')

        # dine_in → 20, takeout → 5 (cup bottleneck); min = 5
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 5)
        self.assertTrue(result.configuration_complete)

    def test_none_order_type_dine_in_complete_takeout_missing(self):
        """
        Scenario A: dine_in complete (12 units), takeout incomplete.
        Rev 3 §E.3b: "If no complete approved plan exists it returns 0 with a
        reason, so the product is not sellable rather than silently under-deducted."
        Both order types are applicable at the cart stage. A broken takeout
        plan means the product cannot be guaranteed sellable → returns 0.
        """
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('200.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('20.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        # No takeout rule — takeout is incomplete.
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete,
            'configuration_complete must be False when takeout has no plan')
        self.assertNotEqual(result.reason, '')

    def test_none_order_type_takeout_complete_dine_in_missing(self):
        """
        Scenario B: takeout complete (10 units), dine_in incomplete.
        Rev 3 §E.3b: product is not sellable when any applicable plan is missing.
        A broken dine_in plan at the cart stage → returns 0.
        """
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('300.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('30.000'))
        _no_packaging_rule(prod, size='none', order_type='takeout')
        # No dine_in rule — dine_in is incomplete.
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete,
            'configuration_complete must be False when dine_in has no plan')
        self.assertNotEqual(result.reason, '')

    def test_none_order_type_both_incomplete_returns_zero(self):
        """Neither dine_in nor takeout has any plan → 0 with complete=False."""
        prod = _ingredient_product()
        # No recipes, no rules.
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)
        self.assertNotEqual(result.reason, '')

    def test_none_order_type_legacy_product_returns_stock_quantity(self):
        """Legacy product with order_type=None still returns stock_quantity."""
        prod = _legacy_product(stock=33)
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 33)
        self.assertTrue(result.configuration_complete)


# ═════════════════════════════════════════════════════════════════════════════
# 16–17. Zero stock / below qty_per_unit
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsZeroStockTests(TestCase):

    def test_zero_available_returns_zero_units(self):
        """On-hand = 0, reserved = 0 → available = 0 → units = 0."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('0.000'),
            qty_per_unit=Decimal('50.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertTrue(result.configuration_complete,
            'configuration_complete must still be True when stock is genuinely 0')

    def test_available_less_than_qty_per_unit_returns_zero(self):
        """Available stock less than one unit's requirement → floor = 0."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('49.999'),
            qty_per_unit=Decimal('50.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertTrue(result.configuration_complete)

    def test_exactly_one_unit_available(self):
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('50.000'),
            qty_per_unit=Decimal('50.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 1)


# ═════════════════════════════════════════════════════════════════════════════
# 18–26. Configuration errors → 0 + configuration_complete=False
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsConfigErrorTests(TestCase):
    """
    Rev 3 §E.3b: "If no complete approved plan exists it returns 0 with a
    reason, so the product is not sellable rather than silently under-deducted."
    """

    def setUp(self):
        self.prod = _ingredient_product()

    def test_missing_recipe(self):
        """No Recipe row → units=0, configuration_complete=False."""
        result = get_available_units(self.prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)
        self.assertNotEqual(result.reason, '')

    def test_draft_only_recipe(self):
        Recipe.objects.create(
            product=self.prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_DRAFT, revision=1,
        )
        result = get_available_units(self.prod, 'none', 'dine_in')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)

    def test_retired_only_recipe(self):
        Recipe.objects.create(
            product=self.prod, size='none', order_type_scope='all',
            status=Recipe.STATUS_RETIRED, revision=1,
        )
        result = get_available_units(self.prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)

    def test_ambiguous_recipe(self):
        """all + dine_in both approved → AmbiguousRecipeError → 0 + complete=False."""
        ing   = _ingredient()
        rec1  = _approved_recipe(self.prod, size='none', scope='all', revision=1)
        rec2  = _approved_recipe(self.prod, size='none', scope='dine_in', revision=1)
        RecipeItem.objects.create(recipe=rec1, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        RecipeItem.objects.create(recipe=rec2, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        _no_packaging_rule(self.prod, size='none', order_type='dine_in')
        result = get_available_units(self.prod, 'none', 'dine_in')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)

    def test_missing_packaging_rule(self):
        """Approved recipe but no ProductPackagingRule → PackagingRuleMissingError."""
        ing = _ingredient()
        rec = _approved_recipe(self.prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        # No rule created.
        result = get_available_units(self.prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)

    def test_inactive_ingredient_item(self):
        """Inactive InventoryItem in recipe → InactiveItemError → 0 + complete=False."""
        ing = _ingredient(is_active=False)
        rec = _approved_recipe(self.prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        _no_packaging_rule(self.prod, size='none', order_type='takeout')
        result = get_available_units(self.prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)

    def test_wrong_item_type_in_recipe(self):
        """Recipe item with item_type='packaging' → InactiveItemError."""
        wrong = _inventory_item(item_type='packaging', base_unit='pcs',
                                is_active=True)
        rec = _approved_recipe(self.prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=wrong,
                                  quantity=Decimal('1.000'))
        _no_packaging_rule(self.prod, size='none', order_type='takeout')
        result = get_available_units(self.prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)

    def test_inactive_packaging_item(self):
        """Inactive packaging InventoryItem → InactiveItemError → 0 + complete=False."""
        ing     = _ingredient()
        rec     = _approved_recipe(self.prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        profile = _approved_profile()
        pkg_inactive = _packaging_inv_item(is_active=False)
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=pkg_inactive,
            quantity=Decimal('1.000'),
        )
        _profile_rule(self.prod, profile, size='none', order_type='takeout')
        result = get_available_units(self.prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)

    def test_draft_packaging_profile_on_rule(self):
        """Draft packaging profile on a rule → InactiveItemError → 0 + complete=False."""
        ing     = _ingredient()
        rec     = _approved_recipe(self.prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        # Create a draft profile and bypass the XOR check (DB allows null=False profile
        # with no_packaging=False as long as profile FK is set — status is unchecked by DB).
        draft_profile = PackagingProfile.objects.create(
            name=f'Draft_{_uid()}', status=PackagingProfile.STATUS_DRAFT,
            revision=1, approved_at=None,
        )
        ProductPackagingRule.objects.create(
            product=self.prod, size='none', order_type='takeout',
            profile=draft_profile, no_packaging=False,
        )
        result = get_available_units(self.prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)


# ═════════════════════════════════════════════════════════════════════════════
# 27–29. AvailabilityResult structure
# ═════════════════════════════════════════════════════════════════════════════

class AvailabilityResultStructureTests(TestCase):

    def test_result_is_namedtuple(self):
        prod = _legacy_product(stock=1)
        result = get_available_units(prod, 'none', 'takeout')
        self.assertIsInstance(result, AvailabilityResult)
        # NamedTuple — can access by index and by name.
        self.assertEqual(result[0], result.units)
        self.assertEqual(result[1], result.configuration_complete)
        self.assertEqual(result[2], result.reason)

    def test_reason_non_empty_when_configuration_incomplete(self):
        prod = _ingredient_product()
        # No recipe → configuration_complete=False.
        result = get_available_units(prod, 'none', 'takeout')
        self.assertFalse(result.configuration_complete)
        self.assertIsInstance(result.reason, str)
        self.assertTrue(len(result.reason) > 0,
            'reason must be a non-empty string when configuration_complete=False')

    def test_reason_empty_string_when_configuration_complete(self):
        prod, ing, rec, rule = _complete_setup()
        result = get_available_units(prod, 'none', 'takeout')
        self.assertTrue(result.configuration_complete)
        self.assertEqual(result.reason, '')


# ═════════════════════════════════════════════════════════════════════════════
# 30–32. Read-only guarantee
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsReadOnlyTests(TestCase):

    def test_no_inventory_item_quantities_changed(self):
        """get_available_units must not modify any InventoryItem quantity."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('500.000'),
            ing_reserved=Decimal('50.000'),
        )
        on_hand_before  = ing.on_hand_quantity
        reserved_before = ing.reserved_quantity
        get_available_units(prod, 'none', 'takeout')
        ing.refresh_from_db()
        self.assertEqual(ing.on_hand_quantity, on_hand_before)
        self.assertEqual(ing.reserved_quantity, reserved_before)

    def test_no_movement_log_rows_created(self):
        """get_available_units must not write any IngredientMovementLog rows."""
        prod, ing, rec, rule = _complete_setup()
        count_before = IngredientMovementLog.objects.count()
        get_available_units(prod, 'none', 'takeout')
        self.assertEqual(IngredientMovementLog.objects.count(), count_before)

    def test_no_mutation_on_error_path(self):
        """Even when configuration is incomplete, quantities must be unchanged."""
        prod = _ingredient_product()
        # Create an unrelated ingredient to ensure no side effects.
        other_ing = _ingredient(on_hand=Decimal('300.000'))
        try:
            get_available_units(prod, 'none', 'takeout')
        except Exception:
            pass
        other_ing.refresh_from_db()
        self.assertEqual(other_ing.on_hand_quantity, Decimal('300.000'))

    def test_no_mutation_with_order_type_none(self):
        """order_type=None path (two resolve_plan calls) still leaves DB unchanged."""
        prod, ing, rec, rule = _complete_setup(order_type='dine_in')
        on_hand_before = ing.on_hand_quantity
        count_before   = IngredientMovementLog.objects.count()
        get_available_units(prod, 'none', None)
        ing.refresh_from_db()
        self.assertEqual(ing.on_hand_quantity, on_hand_before)
        self.assertEqual(IngredientMovementLog.objects.count(), count_before)


# ═════════════════════════════════════════════════════════════════════════════
# 33. Decimal precision
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsDecimalTests(TestCase):

    def test_exact_floor_with_fractional_availability(self):
        """
        Rev 3 §E.3b: floor(available / qty_per_unit).
        99.999g / 18.000g/unit = 5.5554... → floor = 5
        """
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('99.999'),
            qty_per_unit=Decimal('18.000'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        expected = math.floor(Decimal('99.999') / Decimal('18.000'))
        self.assertEqual(result.units, expected)
        self.assertEqual(result.units, 5)

    def test_no_float_contamination(self):
        """Internal calculation must not use floats."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('1000.001'),
            qty_per_unit=Decimal('333.334'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        # 1000.001 / 333.334 = 2.999... → floor = 2
        self.assertEqual(result.units, 2)

    def test_large_quantities(self):
        """No overflow or precision loss with large base-unit quantities."""
        prod, ing, rec, rule = _complete_setup(
            ing_on_hand=Decimal('99999999.999'),
            qty_per_unit=Decimal('0.001'),
        )
        result = get_available_units(prod, 'none', 'takeout')
        # 99_999_999.999 / 0.001 = 99_999_999_999 (floor)
        self.assertEqual(result.units, 99_999_999_999)


# ═════════════════════════════════════════════════════════════════════════════
# Extra: dine-in vs takeout independent configurations
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsDineInTakeoutTests(TestCase):
    """Dine-in and takeout packaging can constrain availability independently."""

    def test_dine_in_and_takeout_return_different_values(self):
        """
        If dine-in has no packaging (more available) and takeout has a
        limited packaging supply, the two calls return different values.
        """
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('500.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))

        # Dine-in: no packaging → floor(500/10) = 50
        _no_packaging_rule(prod, size='none', order_type='dine_in')

        # Takeout: packaging with only 8 cups → floor(8/1) = 8 (bottleneck)
        cup     = _packaging_inv_item(on_hand=Decimal('8.000'))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        _profile_rule(prod, profile, size='none', order_type='takeout')

        dine_in_result = get_available_units(prod, 'none', 'dine_in')
        takeout_result = get_available_units(prod, 'none', 'takeout')

        self.assertEqual(dine_in_result.units, 50)
        self.assertEqual(takeout_result.units, 8)

    def test_different_sizes_independent(self):
        """Separate size configurations resolve independently."""
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('600.000'))
        # size='none': 600 / 20 = 30
        rec_none = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec_none, inventory_item=ing,
                                  quantity=Decimal('20.000'))
        _no_packaging_rule(prod, size='none', order_type='takeout')
        # size='small': 600 / 15 = 40
        rec_small = _approved_recipe(prod, size='small', scope='all')
        RecipeItem.objects.create(recipe=rec_small, inventory_item=ing,
                                  quantity=Decimal('15.000'))
        _no_packaging_rule(prod, size='small', order_type='takeout')

        result_none  = get_available_units(prod, 'none',  'takeout')
        result_small = get_available_units(prod, 'small', 'takeout')

        self.assertEqual(result_none.units, 30)
        self.assertEqual(result_small.units, 40)

    def test_no_configuration_for_size_returns_zero_incomplete(self):
        """A size with no recipe returns 0 with configuration_complete=False."""
        prod = _ingredient_product()
        # Only configure size='none', not size='medium'.
        ing = _ingredient()
        rec = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('10.000'))
        _no_packaging_rule(prod, size='none', order_type='takeout')
        result = get_available_units(prod, 'medium', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)


# ═════════════════════════════════════════════════════════════════════════════
# Audit regression — four scenarios from Phase 4B-4 audit
# ═════════════════════════════════════════════════════════════════════════════

class GetAvailableUnitsOrderTypeNoneAuditTests(TestCase):
    """
    Regression tests for the corrected order_type=None behavior.

    Rev 3 §E.3b authoritative rules:
      1. "use the most restrictive applicable order type"
      2. "If no complete approved plan exists it returns 0 with a reason,
          so the product is not sellable rather than silently under-deducted."

    Both dine_in and takeout are applicable at the cart stage (order type
    not yet known). An incomplete plan for EITHER order type means the
    product cannot be safely sold, so the result must be 0 with
    configuration_complete=False.

    Covers:
      Scenario A — dine_in complete (12 units), takeout incomplete → 0
      Scenario B — dine_in incomplete, takeout complete (8 units) → 0
      Scenario C — both complete: dine_in=12, takeout=8 → 8 (minimum)
      Scenario D — both incomplete → 0

    Also verifies that explicit order_type='dine_in' and order_type='takeout'
    still return the per-channel result unaffected by the audit fix.
    """

    # ── Shared fixture builder ────────────────────────────────────────────────

    def _make_product_with_recipe(self):
        """Ingredient-tracked product with an 'all'-scope recipe. No rules yet."""
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('1000.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('1000.000') / 12)  # ~83.333g
        return prod, ing, rec

    def _add_dine_in_no_packaging(self, prod):
        """Add a complete, no-packaging dine_in rule."""
        return _no_packaging_rule(prod, size='none', order_type='dine_in')

    def _add_takeout_with_packaging(self, prod, cups_available):
        """Add a complete takeout rule backed by a profile with limited cups."""
        cup     = _packaging_inv_item(on_hand=Decimal(str(cups_available)))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        return _profile_rule(prod, profile, size='none', order_type='takeout')

    # ── Scenario A: dine_in=12, takeout=incomplete ────────────────────────────

    def test_scenario_a_order_type_none_returns_zero(self):
        """
        Scenario A: dine_in complete (≈12 units), takeout has no packaging rule.
        order_type=None must return 0, configuration_complete=False.
        Rev 3: "not sellable rather than silently under-deducted."
        """
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('1200.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('100.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        # No takeout rule at all.
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 0,
            'Scenario A: takeout is incomplete; product must not be shown as '
            'available at the cart stage')
        self.assertFalse(result.configuration_complete)
        self.assertNotEqual(result.reason, '')

    def test_scenario_a_explicit_dine_in_still_returns_twelve(self):
        """
        Explicit order_type='dine_in' must still return the dine_in result
        (12 units) even when takeout has no plan.
        """
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('1200.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('100.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        result = get_available_units(prod, 'none', 'dine_in')
        self.assertEqual(result.units, 12)
        self.assertTrue(result.configuration_complete)

    def test_scenario_a_explicit_takeout_returns_zero_incomplete(self):
        """
        Explicit order_type='takeout' returns 0 + incomplete when there is
        no takeout rule (unchanged by the audit fix).
        """
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('1200.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('100.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        # No takeout rule.
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)

    # ── Scenario B: dine_in=incomplete, takeout=8 ────────────────────────────

    def test_scenario_b_order_type_none_returns_zero(self):
        """
        Scenario B: dine_in has no packaging rule, takeout complete (8 units).
        order_type=None must return 0, configuration_complete=False.
        """
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('800.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('100.000'))
        # Takeout: 8 cups available → 8 units.
        cup     = _packaging_inv_item(on_hand=Decimal('8.000'))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        _profile_rule(prod, profile, size='none', order_type='takeout')
        # No dine_in rule at all.
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 0,
            'Scenario B: dine_in is incomplete; product must not be shown as '
            'available at the cart stage')
        self.assertFalse(result.configuration_complete)

    def test_scenario_b_explicit_takeout_returns_eight(self):
        """Explicit order_type='takeout' returns 8 regardless."""
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('800.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('100.000'))
        cup     = _packaging_inv_item(on_hand=Decimal('8.000'))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        _profile_rule(prod, profile, size='none', order_type='takeout')
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 8)
        self.assertTrue(result.configuration_complete)

    # ── Scenario C: both complete, dine_in=12, takeout=8 ─────────────────────

    def test_scenario_c_order_type_none_returns_minimum(self):
        """
        Scenario C: dine_in=12, takeout=8 (both complete).
        order_type=None must return 8 (the minimum — most restrictive).
        configuration_complete=True because both plans are complete.
        """
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('1200.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('100.000'))
        # Dine-in: no packaging → floor(1200/100) = 12
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        # Takeout: 8 cups → min(12, 8) = 8
        cup     = _packaging_inv_item(on_hand=Decimal('8.000'))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        _profile_rule(prod, profile, size='none', order_type='takeout')
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 8,
            'Scenario C: both complete; must return the minimum (8 not 12)')
        self.assertTrue(result.configuration_complete,
            'Both plans complete → configuration_complete must be True')

    def test_scenario_c_explicit_dine_in_returns_twelve(self):
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('1200.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('100.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        cup     = _packaging_inv_item(on_hand=Decimal('8.000'))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        _profile_rule(prod, profile, size='none', order_type='takeout')
        result = get_available_units(prod, 'none', 'dine_in')
        self.assertEqual(result.units, 12)
        self.assertTrue(result.configuration_complete)

    def test_scenario_c_explicit_takeout_returns_eight(self):
        prod = _ingredient_product()
        ing  = _ingredient(on_hand=Decimal('1200.000'))
        rec  = _approved_recipe(prod, size='none', scope='all')
        RecipeItem.objects.create(recipe=rec, inventory_item=ing,
                                  quantity=Decimal('100.000'))
        _no_packaging_rule(prod, size='none', order_type='dine_in')
        cup     = _packaging_inv_item(on_hand=Decimal('8.000'))
        profile = _approved_profile()
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=cup, quantity=Decimal('1.000'),
        )
        _profile_rule(prod, profile, size='none', order_type='takeout')
        result = get_available_units(prod, 'none', 'takeout')
        self.assertEqual(result.units, 8)
        self.assertTrue(result.configuration_complete)

    # ── Scenario D: both incomplete ────────────────────────────────────────────

    def test_scenario_d_both_incomplete_returns_zero(self):
        """
        Scenario D: both dine_in and takeout have no plans.
        order_type=None must return 0, configuration_complete=False.
        This was already correct before the audit; this test confirms it
        remains correct after the fix.
        """
        prod = _ingredient_product()
        result = get_available_units(prod, 'none', None)
        self.assertEqual(result.units, 0)
        self.assertFalse(result.configuration_complete)
        self.assertNotEqual(result.reason, '')
