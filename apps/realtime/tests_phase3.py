"""Phase 3 — mode-aware realtime inventory contract regression tests.

Covers the R4 fix (docs/phase2b §3.2 / docs/phase3-realtime-inventory-fix.md):

  1. Legacy Product save emits legacy availability (available_units ==
     stock_quantity, configuration_complete True, sellable rule).
  2. Ingredient Product save NEVER publishes frozen stock_quantity as
     availability — the payload carries plan-derived available_units.
  3. The payload contract is exact (mode-aware keys only).
  4. Ingredient low-stock is emitted from the movement ledger
     (`ingredient_low`); the retired legacy `inventory_low` event is never
     published (Phase 7C-2).
  5. Ingredient availability events publish only AFTER the enclosing
     transaction commits (captureOnCommitCallbacks).
  6. A reservation (order placement) updates the affected product's
     availability once the transaction commits.
  7. A shared ingredient movement updates EVERY ingredient-tracked
     product that references it.

JS-side behaviour (`main.js` consumes `available_units`, drops malformed
events) is verified by `node --check` + code review; there is no JS test
runner in this project (documented in the Phase 3 report).

Runs on Django's isolated test database; the live development database is
never touched.
"""
import queue
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.menu.models import Category, Product
from apps.orders.services import place_order_inventory
from apps.orders.models import Order, OrderItem
from apps.realtime import broker
from apps.realtime.broker import publish


def _uid():
    import time
    return str(time.monotonic_ns())


def _product(name, *, mode='legacy', stock=50, threshold=10):
    category = Category.objects.create(
        name=f'P3 Cat {name} {_uid()}', slug=f'p3-{name}-{_uid()}')
    return Product.objects.create(
        category=category, name=name, price='50.00',
        stock_quantity=stock, low_stock_threshold=threshold,
        inventory_tracking_mode=mode,
    )


def _configure_ingredient(product, ingredient, qty='10.000'):
    recipe = Recipe.objects.create(
        product=product, size='none', order_type_scope=Recipe.SCOPE_ALL,
        status=Recipe.STATUS_APPROVED, revision=1,
        approved_at=timezone.now(), approval_note='P3 fixture',
    )
    RecipeItem.objects.create(
        recipe=recipe, inventory_item=ingredient, quantity=Decimal(qty))
    for order_type in ('dine_in', 'takeout'):
        ProductPackagingRule.objects.create(
            product=product, size='none', order_type=order_type,
            no_packaging=True,
        )
    return recipe


def _place_order(product, quantity=1):
    order = Order.objects.create(
        customer_name=f'P3 order {_uid()}', order_type='dine_in',
        status='awaiting_payment',
    )
    OrderItem.objects.create(
        order=order, product=product, product_name=product.name,
        category_name=product.category.name, packaging_eligible=False,
        size='none', quantity=quantity, unit_price=Decimal('50.00'),
        subtotal=Decimal('50.00') * quantity,
        inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
    )
    return order


def _wastage(item, delta):
    """Wastage movement + balance update (mirrors the admin flow)."""
    IngredientMovementLog.record(
        inventory_item=item,
        movement_type=IngredientMovementLog.MOVEMENT_WASTAGE,
        on_hand_delta=Decimal(delta),
        reserved_delta=Decimal('0.000'),
        on_hand_before=item.on_hand_quantity,
        reserved_before=item.reserved_quantity,
        note='Phase 3 test wastage',
    )
    item.on_hand_quantity = item.on_hand_quantity + Decimal(delta)
    item.save(update_fields=['on_hand_quantity'])


class BrokerCaptureTestCase(TestCase):
    """Subscribes to the in-memory broker and drains events per test."""

    def setUp(self):
        super().setUp()
        self._q = broker.subscribe()

    def tearDown(self):
        broker.unsubscribe(self._q)
        super().tearDown()

    def _drain(self):
        events = []
        while True:
            try:
                events.append(self._q.get_nowait())
            except queue.Empty:
                break
        return events

    def _events(self, event_type):
        return [e for e in self._drain() if e['event'] == event_type]

    def _payloads(self, event_type):
        return [e['data'] for e in self._events(event_type)]

    def _by_product(self, event_type, product_id):
        return [d for d in self._payloads(event_type)
                if d.get('product_id') == product_id]


