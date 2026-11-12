"""
tests_phase6a.py
================
Phase 6A Stage 6A-1 — Model and constraint tests.

Covers the eight database constraint tests from §11.1 of the implementation
plan:

  1. test_valid_disposition_created
  2. test_unique_disposition_per_order_item
  3. test_units_sum_to_quantity
  4. test_discarded_within_prepared
  5. test_negative_units_rejected
  6. test_invalid_source_rejected
  7. test_invalid_ledger_rejected
  8. test_fk_integrity

Stage 6A-1 scope: model and migration only. No cancellation behavior,
no service changes, no view changes, no existing test modifications.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.menu.models import Category, Product
from apps.orders.models import (
    Order,
    OrderCancellationDisposition,
    OrderItem,
)

User = get_user_model()
_PASSWORD = 'phase6a-test-pass'
_counter = 0
_UNIT_PRICE = Decimal('60.00')


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _category():
    n = f'Cat6A_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower())


def _product(stock=10):
    return Product.objects.create(
        category=_category(), name=f'Prod6A_{_uid()}',
        price='60.00', stock_quantity=stock,
    )


def _order():
    return Order.objects.create(customer_name='6A Test Customer')


def _order_item(order, product, quantity=2):
    return OrderItem.objects.create(
        order=order, product=product,
        product_name=product.name,
        category_name=product.category.name,
        packaging_eligible=False,
        size='none', quantity=quantity,
        unit_price=_UNIT_PRICE,
        subtotal=_UNIT_PRICE * quantity,
        inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
    )


def _valid_disposition(order, order_item, **overrides):
    """Create a valid disposition row with sensible defaults."""
    defaults = {
        'ledger': OrderCancellationDisposition.LEDGER_LEGACY,
        'order_item_quantity': order_item.quantity,
        'prepared_units': 0,
        'unprepared_units': order_item.quantity,
        'discarded_units': 0,
        'source': OrderCancellationDisposition.SOURCE_SYSTEM_UNPAID,
    }
    defaults.update(overrides)
    return OrderCancellationDisposition.objects.create(
        order=order,
        order_item=order_item,
        **defaults,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. test_valid_disposition_created
# ═════════════════════════════════════════════════════════════════════════════

class ValidDispositionTests(TestCase):

    def test_valid_disposition_created(self):
        """A disposition row can be created with valid fields."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=3)
        disp = _valid_disposition(order, item, prepared_units=1,
                                   unprepared_units=2)
        self.assertEqual(disp.order, order)
        self.assertEqual(disp.order_item, item)
        self.assertEqual(disp.ledger, 'legacy')
        self.assertEqual(disp.order_item_quantity, 3)
        self.assertEqual(disp.prepared_units, 1)
        self.assertEqual(disp.unprepared_units, 2)
        self.assertEqual(disp.discarded_units, 0)
        self.assertEqual(disp.source, 'system_unpaid')
        self.assertIsNotNone(disp.decided_at)


# ═════════════════════════════════════════════════════════════════════════════
# 2. test_unique_disposition_per_order_item
# ═════════════════════════════════════════════════════════════════════════════

class UniqueDispositionTests(TestCase):

    def test_unique_disposition_per_order_item(self):
        """A second row for the same (order, order_item) raises IntegrityError."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        _valid_disposition(order, item)
        with self.assertRaises(IntegrityError):
            _valid_disposition(order, item)

    def test_different_order_items_allowed(self):
        """Different order items on the same order each get their own row."""
        prod_a = _product()
        prod_b = _product()
        order = _order()
        item_a = _order_item(order, prod_a, quantity=1)
        item_b = _order_item(order, prod_b, quantity=2)
        disp_a = _valid_disposition(order, item_a)
        disp_b = _valid_disposition(order, item_b, prepared_units=1,
                                     unprepared_units=1)
        self.assertNotEqual(disp_a.pk, disp_b.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 3. test_units_sum_to_quantity
# ═════════════════════════════════════════════════════════════════════════════

class UnitsSumTests(TestCase):

    def test_units_sum_to_quantity(self):
        """prepared + unprepared != quantity → IntegrityError."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=3)
        # prepared=2, unprepared=2 → sum=4 != 3
        with self.assertRaises(IntegrityError):
            OrderCancellationDisposition.objects.create(
                order=order, order_item=item,
                ledger='legacy', order_item_quantity=3,
                prepared_units=2, unprepared_units=2,
                discarded_units=0, source='system_unpaid',
            )

    def test_units_sum_correct_succeeds(self):
        """prepared + unprepared = quantity → OK."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=4)
        disp = OrderCancellationDisposition.objects.create(
            order=order, order_item=item,
            ledger='legacy', order_item_quantity=4,
            prepared_units=1, unprepared_units=3,
            discarded_units=0, source='staff_declared',
        )
        self.assertEqual(disp.prepared_units, 1)
        self.assertEqual(disp.unprepared_units, 3)


# ═════════════════════════════════════════════════════════════════════════════
# 4. test_discarded_within_prepared
# ═════════════════════════════════════════════════════════════════════════════

class DiscardedWithinPreparedTests(TestCase):

    def test_discarded_within_prepared(self):
        """discarded > prepared → IntegrityError."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=3)
        # prepared=1, discarded=2 → discarded > prepared
        with self.assertRaises(IntegrityError):
            OrderCancellationDisposition.objects.create(
                order=order, order_item=item,
                ledger='legacy', order_item_quantity=3,
                prepared_units=1, unprepared_units=2,
                discarded_units=2, source='system_unpaid',
            )

    def test_discarded_equals_prepared_succeeds(self):
        """discarded = prepared → OK (all prepared units discarded)."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        disp = OrderCancellationDisposition.objects.create(
            order=order, order_item=item,
            ledger='legacy', order_item_quantity=2,
            prepared_units=2, unprepared_units=0,
            discarded_units=2, source='system_ready',
        )
        self.assertEqual(disp.discarded_units, 2)


# ═════════════════════════════════════════════════════════════════════════════
# 5. test_negative_units_rejected
# ═════════════════════════════════════════════════════════════════════════════

class NegativeUnitsTests(TestCase):

    def test_negative_prepared_rejected(self):
        """prepared < 0 → IntegrityError (DB CHECK)."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        with self.assertRaises(IntegrityError):
            OrderCancellationDisposition.objects.create(
                order=order, order_item=item,
                ledger='legacy', order_item_quantity=2,
                prepared_units=-1, unprepared_units=3,
                discarded_units=0, source='system_unpaid',
            )

    def test_negative_unprepared_rejected(self):
        """unprepared < 0 → IntegrityError (DB CHECK)."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        with self.assertRaises(IntegrityError):
            OrderCancellationDisposition.objects.create(
                order=order, order_item=item,
                ledger='legacy', order_item_quantity=2,
                prepared_units=3, unprepared_units=-1,
                discarded_units=0, source='system_unpaid',
            )

    def test_negative_discarded_rejected(self):
        """discarded < 0 → IntegrityError (DB CHECK)."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        with self.assertRaises(IntegrityError):
            OrderCancellationDisposition.objects.create(
                order=order, order_item=item,
                ledger='legacy', order_item_quantity=2,
                prepared_units=2, unprepared_units=0,
                discarded_units=-1, source='system_ready',
            )


# ═════════════════════════════════════════════════════════════════════════════
# 6. test_invalid_source_rejected
# ═════════════════════════════════════════════════════════════════════════════

