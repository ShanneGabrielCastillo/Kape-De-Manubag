"""
tests_phase3a.py
================
Phase 3-1 model and database constraint tests.

Covers:
  - UnitConversion creation, constraint enforcement, case-insensitive uniqueness.
  - StockPurchase creation, idempotency-key uniqueness.
  - StockPurchaseItem creation, positive-value constraints.
  - IngredientMovementLog.purchase_item FK link.
  - Regression: existing Phase 2A model behavior unchanged.

All tests are SQLite-reliable (constraint checks, integrity, transaction rollback).
No concurrency or PostgreSQL-only behavior is tested here.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    StockPurchase,
    StockPurchaseItem,
    UnitConversion,
)

# ── Shared helpers ────────────────────────────────────────────────────────────

def _item(name='Coffee Powder', item_type='ingredient', base_unit='g',
          on_hand=Decimal('1000.000')):
    return InventoryItem.objects.create(
        name=name, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=on_hand,
    )


def _purchase(idempotency_key='purchase-001', purchase_date='2026-11-01'):
    import datetime
    return StockPurchase.objects.create(
        purchase_date=datetime.date.fromisoformat(purchase_date),
        idempotency_key=idempotency_key,
    )


def _purchase_item(purchase, item, purchase_unit='kg',
                   purchase_qty=Decimal('5.000'),
                   factor=Decimal('1000.0000'),
                   base_qty=Decimal('5000.000')):
    return StockPurchaseItem.objects.create(
        purchase=purchase,
        inventory_item=item,
        purchase_unit=purchase_unit,
        purchase_quantity=purchase_qty,
        conversion_factor=factor,
        base_quantity=base_qty,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. UnitConversion — creation and constraint enforcement
# ═════════════════════════════════════════════════════════════════════════════

class UnitConversionCreationTests(TestCase):
    """Valid creation and basic field checks."""

    def setUp(self):
        self.item = _item('Milk', 'ingredient', 'ml')

    def test_valid_conversion_created(self):
        conv = UnitConversion.objects.create(
            inventory_item=self.item,
            purchase_unit='L',
            conversion_factor=Decimal('1000.0000'),
        )
        self.assertEqual(conv.inventory_item, self.item)
        self.assertEqual(conv.purchase_unit, 'L')
        self.assertEqual(conv.conversion_factor, Decimal('1000.0000'))
        self.assertTrue(conv.is_active)

    def test_str_representation(self):
        conv = UnitConversion.objects.create(
            inventory_item=self.item,
            purchase_unit='L',
            conversion_factor=Decimal('1000.0000'),
        )
        s = str(conv)
        self.assertIn('Milk', s)
        self.assertIn('L', s)

    def test_different_items_can_share_purchase_unit(self):
        """Same purchase_unit string is allowed for different inventory items."""
        item2 = _item('Sugar', 'ingredient', 'g')
        UnitConversion.objects.create(
            inventory_item=self.item,
            purchase_unit='kg',
            conversion_factor=Decimal('1000.0000'),
        )
        # Must not raise — a different item, same unit name.
        UnitConversion.objects.create(
            inventory_item=item2,
            purchase_unit='kg',
            conversion_factor=Decimal('1000.0000'),
        )
        self.assertEqual(UnitConversion.objects.count(), 2)

    def test_notes_optional(self):
        conv = UnitConversion.objects.create(
            inventory_item=self.item,
            purchase_unit='mL',
            conversion_factor=Decimal('1.0000'),
        )
        self.assertEqual(conv.notes, '')


class UnitConversionConstraintTests(TestCase):
    """Database-level constraint enforcement."""

    def setUp(self):
        self.item = _item('Rice', 'ingredient', 'g')

    def test_zero_conversion_factor_rejected(self):
        """conv_factor_positive: conversion_factor > 0 is required."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                UnitConversion.objects.create(
                    inventory_item=self.item,
                    purchase_unit='kg',
                    conversion_factor=Decimal('0.0000'),
                )

    def test_negative_conversion_factor_rejected(self):
        """conv_factor_positive: negative factor must be rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                UnitConversion.objects.create(
                    inventory_item=self.item,
                    purchase_unit='kg',
                    conversion_factor=Decimal('-500.0000'),
                )

    def test_duplicate_purchase_unit_same_item_rejected(self):
        """conv_unique_unit_per_item: duplicate (item, purchase_unit) rejected."""
        UnitConversion.objects.create(
            inventory_item=self.item,
            purchase_unit='kg',
            conversion_factor=Decimal('1000.0000'),
        )
        with self.assertRaises(Exception):
            with transaction.atomic():
                UnitConversion.objects.create(
                    inventory_item=self.item,
                    purchase_unit='kg',
                    conversion_factor=Decimal('500.0000'),
                )

    def test_duplicate_purchase_unit_case_insensitive_rejected(self):
        """conv_unique_unit_per_item: 'KG' and 'kg' are the same unit."""
        UnitConversion.objects.create(
            inventory_item=self.item,
            purchase_unit='kg',
            conversion_factor=Decimal('1000.0000'),
        )
        with self.assertRaises(Exception):
            with transaction.atomic():
                UnitConversion.objects.create(
                    inventory_item=self.item,
                    purchase_unit='KG',
                    conversion_factor=Decimal('1000.0000'),
                )

    def test_positive_factor_accepted(self):
        """A very small positive factor must be accepted."""
        conv = UnitConversion.objects.create(
            inventory_item=self.item,
            purchase_unit='mg',
            conversion_factor=Decimal('0.0010'),
        )
        self.assertIsNotNone(conv.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 2. StockPurchase — creation and constraint enforcement
# ═════════════════════════════════════════════════════════════════════════════

class StockPurchaseCreationTests(TestCase):
    """Valid creation and field checks."""

    def test_valid_purchase_created(self):
        import datetime
        p = StockPurchase.objects.create(
            purchase_date=datetime.date(2026, 11, 1),
            idempotency_key='p-test-001',
        )
        self.assertIsNotNone(p.pk)
        self.assertIsNotNone(p.created_at)

    def test_performed_by_optional(self):
        import datetime
        p = StockPurchase.objects.create(
            purchase_date=datetime.date(2026, 11, 1),
            idempotency_key='p-test-002',
        )
        self.assertIsNone(p.performed_by)

    def test_notes_optional(self):
        import datetime
        p = StockPurchase.objects.create(
            purchase_date=datetime.date(2026, 11, 1),
            idempotency_key='p-test-003',
        )
        self.assertEqual(p.notes, '')


class StockPurchaseConstraintTests(TestCase):
    """idempotency_key uniqueness."""

    def test_duplicate_idempotency_key_rejected(self):
        import datetime
        StockPurchase.objects.create(
            purchase_date=datetime.date(2026, 11, 1),
            idempotency_key='unique-key-001',
        )
        with self.assertRaises(Exception):
            with transaction.atomic():
                StockPurchase.objects.create(
                    purchase_date=datetime.date(2026, 11, 2),
                    idempotency_key='unique-key-001',
                )

    def test_different_idempotency_keys_accepted(self):
        import datetime
        StockPurchase.objects.create(
            purchase_date=datetime.date(2026, 11, 1),
            idempotency_key='key-A',
        )
        StockPurchase.objects.create(
            purchase_date=datetime.date(2026, 11, 1),
            idempotency_key='key-B',
        )
        self.assertEqual(StockPurchase.objects.count(), 2)


# ═════════════════════════════════════════════════════════════════════════════
# 3. StockPurchaseItem — creation and constraint enforcement
# ═════════════════════════════════════════════════════════════════════════════

class StockPurchaseItemCreationTests(TestCase):
    """Valid creation and field checks."""

    def setUp(self):
        self.item = _item('Oat Milk', 'ingredient', 'ml')
        self.purchase = _purchase('pi-test-001')

    def test_valid_purchase_item_created(self):
        pi = _purchase_item(
            self.purchase, self.item,
            purchase_unit='L',
            purchase_qty=Decimal('10.000'),
            factor=Decimal('1000.0000'),
            base_qty=Decimal('10000.000'),
        )
        self.assertEqual(pi.purchase, self.purchase)
        self.assertEqual(pi.inventory_item, self.item)
        self.assertEqual(pi.purchase_unit, 'L')
        self.assertEqual(pi.purchase_quantity, Decimal('10.000'))
        self.assertEqual(pi.conversion_factor, Decimal('1000.0000'))
        self.assertEqual(pi.base_quantity, Decimal('10000.000'))

    def test_purchase_unit_is_snapshot(self):
        """purchase_unit is a plain CharField snapshot, not tied to UnitConversion."""
        pi = _purchase_item(self.purchase, self.item, purchase_unit='CustomUnit')
        self.assertEqual(pi.purchase_unit, 'CustomUnit')

    def test_str_representation(self):
        pi = _purchase_item(self.purchase, self.item)
        s = str(pi)
        self.assertIn('Oat Milk', s)

    def test_inventory_item_protected_from_deletion(self):
        """InventoryItem with a purchase line cannot be deleted (PROTECT)."""
        _purchase_item(self.purchase, self.item)
        with self.assertRaises(Exception):
            with transaction.atomic():
                self.item.delete()


class StockPurchaseItemConstraintTests(TestCase):
    """Positive-value constraint enforcement."""

    def setUp(self):
        self.item = _item('Straw', 'packaging', 'pcs')
        self.purchase = _purchase('pi-constraint-001')

    def test_zero_purchase_quantity_rejected(self):
        """spi_purchase_quantity_positive: quantity > 0 required."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                StockPurchaseItem.objects.create(
                    purchase=self.purchase,
                    inventory_item=self.item,
                    purchase_unit='box',
                    purchase_quantity=Decimal('0.000'),
                    conversion_factor=Decimal('12.0000'),
                    base_quantity=Decimal('12.000'),
                )

    def test_negative_purchase_quantity_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                StockPurchaseItem.objects.create(
                    purchase=self.purchase,
                    inventory_item=self.item,
                    purchase_unit='box',
                    purchase_quantity=Decimal('-1.000'),
                    conversion_factor=Decimal('12.0000'),
                    base_quantity=Decimal('12.000'),
                )

    def test_zero_base_quantity_rejected(self):
        """spi_base_quantity_positive: base_quantity > 0 required."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                StockPurchaseItem.objects.create(
                    purchase=self.purchase,
                    inventory_item=self.item,
                    purchase_unit='box',
                    purchase_quantity=Decimal('1.000'),
                    conversion_factor=Decimal('12.0000'),
                    base_quantity=Decimal('0.000'),
                )

    def test_negative_base_quantity_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                StockPurchaseItem.objects.create(
                    purchase=self.purchase,
                    inventory_item=self.item,
                    purchase_unit='box',
                    purchase_quantity=Decimal('1.000'),
                    conversion_factor=Decimal('12.0000'),
                    base_quantity=Decimal('-12.000'),
                )

    def test_zero_conversion_factor_rejected(self):
        """spi_factor_positive: conversion_factor > 0 required."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                StockPurchaseItem.objects.create(
                    purchase=self.purchase,
                    inventory_item=self.item,
                    purchase_unit='box',
                    purchase_quantity=Decimal('1.000'),
                    conversion_factor=Decimal('0.0000'),
                    base_quantity=Decimal('12.000'),
                )

    def test_negative_conversion_factor_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                StockPurchaseItem.objects.create(
                    purchase=self.purchase,
                    inventory_item=self.item,
                    purchase_unit='box',
                    purchase_quantity=Decimal('1.000'),
                    conversion_factor=Decimal('-12.0000'),
                    base_quantity=Decimal('12.000'),
                )

    def test_positive_values_accepted(self):
        pi = StockPurchaseItem.objects.create(
            purchase=self.purchase,
            inventory_item=self.item,
            purchase_unit='box',
            purchase_quantity=Decimal('2.000'),
            conversion_factor=Decimal('12.0000'),
            base_quantity=Decimal('24.000'),
        )
        self.assertIsNotNone(pi.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 4. IngredientMovementLog — purchase_item FK
# ═════════════════════════════════════════════════════════════════════════════

class MovementLogPurchaseItemFKTests(TestCase):
    """purchase_item FK is nullable; existing rows unaffected."""

    def setUp(self):
        self.item = _item('Espresso Beans', 'ingredient', 'g',
                          on_hand=Decimal('5000.000'))
        self.purchase = _purchase('mvlog-fk-001')
        self.purchase_item = _purchase_item(
            self.purchase, self.item,
            purchase_unit='kg',
            purchase_qty=Decimal('5.000'),
            factor=Decimal('1000.0000'),
            base_qty=Decimal('5000.000'),
        )

    def test_movement_log_can_reference_purchase_item(self):
        """A purchase movement log row can carry the purchase_item FK."""
        log = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type=IngredientMovementLog.MOVEMENT_PURCHASE,
            on_hand_delta=Decimal('5000.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('0.000'),
            reserved_before=Decimal('0.000'),
            purchase_item=self.purchase_item,
        )
        log.refresh_from_db()
        self.assertEqual(log.purchase_item, self.purchase_item)
        self.assertEqual(log.movement_type, 'purchase')
        self.assertEqual(log.on_hand_delta, Decimal('5000.000'))

    def test_existing_movements_have_null_purchase_item(self):
        """Existing movements created without purchase_item remain valid (null)."""
        log = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type=IngredientMovementLog.MOVEMENT_ADJUSTMENT,
            on_hand_delta=Decimal('100.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('5000.000'),
            reserved_before=Decimal('0.000'),
        )
        log.refresh_from_db()
        self.assertIsNone(log.purchase_item)

    def test_null_purchase_item_accepted_for_non_purchase_movements(self):
        """Non-purchase movements must work without a purchase_item reference."""
        log = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type=IngredientMovementLog.MOVEMENT_WASTAGE,
            on_hand_delta=Decimal('-100.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('5000.000'),
            reserved_before=Decimal('0.000'),
        )
        self.assertIsNone(log.purchase_item)

    def test_purchase_item_set_null_does_not_delete_movement(self):
        """SET_NULL on purchase_item: deleting the purchase item nulls the FK,
        not the movement log row. (StockPurchase is append-only per Rev 3, so
        this scenario should not occur in production, but the FK must be safe.)"""
        log = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type=IngredientMovementLog.MOVEMENT_PURCHASE,
            on_hand_delta=Decimal('5000.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('0.000'),
            reserved_before=Decimal('0.000'),
            purchase_item=self.purchase_item,
        )
        self.assertEqual(log.purchase_item_id, self.purchase_item.pk)
        log_pk = log.pk
        # Deleting the purchase (CASCADE) should null the FK on the movement log.
        self.purchase.delete()
        log.refresh_from_db()
        self.assertIsNone(log.purchase_item)
        # The movement log row itself must still exist.
        self.assertEqual(IngredientMovementLog.objects.filter(pk=log_pk).count(), 1)


# ═════════════════════════════════════════════════════════════════════════════
# 5. Phase 2A regression — existing behavior unchanged
# ═════════════════════════════════════════════════════════════════════════════

class Phase3APhase2ARegressionTests(TestCase):
    """Confirm that Phase 3-1 model additions do not alter Phase 2A behavior."""

    def setUp(self):
        self.item = _item('Reg Milk', 'ingredient', 'ml', on_hand=Decimal('1000.000'))

    def test_inventory_item_creation_unchanged(self):
        item = InventoryItem.objects.create(
            name='Phase3 Reg Item',
            item_type='ingredient',
            base_unit='g',
        )
        self.assertEqual(item.on_hand_quantity, Decimal('0.000'))
        self.assertEqual(item.reserved_quantity, Decimal('0.000'))

    def test_adjustment_movement_unchanged(self):
        from apps.inventory.services import record_adjustment
        with transaction.atomic():
            log = record_adjustment(
                inventory_item_id=self.item.pk,
                new_on_hand=Decimal('1200.000'),
                expected_on_hand=Decimal('1000.000'),
                reason_code='stock_count_correction',
            )
        self.assertEqual(log.movement_type, 'adjustment')
        self.assertIsNone(log.purchase_item)

    def test_wastage_movement_unchanged(self):
        from apps.inventory.services import record_wastage
        with transaction.atomic():
            log = record_wastage(
                inventory_item_id=self.item.pk,
                quantity=Decimal('50.000'),
                reason_code='expired',
            )
        self.assertEqual(log.movement_type, 'wastage')
        self.assertIsNone(log.purchase_item)

    def test_item_on_hand_non_negative_constraint_still_enforced(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                InventoryItem.objects.filter(pk=self.item.pk).update(
                    on_hand_quantity=Decimal('-1.000')
                )

    def test_item_reserved_within_on_hand_still_enforced(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                InventoryItem.objects.filter(pk=self.item.pk).update(
                    reserved_quantity=Decimal('5000.000')  # exceeds on_hand=1000
                )

    def test_mv_shape_purchase_constraint_still_enforced(self):
        """Purchasing with reserved_delta != 0 must be rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    inventory_item=self.item,
                    item_name=self.item.name,
                    base_unit=self.item.base_unit,
                    movement_type='purchase',
                    on_hand_delta=Decimal('100.000'),
                    reserved_delta=Decimal('10.000'),  # must be 0 for purchase
                    on_hand_before=Decimal('1000.000'),
                    on_hand_after=Decimal('1100.000'),
                    reserved_before=Decimal('0.000'),
                    reserved_after=Decimal('10.000'),
                )