class ProductSavePayloadTests(BrokerCaptureTestCase):
    """Scenarios 1, 2, 3(backend contract), 8."""

    def test_non_ingredient_save_emits_guard_payload(self):
        """Phase 7D-2B: a non-ingredient product save broadcasts the GUARD
        payload — zero units, not sellable, never the frozen legacy
        stock_quantity (rollback retired in Phase 7D-2A)."""
        product = _product('P3 Legacy', mode='legacy', stock=50, threshold=10)
        self._drain()   # drop creation-era events (none expected)

        product.is_available = False
        product.save(update_fields=['is_available'])

        payloads = self._by_product('inventory_changed', product.pk)
        self.assertEqual(len(payloads), 1)
        p = payloads[0]
        self.assertEqual(p['inventory_tracking_mode'], 'legacy')
        self.assertEqual(p['available_units'], 0)
        self.assertFalse(p['configuration_complete'])
        self.assertFalse(p['sellable'])
        self.assertFalse(p['is_available'])
        self.assertTrue(p['is_active'])

    def test_legacy_save_publishes_no_retired_low_stock_event(self):
        """Phase 7C-2 retired ``inventory_low``; a legacy product save only
        emits ``inventory_changed`` (guard payload since Phase 7D-2B)."""
        product = _product('P3 Legacy Low', mode='legacy', stock=5, threshold=10)
        self._drain()

        product.stock_quantity = 3
        product.save(update_fields=['stock_quantity'])

        events = self._drain()
        self.assertEqual(
            [e for e in events if e['event'] == 'inventory_low'], [])
        payloads = [e['data'] for e in events
                    if e['event'] == 'inventory_changed'
                    and e['data'].get('product_id') == product.pk]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['available_units'], 0)
        self.assertFalse(payloads[0]['sellable'])

    def test_no_inventory_low_producer_or_consumer_remains(self):
        """Phase 7C-2: the event was retired end-to-end — no server
        publisher, no client dispatch, no dashboard listener — while the
        replacement ``ingredient_low`` channel stays wired to the browser."""
        root = Path(settings.BASE_DIR)
        signals_src = (root / 'apps' / 'realtime' / 'signals.py').read_text(
            encoding='utf-8')
        realtime_js = (root / 'static' / 'js' / 'realtime.js').read_text(
            encoding='utf-8')
        dashboard_html = (
            root / 'templates' / 'dashboard' / 'index.html'
        ).read_text(encoding='utf-8')

        self.assertNotIn("publish('inventory_low'", signals_src)
        self.assertNotIn('inventory_low', realtime_js)
        self.assertNotIn('inventory_low', dashboard_html)

        # The surviving low-stock channel must reach dashboard listeners.
        self.assertIn(
            "eventSource.addEventListener('ingredient_low'", realtime_js)
        self.assertIn(
            "RealtimeConnection.on('ingredient_low'", dashboard_html)

    def test_ingredient_save_never_broadcasts_frozen_stock(self):
        product = _product('P3 Ingr', mode='ingredient', stock=92)
        ingredient = InventoryItem.objects.create(
            name=f'P3 Ingr {_uid()}', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('1000.000'),
        )
        _configure_ingredient(product, ingredient, qty='10.000')
        self._drain()

        product.name = 'P3 Ingr Renamed'
        product.save(update_fields=['name'])

        events = self._drain()
        payloads = [e['data'] for e in events
                    if e['event'] == 'inventory_changed'
                    and e['data'].get('product_id') == product.pk]
        self.assertEqual(len(payloads), 1)
        p = payloads[0]
        # Derived, NOT the frozen 92.
        self.assertEqual(p['inventory_tracking_mode'], 'ingredient')
        self.assertEqual(p['available_units'], 100)
        self.assertTrue(p['configuration_complete'])
        self.assertTrue(p['sellable'])
        self.assertNotIn('stock_quantity', p)
        self.assertNotIn('low_stock_threshold', p)
        # Contract keys are exact — no legacy fields ride along.
        self.assertEqual(set(p.keys()), {
            'product_id', 'product_name', 'inventory_tracking_mode',
            'available_units', 'configuration_complete', 'sellable',
            'is_available', 'is_active',
        })
        # The retired legacy low-stock event never fires.
        self.assertEqual(
            [e for e in events if e['event'] == 'inventory_low'], [])

    def test_ingredient_incomplete_configuration_reports_zero_and_incomplete(self):
        # A second size (large) has no recipe → configuration_complete False
        # and the POS aggregation reports the max over COMPLETE sizes only.
        product = _product('P3 Partial', mode='ingredient', stock=77)
        product.has_sizes = True
        product.price_large = '80.00'
        product.save(update_fields=['has_sizes', 'price_large'])
        ingredient = InventoryItem.objects.create(
            name=f'P3 Partial Ingr {_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal('100.000'),
        )
        _configure_ingredient(product, ingredient, qty='10.000')
        self._drain()

        product.save(update_fields=['name'])

        p = self._by_product('inventory_changed', product.pk)[0]
        self.assertEqual(p['inventory_tracking_mode'], 'ingredient')
        self.assertEqual(p['available_units'], 10)     # complete 'none' size
        self.assertFalse(p['configuration_complete'])
        self.assertFalse(p['sellable'])                # incomplete → not sellable