class InvalidSourceTests(TestCase):

    def test_invalid_source_rejected(self):
        """source not in valid choices → IntegrityError (DB CHECK)."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        with self.assertRaises(IntegrityError):
            OrderCancellationDisposition.objects.create(
                order=order, order_item=item,
                ledger='legacy', order_item_quantity=2,
                prepared_units=0, unprepared_units=2,
                discarded_units=0, source='invalid_source',
            )


# ═════════════════════════════════════════════════════════════════════════════
# 7. test_invalid_ledger_rejected
# ═════════════════════════════════════════════════════════════════════════════

class InvalidLedgerTests(TestCase):

    def test_invalid_ledger_rejected(self):
        """ledger not in valid choices → IntegrityError (DB CHECK)."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        with self.assertRaises(IntegrityError):
            OrderCancellationDisposition.objects.create(
                order=order, order_item=item,
                ledger='bogus', order_item_quantity=2,
                prepared_units=0, unprepared_units=2,
                discarded_units=0, source='system_unpaid',
            )


# ═════════════════════════════════════════════════════════════════════════════
# 8. test_fk_integrity
# ═════════════════════════════════════════════════════════════════════════════

class FKIntegrityTests(TestCase):
    """
    Verify that the PROTECT foreign keys prevent deletion of referenced
    rows while dispositions exist.

    Order has a pre_delete signal that blocks ALL hard deletes, so the
    PROTECT on the `order` FK is redundant for Order but still important
    as a DB-level backstop.  OrderItem has no pre_delete signal, so the
    PROTECT on `order_item` is the primary guard.
    """

    def test_order_item_protect_prevents_deletion(self):
        """Deleting an OrderItem with a disposition → blocked by PROTECT."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        _valid_disposition(order, item)
        with self.assertRaises(IntegrityError):
            item.delete()

    def test_order_protect_prevents_deletion(self):
        """Deleting an Order with a disposition → blocked by PROTECT (or
        by the pre_delete signal, which fires first)."""
        prod = _product()
        order = _order()
        item = _order_item(order, prod, quantity=2)
        _valid_disposition(order, item)
        # The pre_delete signal raises ValidationError before the PROTECT
        # constraint can fire, but the PROTECT FK is still the DB-level
        # backstop.  Either way, the order cannot be deleted.
        from django.core.exceptions import ValidationError
        with self.assertRaises((IntegrityError, ValidationError)):
            order.delete()


# ═════════════════════════════════════════════════════════════════════════════
# Phase 6A Stage 6A-2 — Cancellation service tests
# ═════════════════════════════════════════════════════════════════════════════

from apps.orders.services import (
    deduct_inventory_for_order,
    on_status_changed,
    place_order_inventory,
    restore_inventory_for_order,
    DispositionRequired,
)
from apps.inventory.models import InventoryLog
from django.db import transaction as db_transaction


def _deducted_order(prod, qty=2, stock=10):
    """Create an order with stock already deducted (legacy)."""
    prod.stock_quantity = stock
    prod.save(update_fields=['stock_quantity'])
    order = _order()
    item = OrderItem.objects.create(
        order=order, product=prod,
        product_name=prod.name,
        category_name=prod.category.name,
        packaging_eligible=False,
        size='none', quantity=qty,
        unit_price=_UNIT_PRICE,
        subtotal=_UNIT_PRICE * qty,
        inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
    )
    prod.stock_quantity -= qty
    prod.save(update_fields=['stock_quantity'])
    order.stock_deducted = True
    order.save(update_fields=['stock_deducted'])
    return order, item


# ── place_order_inventory status guard ───────────────────────────────────────

class PlaceOrderInventoryStatusGuardTests(TestCase):

    def setUp(self):
        self.prod = _product()

    def test_place_order_inventory_rejects_cancelled_order(self):
        """Status guard prevents re-deduction on cancelled orders."""
        order = _order()
        order.status = 'cancelled'
        order.save(update_fields=['status'])
        with db_transaction.atomic():
            with self.assertRaises(ValueError) as ctx:
                place_order_inventory(order.pk)
        self.assertIn('cancelled', str(ctx.exception))
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 10)

    def test_place_order_inventory_rejects_completed_order(self):
        order = _order()
        order.status = 'completed'
        order.save(update_fields=['status'])
        with db_transaction.atomic():
            with self.assertRaises(ValueError):
                place_order_inventory(order.pk)

    def test_place_order_inventory_rejects_ready_order(self):
        order = _order()
        order.status = 'ready'
        order.save(update_fields=['status'])
        with db_transaction.atomic():
            with self.assertRaises(ValueError):
                place_order_inventory(order.pk)

    def test_place_order_inventory_accepts_awaiting_payment(self):
        """Valid awaiting_payment order still works (regression)."""
        order = _order()
        _order_item(order, self.prod, quantity=2)
        with db_transaction.atomic():
            result = place_order_inventory(order.pk)
        self.assertTrue(result['stock_deducted'])


# ── deduct_inventory_for_order status guard + transaction ─────────────────────

class DeductInventoryStatusGuardTests(TestCase):

    def setUp(self):
        self.prod = _product()

    def test_deduct_inventory_for_order_rejects_cancelled_order(self):
        """Direct call on cancelled order with stock_deducted=False → ValueError."""
        order = _order()
        _order_item(order, self.prod, quantity=2)
        order.status = 'cancelled'
        order.stock_deducted = False
        order.save()
        with self.assertRaises(ValueError) as ctx:
            deduct_inventory_for_order(order)
        self.assertIn('cancelled', str(ctx.exception))
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 10)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_deduct_inventory_for_order_rejects_completed_order(self):
        order = _order()
        _order_item(order, self.prod, quantity=2)
        order.status = 'completed'
        order.stock_deducted = False
        order.save()
        with self.assertRaises(ValueError):
            deduct_inventory_for_order(order)

    def test_deduct_inventory_for_order_rejects_stale_cancelled_order(self):
        """Stale in-memory instance (status='awaiting_payment') but DB row is
        cancelled → ValueError from fresh locked read, no stock change."""
        order = _order()
        _order_item(order, self.prod, quantity=2)
        # Keep a stale in-memory instance
        stale_order = Order.objects.get(pk=order.pk)
        # Change DB row to cancelled
        order.status = 'cancelled'
        order.stock_deducted = False
        order.save()
        # stale_order still has status='awaiting_payment' in memory
        self.assertEqual(stale_order.status, 'awaiting_payment')
        with self.assertRaises(ValueError) as ctx:
            deduct_inventory_for_order(stale_order)
        self.assertIn('cancelled', str(ctx.exception))
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 10)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_deduct_inventory_for_order_accepts_awaiting_payment(self):
        """Valid awaiting_payment order still deducts correctly (regression)."""
        order = _order()
        _order_item(order, self.prod, quantity=2)
        deduct_inventory_for_order(order)
        self.prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 8)
        self.assertTrue(order.stock_deducted)


# ── Restoration mapping (Issue 1 from plan) ──────────────────────────────────

class RestorationMappingTests(TestCase):
    """Verify _apply_inventory_for_order correctly handles item_quantities."""

    def setUp(self):
        self.prod_a = _product()
        self.prod_b = _product()

    def _deducted_order_two_items(self, qty_a=3, qty_b=2):
        order = _order()
        item_a = OrderItem.objects.create(
            order=order, product=self.prod_a,
            product_name=self.prod_a.name,
            category_name=self.prod_a.category.name,
            packaging_eligible=False, size='none', quantity=qty_a,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE * qty_a,
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        item_b = OrderItem.objects.create(
            order=order, product=self.prod_b,
            product_name=self.prod_b.name,
            category_name=self.prod_b.category.name,
            packaging_eligible=False, size='none', quantity=qty_b,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE * qty_b,
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        self.prod_a.stock_quantity -= qty_a
        self.prod_a.save()
        self.prod_b.stock_quantity -= qty_b
        self.prod_b.save()
        order.stock_deducted = True
        order.save()
        return order, item_a, item_b

    def test_none_mapping_restores_full(self):
        """item_quantities=None: each item restores full quantity (existing)."""
        order, item_a, item_b = self._deducted_order_two_items()
        restore_inventory_for_order(order)
        self.prod_a.refresh_from_db()
        self.prod_b.refresh_from_db()
        self.assertEqual(self.prod_a.stock_quantity, 10)
        self.assertEqual(self.prod_b.stock_quantity, 10)

    def test_zero_quantity_skips_item(self):
        """Item with unprepared=0: no restore_stock, no InventoryLog."""
        order, item_a, item_b = self._deducted_order_two_items()
        stock_a_before = self.prod_a.stock_quantity
        restore_inventory_for_order(
            order, item_quantities={item_a.pk: 0, item_b.pk: 2}
        )
        self.prod_a.refresh_from_db()
        self.prod_b.refresh_from_db()
        self.assertEqual(self.prod_a.stock_quantity, stock_a_before)  # unchanged
        self.assertEqual(self.prod_b.stock_quantity, 10)  # restored 2
        # Only one InventoryLog for prod_b, not prod_a
        logs = InventoryLog.objects.filter(action='adjustment')
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.first().product, self.prod_b)

    def test_omitted_key_treated_as_zero(self):
        """Item pk not in item_quantities: skipped, NOT restored to full."""
        order, item_a, item_b = self._deducted_order_two_items()
        stock_a_before = self.prod_a.stock_quantity
        restore_inventory_for_order(
            order, item_quantities={item_b.pk: 2}  # item_a omitted
        )
        self.prod_a.refresh_from_db()
        self.assertEqual(self.prod_a.stock_quantity, stock_a_before)  # unchanged

    def test_empty_mapping_skips_all(self):
        """Empty item_quantities dict: all items skipped, no stock change."""
        order, item_a, item_b = self._deducted_order_two_items()
        stock_a = self.prod_a.stock_quantity
        stock_b = self.prod_b.stock_quantity
        restore_inventory_for_order(order, item_quantities={})
        self.prod_a.refresh_from_db()
        self.prod_b.refresh_from_db()
        self.assertEqual(self.prod_a.stock_quantity, stock_a)
        self.assertEqual(self.prod_b.stock_quantity, stock_b)

    def test_mixed_zero_and_positive_quantities(self):
        """One item 3, one item 0: item_a restored 3, item_b skipped."""
        order, item_a, item_b = self._deducted_order_two_items(qty_a=3, qty_b=2)
        restore_inventory_for_order(
            order, item_quantities={item_a.pk: 3, item_b.pk: 0}
        )
        self.prod_a.refresh_from_db()
        self.prod_b.refresh_from_db()
        self.assertEqual(self.prod_a.stock_quantity, 10)  # restored 3
        stock_b_before = 10 - 2  # was 10, deducted 2
        self.assertEqual(self.prod_b.stock_quantity, stock_b_before)  # unchanged


# ── Cancellation service: awaiting_payment ───────────────────────────────────

class CancelAwaitingPaymentTests(TestCase):

    def setUp(self):
        self.prod = _product(stock=10)

    def test_cancel_awaiting_restores_all_legacy(self):
        """All legacy units restored, stock_deducted=False."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled')
        self.prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 10)
        self.assertFalse(order.stock_deducted)

    def test_cancel_awaiting_creates_disposition_rows(self):
        """One disposition row per order item, all with prepared=0."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled')
        disps = OrderCancellationDisposition.objects.filter(order=order)
        self.assertEqual(disps.count(), 1)
        disp = disps.first()
        self.assertEqual(disp.prepared_units, 0)
        self.assertEqual(disp.unprepared_units, 2)
        self.assertEqual(disp.discarded_units, 0)
        self.assertEqual(disp.source, 'system_unpaid')

    def test_cancel_awaiting_idempotent(self):
        """Second cancel call is a no-op (same-status no-op in validator)."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled')
        # The caller sets order.status = 'cancelled' — simulate it.
        order.status = 'cancelled'
        order.save(update_fields=['status'])
        # Second call: same-status no-op returns without error.
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled')
        # No duplicate dispositions
        self.assertEqual(
            OrderCancellationDisposition.objects.filter(order=order).count(), 1
        )


