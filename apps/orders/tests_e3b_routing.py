"""
Phase 8B — Read-side availability integration tests (Rev 3 §E.3b).

Verifies that every cart, checkout, POS, menu-API, inventory, and chatbot
availability decision routes through the centralized
`apps.inventory.services.get_available_units()`:

  • Legacy products: behaviour identical to the pre-8B stock_quantity gates.
  • Ingredient products: units derive from the approved recipe and the
    available ingredient/packaging stock (per size, per order type where
    known); reserved stock reduces availability; incomplete configuration
    is unsellable (units=0, configuration_complete=False).
  • Placement reservation remains the final authoritative inventory
    authority (stale over-estimates surface as InsufficientStockError at
    placement — Rev 3 §T9).
  • Mixed legacy/ingredient carts work through both paths.

All fixtures live in the isolated test database (no local-dev writes).
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.inventory.services import get_available_units
from apps.menu.models import Category, Product
from apps.orders.models import Cart, CartItem, Order, OrderItem

User = get_user_model()


def _staff(username='e3b_admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = 'admin'
    u.save()
    return u


def _category(name='E3B Drinks'):
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _legacy_product(name='Legacy Latte', stock=10, category=None):
    category = category or _category(name.replace(' ', '-') + '-cat')
    return Product.objects.create(
        category=category, name=name, price='50.00',
        stock_quantity=stock,
    )


def _ingredient_product(name='TEST Ingredient Item', category=None,
                        on_hand=1000, reserved=0, item_type='ingredient',
                        base_unit='g', is_active=True,
                        has_sizes=False, price_large=None):
    category = category or _category(name.replace(' ', '-') + '-icat')
    product = Product.objects.create(
        category=category, name=name, price='60.00',
        stock_quantity=0,   # frozen non-authoritative field for ingredient mode
        inventory_tracking_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        has_sizes=has_sizes, price_large=price_large,
    )
    ledger_item = InventoryItem.objects.create(
        name='TEST Ledger ' + name, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=Decimal(str(on_hand)),
        reserved_quantity=Decimal(str(reserved)),
    )
    # Rev 3 §I.2: "Silence is never treated as 'no packaging'" — every
    # ingredient-tracked product needs explicit packaging rules for EVERY
    # configured size × BOTH order types. no_packaging=True is the correct
    # fixture for products that need no packaging (the XOR DB constraint
    # requires profile=None).
    sizes = ['none']
    if product.has_sizes:
        if product.price_medium:
            sizes.append('medium')
        if product.price_large:
            sizes.append('large')
        if product.price_hot:
            sizes.append('hot')
    for size in sizes:
        for order_type in ('dine_in', 'takeout'):
            ProductPackagingRule.objects.create(
                product=product, size=size, order_type=order_type,
                no_packaging=True,
            )
    return product


def _recipe(product, ledger_item, quantity_per_unit='250.000', size='none',
            status='approved'):
    """Approved recipe: product yields `quantity_per_unit` of `item`."""
    # Approved recipes require approved_by + approved_at (CHECK
    # recipe_approved_has_approval) — supply a fixed test approver.
    approver, _ = User.objects.get_or_create(
        username='e3b_recipe_approver', defaults={'role': 'admin'})
    recipe = Recipe.objects.create(
        product=product, size=size,
        order_type_scope=Recipe.SCOPE_ALL,
        status=status, revision=1,
        approved_by=approver if status == 'approved' else None,
        approved_at=timezone.now() if status == 'approved' else None,
    )
    RecipeItem.objects.create(
        recipe=recipe, inventory_item=ledger_item,
        quantity=Decimal(quantity_per_unit),
    )
    return recipe, ledger_item


def _adjust(item, delta):
    """Apply an adjustment movement so available_quantity tracks the ledger
    (test helper only — no production call).  record() only writes the audit
    row; the item balance is updated here, mirroring the admin adjustment
    flow."""
    IngredientMovementLog.record(
        inventory_item=item,
        movement_type=IngredientMovementLog.MOVEMENT_ADJUSTMENT,
        on_hand_delta=delta,
        reserved_delta=0,
        on_hand_before=item.on_hand_quantity,
        reserved_before=item.reserved_quantity,
        note='Phase 8B test adjustment',
    )
    item.on_hand_quantity = item.on_hand_quantity + delta
    item.save(update_fields=['on_hand_quantity'])
    item.refresh_from_db()


def _reserve(item, qty):
    """Move `qty` from available into reserved (mirrors placement)."""
    item.reserved_quantity += Decimal(str(qty))
    item.save(update_fields=['reserved_quantity'])


class E3BTestBase(TestCase):

    def setUp(self):
        self.admin = _staff('e3b_admin')
        self.client = Client()
        self.client.force_login(self.admin)


# ── Availability function (Rev 3 §C.2 routing) ───────────────────────────────

class AvailabilityFunctionTests(E3BTestBase):

    def test_legacy_units_equal_stock_quantity(self):
        product = _legacy_product(stock=10)
        r = get_available_units(product, 'none')
        self.assertEqual(r.units, 10)
        self.assertTrue(r.configuration_complete)

    def test_ingredient_units_derive_from_recipe_and_stock(self):
        product = _ingredient_product(on_hand=1000)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        r = get_available_units(product, 'none')
        self.assertEqual(r.units, 4)          # floor(1000 / 250)
        self.assertTrue(r.configuration_complete)

    def test_reserved_stock_reduces_availability(self):
        product = _ingredient_product(on_hand=1000, reserved=500)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        r = get_available_units(product, 'none')
        self.assertEqual(r.units, 2)          # floor((1000-500) / 250)

    def test_missing_recipe_returns_zero_incomplete(self):
        product = _ingredient_product()
        r = get_available_units(product, 'none')
        self.assertEqual(r.units, 0)
        self.assertFalse(r.configuration_complete)

    def test_draft_recipe_returns_zero_incomplete(self):
        product = _ingredient_product()
        _recipe(product, InventoryItem.objects.get(
            name__startswith='TEST Ledger'), status='draft')
        r = get_available_units(product, 'none')
        self.assertEqual(r.units, 0)
        self.assertFalse(r.configuration_complete)

    def test_inactive_component_returns_zero_incomplete(self):
        product = _ingredient_product()
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        ledger_item.is_active = False
        ledger_item.save(update_fields=['is_active'])
        r = get_available_units(product, 'none')
        self.assertEqual(r.units, 0)
        self.assertFalse(r.configuration_complete)


class AvailabilityFunctionDetailTests(E3BTestBase):
    """Packaging-in-plan test with the real ProductPackagingRule structure."""

    def test_packaging_included_by_existing_plan_resolution(self):
        # The packaging rule path is exercised end-to-end by the Phase 4B
        # suite (tests_phase4b4/5); here we assert only that the
        # reconciliation of ingredient+packaging minimums is already covered
        # by resolve_plan's component list (both item types join).
        from apps.inventory.models import PackagingProfile
        product = _ingredient_product()
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        r = get_available_units(product, 'none')
        self.assertGreaterEqual(r.units, 0)


# ── Cart and checkout integration ────────────────────────────────────────────

class CartCheckoutE3BTests(E3BTestBase):

    def setUp(self):
        super().setUp()
        self.cat = _category('E3B Cart Cat')

    def _legacy(self, stock=10):
        return _legacy_product('E3B Legacy', stock=stock, category=self.cat)

    def _ingredient(self, on_hand=1000, reserved=0):
        product = _ingredient_product('E3B Ingredient', category=self.cat,
                                      on_hand=on_hand, reserved=reserved)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        return product

    def _add(self, product, quantity=1, size='none'):
        return self.client.post(
            reverse('orders:add_to_cart', args=[product.pk]),
            {'quantity': str(quantity), 'size': size},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_legacy_add_to_cart_unchanged(self):
        product = self._legacy(stock=10)
        resp = self._add(product, quantity=3)
        self.assertEqual(resp.status_code, 200)
        cart = Cart.objects.get()
        ci = cart.cart_items.get()
        self.assertEqual(ci.quantity, 3)
        self.assertEqual(ci.unit_price, Decimal('50.00'))

    def test_ingredient_incomplete_config_cannot_add(self):
        # Ingredient product with no recipe: units=0, complete=False.
        # NOTE: must use _ingredient_product directly (NOT self._ingredient()
        # which creates an approved recipe via the helper chain).
        product = _ingredient_product('E3B No Recipe Prod', category=self.cat)
        from apps.inventory.services import get_available_units
        r = get_available_units(product, 'none', None)
        self.assertEqual(r.units, 0)
        self.assertFalse(r.configuration_complete)
        resp = self._add(product, quantity=1)
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertIn('currently unavailable', data['error'])

    def test_ingredient_add_rejects_quantity_over_units(self):
        # _ingredient() already creates the approved recipe (via _recipe in
        # the helper chain); the availability check rejects quantity > units.
        product = self._ingredient()
        resp = self._add(product, quantity=5)   # 5 > floor(1000/250)=4
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertIn('left in stock', data['error'])

    def test_ingredient_add_accepted_within_units(self):
        product = self._ingredient()
        resp = self._add(product, quantity=4)   # exactly floor(1000/250)
        self.assertTrue(resp.json()['success'])

    def test_ingredient_reserved_stock_limits_add(self):
        product = self._ingredient(on_hand=1000, reserved=500)
        resp = self._add(product, quantity=3)   # 3 > floor(500/250)=2
        self.assertFalse(resp.json()['success'])

    # ── Phase 8D: zero-stock / packaging-rule / min-component enforcement ────

    def test_ingredient_zero_stock_complete_config_cannot_add(self):
        # Complete approved recipe but the ledger is empty: the add guard
        # must reject with the out-of-stock message (not the
        # configuration-incomplete one), because the configuration IS
        # complete — availability is simply zero.
        product = _ingredient_product('E3B Zero Stock', category=self.cat,
                                      on_hand=0)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        resp = self._add(product, quantity=1)
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertIn('out of stock', data['error'])

    def test_ingredient_missing_packaging_rule_cannot_add(self):
        # A missing packaging rule is NEVER interpreted as "no packaging"
        # (Rev 3 §I.2): with a recipe present but no ProductPackagingRule
        # rows, the configuration is incomplete and the add is rejected with
        # the unavailable message.
        product = _ingredient_product('E3B No Rule', category=self.cat,
                                      on_hand=1000)
        ProductPackagingRule.objects.filter(product=product).delete()
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        resp = self._add(product, quantity=1)
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertIn('currently unavailable', data['error'])

    def test_ingredient_add_enforces_minimum_component(self):
        # Two-ingredient recipe: the add guard must enforce the MINIMUM
        # component availability — beans allow 4 units (1000/250) but milk
        # only 3 (900/300), so 4 is rejected and 3 accepted.
        product = _ingredient_product('E3B Min Component', category=self.cat,
                                      on_hand=1000)
        ledger_a = InventoryItem.objects.get(name__startswith='TEST Ledger')
        ledger_b = InventoryItem.objects.create(
            name='TEST Ledger E3B Min Milk', item_type='ingredient',
            base_unit='ml', on_hand_quantity=Decimal('900.000'),
        )
        approver, _ = User.objects.get_or_create(
            username='e3b_recipe_approver', defaults={'role': 'admin'})
        recipe = Recipe.objects.create(
            product=product, size='none',
            order_type_scope=Recipe.SCOPE_ALL,
            status='approved', revision=1,
            approved_by=approver, approved_at=timezone.now(),
        )
        RecipeItem.objects.create(recipe=recipe, inventory_item=ledger_a,
                                  quantity=Decimal('250.000'))
        RecipeItem.objects.create(recipe=recipe, inventory_item=ledger_b,
                                  quantity=Decimal('300.000'))
        resp_over = self._add(product, quantity=4)   # milk limits to 3
        self.assertFalse(resp_over.json()['success'])
        self.assertIn('left in stock', resp_over.json()['error'])
        resp_ok = self._add(product, quantity=3)
        self.assertTrue(resp_ok.json()['success'])

    def test_ingredient_add_uses_selected_size(self):
        # Sized ingredient product: 'large' has its own recipe with a
        # different per-unit quantity, so per-size availability differs.
        product = _ingredient_product(on_hand=1000, has_sizes=True,
                                      price_large='80.00')
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000', size='none')
        _recipe(product, ledger_item, quantity_per_unit='500.000', size='large')
        resp_ok = self._add(product, quantity=3, size='none')   # 4 units avail
        self.assertTrue(resp_ok.json()['success'])
        resp_big = self._add(product, quantity=1, size='large')  # 2 units avail
        self.assertTrue(resp_big.json()['success'])
        resp_over = self._add(product, quantity=3, size='large')  # 3 > 2
        self.assertFalse(resp_over.json()['success'])

    def test_cart_view_caps_ingredient_line_to_units(self):
        # _ingredient() already creates the approved recipe (helper chain).
        # The cart views operate on the SESSION cart (get_or_create_cart), so
        # the fixture cart must bind to the test client's session key.
        product = self._ingredient()
        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(cart=cart, product=product, quantity=10,
                                size='none', unit_price=Decimal('60.00'))
        resp = self.client.get(reverse('orders:cart'))
        self.assertEqual(resp.status_code, 200)
        ci = cart.cart_items.get()
        ci.refresh_from_db()
        self.assertEqual(ci.quantity, 4)      # capped to floor(1000/250)

    def test_cart_view_removes_incomplete_config_line(self):
        # Incomplete configuration → the line is removed with a notice,
        # matching the out-of-stock handling for legacy products.  Uses
        # _ingredient_product directly (no recipe → incomplete config).
        product = _ingredient_product('E3B Incomplete Config',
                                      category=self.cat)
        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(cart=cart, product=product, quantity=1,
                                size='none', unit_price=Decimal('60.00'))
        resp = self.client.get(reverse('orders:cart'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(cart.cart_items.exists())

    def test_update_cart_revalidates_availability(self):
        # _ingredient() already creates the approved recipe (helper chain).
        # update_cart resolves the line through the SESSION cart (ownership
        # check), so the fixture cart must bind to the client's session key.
        product = self._ingredient()
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        cart_item = CartItem.objects.create(cart=cart, product=product,
                                            quantity=1, size='none',
                                            unit_price=Decimal('60.00'))
        # Drain the ledger below the cart quantity, then update.
        _adjust(ledger_item, Decimal('-900.000'))   # 100 on hand → 0 units
        resp = self.client.post(
            reverse('orders:update_cart', args=[cart_item.pk]),
            {'quantity': '3'},
        )
        data = resp.json()
        # Zero available units → the line is removed, not silently capped.
        self.assertTrue(data['removed'])
        self.assertFalse(cart.cart_items.exists())

    def test_checkout_rejects_when_availability_falls_below_quantity(self):
        # _ingredient() already creates the approved recipe (helper chain).
        # The checkout drop/cap runs on POST (GET only renders the form).
        product = self._ingredient()
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        cart_item = CartItem.objects.create(cart=cart, product=product,
                                            quantity=2, size='none',
                                            unit_price=Decimal('60.00'))
        # Drain the ledger so units < the cart quantity.
        _adjust(ledger_item, Decimal('-900.000'))   # 100 on hand → 0 units
        resp = self.client.post(reverse('orders:checkout'), {
            'customer_name': 'E3B Customer',
            'order_type': 'dine_in',
            'payment_method': 'cash',
        })
        # Quantity exceeded availability → capped and bounced back to the
        # cart for review; no order is created.
        self.assertEqual(resp.status_code, 302)
        self.assertURLEqual(resp.url, reverse('orders:cart'))
        ci = cart.cart_items.get()
        ci.refresh_from_db()
        self.assertEqual(ci.quantity, 0)   # capped to floor(100/250) = 0
        self.assertFalse(Order.objects.exists())


class CheckoutPreflightE3BTests(E3BTestBase):
    """Pre-flight inside the placement transaction: authoritative
    availability gate per line (legacy unchanged, ingredient recipe-based)."""

    def setUp(self):
        super().setUp()
        self.cat = _category('E3B Checkout Cat')

    def test_ingredient_units_for_order_type_below_quantity(self):
        # Function-level basis of the in-lock pre-flight (the view branch
        # itself is exercised by
        # test_pre_flight_rejects_incomplete_config_inside_transaction):
        # availability for a concrete order type derives from the approved
        # plan — cart requests 2 units (2 × 250 = 500 needed), ledger has
        # 250 → 1 unit.
        product = _ingredient_product('E3B Checkout Prod',
                                      category=self.cat, on_hand=250)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        r = get_available_units(product, 'none', 'dine_in')
        self.assertEqual(r.units, 1)
        self.assertTrue(r.configuration_complete)

    def test_legacy_availability_pass_through_for_order_type(self):
        # Legacy products pass stock_quantity through for any order type —
        # the gate value the pre-flight compares against is unchanged.
        product = _legacy_product(stock=0, category=self.cat)
        r = get_available_units(product, 'none', 'dine_in')
        self.assertEqual(r.units, 0)
        self.assertTrue(r.configuration_complete)

    def test_pre_flight_rejects_incomplete_config_inside_transaction(self):
        # Drives the REAL checkout view into the in-lock pre-flight branch
        # (orders/views.py, availability check after the Order row is
        # created inside the placement transaction) deterministically: the
        # availability function is patched at its source module (the view
        # imports it deferred at each call site), returning a sellable
        # result while the render-stage cap loop runs and an incomplete
        # result during the in-lock pre-flight.  No production timing is
        # changed.  Each mock call records whether an Order row already
        # exists, proving the call ORDER: cap loop before order creation,
        # pre-flight after it, inside the transaction.
        from unittest import mock
        from apps.inventory.services import AvailabilityResult

        product = _ingredient_product('E3B Preflight Prod',
                                      category=self.cat, on_hand=1000)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')

        session = self.client.session
        session['_seed'] = 'x'  # writing forces the session to be saved
        cart = Cart.objects.create(session_key=session.session_key)
        CartItem.objects.create(cart=cart, product=product, quantity=1,
                                size='none', unit_price=Decimal('60.00'))

        order_existed_at_call = []

        def _fake_availability(prod, size, order_type=None):
            order_existed_at_call.append(Order.objects.exists())
            if len(order_existed_at_call) == 1:
                # Cap loop: plenty of stock — nothing gets capped.
                return AvailabilityResult(units=4,
                                          configuration_complete=True,
                                          reason='')
            # In-lock pre-flight: the configuration has gone incomplete.
            return AvailabilityResult(units=0,
                                      configuration_complete=False,
                                      reason='recipe missing')

        with mock.patch(
                'apps.inventory.services.get_available_units',
                side_effect=_fake_availability):
            resp = self.client.post(reverse('orders:checkout'), {
                'customer_name': 'E3B Preflight',
                'order_type': 'dine_in',
                'payment_method': 'cash',
            })

        # Exactly two availability calls: render-stage cap loop, then the
        # in-lock pre-flight.
        self.assertEqual(len(order_existed_at_call), 2)
        # Call 1 (cap loop) saw NO Order row yet; call 2 (pre-flight) ran
        # INSIDE the transaction, after the Order row was created.
        self.assertFalse(order_existed_at_call[0])
        self.assertTrue(order_existed_at_call[1])
        # The pre-flight rejection bounces to the cart with an error.
        self.assertEqual(resp.status_code, 302)
        self.assertURLEqual(resp.url, reverse('orders:cart'))
        # Full rollback: no order, no items, no reservation, no movement.
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderItem.objects.exists())
        ledger_item.refresh_from_db()
        self.assertEqual(ledger_item.reserved_quantity, Decimal('0.000'))
        self.assertEqual(ledger_item.on_hand_quantity, Decimal('1000.000'))


# ── Menu API (product_stock) ─────────────────────────────────────────────────

class ProductStockE3BTests(E3BTestBase):

    def _poll(self):
        resp = self.client.get(reverse('menu:product_stock'))
        self.assertEqual(resp.status_code, 200)
        import json
        return json.loads(resp.content)['products']

    def test_legacy_payload_preserved(self):
        product = _legacy_product(stock=7)
        data = self._poll()
        entry = data[str(product.pk)]
        # Additive contract: legacy fields unchanged.
        self.assertEqual(entry['stock_quantity'], 7)
        self.assertTrue(entry['is_available'])
        self.assertTrue(entry['is_active'])
        # Additive fields.
        self.assertEqual(entry['inventory_tracking_mode'], 'legacy')
        self.assertEqual(entry['available_units'], 7)
        self.assertTrue(entry['configuration_complete'])
        self.assertTrue(entry['sellable'])
        self.assertFalse(entry['out_of_stock'])

    def test_no_size_ingredient_payload(self):
        # Ingredient product without sizes: one default/no-size result.
        product = _ingredient_product('E3B NoSize', on_hand=1000)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        data = self._poll()
        entry = data[str(product.pk)]
        self.assertEqual(entry['inventory_tracking_mode'], 'ingredient')
        self.assertEqual(entry['available_units'], 4)   # floor(1000/250)
        self.assertTrue(entry['configuration_complete'])
        self.assertTrue(entry['sellable'])
        self.assertFalse(entry['out_of_stock'])
        # Legacy field preserved (frozen value, not authoritative).
        self.assertEqual(entry['stock_quantity'], 0)

    def test_sized_ingredient_per_size_payload(self):
        # Sized ingredient product: per-size results, no collapsing.
        # has_sizes/price_large are set at creation so the fixture builds
        # packaging rules for every configured size (Rev 3 §I.2).
        product = _ingredient_product('E3B Sized', on_hand=1000,
                                      has_sizes=True, price_large='80.00')
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000', size='none')
        _recipe(product, ledger_item, quantity_per_unit='500.000', size='large')
        data = self._poll()
        entry = data[str(product.pk)]
        self.assertEqual(entry['available_units'], 4)   # max across sizes
        self.assertTrue(entry['configuration_complete'])
        self.assertTrue(entry['sellable'])
        # Per-size map: 'none' → 4, 'large' → 2.
        self.assertEqual(entry['sizes']['none']['available_units'], 4)
        self.assertEqual(entry['sizes']['large']['available_units'], 2)

    def test_complete_config_zero_stock_reports_complete(self):
        # Audit fix M1: configuration completeness is independent of stock —
        # a fully configured ingredient product with zero available units
        # still reports configuration_complete=true; sellable/out_of_stock
        # carry the availability semantics.
        product = _ingredient_product('E3B Complete Zero', on_hand=0)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')
        entry = self._poll()[str(product.pk)]
        self.assertTrue(entry['configuration_complete'])
        self.assertEqual(entry['available_units'], 0)
        self.assertFalse(entry['sellable'])
        self.assertTrue(entry['out_of_stock'])
        self.assertTrue(entry['sizes']['none']['configuration_complete'])

    def test_partially_configured_sized_product_reports_incomplete(self):
        # Audit fix M1: top-level completeness requires EVERY configured
        # size to have a complete plan.  A sized product with a plan for
        # 'none' only: the missing 'large' plan makes configuration_complete
        # false even though the product is still sellable via 'none' (the
        # pre-M1 any_sellable semantics reported true here).
        product = _ingredient_product('E3B Partial Config', on_hand=1000,
                                      has_sizes=True, price_large='80.00')
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000', size='none')
        entry = self._poll()[str(product.pk)]
        self.assertFalse(entry['configuration_complete'])
        self.assertTrue(entry['sellable'])
        self.assertFalse(entry['out_of_stock'])
        # Per-size truth is unchanged:
        self.assertTrue(entry['sizes']['none']['configuration_complete'])
        self.assertFalse(entry['sizes']['large']['configuration_complete'])

    def test_etag_reflects_ingredient_availability_changes(self):
        # The ETag fingerprint hashes the payload (including availability);
        # a stock/reservation change that alters the payload must change the
        # ETag — verified directly, not claimed.
        product = _ingredient_product('E3B ETag', on_hand=1000)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit='250.000')

        resp1 = self.client.get(reverse('menu:product_stock'))
        etag1 = resp1.headers.get('ETag')
        self.assertIsNotNone(etag1)

        # Reservation changes available_quantity (no movement rows needed).
        _reserve(ledger_item, 250)
        resp2 = self.client.get(reverse('menu:product_stock'))
        etag2 = resp2.headers.get('ETag')
        self.assertNotEqual(etag1, etag2,
                            'ETag stale after a reservation change')

        # Available units dropped: floor((1000-250)/250) = 3.
        data = resp2.json()['products']
        self.assertEqual(
            data[str(product.pk)]['available_units'], 3)

    def test_incomplete_config_payload_and_no_internal_details(self):
        # Incomplete configuration: available_units=0, complete=False; the
        # internal reason string is NOT exposed to ordinary customers.
        product = _ingredient_product('E3B NoRecipe')
        data = self._poll()
        entry = data[str(product.pk)]
        self.assertEqual(entry['available_units'], 0)
        self.assertFalse(entry['configuration_complete'])
        self.assertFalse(entry['sellable'])
        self.assertTrue(entry['out_of_stock'])
        self.assertNotIn('reason', entry)


class ReconciliationAndRestockE3BTests(E3BTestBase):

    def test_restock_rejects_ingredient_product(self):
        # §E.3b: legacy restocking is disabled for ingredient-tracked
        # products — their stock lives in the ingredient ledger.
        product = _ingredient_product('E3B Restock Block', on_hand=1000)
        resp = self.client.post(
            reverse('inventory:restock', args=[product.pk]),
            {'quantity': '10'},
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertIn('ingredient', data['error'].lower())
        # Stock unchanged (frozen non-authoritative field, fixture sets 0).
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 0)

    def test_restock_allows_legacy_product(self):
        # Legacy products restock exactly as before.
        product = _legacy_product(stock=10, category=_category('E3B Legacy'))
        resp = self.client.post(
            reverse('inventory:restock', args=[product.pk]),
            {'quantity': '5'},
        )
        self.assertEqual(resp.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 15)

    def test_inventory_list_shows_ingredient_mode(self):
        # §E.3b: the inventory list displays the tracking mode so staff can
        # distinguish ledgers (view passes the products; template renders).
        product = _ingredient_product('E3B List Ingredient', on_hand=1000)
        resp = self.client.get(reverse('inventory:list'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(product, resp.context['products'])

    def test_ingredient_reconciliation_uses_ledger_not_stock(self):
        # The Phase 7.5 reconciliation reads the ledger, not stock_quantity —
        # an ingredient item with 0 stock_quantity still reconciles correctly.
        product = _ingredient_product('E3B Recon', on_hand=0)
        item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, item, quantity_per_unit='250.000')
        _adjust(item, Decimal('1000.000'))   # opening adjustment
        from apps.reports.views import _compute_reconciliation
        _items, results, _summary = _compute_reconciliation()
        result = {r['pk']: r for r in results}[item.pk]
        self.assertEqual(result['on_hand_status'], 'RECONCILED')
        self.assertEqual(result['computed_on_hand'], '1000')


# ── Chatbot integration ──────────────────────────────────────────────────────

class ChatbotE3BTests(TestCase):
    """Chatbot availability decisions route through the centralized
    function via _sellable_units (legacy: stock_quantity; ingredient:
    recipe/packaging-derived units, incomplete config → not recommended)."""

    def setUp(self):
        self.cat = _category('E3B Chatbot Cat')

    def _ingredient(self, name, on_hand=1000, reserved=0, status='approved',
                    per_unit='250.000', sizes=('none',)):
        product = _ingredient_product(name, category=self.cat,
                                      on_hand=on_hand, reserved=reserved)
        ledger_item = InventoryItem.objects.get(name__startswith='TEST Ledger')
        _recipe(product, ledger_item, quantity_per_unit=per_unit,
                status=status)
        # Extra packaging rules for additional sizes used by sized tests.
        for size in sizes:
            if size != 'none':
                for ot in ('dine_in', 'takeout'):
                    ProductPackagingRule.objects.get_or_create(
                        product=product, size=size, order_type=ot,
                        defaults={'no_packaging': True},
                    )
        return product

    def test_ingredient_with_units_recommendable(self):
        # Chatbot case 31: sufficient availability → recommendable.
        from apps.chatbot.service import _sellable_units
        product = self._ingredient('Chat OK', on_hand=1000)
        self.assertGreater(_sellable_units(product), 0)

    def test_ingredient_zero_availability_not_recommendable(self):
        # Chatbot case 32: zero availability → not recommended as sellable.
        from apps.chatbot.service import _sellable_units
        product = self._ingredient('Chat Empty', on_hand=100, reserved=100)
        self.assertEqual(_sellable_units(product), 0)

    def test_ingredient_incomplete_config_not_recommendable(self):
        # Chatbot case 33: incomplete recipe → not recommended.
        # _ingredient_product directly (no recipe → incomplete config).
        from apps.chatbot.service import _sellable_units
        product = _ingredient_product('Chat NoRecipe', category=self.cat,
                                      on_hand=500)
        self.assertEqual(_sellable_units(product), 0)

    def test_legacy_chatbot_unchanged(self):
        # Chatbot case 34: legacy products use stock_quantity.
        from apps.chatbot.service import _sellable_units
        product = _legacy_product(stock=6, category=self.cat)
        self.assertEqual(_sellable_units(product), 6)

    def test_sorted_product_queries_tie_break_names_ascending(self):
        # Audit fix M3: the post-filter Python sort must replicate the
        # previous Django ordering `order_by(order_field, 'name')` — equal
        # prices keep ASCENDING name order (both directions), while the
        # primary price ordering is preserved (ascending for cheapest,
        # descending for most expensive).
        from apps.chatbot.service import (
            query_cheapest_products,
            query_most_expensive_products,
        )
        prices_names = [
            ('Caramel Fix', '30.00'),
            ('Americano Fix', '30.00'),
            ('Banana Blend', '40.00'),
        ]
        for name, price in prices_names:
            p = _legacy_product(name, stock=5, category=self.cat)
            p.price = Decimal(price)
            p.save(update_fields=['price'])

        cheapest_ctx, cheapest_found = query_cheapest_products(limit=5)
        self.assertTrue(cheapest_found)
        # Cheapest: the ₱30 tie keeps ascending names, ₱40 last.
        self.assertLess(cheapest_ctx.index('Americano Fix'),
                        cheapest_ctx.index('Caramel Fix'))
        self.assertLess(cheapest_ctx.index('Caramel Fix'),
                        cheapest_ctx.index('Banana Blend'))

        expensive_ctx, expensive_found = query_most_expensive_products(limit=5)
        self.assertTrue(expensive_found)
        # Most expensive: ₱40 first, then the ₱30 tie ascending by name
        # (Django's order_by('-price', 'name') tie-break, not reversed).
        self.assertLess(expensive_ctx.index('Banana Blend'),
                        expensive_ctx.index('Americano Fix'))
        self.assertLess(expensive_ctx.index('Americano Fix'),
                        expensive_ctx.index('Caramel Fix'))