class IngredientLedgerEventTests(BrokerCaptureTestCase):
    """Scenarios 5, 6, 7: ledger-driven events, commit timing, shared items."""

    def _ingredient_setup(self, on_hand=Decimal('150.000'), threshold=100):
        product = _product('P3 Ledger', mode='ingredient', stock=40)
        ingredient = InventoryItem.objects.create(
            name=f'P3 Ledger Ingr {_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=on_hand,
            low_stock_threshold=Decimal(str(threshold)),
        )
        _configure_ingredient(product, ingredient, qty='10.000')
        return product, ingredient

    def test_wastage_crossing_threshold_emits_ingredient_low_only(self):
        product, ingredient = self._ingredient_setup(
            on_hand=Decimal('150.000'), threshold=100)
        self._drain()

        with self.captureOnCommitCallbacks(execute=True):
            _wastage(ingredient, '-100')   # available 150 → 50, crosses 100

        events = self._drain()   # single drain — partial drains discard siblings
        lows = [e['data'] for e in events if e['event'] == 'ingredient_low']
        changed = [e['data'] for e in events if e['event'] == 'inventory_changed'
                   and e['data'].get('product_id') == product.pk]
        self.assertEqual(len(lows), 1)
        self.assertEqual(lows[0]['inventory_item_id'], ingredient.pk)
        self.assertEqual(lows[0]['item_type'], 'ingredient')
        self.assertEqual(lows[0]['available_quantity'], '50.000')
        # The retired legacy product event must never fire.
        self.assertEqual(
            [e for e in events if e['event'] == 'inventory_low'], [])
        # The product's availability was refreshed by the ledger event too.
        self.assertTrue(changed and changed[-1]['available_units'] == 5)

    def test_events_publish_only_after_commit(self):
        product, ingredient = self._ingredient_setup(
            on_hand=Decimal('150.000'), threshold=100)
        self._drain()

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            with transaction.atomic():
                _wastage(ingredient, '-100')

        # Inside the uncommitted transaction: nothing published yet.
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self._drain(), [])

        # Commit (callbacks executed manually — the context manager with
        # execute=False returns the raw callback list): events fire once.
        for callback in callbacks:
            callback()
        events = self._drain()
        self.assertEqual(len([e for e in events if e['event'] == 'ingredient_low']), 1)
        self.assertEqual(len([e for e in events if e['event'] == 'inventory_changed'
                              and e['data'].get('product_id') == product.pk]), 1)

    def test_reservation_updates_affected_product_availability(self):
        product = _product('P3 Resv', mode='ingredient', stock=40)
        ingredient = InventoryItem.objects.create(
            name=f'P3 Resv Ingr {_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal('100.000'),
        )
        _configure_ingredient(product, ingredient, qty='10.000')
        order = _place_order(product, quantity=1)
        self._drain()

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                place_order_inventory(order.pk)

        self.assertTrue(callbacks)   # the ledger write registered a broadcast
        changed = self._by_product('inventory_changed', product.pk)
        self.assertTrue(changed)
        p = changed[-1]
        # 100 g on hand − 10 g reserved → floor((100−10)/10) = 9 units.
        self.assertEqual(p['available_units'], 9)
        self.assertTrue(p['configuration_complete'])
        self.assertTrue(p['sellable'])
        ingredient.refresh_from_db()
        self.assertEqual(ingredient.reserved_quantity, Decimal('10.000'))

    def test_shared_ingredient_updates_every_referencing_product(self):
        shared = InventoryItem.objects.create(
            name=f'P3 Shared {_uid()}', item_type='ingredient',
            base_unit='g', on_hand_quantity=Decimal('500.000'),
        )
        p1 = _product('P3 Shared A', mode='ingredient', stock=10)
        p2 = _product('P3 Shared B', mode='ingredient', stock=11)
        _configure_ingredient(p1, shared, qty='10.000')
        _configure_ingredient(p2, shared, qty='20.000')
        self._drain()

        with self.captureOnCommitCallbacks(execute=True):
            _wastage(shared, '-100')   # 500 → 400

        events = self._drain()
        changed = [e['data'] for e in events if e['event'] == 'inventory_changed']
        changed1 = [d for d in changed if d.get('product_id') == p1.pk]
        changed2 = [d for d in changed if d.get('product_id') == p2.pk]
        self.assertTrue(changed1 and changed1[-1]['available_units'] == 40)
        self.assertTrue(changed2 and changed2[-1]['available_units'] == 20)

    def test_rollback_publishes_nothing(self):
        product, ingredient = self._ingredient_setup(
            on_hand=Decimal('150.000'), threshold=100)
        self._drain()

        try:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                with transaction.atomic():
                    _wastage(ingredient, '-100')
                    raise RuntimeError('force rollback')
        except RuntimeError:
            pass

        for callback in callbacks:   # nothing registered → nothing published
            callback()
        self.assertEqual(self._events('ingredient_low'), [])
        self.assertEqual(self._events('inventory_changed'), [])

    def test_ledger_write_creates_no_audit_rows(self):
        product, ingredient = self._ingredient_setup()
        self._drain()
        before = AuditLog.objects.count()

        with self.captureOnCommitCallbacks(execute=True):
            _wastage(ingredient, '-10')

        self.assertEqual(AuditLog.objects.count(), before)


class ContractRegressionTests(BrokerCaptureTestCase):
    """Scenario 8/10 support: legacy contract byte-stability and the
    payload of an unrelated (non-inventory) product save."""

    def test_legacy_payload_keys_exact(self):
        """Phase 7D-2B: the non-ingredient guard payload keeps the same key
        set as the ingredient payload — no stock/threshold keys ever leave
        the server."""
        product = _product('P3 Keys', mode='legacy', stock=30)
        self._drain()
        product.save(update_fields=['name'])
        p = self._by_product('inventory_changed', product.pk)[0]
        self.assertEqual(set(p.keys()), {
            'product_id', 'product_name', 'inventory_tracking_mode',
            'available_units', 'configuration_complete', 'sellable',
            'is_available', 'is_active',
        })
        self.assertEqual(p['available_units'], 0)
        self.assertNotIn('stock_quantity', p)
        self.assertNotIn('low_stock_threshold', p)

    def test_new_products_emit_nothing_on_create(self):
        self._drain()
        _product('P3 Created', mode='legacy', stock=30)
        _product('P3 Created2', mode='ingredient', stock=30)
        self.assertEqual(self._events('inventory_changed'), [])