# ── Cancellation service: preparing ─────────────────────────────────────────

class CancelPreparingTests(TestCase):

    def setUp(self):
        self.prod = _product(stock=10)

    def test_cancel_preparing_restores_only_unprepared(self):
        """Only unprepared units restored; prepared stay deducted."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'prepared_units': 1, 'discarded_units': 0}}
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        self.prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 9)  # 7 + 2 unprepared restored
        self.assertFalse(order.stock_deducted)

    def test_cancel_preparing_requires_disposition(self):
        """Missing disposition → DispositionRequired error."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        with db_transaction.atomic():
            with self.assertRaises(DispositionRequired):
                on_status_changed(order.pk, 'cancelled')

    def test_cancel_preparing_invalid_quantity_rejected(self):
        """prepared > quantity → ValueError."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'prepared_units': 5, 'discarded_units': 0}}
        with db_transaction.atomic():
            with self.assertRaises(ValueError):
                on_status_changed(order.pk, 'cancelled', disposition=disposition)

    def test_cancel_preparing_all_prepared(self):
        """All prepared → 0 restored, stock_deducted becomes False (settled)."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'prepared_units': 2, 'discarded_units': 0}}
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        self.prod.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 8)  # unchanged
        self.assertFalse(order.stock_deducted)  # settled

    def test_cancel_preparing_discarded_recorded(self):
        """Discarded units recorded in disposition row."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'prepared_units': 2, 'discarded_units': 1}}
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        disp = OrderCancellationDisposition.objects.get(order=order, order_item=item)
        self.assertEqual(disp.prepared_units, 2)
        self.assertEqual(disp.unprepared_units, 1)
        self.assertEqual(disp.discarded_units, 1)
        self.assertEqual(disp.source, 'staff_declared')

    def test_cancel_preparing_no_inventorylog_for_prepared(self):
        """No InventoryLog row for prepared/discarded units."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'prepared_units': 2, 'discarded_units': 1}}
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        # Only one adjustment log for the 1 unprepared unit restored
        logs = InventoryLog.objects.filter(action='adjustment')
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.first().quantity_change, 1)


# ── Cancellation service: ready ──────────────────────────────────────────────

