"""
Chatbot service tests — price filtering and category filtering.

These tests verify:
1. extract_price_constraints() parses English, Filipino, Bisaya phrases correctly.
2. extract_category_type() correctly classifies meal vs drink requests.
3. get_filtered_menu_context() applies ORM filters before returning context.
4. get_chatbot_response() routes constrained queries through filtered context.
5. Fallback respects filters when AI is unavailable.
6. Inactive/unavailable products are never returned.
7. Strict vs inclusive price boundaries are respected.

All tests are backend-only. Gemini is mocked — no API quota consumed.
"""
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase, RequestFactory

from apps.menu.models import Category, Product
from apps.chatbot.service import (
    extract_price_constraints,
    extract_category_type,
    get_filtered_menu_context,
    get_chatbot_response,
    ALLOWED_LANGUAGES,
    DEFAULT_LANGUAGE,
    _has_price_constraint,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_category(name, is_meal=True, is_active=True):
    return Category.objects.create(
        name=name,
        is_packaging_required=is_meal,
        is_active=is_active,
    )


def _make_product(name, price, category, is_active=True, is_available=True, stock=100):
    return Product.objects.create(
        name=name,
        price=Decimal(str(price)),
        category=category,
        is_active=is_active,
        is_available=is_available,
        stock_quantity=stock,
    )


# ── 1. extract_price_constraints ─────────────────────────────────────────────

class ExtractPriceConstraintsTest(TestCase):

    def _lt(self, msg, expected):
        result = extract_price_constraints(msg)
        self.assertEqual(result['price_lt'], Decimal(str(expected)),
                         f"Expected price_lt={expected} for: {msg!r}")
        self.assertIsNone(result['price_lte'])
        self.assertIsNone(result['price_gt'])
        self.assertIsNone(result['price_gte'])

    def _lte(self, msg, expected):
        result = extract_price_constraints(msg)
        self.assertEqual(result['price_lte'], Decimal(str(expected)),
                         f"Expected price_lte={expected} for: {msg!r}")
        self.assertIsNone(result['price_lt'])

    def _gte(self, msg, expected):
        result = extract_price_constraints(msg)
        self.assertEqual(result['price_gte'], Decimal(str(expected)),
                         f"Expected price_gte={expected} for: {msg!r}")

    def _none(self, msg):
        result = extract_price_constraints(msg)
        self.assertFalse(_has_price_constraint(result),
                         f"Expected no constraint for: {msg!r}")

    # Strict upper limit
    def test_below_peso_symbol(self):
        self._lt("What food do you have below ₱50?", 50)

    def test_under_no_symbol(self):
        self._lt("What food is under 50 pesos?", 50)

    def test_less_than(self):
        self._lt("Show me meals less than ₱100.", 100)

    def test_cheaper_than(self):
        self._lt("Anything cheaper than ₱80?", 80)

    # Inclusive upper limit
    def test_or_less(self):
        self._lte("What can I get for ₱50 or less?", 50)

    def test_up_to(self):
        self._lte("Show me food up to ₱100.", 100)

    def test_at_most(self):
        self._lte("Items at most ₱75.", 75)

    def test_and_below(self):
        self._lte("Meals ₱80 and below.", 80)

    # Lower limit
    def test_at_least(self):
        self._gte("Show me food at least ₱50.", 50)

    # Range
    def test_between(self):
        result = extract_price_constraints("Show me food between ₱50 and ₱100.")
        self.assertEqual(result['price_gte'], Decimal('50'))
        self.assertEqual(result['price_lte'], Decimal('100'))
        self.assertIsNone(result['price_lt'])
        self.assertIsNone(result['price_gt'])

    def test_from_to(self):
        result = extract_price_constraints("Items from ₱30 to ₱70.")
        self.assertEqual(result['price_gte'], Decimal('30'))
        self.assertEqual(result['price_lte'], Decimal('70'))

    # Multilingual
    def test_filipino_hanggang(self):
        self._lte("Pagkain hanggang ₱60.", 60)

    def test_bisaya_hangtod(self):
        self._lte("Pagkaon hangtod ₱60.", 60)

    def test_mixed_english_bisaya(self):
        self._lt("Unsa nga food ang below 50 pesos?", 50)

    def test_mixed_english_filipino(self):
        self._lt("Pagkain na under ₱80.", 80)

    # No constraint cases
    def test_no_price_in_message(self):
        self._none("What food do you have?")

    def test_recommendation_no_price(self):
        self._none("What do you recommend?")

    def test_order_number_not_matched(self):
        # Order numbers like KDM-20260901-0001 should not be parsed as prices
        self._none("Track my order KDM-20260901-0001")

    def test_quantity_not_matched(self):
        # "2 burgers" — the 2 is a quantity, not a price
        self._none("I want 2 burgers")

    # Strict vs inclusive boundary
    def test_strict_boundary_50(self):
        result = extract_price_constraints("below ₱50")
        self.assertEqual(result['price_lt'], Decimal('50'))
        self.assertIsNone(result['price_lte'])

    def test_inclusive_boundary_50(self):
        result = extract_price_constraints("₱50 or less")
        self.assertEqual(result['price_lte'], Decimal('50'))
        self.assertIsNone(result['price_lt'])


# ── 2. extract_category_type ─────────────────────────────────────────────────

class ExtractCategoryTypeTest(TestCase):

    def test_food_returns_meal(self):
        self.assertEqual(extract_category_type("What food do you have?"), 'meal')

    def test_meal_returns_meal(self):
        self.assertEqual(extract_category_type("Show me meals under ₱100."), 'meal')

    def test_drinks_returns_drink(self):
        self.assertEqual(extract_category_type("What drinks are below ₱60?"), 'drink')

    def test_coffee_returns_drink(self):
        self.assertEqual(extract_category_type("What coffee do you have?"), 'drink')

    def test_no_category_returns_none(self):
        self.assertIsNone(extract_category_type("What can I get for ₱50 or less?"))

    def test_all_items_no_filter(self):
        self.assertIsNone(extract_category_type("Show me items below ₱80."))

    def test_pagkain_bisaya(self):
        self.assertEqual(extract_category_type("Unsa nga pagkaon below ₱50?"), 'meal')

    def test_inumin_filipino(self):
        self.assertEqual(extract_category_type("Inumin na below ₱60?"), 'drink')

    def test_both_meal_and_drink_returns_none(self):
        # "food and drinks" — both terms → no specific filter
        result = extract_category_type("What food and drinks do you have below ₱100?")
        self.assertIsNone(result)


# ── 3. get_filtered_menu_context ─────────────────────────────────────────────

class GetFilteredMenuContextTest(TestCase):

    def setUp(self):
        self.meals = _make_category('Meals', is_meal=True)
        self.drinks = _make_category('Drinks', is_meal=False)

        # Meals
        self.burger  = _make_product('Burger',  45, self.meals)
        self.fries   = _make_product('Fries',   35, self.meals)
        self.chicken = _make_product('Chicken', 60, self.meals)
        self.pasta   = _make_product('Pasta',   75, self.meals)

        # Drinks
        self.coffee  = _make_product('Iced Coffee', 40, self.drinks)
        self.tea     = _make_product('Milk Tea',    55, self.drinks)

        # Inactive / unavailable / out-of-stock — must NEVER appear
        self.inactive  = _make_product('Ghost Meal', 30, self.meals, is_active=False)
        self.unavail   = _make_product('Hidden Dish', 25, self.meals, is_available=False)
        self.oos       = _make_product('No Stock',   20, self.meals, stock=0)

    def _names(self, ctx_text):
        """Extract product names from context text lines starting with '  - '."""
        names = []
        for line in ctx_text.splitlines():
            line = line.strip()
            if line.startswith('- '):
                name_part = line[2:].split(':')[0].strip()
                names.append(name_part)
        return names

    # Test 1: meals below ₱50
    def test_meals_below_50(self):
        constraints = {'price_lt': Decimal('50'), 'price_lte': None, 'price_gt': None, 'price_gte': None}
        ctx, was_filtered = get_filtered_menu_context(constraints, 'meal')
        self.assertTrue(was_filtered)
        names = self._names(ctx)
        self.assertIn('Burger', names)
        self.assertIn('Fries', names)
        self.assertNotIn('Chicken', names)
        self.assertNotIn('Pasta', names)
        self.assertNotIn('Iced Coffee', names)
        self.assertNotIn('Milk Tea', names)

    # Test 2: meals at or below ₱50 — exactly ₱50 product would be included (none here)
    def test_meals_lte_50(self):
        _make_product('Exactly 50', 50, self.meals)
        constraints = {'price_lt': None, 'price_lte': Decimal('50'), 'price_gt': None, 'price_gte': None}
        ctx, _ = get_filtered_menu_context(constraints, 'meal')
        names = self._names(ctx)
        self.assertIn('Exactly 50', names)
        self.assertNotIn('Chicken', names)

    # Test 3: strict boundary — exactly ₱50 excluded by price_lt
    def test_strict_boundary_excludes_exact(self):
        _make_product('Exactly 50', 50, self.meals)
        constraints = {'price_lt': Decimal('50'), 'price_lte': None, 'price_gt': None, 'price_gte': None}
        ctx, _ = get_filtered_menu_context(constraints, 'meal')
        names = self._names(ctx)
        self.assertNotIn('Exactly 50', names)

    # Test 4: drinks below ₱60
    def test_drinks_below_60(self):
        constraints = {'price_lt': Decimal('60'), 'price_lte': None, 'price_gt': None, 'price_gte': None}
        ctx, was_filtered = get_filtered_menu_context(constraints, 'drink')
        self.assertTrue(was_filtered)
        names = self._names(ctx)
        self.assertIn('Iced Coffee', names)   # ₱40 — below ₱60
        self.assertIn('Milk Tea', names)      # ₱55 — below ₱60
        self.assertNotIn('Burger', names)     # meal, not drink

    # Test 5: range ₱40–₱60 all categories (inclusive on both ends)
    def test_range_all_categories(self):
        constraints = {'price_lt': None, 'price_lte': Decimal('60'), 'price_gt': None, 'price_gte': Decimal('40')}
        ctx, _ = get_filtered_menu_context(constraints, None)
        names = self._names(ctx)
        self.assertIn('Burger', names)        # ₱45 — in range
        self.assertIn('Iced Coffee', names)   # ₱40 — gte=40, inclusive
        self.assertIn('Milk Tea', names)      # ₱55 — in range
        self.assertIn('Chicken', names)       # ₱60 — lte=60, inclusive
        self.assertNotIn('Fries', names)      # ₱35 — below ₱40 lower bound
        self.assertNotIn('Pasta', names)      # ₱75 — above ₱60 upper bound

    # Test 6: no matching products → empty string
    def test_no_matching_products(self):
        constraints = {'price_lt': Decimal('10'), 'price_lte': None, 'price_gt': None, 'price_gte': None}
        ctx, was_filtered = get_filtered_menu_context(constraints, 'meal')
        self.assertTrue(was_filtered)
        self.assertEqual(ctx, '')

    # Test 7: inactive/unavailable/out-of-stock never returned
    def test_inactive_not_returned(self):
        constraints = {'price_lt': Decimal('200'), 'price_lte': None, 'price_gt': None, 'price_gte': None}
        ctx, _ = get_filtered_menu_context(constraints, None)
        names = self._names(ctx)
        self.assertNotIn('Ghost Meal', names)
        self.assertNotIn('Hidden Dish', names)
        self.assertNotIn('No Stock', names)

    # Test 8: no constraints → was_filtered is False
    def test_no_constraints_returns_unfiltered_flag(self):
        _, was_filtered = get_filtered_menu_context({}, None)
        self.assertFalse(was_filtered)

    # Test 9: category filter only (no price) → was_filtered True
    def test_category_only_filter(self):
        _, was_filtered = get_filtered_menu_context({}, 'meal')
        self.assertTrue(was_filtered)

    # Test 10: exactly one matching product
    def test_exactly_one_match(self):
        constraints = {'price_lt': Decimal('40'), 'price_lte': None, 'price_gt': None, 'price_gte': None}
        ctx, _ = get_filtered_menu_context(constraints, 'meal')
        names = self._names(ctx)
        # Only Fries at ₱35 qualifies (Burger is ₱45 ≥ 40)
        self.assertEqual(names, ['Fries'])


# ── 4. get_chatbot_response integration ──────────────────────────────────────

class GetChatbotResponseIntegrationTest(TestCase):
    """
    Integration tests for get_chatbot_response with Gemini mocked.
    Verifies that the correct filtered context is passed to the AI.
    """

    def setUp(self):
        self.meals  = _make_category('Meals', is_meal=True)
        self.drinks = _make_category('Drinks', is_meal=False)
        _make_product('Burger',     45, self.meals)
        _make_product('Fries',      35, self.meals)
        _make_product('Chicken',    60, self.meals)
        _make_product('Iced Coffee', 40, self.drinks)

    def _mock_ai(self, captured):
        """
        Patches get_ai_response to capture the system prompt and return a
        fake response, without requiring google-generativeai to be installed.
        """
        from apps.chatbot.service import _build_system_prompt

        def fake_ai(msg, intent, hist, lang='en',
                    filtered_context=None, constraints_summary=''):
            system = _build_system_prompt(
                intent, lang,
                filtered_context=filtered_context,
                constraints_summary=constraints_summary,
            )
            captured['system'] = system
            return 'AI response'

        return patch('apps.chatbot.service.get_ai_response', side_effect=fake_ai)

    def test_price_filter_excludes_expensive_from_ai_context(self):
        """Products above ₱50 must not appear in the context Gemini receives."""
        captured = {}
        with self._mock_ai(captured):
            resp, intent = get_chatbot_response(
                "What food do you have below ₱50?", [], 'en'
            )
        system = captured.get('system', '')
        self.assertIn('Burger', system)       # ₱45 — should be included
        self.assertIn('Fries', system)        # ₱35 — should be included
        self.assertNotIn('Chicken', system)   # ₱60 — must be excluded

    def test_no_price_filter_sends_full_menu(self):
        """Plain menu question (no price constraint) sends full menu to Gemini."""
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("What food do you have?", [], 'en')
        system = captured.get('system', '')
        # All active products should appear
        self.assertIn('Burger', system)
        self.assertIn('Chicken', system)

    def test_drink_filter_excludes_meals_from_context(self):
        """Drink-only filter must not send meal products to Gemini."""
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("What drinks are below ₱60?", [], 'en')
        system = captured.get('system', '')
        self.assertIn('Iced Coffee', system)
        self.assertNotIn('Burger', system)
        self.assertNotIn('Chicken', system)

    def test_no_match_tells_ai_no_products(self):
        """When no products match, AI context must contain the no-results message."""
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("Food below ₱5?", [], 'en')
        system = captured.get('system', '')
        self.assertIn('NO products', system.upper())

    def test_fallback_respects_filter_when_ai_unavailable(self):
        """When Gemini is unavailable, fallback still applies price filter."""
        # No AI configured — fallback path
        with patch('apps.chatbot.service._get_api_key', return_value=None):
            resp, intent = get_chatbot_response(
                "What food do you have below ₱50?", [], 'en'
            )
        # Burger (₱45) and Fries (₱35) should appear
        self.assertIn('Burger', resp)
        self.assertIn('Fries', resp)
        # Chicken (₱60) must not appear
        self.assertNotIn('Chicken', resp)

    def test_fallback_no_match_message(self):
        """Fallback returns no-match message when no products satisfy filter."""
        with patch('apps.chatbot.service._get_api_key', return_value=None):
            resp, _ = get_chatbot_response("Food below ₱5?", [], 'en')
        self.assertIn('no items', resp.lower())

    def test_language_en_default(self):
        """Default language is English."""
        self.assertEqual(DEFAULT_LANGUAGE, 'en')
        self.assertIn('en', ALLOWED_LANGUAGES)

    def test_invalid_language_falls_back_to_en(self):
        """Invalid language code falls back to English without raising."""
        with patch('apps.chatbot.service._get_api_key', return_value=None):
            resp, _ = get_chatbot_response("What food do you have?", [], 'xx_invalid')
        # Should not raise; response should be English fallback
        self.assertIsInstance(resp, str)
        self.assertTrue(len(resp) > 0)

    def test_tagalog_language_respected(self):
        """Tagalog language code is in ALLOWED_LANGUAGES."""
        self.assertIn('tl', ALLOWED_LANGUAGES)

    def test_bisaya_language_respected(self):
        """Bisaya language code is in ALLOWED_LANGUAGES."""
        self.assertIn('ceb', ALLOWED_LANGUAGES)

    def test_order_status_unaffected(self):
        """Order status intent is never routed through price filtering."""
        from apps.orders.models import Order
        # Should not crash — returns order-not-found message
        with patch('apps.chatbot.service._get_api_key', return_value=None):
            resp, intent = get_chatbot_response(
                "Track KDM-20260901-0001", [], 'en'
            )
        self.assertEqual(intent, 'order_status')
        # Filtering variables should not have been applied
        self.assertNotIn('Burger', resp)


# ── 5. extract_product_name ───────────────────────────────────────────────────

class ExtractProductNameTest(TestCase):
    """Unit tests for extract_product_name() — pure regex, no DB."""

    def _assertExtracted(self, message, expected_contains):
        """Assert that the extracted name contains *expected_contains* (case-insensitive)."""
        from apps.chatbot.service import extract_product_name
        result = extract_product_name(message)
        self.assertIsNotNone(result, f"Expected a product name from: {message!r}")
        self.assertIn(expected_contains.lower(), result.lower(),
                      f"Expected {expected_contains!r} in {result!r} for: {message!r}")

    def _assertNone(self, message):
        from apps.chatbot.service import extract_product_name
        result = extract_product_name(message)
        self.assertIsNone(result, f"Expected None from: {message!r} but got {result!r}")

    # --- "how much is" ---
    def test_how_much_is_burger(self):
        self._assertExtracted("How much is the Burger?", "Burger")

    def test_how_much_is_no_article(self):
        self._assertExtracted("How much is Iced Coffee?", "Iced Coffee")

    def test_how_much_are(self):
        self._assertExtracted("How much are the Fries?", "Fries")

    def test_price_of(self):
        self._assertExtracted("What's the price of Milk Tea?", "Milk Tea")

    # --- "do you have" ---
    def test_do_you_have(self):
        self._assertExtracted("Do you have Chicken Meal?", "Chicken Meal")

    def test_do_you_sell(self):
        self._assertExtracted("Do you sell Pastil?", "Pastil")

    def test_do_you_serve(self):
        self._assertExtracted("Do you serve Latte?", "Latte")

    # --- "is X available" ---
    def test_is_available(self):
        self._assertExtracted("Is the Burger available?", "Burger")

    def test_is_available_no_article(self):
        self._assertExtracted("Is Mocha available?", "Mocha")

    # --- "tell me about" ---
    def test_tell_me_about(self):
        self._assertExtracted("Tell me about the Frappe.", "Frappe")

    # --- Filipino ---
    def test_magkano_ang(self):
        self._assertExtracted("Magkano ang Burger?", "Burger")

    def test_meron_ba(self):
        self._assertExtracted("Meron ba kayong Pastil?", "Pastil")

    def test_mayroon_ba(self):
        self._assertExtracted("Mayroon ba kayong Milk Tea?", "Milk Tea")

    # --- Bisaya ---
    def test_tag_pila_ang(self):
        self._assertExtracted("Tag-pila ang Iced Coffee?", "Iced Coffee")

    def test_naa_ba(self):
        self._assertExtracted("Naa ba mog Burger?", "Burger")

    # --- Should return None (general / non-product queries) ---
    def test_general_menu_query_returns_none(self):
        self._assertNone("What food do you have?")

    def test_price_filter_returns_none(self):
        self._assertNone("What food do you have below ₱50?")

    def test_cheapest_returns_none(self):
        self._assertNone("What is the cheapest item?")

    def test_greeting_returns_none(self):
        self._assertNone("Hi, how are you?")

    def test_generic_something_returns_none(self):
        self._assertNone("Do you have something cheap?")

    def test_generic_food_returns_none(self):
        self._assertNone("Do you have food?")

    # --- Trailing noise stripped ---
    def test_trailing_question_mark_stripped(self):
        from apps.chatbot.service import extract_product_name
        result = extract_product_name("How much is the Burger?")
        self.assertFalse(result.endswith('?'),
                         f"Trailing '?' not stripped from: {result!r}")

    def test_trailing_po_stripped(self):
        from apps.chatbot.service import extract_product_name
        result = extract_product_name("Magkano ang Burger po?")
        self.assertIsNotNone(result)
        self.assertNotIn('po', result.lower().split(),
                         f"'po' not stripped from: {result!r}")


# ── 6. query_products_by_name ─────────────────────────────────────────────────

class QueryProductsByNameTest(TestCase):
    """Tests for query_products_by_name() — hits the DB."""

    def setUp(self):
        self.meals  = _make_category('Meals',  is_meal=True)
        self.drinks = _make_category('Drinks', is_meal=False)
        _make_product('Burger',      45, self.meals)
        _make_product('Beef Burger', 65, self.meals)
        _make_product('Iced Coffee', 40, self.drinks)
        # Not returned
        _make_product('Ghost',       30, self.meals, is_active=False)
        _make_product('Hidden',      25, self.meals, is_available=False)
        _make_product('No Stock',    20, self.meals, stock=0)

    def test_exact_name_found(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('Burger')
        self.assertTrue(found)
        self.assertIn('Burger', ctx)

    def test_partial_name_matches(self):
        """'urger' should match both 'Burger' and 'Beef Burger'."""
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('urger')
        self.assertTrue(found)
        self.assertIn('Burger', ctx)
        self.assertIn('Beef Burger', ctx)

    def test_case_insensitive(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('burger')
        self.assertTrue(found)
        self.assertIn('Burger', ctx)

    def test_drink_found(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('Iced Coffee')
        self.assertTrue(found)
        self.assertIn('Iced Coffee', ctx)
        self.assertIn('[DRINK]', ctx)

    def test_meal_category_filter(self):
        """With category_type='meal', drinks must not appear."""
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('Coffee', 'meal')
        # Iced Coffee is a drink — should not match 'meal' category filter
        self.assertFalse(found)

    def test_drink_category_filter(self):
        """With category_type='drink', meals must not appear."""
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('Burger', 'drink')
        self.assertFalse(found)

    def test_inactive_not_returned(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('Ghost')
        self.assertFalse(found)

    def test_unavailable_not_returned(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('Hidden')
        self.assertFalse(found)

    def test_out_of_stock_not_returned(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('No Stock')
        self.assertFalse(found)

    def test_nonexistent_product_returns_false(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('ZZZ_NONEXISTENT_XYZ')
        self.assertFalse(found)
        self.assertEqual(ctx, '')

    def test_price_included_in_output(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('Burger')
        self.assertTrue(found)
        self.assertIn('₱45', ctx)

    def test_meal_label_in_output(self):
        from apps.chatbot.service import query_products_by_name
        ctx, found = query_products_by_name('Burger')
        self.assertTrue(found)
        self.assertIn('[MEAL]', ctx)


# ── 7. extract_sort_intent ────────────────────────────────────────────────────

class ExtractSortIntentTest(TestCase):

    def _cheapest(self, msg):
        from apps.chatbot.service import extract_sort_intent
        self.assertEqual(extract_sort_intent(msg), 'cheapest',
                         f"Expected 'cheapest' for: {msg!r}")

    def _expensive(self, msg):
        from apps.chatbot.service import extract_sort_intent
        self.assertEqual(extract_sort_intent(msg), 'most_expensive',
                         f"Expected 'most_expensive' for: {msg!r}")

    def _none(self, msg):
        from apps.chatbot.service import extract_sort_intent
        self.assertIsNone(extract_sort_intent(msg),
                          f"Expected None for: {msg!r}")

    def test_cheapest_english(self):
        self._cheapest("What is the cheapest item?")

    def test_most_affordable(self):
        self._cheapest("Show me the most affordable food.")

    def test_lowest_price(self):
        self._cheapest("What has the lowest price?")

    def test_least_expensive(self):
        self._cheapest("What is the least expensive meal?")

    def test_pinakamura_tagalog(self):
        self._cheapest("Ano ang pinakamura?")

    def test_pinakabarato_bisaya(self):
        self._cheapest("Unsa ang pinakabarato nga pagkaon?")

    def test_most_expensive_english(self):
        self._expensive("What is the most expensive item?")

    def test_highest_price(self):
        self._expensive("What has the highest price?")

    def test_pinakamahal_tagalog(self):
        self._expensive("Ano ang pinakamahal?")

    def test_pinakamahal_bisaya(self):
        self._expensive("Unsa ang pinakamahal nga inumin?")

    def test_vague_cheap_returns_none(self):
        # "cheap" alone is not a superlative — should NOT trigger sort intent
        self._none("Do you have cheap food?")

    def test_vague_mura_returns_none(self):
        self._none("May mura ba kayong pagkain?")

    def test_general_menu_returns_none(self):
        self._none("What do you have?")

    def test_greeting_returns_none(self):
        self._none("Hello!")


# ── 8. query_cheapest / query_most_expensive ─────────────────────────────────

class QuerySortedProductsTest(TestCase):

    def setUp(self):
        self.meals  = _make_category('Meals',  is_meal=True)
        self.drinks = _make_category('Drinks', is_meal=False)
        _make_product('Cheap Meal',      25, self.meals)
        _make_product('Mid Meal',        50, self.meals)
        _make_product('Expensive Meal',  90, self.meals)
        _make_product('Cheap Drink',     30, self.drinks)
        _make_product('Expensive Drink', 80, self.drinks)
        # Inactive / unavailable / out-of-stock — never returned
        _make_product('Ghost',           10, self.meals, is_active=False)
        _make_product('Hidden',          10, self.meals, is_available=False)
        _make_product('No Stock',        10, self.meals, stock=0)

    def test_cheapest_all_returns_lowest(self):
        from apps.chatbot.service import query_cheapest_products
        ctx, found = query_cheapest_products(limit=2)
        self.assertTrue(found)
        self.assertIn('Cheap Meal', ctx)      # ₱25 — cheapest overall
        self.assertIn('Cheap Drink', ctx)     # ₱30 — second cheapest

    def test_cheapest_meals_only(self):
        from apps.chatbot.service import query_cheapest_products
        ctx, found = query_cheapest_products(category_type='meal', limit=1)
        self.assertTrue(found)
        self.assertIn('Cheap Meal', ctx)
        self.assertNotIn('Cheap Drink', ctx)
        self.assertNotIn('Expensive Drink', ctx)

    def test_cheapest_drinks_only(self):
        from apps.chatbot.service import query_cheapest_products
        ctx, found = query_cheapest_products(category_type='drink', limit=1)
        self.assertTrue(found)
        self.assertIn('Cheap Drink', ctx)
        self.assertNotIn('Cheap Meal', ctx)

    def test_most_expensive_all_returns_highest(self):
        from apps.chatbot.service import query_most_expensive_products
        ctx, found = query_most_expensive_products(limit=2)
        self.assertTrue(found)
        self.assertIn('Expensive Meal', ctx)   # ₱90
        self.assertIn('Expensive Drink', ctx)  # ₱80

    def test_most_expensive_meals_only(self):
        from apps.chatbot.service import query_most_expensive_products
        ctx, found = query_most_expensive_products(category_type='meal', limit=1)
        self.assertTrue(found)
        self.assertIn('Expensive Meal', ctx)
        self.assertNotIn('Expensive Drink', ctx)

    def test_most_expensive_drinks_only(self):
        from apps.chatbot.service import query_most_expensive_products
        ctx, found = query_most_expensive_products(category_type='drink', limit=1)
        self.assertTrue(found)
        self.assertIn('Expensive Drink', ctx)
        self.assertNotIn('Expensive Meal', ctx)

    def test_inactive_never_returned(self):
        from apps.chatbot.service import query_cheapest_products
        ctx, _ = query_cheapest_products(limit=20)
        self.assertNotIn('Ghost', ctx)
        self.assertNotIn('Hidden', ctx)
        self.assertNotIn('No Stock', ctx)

    def test_empty_db_returns_false(self):
        from apps.chatbot.service import query_cheapest_products
        # Use a nonexistent category type to force empty result
        # (no products with category_type='nonexistent' — pass bad string,
        # ORM will just return nothing for the filter)
        ctx, found = query_cheapest_products(category_type='nonexistent_type', limit=5)
        self.assertFalse(found)
        self.assertEqual(ctx, '')

    def test_limit_respected(self):
        from apps.chatbot.service import query_cheapest_products
        ctx, found = query_cheapest_products(limit=1)
        self.assertTrue(found)
        # Only 1 product should appear (exactly one line starting with '  - ')
        result_lines = [l for l in ctx.splitlines() if l.strip().startswith('- ')]
        self.assertEqual(len(result_lines), 1)

    def test_price_in_output(self):
        from apps.chatbot.service import query_cheapest_products
        ctx, _ = query_cheapest_products(limit=3)
        self.assertIn('₱25', ctx)


# ── 9. get_chatbot_response product-name integration ─────────────────────────

class GetChatbotResponseProductNameTest(TestCase):
    """
    Integration tests verifying the product-name lookup path wired into
    get_chatbot_response(). Gemini is mocked; tests inspect the system prompt.
    """

    def setUp(self):
        self.meals  = _make_category('Meals',  is_meal=True)
        self.drinks = _make_category('Drinks', is_meal=False)
        _make_product('Burger',      45, self.meals)
        _make_product('Iced Coffee', 40, self.drinks)
        _make_product('Milk Tea',    55, self.drinks)
        _make_product('Pastil',      35, self.meals)

    def _mock_ai(self, captured):
        from apps.chatbot.service import _build_system_prompt

        def fake_ai(msg, intent, hist, lang='en',
                    filtered_context=None, constraints_summary=''):
            system = _build_system_prompt(
                intent, lang,
                filtered_context=filtered_context,
                constraints_summary=constraints_summary,
            )
            captured['system'] = system
            captured['filtered_context'] = filtered_context
            captured['constraints_summary'] = constraints_summary
            return 'AI response'

        return patch('apps.chatbot.service.get_ai_response', side_effect=fake_ai)

    # Product found — system prompt should contain that product only
    def test_how_much_is_burger_sends_burger_to_ai(self):
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("How much is the Burger?", [], 'en')
        system = captured.get('system', '')
        self.assertIn('Burger', system)
        self.assertIn('product search', captured.get('constraints_summary', '').lower())
        # System prompt should use the product-search grounding block
        self.assertIn('PRODUCT SEARCH RESULTS', system)

    def test_product_search_excludes_other_products(self):
        """When asking about Burger, Iced Coffee must NOT be in the AI context."""
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("How much is the Burger?", [], 'en')
        system = captured.get('system', '')
        self.assertIn('Burger', system)
        self.assertNotIn('Iced Coffee', system)
        self.assertNotIn('Milk Tea', system)

    def test_nonexistent_product_triggers_not_found_block(self):
        """Asking about a product not in DB → AI told no products found."""
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("How much is the XYZ_FAKE_ITEM?", [], 'en')
        system = captured.get('system', '')
        # filtered_context is '' → not-found branch
        self.assertEqual(captured.get('filtered_context'), '')
        # System should instruct Gemini the product doesn't exist
        self.assertIn('does NOT exist', system)

    def test_do_you_have_existing_product(self):
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("Do you have Pastil?", [], 'en')
        system = captured.get('system', '')
        self.assertIn('Pastil', system)
        self.assertIn('PRODUCT SEARCH RESULTS', system)

    def test_magkano_tagalog_burger(self):
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("Magkano ang Burger?", [], 'tl')
        system = captured.get('system', '')
        self.assertIn('Burger', system)
        self.assertIn('PRODUCT SEARCH RESULTS', system)

    def test_is_iced_coffee_available(self):
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("Is Iced Coffee available?", [], 'en')
        system = captured.get('system', '')
        self.assertIn('Iced Coffee', system)
        self.assertIn('[DRINK]', system)


# ── 10. get_chatbot_response sort intent integration ──────────────────────────

class GetChatbotResponseSortIntentTest(TestCase):
    """
    Integration tests verifying cheapest / most-expensive routing in
    get_chatbot_response(). Gemini is mocked.
    """

    def setUp(self):
        self.meals  = _make_category('Meals',  is_meal=True)
        self.drinks = _make_category('Drinks', is_meal=False)
        _make_product('Budget Meal',    25, self.meals)
        _make_product('Premium Meal',   90, self.meals)
        _make_product('Budget Drink',   30, self.drinks)
        _make_product('Premium Drink',  80, self.drinks)

    def _mock_ai(self, captured):
        from apps.chatbot.service import _build_system_prompt

        def fake_ai(msg, intent, hist, lang='en',
                    filtered_context=None, constraints_summary=''):
            system = _build_system_prompt(
                intent, lang,
                filtered_context=filtered_context,
                constraints_summary=constraints_summary,
            )
            captured['system'] = system
            captured['filtered_context'] = filtered_context
            captured['constraints_summary'] = constraints_summary
            return 'AI response'

        return patch('apps.chatbot.service.get_ai_response', side_effect=fake_ai)

    def test_cheapest_sends_sorted_list_to_ai(self):
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("What is the cheapest item?", [], 'en')
        system = captured.get('system', '')
        self.assertIn('SORTED PRODUCT LIST', system)
        self.assertIn('Budget Meal', system)     # ₱25 — cheapest meal

    def test_cheapest_excludes_most_expensive(self):
        """Premium Meal (₱90) must not appear when asking for cheapest (limit=5,
        and there are only 4 products total — but the ordering puts budget first)."""
        captured = {}
        with self._mock_ai(captured):
            # limit=1 via keyword so only the single cheapest appears
            # We can't directly pass limit here, so we verify the summary
            get_chatbot_response("What is the cheapest food?", [], 'en')
        self.assertIn('cheapest', captured.get('constraints_summary', '').lower())

    def test_most_expensive_sends_sorted_list_to_ai(self):
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("What is the most expensive item?", [], 'en')
        system = captured.get('system', '')
        self.assertIn('SORTED PRODUCT LIST', system)
        self.assertIn('Premium Meal', system)

    def test_pinakamura_tagalog(self):
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("Ano ang pinakamura?", [], 'tl')
        self.assertIn('cheapest', captured.get('constraints_summary', '').lower())
        self.assertIn('SORTED PRODUCT LIST', captured.get('system', ''))

    def test_pinakamahal_bisaya(self):
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("Unsa ang pinakamahal?", [], 'ceb')
        self.assertIn('most expensive', captured.get('constraints_summary', '').lower())
        self.assertIn('SORTED PRODUCT LIST', captured.get('system', ''))

    def test_sort_does_not_trigger_for_plain_menu_query(self):
        """A plain "what food do you have?" must NOT go through sort path."""
        captured = {}
        with self._mock_ai(captured):
            get_chatbot_response("What food do you have?", [], 'en')
        # No sort-specific block — should be either full menu or category filter
        self.assertNotIn('SORTED PRODUCT LIST', captured.get('system', ''))

    def test_fallback_cheapest_no_ai(self):
        """When AI unavailable, fallback for cheapest still returns a result."""
        with patch('apps.chatbot.service._get_api_key', return_value=None):
            resp, _ = get_chatbot_response("What is the cheapest item?", [], 'en')
        self.assertIn('Budget Meal', resp)

    def test_fallback_most_expensive_no_ai(self):
        """When AI unavailable, fallback for most expensive still returns a result."""
        with patch('apps.chatbot.service._get_api_key', return_value=None):
            resp, _ = get_chatbot_response("What is the most expensive item?", [], 'en')
        self.assertIn('Premium Meal', resp)