class CancelReadyTests(TestCase):

    def setUp(self):
        self.prod = _product(stock=10)

    def test_cancel_ready_no_restore(self):
        """No legacy stock restored for ready-order cancellation."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'ready'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'discarded_units': 0}}
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 8)  # unchanged

    def test_cancel_ready_settles_stock_deducted(self):
        """stock_deducted becomes False without any stock restoration."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'ready'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'discarded_units': 0}}
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        order.refresh_from_db()
        self.assertFalse(order.stock_deducted)

    def test_cancel_ready_no_inventorylog_written(self):
        """No InventoryLog rows written for ready-order cancellation."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'ready'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'discarded_units': 0}}
        logs_before = InventoryLog.objects.count()
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        self.assertEqual(InventoryLog.objects.count(), logs_before)

    def test_cancel_ready_discarded_recorded(self):
        """Staff enters discarded > 0; recorded correctly."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'ready'
        order.save(update_fields=['status'])
        disposition = {item.pk: {'discarded_units': 1}}
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        disp = OrderCancellationDisposition.objects.get(order=order, order_item=item)
        self.assertEqual(disp.prepared_units, 3)  # all prepared
        self.assertEqual(disp.unprepared_units, 0)
        self.assertEqual(disp.discarded_units, 1)
        self.assertEqual(disp.source, 'system_ready')

    def test_cancel_ready_requires_disposition(self):
        """Missing disposition → DispositionRequired error."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'ready'
        order.save(update_fields=['status'])
        with db_transaction.atomic():
            with self.assertRaises(DispositionRequired):
                on_status_changed(order.pk, 'cancelled')


# ── Cancellation service: ingredient-only orders ────────────────────────────

class CancelIngredientOnlyTests(TestCase):

    def setUp(self):
        self.prod = Product.objects.create(
            category=_category(), name=f'Ingr6A_{_uid()}',
            price='60.00', stock_quantity=50,
            inventory_tracking_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )

    def test_ingredient_only_cancel_proceeds_with_stock_deducted_false(self):
        """Ingredient-only order: stock_deducted=False, coordinator still
        creates dispositions and settles."""
        order = _order()
        item = OrderItem.objects.create(
            order=order, product=self.prod,
            product_name=self.prod.name,
            category_name=self.prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE * 2,
            inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
        )
        # stock_deducted stays False for ingredient-only orders
        self.assertFalse(order.stock_deducted)
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled')
        disps = OrderCancellationDisposition.objects.filter(order=order)
        self.assertEqual(disps.count(), 1)
        disp = disps.first()
        self.assertEqual(disp.prepared_units, 0)
        self.assertEqual(disp.unprepared_units, 2)
        self.assertEqual(disp.discarded_units, 0)


# ── Cancellation service: mixed orders ───────────────────────────────────────

class CancelMixedOrderTests(TestCase):

    def test_mixed_cancel_legacy_restored_ingredient_noop(self):
        """Legacy restored by disposition, ingredient lines no-op."""
        legacy_prod = _product(stock=10)
        ingr_prod = Product.objects.create(
            category=_category(), name=f'IngrMix_{_uid()}',
            price='65.00', stock_quantity=50,
            inventory_tracking_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        order = _order()
        legacy_item = OrderItem.objects.create(
            order=order, product=legacy_prod,
            product_name=legacy_prod.name,
            category_name=legacy_prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE * 2,
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        ingr_item = OrderItem.objects.create(
            order=order, product=ingr_prod,
            product_name=ingr_prod.name,
            category_name=ingr_prod.category.name,
            packaging_eligible=False, size='none', quantity=1,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE,
            inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
        )
        legacy_prod.stock_quantity -= 2
        legacy_prod.save()
        order.stock_deducted = True
        order.save()
        disposition = {
            legacy_item.pk: {'prepared_units': 0, 'discarded_units': 0},
            ingr_item.pk: {'prepared_units': 0, 'discarded_units': 0},
        }
        with db_transaction.atomic():
            on_status_changed(order.pk, 'cancelled', disposition=disposition)
        legacy_prod.refresh_from_db()
        self.assertEqual(legacy_prod.stock_quantity, 10)  # restored
        disps = OrderCancellationDisposition.objects.filter(order=order)
        self.assertEqual(disps.count(), 2)  # both items get dispositions


# ═════════════════════════════════════════════════════════════════════════════
# Phase 6A Stage 6A-3 — Cashier cancellation workflow (view integration)
# ═════════════════════════════════════════════════════════════════════════════

from django.urls import reverse


def _cashier():
    return User.objects.create_user(
        username=f'cashier6A3_{_uid()}', password=_PASSWORD, role='cashier'
    )


def _post_cancel(client, order_pk, **extra_fields):
    """POST to update_order_status with status=cancelled and optional disposition fields."""
    post_data = {'status': 'cancelled'}
    post_data.update(extra_fields)
    return client.post(
        reverse('orders:update_status', args=[order_pk]),
        post_data,
    )


class CashierCancelAwaitingPaymentTests(TestCase):
    """Cashier cancels an awaiting_payment order — no disposition needed."""

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)

    def test_cashier_cancel_awaiting_restores_stock(self):
        """Cashier cancels awaiting_payment order → all stock restored."""
        response = _post_cancel(self.client, self.order.pk)
        self.assertTrue(response.json()['success'])
        self.prod.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 10)
        self.assertFalse(self.order.stock_deducted)
        self.assertEqual(self.order.status, 'cancelled')

    def test_cashier_cancel_awaiting_creates_disposition(self):
        """Disposition row created with system_unpaid source."""
        _post_cancel(self.client, self.order.pk)
        disp = OrderCancellationDisposition.objects.get(order=self.order)
        self.assertEqual(disp.source, 'system_unpaid')
        self.assertEqual(disp.prepared_units, 0)
        self.assertEqual(disp.unprepared_units, 2)

    def test_cashier_cancel_awaiting_no_disposition_post_needed(self):
        """No prepared_/discarded_ POST fields needed for awaiting_payment."""
        # Just status=cancelled — no extra fields
        response = _post_cancel(self.client, self.order.pk)
        self.assertTrue(response.json()['success'])


class CashierCancelPreparingTests(TestCase):
    """Cashier cancels a preparing order — disposition required."""

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=3, stock=10)
        self.order.status = 'preparing'
        self.order.save(update_fields=['status'])

    def test_cashier_cancel_preparing_with_valid_disposition(self):
        """Valid prepared/discarded → order cancelled, only unprepared restored."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 1,
               f'discarded_{self.item.pk}': 0}
        )
        self.assertTrue(response.json()['success'])
        self.prod.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 9)  # 7+2 unprepared restored (3-1=2)
        self.assertFalse(self.order.stock_deducted)
        self.assertEqual(self.order.status, 'cancelled')

    def test_cashier_cancel_preparing_missing_prepared_rejected(self):
        """Missing prepared_ field → error, no cancellation."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 0}
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('prepared', data['error'].lower())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'preparing')  # unchanged

    def test_cashier_cancel_preparing_missing_discarded_rejected(self):
        """Missing discarded_ field → error, no cancellation."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 1}
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('discarded', data['error'].lower())

    def test_cashier_cancel_preparing_explicit_zero_accepted(self):
        """Explicit 0 for both prepared and discarded → valid."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 0,
               f'discarded_{self.item.pk}': 0}
        )
        self.assertTrue(response.json()['success'])
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 10)  # all 3 restored

    def test_cashier_cancel_preparing_invalid_quantity_rejected(self):
        """prepared > quantity → error, no stock change."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 5,
               f'discarded_{self.item.pk}': 0}
        )
        self.assertFalse(response.json()['success'])
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 7)  # unchanged

    def test_cashier_cancel_preparing_records_disposition(self):
        """Disposition row created with staff_declared source."""
        _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 2,
               f'discarded_{self.item.pk}': 1}
        )
        disp = OrderCancellationDisposition.objects.get(order=self.order)
        self.assertEqual(disp.source, 'staff_declared')
        self.assertEqual(disp.prepared_units, 2)
        self.assertEqual(disp.unprepared_units, 1)
        self.assertEqual(disp.discarded_units, 1)


class CashierCancelReadyTests(TestCase):
    """Cashier cancels a ready order — discarded quantities required for legacy items."""

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)
        self.order.status = 'ready'
        self.order.save(update_fields=['status'])

    def test_cashier_cancel_ready_with_explicit_zero_discarded(self):
        """Explicit discarded=0 → valid, no restore, no waste."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 0}
        )
        self.assertTrue(response.json()['success'])
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 8)  # unchanged (no restore)
        self.order.refresh_from_db()
        self.assertFalse(self.order.stock_deducted)  # settled
        self.assertEqual(self.order.status, 'cancelled')

    def test_cashier_cancel_ready_missing_discarded_rejected(self):
        """Missing discarded_ field → error, no cancellation."""
        response = _post_cancel(self.client, self.order.pk)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('discarded', data['error'].lower())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'ready')  # unchanged

    def test_cashier_cancel_ready_with_discarded_recorded(self):
        """Discarded > 0 → recorded in disposition."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 1}
        )
        self.assertTrue(response.json()['success'])
        disp = OrderCancellationDisposition.objects.get(order=self.order)
        self.assertEqual(disp.prepared_units, 2)  # all prepared
        self.assertEqual(disp.unprepared_units, 0)
        self.assertEqual(disp.discarded_units, 1)
        self.assertEqual(disp.source, 'system_ready')

    def test_cashier_cancel_ready_no_inventorylog(self):
        """No InventoryLog rows written for ready-order cancellation."""
        logs_before = InventoryLog.objects.count()
        _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 0}
        )
        self.assertEqual(InventoryLog.objects.count(), logs_before)

    def test_cashier_cancel_ready_invalid_discarded_rejected(self):
        """discarded > quantity → error."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 5}
        )
        self.assertFalse(response.json()['success'])

    def test_cashier_cancel_ready_non_integer_rejected(self):
        """Non-integer discarded value → error."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 'abc'}
        )
        self.assertFalse(response.json()['success'])


class CashierCancelMixedOrderTests(TestCase):
    """Cashier cancels a mixed legacy + ingredient order."""

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.legacy_prod = _product(stock=10)
        self.ingr_prod = Product.objects.create(
            category=_category(), name=f'IngrMix6A3_{_uid()}',
            price='65.00', stock_quantity=50,
            inventory_tracking_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        self.order = _order()
        self.legacy_item = OrderItem.objects.create(
            order=self.order, product=self.legacy_prod,
            product_name=self.legacy_prod.name,
            category_name=self.legacy_prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE * 2,
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        self.ingr_item = OrderItem.objects.create(
            order=self.order, product=self.ingr_prod,
            product_name=self.ingr_prod.name,
            category_name=self.ingr_prod.category.name,
            packaging_eligible=False, size='none', quantity=1,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE,
            inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
        )
        self.legacy_prod.stock_quantity -= 2
        self.legacy_prod.save()
        self.order.stock_deducted = True
        self.order.save()

    def test_cashier_cancel_preparing_mixed_valid(self):
        """Both legacy and ingredient items get dispositions."""
        self.order.status = 'preparing'
        self.order.save(update_fields=['status'])
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.legacy_item.pk}': 1,
               f'discarded_{self.legacy_item.pk}': 0,
               f'prepared_{self.ingr_item.pk}': 0,
               f'discarded_{self.ingr_item.pk}': 0}
        )
        self.assertTrue(response.json()['success'])
        disps = OrderCancellationDisposition.objects.filter(order=self.order)
        self.assertEqual(disps.count(), 2)
        self.legacy_prod.refresh_from_db()
        self.assertEqual(self.legacy_prod.stock_quantity, 9)  # 8+1 restored

    def test_cashier_cancel_ready_mixed_ingredient_no_input(self):
        """Ready cancel: ingredient items don't need discarded input."""
        self.order.status = 'ready'
        self.order.save(update_fields=['status'])
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.legacy_item.pk}': 0}
            # No discarded input for ingredient item — not required
        )
        self.assertTrue(response.json()['success'])
        disps = OrderCancellationDisposition.objects.filter(order=self.order)
        self.assertEqual(disps.count(), 2)
        ingr_disp = disps.get(order_item=self.ingr_item)
        self.assertEqual(ingr_disp.discarded_units, 0)
        self.assertEqual(ingr_disp.source, 'system_ready')


class CashierCancelSecurityTests(TestCase):
    """Security: unauthorized users, non-staff roles."""

    def test_unauthorized_user_cannot_cancel(self):
        """Anonymous user → redirect to login (not JSON)."""
        prod = _product(stock=10)
        order, item = _deducted_order(prod, qty=2, stock=10)
        response = self.client.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )
        self.assertEqual(response.status_code, 302)  # redirect to login

    def test_kitchen_staff_cannot_cancel(self):
        """Kitchen staff role → blocked by cashier_or_admin_required."""
        ks = User.objects.create_user(
            username=f'ks6A3_{_uid()}', password=_PASSWORD, role='kitchen_staff'
        )
        self.client.force_login(ks)
        prod = _product(stock=10)
        order, item = _deducted_order(prod, qty=2, stock=10)
        response = self.client.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )
        self.assertEqual(response.status_code, 302)  # redirect (access denied)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 6A-3 Transaction Rollback Regression Test
# ═════════════════════════════════════════════════════════════════════════════

from unittest import mock


class CashierCancelTransactionRollbackTests(TestCase):
    """
    Proves that when on_status_changed() raises after making a real DB write,
    the atomic block rolls back all partial writes — no disposition rows,
    no InventoryLog entries, no stock changes, and the order status is unchanged.

    This directly tests the corrected transaction structure in update_order_status()
    where exceptions from on_status_changed() are caught *outside* atomic().
    """

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)
        # Order is awaiting_payment with stock_deducted=True

    def test_rollback_when_service_raises_after_partial_write(self):
        """
        Simulate on_status_changed() creating a disposition row and then
        raising ValueError before completing.  The view must roll back everything:
        no disposition rows, no InventoryLog entries, stock unchanged, status unchanged.
        """
        stock_before = self.prod.stock_quantity
        disps_before = OrderCancellationDisposition.objects.count()
        logs_before = InventoryLog.objects.count()

        # Patch _create_dispositions inside on_status_changed to write one
        # disposition row and then raise ValueError, simulating a partial failure
        # mid-way through the coordinator.
        original_create = __import__(
            'apps.orders.services',
            fromlist=['_create_dispositions']
        )._create_dispositions

        call_count = {'n': 0}

        def failing_create_dispositions(order, disp_data, actor=None):
            call_count['n'] += 1
            # Write the first disposition row ...
            original_create(order, disp_data, actor)
            # ... then raise to simulate a failure mid-coordinator
            raise ValueError('Simulated mid-coordinator failure for rollback test')

        with mock.patch(
            'apps.orders.services._create_dispositions',
            side_effect=failing_create_dispositions,
        ):
            response = self.client.post(
                reverse('orders:update_status', args=[self.order.pk]),
                {'status': 'cancelled'},
            )

        # The view must return an error response
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('rollback', data['error'].lower())

        # The atomic block must have rolled back everything
        self.prod.refresh_from_db()
        self.order.refresh_from_db()

        # Stock must be unchanged
        self.assertEqual(self.prod.stock_quantity, stock_before)

        # No partial disposition rows must survive
        self.assertEqual(
            OrderCancellationDisposition.objects.count(), disps_before
        )

        # No partial InventoryLog entries must survive
        self.assertEqual(InventoryLog.objects.count(), logs_before)

        # Order status must remain awaiting_payment
        self.assertEqual(self.order.status, 'awaiting_payment')

        # stock_deducted must remain True
        self.assertTrue(self.order.stock_deducted)

        # Confirm the patched function was actually called
        self.assertEqual(call_count['n'], 1)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 6A Stage 6A-4 — Admin Cancellation Workflow
# ═════════════════════════════════════════════════════════════════════════════

from django.contrib.auth import get_user_model as _get_user_model


def _superuser():
    User = _get_user_model()
    return User.objects.create_superuser(
        username=f'su6A4_{_uid()}',
        password=_PASSWORD,
        email=f'su6A4_{_uid()}@test.com',
    )


def _admin_cancel_url(order_pk):
    return f'/admin/orders/order/{order_pk}/cancel/'


def _admin_change_url(order_pk):
    return f'/admin/orders/order/{order_pk}/change/'


class AdminCancelAwaitingPaymentTests(TestCase):
    """Admin cancels an awaiting_payment order via the intermediate page."""

    def setUp(self):
        self.su = _superuser()
        self.client.force_login(self.su)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)

    def test_admin_cancel_awaiting_get_renders_page(self):
        """GET the cancel page — renders without errors."""
        response = self.client.get(_admin_cancel_url(self.order.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order.order_number)

    def test_admin_cancel_awaiting_post_no_input_needed(self):
        """POST with no item inputs → order cancelled, stock restored."""
        response = self.client.post(
            _admin_cancel_url(self.order.pk), {}, follow=True
        )
        self.order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelled')
        self.assertEqual(self.prod.stock_quantity, 10)
        self.assertFalse(self.order.stock_deducted)

    def test_admin_cancel_awaiting_creates_disposition(self):
        """system_unpaid disposition rows created."""
        self.client.post(_admin_cancel_url(self.order.pk), {})
        disp = OrderCancellationDisposition.objects.get(order=self.order)
        self.assertEqual(disp.source, 'system_unpaid')
        self.assertEqual(disp.prepared_units, 0)
        self.assertEqual(disp.unprepared_units, 2)


class AdminCancelPreparingTests(TestCase):
    """Admin cancels a preparing order via the intermediate page."""

    def setUp(self):
        self.su = _superuser()
        self.client.force_login(self.su)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=3, stock=10)
        self.order.status = 'preparing'
        self.order.save(update_fields=['status'])

    def test_admin_cancel_preparing_get_renders_form(self):
        """GET shows prepared + discarded inputs."""
        response = self.client.get(_admin_cancel_url(self.order.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'prepared_{self.item.pk}')
        self.assertContains(response, f'discarded_{self.item.pk}')

    def test_admin_cancel_preparing_valid_disposition(self):
        """Valid prepared + discarded → partial restore."""
        response = self.client.post(
            _admin_cancel_url(self.order.pk),
            {f'prepared_{self.item.pk}': 1, f'discarded_{self.item.pk}': 0},
            follow=True,
        )
        self.order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelled')
        self.assertEqual(self.prod.stock_quantity, 9)  # 7+2 unprepared restored

    def test_admin_cancel_preparing_missing_prepared_rejected(self):
        """Missing prepared_ → error, order unchanged."""
        response = self.client.post(
            _admin_cancel_url(self.order.pk),
            {f'discarded_{self.item.pk}': 0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Missing prepared')
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'preparing')

    def test_admin_cancel_preparing_missing_discarded_rejected(self):
        """Missing discarded_ → error, order unchanged."""
        response = self.client.post(
            _admin_cancel_url(self.order.pk),
            {f'prepared_{self.item.pk}': 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Missing discarded')
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'preparing')

    def test_admin_cancel_preparing_explicit_zero_accepted(self):
        """Explicit zero for prepared and discarded → all restored."""
        self.client.post(
            _admin_cancel_url(self.order.pk),
            {f'prepared_{self.item.pk}': 0, f'discarded_{self.item.pk}': 0},
            follow=True,
        )
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 10)

    def test_admin_cancel_preparing_invalid_prepared_rejected(self):
        """prepared > quantity → error, no stock change."""
        response = self.client.post(
            _admin_cancel_url(self.order.pk),
            {f'prepared_{self.item.pk}': 5, f'discarded_{self.item.pk}': 0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'must be 0 to')
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 7)  # unchanged


class AdminCancelReadyTests(TestCase):
    """Admin cancels a ready order via the intermediate page."""

    def setUp(self):
        self.su = _superuser()
        self.client.force_login(self.su)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)
        self.order.status = 'ready'
        self.order.save(update_fields=['status'])

    def test_admin_cancel_ready_get_shows_discarded_input(self):
        """GET shows discarded-only inputs for legacy items."""
        response = self.client.get(_admin_cancel_url(self.order.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'discarded_{self.item.pk}')
        self.assertNotContains(response, f'prepared_{self.item.pk}')

    def test_admin_cancel_ready_explicit_zero_accepted(self):
        """Explicit discarded=0 → no restore, settled."""
        self.client.post(
            _admin_cancel_url(self.order.pk),
            {f'discarded_{self.item.pk}': 0},
            follow=True,
        )
        self.prod.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 8)  # unchanged
        self.assertEqual(self.order.status, 'cancelled')

    def test_admin_cancel_ready_missing_discarded_rejected(self):
        """Missing discarded_ → error, status unchanged."""
        response = self.client.post(_admin_cancel_url(self.order.pk), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Missing discarded')
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'ready')

    def test_admin_cancel_ready_no_restore(self):
        """No InventoryLog for ready-order cancellation."""
        logs_before = InventoryLog.objects.count()
        self.client.post(
            _admin_cancel_url(self.order.pk),
            {f'discarded_{self.item.pk}': 0},
            follow=True,
        )
        self.assertEqual(InventoryLog.objects.count(), logs_before)


class AdminCancelBypassPreventionTests(TestCase):
    """Direct cancellation via ordinary Admin change form is rejected."""

    def setUp(self):
        self.su = _superuser()
        self.client.force_login(self.su)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)

    def test_admin_direct_cancel_via_change_form_blocked(self):
        """POSTing status=cancelled on the change form reverts the change."""
        # Simulate what the admin change form would POST
        response = self.client.post(
            _admin_change_url(self.order.pk),
            {
                'status': 'cancelled',
                'customer_name': self.order.customer_name,
                'order_type': self.order.order_type,
                '_save': '1',
                # minimal required fields
                'items-TOTAL_FORMS': '0',
                'items-INITIAL_FORMS': '0',
                'items-MIN_NUM_FORMS': '0',
                'items-MAX_NUM_FORMS': '1000',
            },
        )
        self.order.refresh_from_db()
        # Status must NOT have changed to cancelled
        self.assertNotEqual(self.order.status, 'cancelled')
        self.prod.refresh_from_db()
        # Stock must be unchanged
        self.assertEqual(self.prod.stock_quantity, 8)

    def test_admin_cancel_page_inaccessible_to_non_admin(self):
        """Non-admin cashier cannot access the admin cancel page."""
        cashier = _cashier()
        self.client.force_login(cashier)
        response = self.client.get(_admin_cancel_url(self.order.pk))
        # Admin wraps the view — non-staff get redirected to admin login
        self.assertIn(response.status_code, [302, 403])

    def test_admin_cancel_page_inaccessible_to_anonymous(self):
        """Anonymous user cannot access the admin cancel page."""
        self.client.logout()
        response = self.client.get(_admin_cancel_url(self.order.pk))
        self.assertIn(response.status_code, [302, 403])


class AdminCancelTransactionRollbackTests(TestCase):
    """Admin cancel: partial writes roll back when on_status_changed raises."""

    def setUp(self):
        self.su = _superuser()
        self.client.force_login(self.su)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)

    def test_admin_cancel_rollback_on_service_error(self):
        """Admin cancel: service raises mid-write → no partial commits."""
        stock_before = self.prod.stock_quantity
        disps_before = OrderCancellationDisposition.objects.count()
        logs_before = InventoryLog.objects.count()

        original_create = __import__(
            'apps.orders.services',
            fromlist=['_create_dispositions'],
        )._create_dispositions

        def failing_create(order, disp_data, actor=None):
            original_create(order, disp_data, actor)
            raise ValueError('Admin cancel rollback test')

        with mock.patch(
            'apps.orders.services._create_dispositions',
            side_effect=failing_create,
        ):
            response = self.client.post(
                _admin_cancel_url(self.order.pk), {}, follow=False
            )

        # Should re-render the page with an error (200) or redirect with message
        self.order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertNotEqual(self.order.status, 'cancelled')
        self.assertEqual(self.prod.stock_quantity, stock_before)
        self.assertEqual(OrderCancellationDisposition.objects.count(), disps_before)
        self.assertEqual(InventoryLog.objects.count(), logs_before)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 6A Stage 6A-5 — Template and UI Tests
# ═════════════════════════════════════════════════════════════════════════════
#
# Verifies the UI layer for both the cashier (order-detail page) and admin
# (intermediate cancellation page) cancellation workflows.
#
# Scope:
#   - order_detail page renders disposition inputs for preparing / ready orders
#   - order_detail page requires NO extra input for awaiting_payment cancels
#   - order_list page has NO cancel button in table rows
#   - admin intermediate page renders correct inputs per order status
#   - Server-side validation rejects missing and invalid field values
#   - Explicit zero is accepted; omitted field is rejected
#   - Invalid submissions leave order, inventory, and disposition unchanged
# ═════════════════════════════════════════════════════════════════════════════


# ── Helpers ──────────────────────────────────────────────────────────────────

def _order_detail_url(order_pk):
    return reverse('orders:order_detail', args=[order_pk])


def _order_list_url():
    return reverse('orders:order_list')


class OrderDetailUIPreparingTests(TestCase):
    """order_detail page renders correct disposition inputs for preparing orders."""

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=3, stock=10)
        self.order.status = 'preparing'
        self.order.save(update_fields=['status'])

    def test_order_detail_preparing_contains_prepared_field_name(self):
        """GET order-detail for a preparing order — page contains prepared_<pk> field name."""
        response = self.client.get(_order_detail_url(self.order.pk))
        self.assertEqual(response.status_code, 200)
        # The JS embeds item pk so it can build prepared_<pk> inputs dynamically.
        # The template must include the item pk in the embedded items array.
        content = response.content.decode()
        self.assertIn(str(self.item.pk), content)

    def test_order_detail_preparing_contains_cancel_button(self):
        """GET order-detail for a preparing order — Cancel Order button is present."""
        response = self.client.get(_order_detail_url(self.order.pk))
        content = response.content.decode()
        self.assertIn('cancelOrder', content)
        self.assertIn('Cancel Order', content)

    def test_order_detail_preparing_embeds_order_status(self):
        """order_detail for preparing order embeds status='preparing' in page."""
        response = self.client.get(_order_detail_url(self.order.pk))
        content = response.content.decode()
        self.assertIn('preparing', content)

    def test_order_detail_preparing_embeds_item_quantity(self):
        """Order item quantity is embedded in the page for JS to use."""
        response = self.client.get(_order_detail_url(self.order.pk))
        content = response.content.decode()
        # Item quantity (3) must appear in the embedded JS items array
        self.assertIn('quantity', content)
        self.assertIn('3', content)

    def test_order_detail_preparing_cancel_post_with_valid_disposition(self):
        """POST cancel with valid prepared + discarded → order cancelled, stock restored."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 1, f'discarded_{self.item.pk}': 0},
        )
        self.assertTrue(response.json()['success'])
        self.order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelled')
        self.assertEqual(self.prod.stock_quantity, 9)  # 7 + 2 unprepared restored

    def test_order_detail_preparing_cancel_missing_prepared_rejected(self):
        """POST cancel from preparing with only discarded_ → error returned."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 0},
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('prepared', data['error'].lower())

    def test_order_detail_preparing_cancel_missing_discarded_rejected(self):
        """POST cancel from preparing with only prepared_ → error returned."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 1},
        )
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('discarded', data['error'].lower())

    def test_order_detail_preparing_cancel_explicit_zero_accepted(self):
        """POST cancel from preparing with prepared=0 discarded=0 → success."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 0, f'discarded_{self.item.pk}': 0},
        )
        self.assertTrue(response.json()['success'])
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 10)  # all 3 restored

    def test_order_detail_preparing_cancel_invalid_leaves_order_unchanged(self):
        """Invalid POST (prepared > quantity) → order remains preparing, no stock change."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'prepared_{self.item.pk}': 99, f'discarded_{self.item.pk}': 0},
        )
        self.assertFalse(response.json()['success'])
        self.order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertEqual(self.order.status, 'preparing')
        self.assertEqual(self.prod.stock_quantity, 7)  # unchanged (10 - 3 deducted)


class OrderDetailUIReadyTests(TestCase):
    """order_detail page renders correct disposition inputs for ready orders."""

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)
        self.order.status = 'ready'
        self.order.save(update_fields=['status'])

    def test_order_detail_ready_contains_cancel_button(self):
        """GET order-detail for a ready order — Cancel Order button is present."""
        response = self.client.get(_order_detail_url(self.order.pk))
        content = response.content.decode()
        self.assertIn('cancelOrder', content)
        self.assertIn('Cancel Order', content)

    def test_order_detail_ready_embeds_order_status(self):
        """order_detail for ready order embeds status='ready' in page."""
        response = self.client.get(_order_detail_url(self.order.pk))
        content = response.content.decode()
        self.assertIn('ready', content)

    def test_order_detail_ready_embeds_item_as_legacy(self):
        """Legacy item is flagged isLegacy=true in embedded JS items array."""
        response = self.client.get(_order_detail_url(self.order.pk))
        content = response.content.decode()
        self.assertIn('isLegacy', content)
        self.assertIn('true', content)

    def test_order_detail_ready_cancel_with_discarded_zero_accepted(self):
        """POST cancel from ready with discarded=0 → success, no stock restored."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 0},
        )
        self.assertTrue(response.json()['success'])
        self.prod.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.prod.stock_quantity, 8)  # unchanged (all prepared)
        self.assertEqual(self.order.status, 'cancelled')

    def test_order_detail_ready_cancel_missing_discarded_rejected(self):
        """POST cancel from ready with no discarded_ field → error."""
        response = _post_cancel(self.client, self.order.pk)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('discarded', data['error'].lower())

    def test_order_detail_ready_cancel_invalid_leaves_order_unchanged(self):
        """Invalid POST (discarded > quantity) → order remains ready, no stock change."""
        response = _post_cancel(
            self.client, self.order.pk,
            **{f'discarded_{self.item.pk}': 99},
        )
        self.assertFalse(response.json()['success'])
        self.order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertEqual(self.order.status, 'ready')
        self.assertEqual(self.prod.stock_quantity, 8)  # unchanged


class OrderDetailUIAwaitingPaymentTests(TestCase):
    """order_detail for awaiting_payment — no disposition input needed."""

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod = _product(stock=10)
        self.order, self.item = _deducted_order(self.prod, qty=2, stock=10)
        # order starts as awaiting_payment (default from _deducted_order)

    def test_order_detail_awaiting_contains_cancel_button(self):
        """GET order-detail for an awaiting_payment order — Cancel Order button present."""
        response = self.client.get(_order_detail_url(self.order.pk))
        content = response.content.decode()
        self.assertIn('cancelOrder', content)
        self.assertIn('Cancel Order', content)

    def test_order_detail_awaiting_cancel_no_extra_post_fields_needed(self):
        """POST cancel from awaiting_payment with no disposition fields → success."""
        response = _post_cancel(self.client, self.order.pk)
        self.assertTrue(response.json()['success'])
        self.order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelled')
        self.assertEqual(self.prod.stock_quantity, 10)  # all restored

    def test_order_detail_awaiting_cancel_creates_system_unpaid_disposition(self):
        """Cancelling from awaiting_payment creates system_unpaid disposition."""
        _post_cancel(self.client, self.order.pk)
        disp = OrderCancellationDisposition.objects.get(order=self.order)
        self.assertEqual(disp.source, 'system_unpaid')
        self.assertEqual(disp.prepared_units, 0)
        self.assertEqual(disp.unprepared_units, 2)


class OrderListNoCancelButtonTests(TestCase):
    """The order-list page must NOT show a cancel button in table rows.

    Cancellation is initiated from the order-detail page only (Option B per plan §6.5).
    The ``cancelOrder()`` function in order_list.html is used for real-time status
    updates pushed by the server — it does NOT render a cancel button.
    """

    def setUp(self):
        self.cashier = _cashier()
        self.client.force_login(self.cashier)
        self.prod = _product(stock=10)
        # Create one order in each active status to ensure no cancel button appears
        self.order_ap, _ = _deducted_order(self.prod, qty=1, stock=10)
        self.order_prep, _ = _deducted_order(self.prod, qty=1, stock=9)
        self.order_prep.status = 'preparing'
        self.order_prep.save(update_fields=['status'])

    def test_order_list_has_no_cancel_button_in_rows(self):
        """GET order-list — no Cancel Order button rendered inside table rows."""
        response = self.client.get(_order_list_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # The page must not render an inline cancel button tied to a specific order.
        # 'cancelOrder(' in an onclick attribute is the signal — the list page
        # must not have any such inline onclick calling cancelOrder with an order pk.
        self.assertNotIn('onclick="cancelOrder(', content)

    def test_order_list_does_not_show_cancel_order_text_in_rows(self):
        """GET order-list — 'Cancel Order' text does not appear as a row action."""
        response = self.client.get(_order_list_url())
        content = response.content.decode()
        # The text 'Cancel Order' should not appear in the list page at all
        # (it lives only on the order-detail page).
        self.assertNotIn('Cancel Order', content)


class AdminCancelTemplateRenderingTests(TestCase):
    """Admin intermediate cancellation page renders correct inputs per status."""

    def setUp(self):
        self.su = _superuser()
        self.client.force_login(self.su)
        self.prod = _product(stock=10)

    def test_admin_cancel_awaiting_shows_no_item_inputs(self):
        """GET admin cancel for awaiting_payment — no prepared/discarded inputs rendered."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        response = self.client.get(_admin_cancel_url(order.pk))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # No per-item number inputs for awaiting_payment
        self.assertNotIn(f'prepared_{item.pk}', content)
        self.assertNotIn(f'discarded_{item.pk}', content)
        # Confirm the page describes the awaiting_payment behaviour —
        # the template's awaiting_payment branch says "No further input is required"
        self.assertIn('no further input is required', content.lower())

    def test_admin_cancel_awaiting_shows_order_number(self):
        """Admin cancel page displays the order number."""
        order, _ = _deducted_order(self.prod, qty=2, stock=10)
        response = self.client.get(_admin_cancel_url(order.pk))
        self.assertContains(response, order.order_number)

    def test_admin_cancel_preparing_shows_both_inputs_per_item(self):
        """GET admin cancel for preparing — prepared_ and discarded_ inputs shown."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        response = self.client.get(_admin_cancel_url(order.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'prepared_{item.pk}')
        self.assertContains(response, f'discarded_{item.pk}')

    def test_admin_cancel_preparing_shows_ordered_quantity(self):
        """Admin cancel page shows the ordered quantity for each item."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        response = self.client.get(_admin_cancel_url(order.pk))
        # Item quantity (3) should appear in the table
        self.assertContains(response, '3')

    def test_admin_cancel_ready_shows_discarded_only(self):
        """GET admin cancel for ready — only discarded_ input shown (not prepared_)."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'ready'
        order.save(update_fields=['status'])
        response = self.client.get(_admin_cancel_url(order.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'discarded_{item.pk}')
        self.assertNotContains(response, f'prepared_{item.pk}')

    def test_admin_cancel_ready_ingredient_item_shows_dash_not_input(self):
        """GET admin cancel for ready order with ingredient item — no discarded_ input for it."""
        ingr_prod = Product.objects.create(
            category=_category(), name=f'IngrUI_{_uid()}',
            price='65.00', stock_quantity=50,
            inventory_tracking_mode=Product.INVENTORY_TRACKING_INGREDIENT,
        )
        order = _order()
        legacy_item = OrderItem.objects.create(
            order=order, product=self.prod,
            product_name=self.prod.name,
            category_name=self.prod.category.name,
            packaging_eligible=False, size='none', quantity=2,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE * 2,
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        ingr_item = OrderItem.objects.create(
            order=order, product=ingr_prod,
            product_name=ingr_prod.name,
            category_name=ingr_prod.category.name,
            packaging_eligible=False, size='none', quantity=1,
            unit_price=_UNIT_PRICE, subtotal=_UNIT_PRICE,
            inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
        )
        self.prod.stock_quantity -= 2
        self.prod.save()
        order.stock_deducted = True
        order.status = 'ready'
        order.save()

        response = self.client.get(_admin_cancel_url(order.pk))
        self.assertEqual(response.status_code, 200)
        # Legacy item gets a discarded_ input; ingredient item does not
        self.assertContains(response, f'discarded_{legacy_item.pk}')
        self.assertNotContains(response, f'discarded_{ingr_item.pk}')

    def test_admin_cancel_shows_confirm_button(self):
        """Admin cancel page has a Confirm Cancellation submit button."""
        order, _ = _deducted_order(self.prod, qty=2, stock=10)
        response = self.client.get(_admin_cancel_url(order.pk))
        content = response.content.decode()
        self.assertIn('Confirm Cancellation', content)

    def test_admin_cancel_shows_go_back_link(self):
        """Admin cancel page has a Go Back link to the change page."""
        order, _ = _deducted_order(self.prod, qty=2, stock=10)
        response = self.client.get(_admin_cancel_url(order.pk))
        content = response.content.decode()
        self.assertIn('Go Back', content)

    def test_admin_cancel_already_cancelled_redirects(self):
        """GET admin cancel for an already-cancelled order → redirect, no crash."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'cancelled'
        order.stock_deducted = False
        order.save()
        response = self.client.get(_admin_cancel_url(order.pk))
        # Should redirect back to the change page, not 500
        self.assertIn(response.status_code, [302, 200])

    def test_admin_cancel_preparing_missing_prepared_shows_error(self):
        """POST admin cancel for preparing with missing prepared_ → error on page."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        response = self.client.post(
            _admin_cancel_url(order.pk),
            {f'discarded_{item.pk}': 0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Missing prepared')
        # Order must remain unchanged
        order.refresh_from_db()
        self.assertEqual(order.status, 'preparing')

    def test_admin_cancel_ready_missing_discarded_shows_error(self):
        """POST admin cancel for ready with missing discarded_ → error on page."""
        order, item = _deducted_order(self.prod, qty=2, stock=10)
        order.status = 'ready'
        order.save(update_fields=['status'])
        response = self.client.post(_admin_cancel_url(order.pk), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Missing discarded')
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')

    def test_admin_cancel_preparing_invalid_leaves_everything_unchanged(self):
        """Invalid admin POST (prepared > quantity) → no inventory or status change."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        stock_before = self.prod.stock_quantity
        logs_before = InventoryLog.objects.count()
        disps_before = OrderCancellationDisposition.objects.count()
        response = self.client.post(
            _admin_cancel_url(order.pk),
            {f'prepared_{item.pk}': 99, f'discarded_{item.pk}': 0},
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertEqual(order.status, 'preparing')
        self.assertEqual(self.prod.stock_quantity, stock_before)
        self.assertEqual(InventoryLog.objects.count(), logs_before)
        self.assertEqual(OrderCancellationDisposition.objects.count(), disps_before)

    def test_admin_cancel_explicit_zero_accepted_and_cancels(self):
        """POST admin cancel for preparing with prepared=0 discarded=0 → success."""
        order, item = _deducted_order(self.prod, qty=3, stock=10)
        order.status = 'preparing'
        order.save(update_fields=['status'])
        self.client.post(
            _admin_cancel_url(order.pk),
            {f'prepared_{item.pk}': 0, f'discarded_{item.pk}': 0},
            follow=True,
        )
        order.refresh_from_db()
        self.prod.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(self.prod.stock_quantity, 10)  # all 3 restored
