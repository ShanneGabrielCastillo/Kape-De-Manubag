"""
Phase 2A Tests — Ingredient-Based Inventory Foundation
=======================================================

Covers:
  - InventoryItem model: creation, validation, constraints
  - IngredientMovementLog model: creation, relationships, constraints
  - lock_inventory_items() ordered-lock helper
  - record_adjustment() service
  - record_wastage() service
  - Atomic rollback behaviour
  - Legacy compatibility: Product.stock_quantity and InventoryLog unchanged

SQLite note
-----------
All tests in this file run reliably on SQLite (the default development DB).
SQLite does not support real row-level locking (SELECT FOR UPDATE is a no-op
there), but the database-level CHECK constraints, the conditional-UPDATE
backstops, and the transactional rollback tests all work correctly under
SQLite's single-writer serialisation.

Concurrency tests that depend on two concurrent connections actually blocking
each other are PostgreSQL-only and must be run in a separate CI PostgreSQL
job (Phase 2A requirement, Rev 3 §M.7). Those tests are marked with a skip
decorator when the backend is not PostgreSQL.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from apps.inventory.models import IngredientMovementLog, InventoryItem, InventoryLog
from apps.inventory.services import (
    ExceedsAvailableError,
    InventoryServiceError,
    ReservedConflictError,
    StaleExpectedQuantityError,
    lock_inventory_items,
    record_adjustment,
    record_wastage,
)
from apps.menu.models import Category, Product

User = get_user_model()

PASSWORD = 'phase2a-test-pass'


# ── Helpers ───────────────────────────────────────────────────────────────────

def _item(name='Rice', item_type='ingredient', base_unit='g',
          on_hand=Decimal('1000.000'), reserved=Decimal('0.000'),
          low_stock_threshold=Decimal('100.000'), is_active=True):
    """Factory for InventoryItem instances."""
    return InventoryItem.objects.create(
        name=name,
        item_type=item_type,
        base_unit=base_unit,
        on_hand_quantity=on_hand,
        reserved_quantity=reserved,
        low_stock_threshold=low_stock_threshold,
        is_active=is_active,
    )


def _is_postgresql():
    return connection.vendor == 'postgresql'


# ═════════════════════════════════════════════════════════════════════════════
# 1. InventoryItem — model tests
# ═════════════════════════════════════════════════════════════════════════════

class InventoryItemCreationTests(TestCase):
    """Basic creation and field defaults."""

    def test_ingredient_creation(self):
        item = InventoryItem.objects.create(
            name='Coffee Powder',
            item_type='ingredient',
            base_unit='g',
        )
        self.assertEqual(item.name, 'Coffee Powder')
        self.assertEqual(item.item_type, 'ingredient')
        self.assertEqual(item.base_unit, 'g')
        self.assertEqual(item.on_hand_quantity, Decimal('0.000'))
        self.assertEqual(item.reserved_quantity, Decimal('0.000'))
        self.assertTrue(item.is_active)

    def test_packaging_creation(self):
        item = InventoryItem.objects.create(
            name='Medium Cup',
            item_type='packaging',
            base_unit='pcs',
            on_hand_quantity=Decimal('200.000'),
        )
        self.assertEqual(item.item_type, 'packaging')
        self.assertEqual(item.base_unit, 'pcs')

    def test_available_quantity_property(self):
        item = _item(on_hand=Decimal('500.000'), reserved=Decimal('150.000'))
        self.assertEqual(item.available_quantity, Decimal('350.000'))

    def test_available_quantity_zero_when_fully_reserved(self):
        item = _item(on_hand=Decimal('100.000'), reserved=Decimal('100.000'))
        self.assertEqual(item.available_quantity, Decimal('0.000'))

    def test_is_low_stock_true(self):
        item = _item(on_hand=Decimal('50.000'), reserved=Decimal('0.000'),
                     low_stock_threshold=Decimal('100.000'))
        self.assertTrue(item.is_low_stock)

    def test_is_low_stock_false_when_above_threshold(self):
        item = _item(on_hand=Decimal('500.000'), reserved=Decimal('0.000'),
                     low_stock_threshold=Decimal('100.000'))
        self.assertFalse(item.is_low_stock)

    def test_is_low_stock_true_when_available_at_threshold(self):
        # Exactly at threshold → still low stock
        item = _item(on_hand=Decimal('100.000'), reserved=Decimal('0.000'),
                     low_stock_threshold=Decimal('100.000'))
        self.assertTrue(item.is_low_stock)

    def test_str_representation(self):
        item = _item(name='Milk', base_unit='ml')
        self.assertIn('Milk', str(item))
        self.assertIn('ml', str(item))

    def test_all_three_base_units_accepted(self):
        for unit in ['g', 'ml', 'pcs']:
            InventoryItem.objects.create(
                name=f'Test {unit}', item_type='ingredient', base_unit=unit
            )
        self.assertEqual(InventoryItem.objects.count(), 3)

    def test_both_item_types_accepted(self):
        InventoryItem.objects.create(name='Sugar', item_type='ingredient', base_unit='g')
        InventoryItem.objects.create(name='Straw', item_type='packaging', base_unit='pcs')
        self.assertEqual(InventoryItem.objects.count(), 2)


class InventoryItemConstraintTests(TestCase):
    """Database-level constraint enforcement."""

    def test_case_insensitive_unique_name(self):
        InventoryItem.objects.create(name='Rice', item_type='ingredient', base_unit='g')
        with self.assertRaises(IntegrityError):
            InventoryItem.objects.create(name='rice', item_type='ingredient', base_unit='g')

    def test_case_insensitive_unique_name_mixed_case(self):
        InventoryItem.objects.create(name='Coffee Powder', item_type='ingredient', base_unit='g')
        with self.assertRaises(IntegrityError):
            InventoryItem.objects.create(name='COFFEE POWDER', item_type='ingredient', base_unit='g')

    def test_same_name_different_type_still_unique(self):
        """Name uniqueness is across all items regardless of type."""
        InventoryItem.objects.create(name='Box', item_type='packaging', base_unit='pcs')
        with self.assertRaises(IntegrityError):
            InventoryItem.objects.create(name='box', item_type='ingredient', base_unit='pcs')

    def test_on_hand_non_negative_constraint(self):
        """on_hand_quantity < 0 must be rejected at the DB level."""
        with self.assertRaises(Exception):  # IntegrityError or similar
            with transaction.atomic():
                InventoryItem.objects.create(
                    name='NegItem', item_type='ingredient', base_unit='g',
                    on_hand_quantity=Decimal('-1.000'),
                )

    def test_reserved_non_negative_constraint(self):
        """reserved_quantity < 0 must be rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                InventoryItem.objects.create(
                    name='NegRes', item_type='ingredient', base_unit='g',
                    reserved_quantity=Decimal('-1.000'),
                )

    def test_reserved_within_on_hand_constraint(self):
        """reserved_quantity > on_hand_quantity must be rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                InventoryItem.objects.create(
                    name='BadRes', item_type='ingredient', base_unit='g',
                    on_hand_quantity=Decimal('10.000'),
                    reserved_quantity=Decimal('11.000'),
                )

    def test_threshold_non_negative_constraint(self):
        """low_stock_threshold < 0 must be rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                InventoryItem.objects.create(
                    name='BadThresh', item_type='ingredient', base_unit='g',
                    low_stock_threshold=Decimal('-1.000'),
                )

    def test_invalid_item_type_rejected(self):
        """item_type values outside ['ingredient', 'packaging'] are rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                InventoryItem.objects.create(
                    name='BadType', item_type='random', base_unit='g',
                )

    def test_invalid_base_unit_rejected(self):
        """base_unit values outside ['g', 'ml', 'pcs'] are rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                InventoryItem.objects.create(
                    name='BadUnit', item_type='ingredient', base_unit='kg',
                )

    def test_clean_validates_reserved_within_on_hand(self):
        """model.clean() provides a clear ValidationError before DB write."""
        from django.core.exceptions import ValidationError
        item = InventoryItem(
            name='ValidationTest', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('10.000'),
            reserved_quantity=Decimal('20.000'),
        )
        with self.assertRaises(ValidationError) as ctx:
            item.clean()
        self.assertIn('reserved_quantity', ctx.exception.message_dict)

    def test_clean_validates_negative_on_hand(self):
        from django.core.exceptions import ValidationError
        item = InventoryItem(
            name='NegClean', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('-1.000'),
        )
        with self.assertRaises(ValidationError):
            item.clean()


# ═════════════════════════════════════════════════════════════════════════════
# 2. IngredientMovementLog — model tests
# ═════════════════════════════════════════════════════════════════════════════

class IngredientMovementLogCreationTests(TestCase):
    """Creation, snapshots, and relationships."""

    def setUp(self):
        self.item = _item(name='Milk', base_unit='ml',
                          on_hand=Decimal('500.000'), reserved=Decimal('0.000'))

    def test_record_classmethod_creates_row(self):
        log = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type='adjustment',
            on_hand_delta=Decimal('100.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('500.000'),
            reserved_before=Decimal('0.000'),
            note='Initial stock count',
        )
        self.assertIsNotNone(log.pk)
        self.assertEqual(log.item_name, 'Milk')
        self.assertEqual(log.base_unit, 'ml')
        self.assertEqual(log.on_hand_after, Decimal('600.000'))
        self.assertEqual(log.reserved_after, Decimal('0.000'))

    def test_snapshots_are_set_at_record_time(self):
        """item_name snapshot is taken at record time, not later."""
        log = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type='wastage',
            on_hand_delta=Decimal('-50.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('500.000'),
            reserved_before=Decimal('0.000'),
        )
        # Rename the item after the log was written.
        self.item.name = 'Full-Cream Milk'
        self.item.save(update_fields=['name'])
        # Snapshot is preserved.
        log.refresh_from_db()
        self.assertEqual(log.item_name, 'Milk')

    def test_idempotency_key_prevents_duplicate(self):
        log1 = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type='adjustment',
            on_hand_delta=Decimal('50.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('500.000'),
            reserved_before=Decimal('0.000'),
            idempotency_key='test-idem-001',
        )
        log2 = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type='adjustment',
            on_hand_delta=Decimal('50.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('500.000'),
            reserved_before=Decimal('0.000'),
            idempotency_key='test-idem-001',
        )
        self.assertEqual(log1.pk, log2.pk)
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_null_idempotency_key_allows_multiple_rows(self):
        """No idempotency key → multiple identical rows are allowed."""
        for _ in range(3):
            IngredientMovementLog.record(
                inventory_item=self.item,
                movement_type='wastage',
                on_hand_delta=Decimal('-10.000'),
                reserved_delta=Decimal('0.000'),
                on_hand_before=Decimal('500.000'),
                reserved_before=Decimal('0.000'),
                idempotency_key=None,
            )
        self.assertEqual(IngredientMovementLog.objects.count(), 3)

    def test_str_representation(self):
        log = IngredientMovementLog.record(
            inventory_item=self.item,
            movement_type='purchase',
            on_hand_delta=Decimal('200.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('0.000'),
            reserved_before=Decimal('0.000'),
        )
        s = str(log)
        self.assertIn('Milk', s)
        self.assertIn('Purchase', s)


class IngredientMovementLogConstraintTests(TestCase):
    """DB-level constraint enforcement on movement log rows."""

    def setUp(self):
        self.item = _item(name='Sugar', base_unit='g',
                          on_hand=Decimal('1000.000'), reserved=Decimal('0.000'))

    def _base_kwargs(self, **overrides):
        kwargs = dict(
            inventory_item=self.item,
            item_name='Sugar',
            base_unit='g',
            movement_type='adjustment',
            on_hand_delta=Decimal('10.000'),
            reserved_delta=Decimal('0.000'),
            on_hand_before=Decimal('1000.000'),
            on_hand_after=Decimal('1010.000'),
            reserved_before=Decimal('0.000'),
            reserved_after=Decimal('0.000'),
        )
        kwargs.update(overrides)
        return kwargs

    def test_on_hand_arithmetic_constraint_violated(self):
        """on_hand_after != on_hand_before + on_hand_delta → rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    **self._base_kwargs(
                        on_hand_after=Decimal('999.000')  # wrong: should be 1010
                    )
                )

    def test_reserved_arithmetic_constraint_violated(self):
        """reserved_after != reserved_before + reserved_delta → rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    **self._base_kwargs(
                        reserved_after=Decimal('5.000')  # wrong: should be 0
                    )
                )

    def test_mv_after_valid_negative_on_hand_after(self):
        """on_hand_after < 0 is rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    **self._base_kwargs(
                        on_hand_delta=Decimal('-1100.000'),
                        on_hand_after=Decimal('-100.000'),
                    )
                )

    def test_reservation_shape_must_have_zero_on_hand_delta(self):
        """reservation movement: on_hand_delta != 0 → rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    **self._base_kwargs(
                        movement_type='reservation',
                        on_hand_delta=Decimal('10.000'),   # must be 0
                        reserved_delta=Decimal('10.000'),
                        on_hand_after=Decimal('1010.000'),
                        reserved_after=Decimal('10.000'),
                    )
                )

    def test_wastage_shape_must_have_zero_reserved_delta(self):
        """wastage movement: reserved_delta != 0 → rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    **self._base_kwargs(
                        movement_type='wastage',
                        on_hand_delta=Decimal('-10.000'),
                        reserved_delta=Decimal('-10.000'),  # must be 0
                        on_hand_after=Decimal('990.000'),
                        reserved_after=Decimal('-10.000'),
                    )
                )

    def test_purchase_shape_requires_positive_on_hand_delta(self):
        """purchase movement: on_hand_delta <= 0 → rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    **self._base_kwargs(
                        movement_type='purchase',
                        on_hand_delta=Decimal('-5.000'),  # must be > 0
                        on_hand_after=Decimal('995.000'),
                    )
                )

    def test_adjustment_shape_requires_nonzero_on_hand_delta(self):
        """adjustment movement: on_hand_delta = 0 → rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    **self._base_kwargs(
                        on_hand_delta=Decimal('0.000'),
                        on_hand_after=Decimal('1000.000'),
                    )
                )

    def test_valid_purchase_movement_accepted(self):
        """A correctly formed purchase movement row is created without error."""
        log = IngredientMovementLog.objects.create(
            **self._base_kwargs(
                movement_type='purchase',
                on_hand_delta=Decimal('200.000'),
                reserved_delta=Decimal('0.000'),
                on_hand_after=Decimal('1200.000'),
                reserved_after=Decimal('0.000'),
            )
        )
        self.assertIsNotNone(log.pk)

    def test_valid_reservation_movement_accepted(self):
        log = IngredientMovementLog.objects.create(
            **self._base_kwargs(
                movement_type='reservation',
                on_hand_delta=Decimal('0.000'),
                reserved_delta=Decimal('100.000'),
                on_hand_after=Decimal('1000.000'),
                reserved_after=Decimal('100.000'),
            )
        )
        self.assertIsNotNone(log.pk)

    def test_invalid_movement_type_rejected(self):
        """Unknown movement type → rejected."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                IngredientMovementLog.objects.create(
                    **self._base_kwargs(movement_type='not_a_type')
                )


# ═════════════════════════════════════════════════════════════════════════════
# 3. lock_inventory_items — ordered-lock helper
# ═════════════════════════════════════════════════════════════════════════════

class LockInventoryItemsTests(TestCase):
    """Unit tests for the lock helper (sequential, SQLite-reliable)."""

    def setUp(self):
        self.a = _item(name='ItemA', base_unit='g', on_hand=Decimal('100.000'))
        self.b = _item(name='ItemB', base_unit='ml', on_hand=Decimal('200.000'))
        self.c = _item(name='ItemC', base_unit='pcs', on_hand=Decimal('50.000'))

    def test_locks_all_requested_items(self):
        with transaction.atomic():
            locked = lock_inventory_items([self.a.pk, self.b.pk, self.c.pk])
        self.assertIn(self.a.pk, locked)
        self.assertIn(self.b.pk, locked)
        self.assertIn(self.c.pk, locked)

    def test_returns_correct_instances(self):
        with transaction.atomic():
            locked = lock_inventory_items([self.b.pk, self.a.pk])
        self.assertEqual(locked[self.a.pk].name, 'ItemA')
        self.assertEqual(locked[self.b.pk].name, 'ItemB')

    def test_empty_list_returns_empty_dict(self):
        with transaction.atomic():
            locked = lock_inventory_items([])
        self.assertEqual(locked, {})

    def test_duplicate_ids_deduplicated(self):
        with transaction.atomic():
            locked = lock_inventory_items([self.a.pk, self.a.pk, self.a.pk])
        self.assertEqual(len(locked), 1)
        self.assertIn(self.a.pk, locked)

    def test_missing_pk_raises_service_error(self):
        nonexistent_pk = 99999
        with self.assertRaises(InventoryServiceError):
            with transaction.atomic():
                lock_inventory_items([self.a.pk, nonexistent_pk])

    def test_result_contains_all_ids_from_input(self):
        all_pks = [self.a.pk, self.b.pk, self.c.pk]
        with transaction.atomic():
            locked = lock_inventory_items(all_pks)
        for pk in all_pks:
            self.assertIn(pk, locked)


# ═════════════════════════════════════════════════════════════════════════════
# 4. record_adjustment — service tests
# ═════════════════════════════════════════════════════════════════════════════

class RecordAdjustmentTests(TestCase):

    def setUp(self):
        self.item = _item(name='Coffee', base_unit='g', on_hand=Decimal('1000.000'))

    def _adjust(self, new_on_hand, expected=None, **kwargs):
        if expected is None:
            expected = self.item.on_hand_quantity
        with transaction.atomic():
            return record_adjustment(
                inventory_item_id=self.item.pk,
                new_on_hand=new_on_hand,
                expected_on_hand=expected,
                **kwargs,
            )

    # ── Success paths ─────────────────────────────────────────────────────────

    def test_upward_adjustment_changes_stock(self):
        log = self._adjust(Decimal('1200.000'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1200.000'))
        self.assertEqual(log.movement_type, 'adjustment')
        self.assertEqual(log.on_hand_delta, Decimal('200.000'))

    def test_downward_adjustment_changes_stock(self):
        log = self._adjust(Decimal('800.000'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('800.000'))
        self.assertEqual(log.on_hand_delta, Decimal('-200.000'))

    def test_adjustment_creates_movement_log_row(self):
        self._adjust(Decimal('1100.000'))
        self.assertEqual(IngredientMovementLog.objects.count(), 1)
        log = IngredientMovementLog.objects.first()
        self.assertEqual(log.movement_type, 'adjustment')

    def test_adjustment_stores_correct_before_after_values(self):
        log = self._adjust(Decimal('1100.000'))
        self.assertEqual(log.on_hand_before, Decimal('1000.000'))
        self.assertEqual(log.on_hand_after, Decimal('1100.000'))
        self.assertEqual(log.reserved_before, Decimal('0.000'))
        self.assertEqual(log.reserved_after, Decimal('0.000'))

    def test_adjustment_with_reason_and_note(self):
        log = self._adjust(
            Decimal('950.000'),
            reason_code='stock_count',
            note='Physical count on 2026-10-31',
        )
        self.assertEqual(log.reason_code, 'stock_count')
        self.assertEqual(log.note, 'Physical count on 2026-10-31')

    def test_adjustment_records_performer(self):
        user = User.objects.create_user(username='inv_adj', password=PASSWORD, role='admin')
        log = self._adjust(Decimal('900.000'), performed_by=user)
        self.assertEqual(log.performed_by, user)

    def test_adjustment_with_idempotency_key(self):
        log1 = self._adjust(Decimal('1100.000'), idempotency_key='adj-idem-001')
        # Second call with same key should return the same row without
        # writing a second movement or changing the already-changed stock.
        # Note: the stock is already 1100 after the first call, so we pass
        # expected=1100 for the second; but since the key matches, the
        # existing row is returned before any write.
        with transaction.atomic():
            log2 = record_adjustment(
                inventory_item_id=self.item.pk,
                new_on_hand=Decimal('1200.000'),
                expected_on_hand=Decimal('1100.000'),
                idempotency_key='adj-idem-001',
            )
        self.assertEqual(log1.pk, log2.pk)
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_adjustment_with_partial_reservation_allowed_above_reserved(self):
        """Can adjust down to a value >= reserved_quantity."""
        item = _item(name='AdjReserved', base_unit='g',
                     on_hand=Decimal('500.000'), reserved=Decimal('100.000'))
        with transaction.atomic():
            log = record_adjustment(
                inventory_item_id=item.pk,
                new_on_hand=Decimal('100.000'),   # exactly equals reserved
                expected_on_hand=Decimal('500.000'),
            )
        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('100.000'))

    # ── Rejection paths ───────────────────────────────────────────────────────

    def test_adjustment_to_same_value_raises_error(self):
        with self.assertRaises(InventoryServiceError):
            self._adjust(Decimal('1000.000'))   # same as current

    def test_adjustment_with_stale_expected_raises_error(self):
        # Simulate another process already having changed stock.
        self.item.on_hand_quantity = Decimal('1050.000')
        self.item.save(update_fields=['on_hand_quantity'])
        with self.assertRaises(StaleExpectedQuantityError):
            # We think it's 1000 but it's now 1050.
            self._adjust(Decimal('900.000'), expected=Decimal('1000.000'))

    def test_adjustment_below_reserved_raises_error(self):
        item = _item(name='AdjBelowReserved', base_unit='g',
                     on_hand=Decimal('500.000'), reserved=Decimal('200.000'))
        with self.assertRaises(ReservedConflictError):
            with transaction.atomic():
                record_adjustment(
                    inventory_item_id=item.pk,
                    new_on_hand=Decimal('100.000'),   # below reserved 200
                    expected_on_hand=Decimal('500.000'),
                )

    def test_adjustment_to_negative_raises_error(self):
        with self.assertRaises(InventoryServiceError):
            self._adjust(Decimal('-1.000'))

    def test_stock_unchanged_on_stale_expected_error(self):
        self.item.on_hand_quantity = Decimal('1050.000')
        self.item.save(update_fields=['on_hand_quantity'])
        try:
            self._adjust(Decimal('900.000'), expected=Decimal('1000.000'))
        except StaleExpectedQuantityError:
            pass
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1050.000'))

    def test_no_movement_log_on_error(self):
        """A failed adjustment must create no movement log row."""
        try:
            self._adjust(Decimal('1000.000'))  # same value → error
        except InventoryServiceError:
            pass
        self.assertEqual(IngredientMovementLog.objects.count(), 0)


# ═════════════════════════════════════════════════════════════════════════════
# 5. record_wastage — service tests
# ═════════════════════════════════════════════════════════════════════════════

class RecordWastageTests(TestCase):

    def setUp(self):
        self.item = _item(name='Chicken', base_unit='g',
                          on_hand=Decimal('2000.000'), reserved=Decimal('300.000'))

    def _waste(self, quantity, **kwargs):
        with transaction.atomic():
            return record_wastage(
                inventory_item_id=self.item.pk,
                quantity=quantity,
                **kwargs,
            )

    # ── Success paths ─────────────────────────────────────────────────────────

    def test_wastage_reduces_on_hand(self):
        self._waste(Decimal('100.000'))
        self.item.refresh_from_db()
        # on_hand: 2000 - 100 = 1900; reserved stays 300
        self.assertEqual(self.item.on_hand_quantity, Decimal('1900.000'))
        self.assertEqual(self.item.reserved_quantity, Decimal('300.000'))

    def test_wastage_creates_movement_log_row(self):
        self._waste(Decimal('50.000'))
        log = IngredientMovementLog.objects.get()
        self.assertEqual(log.movement_type, 'wastage')
        self.assertEqual(log.on_hand_delta, Decimal('-50.000'))
        self.assertEqual(log.reserved_delta, Decimal('0.000'))

    def test_wastage_stores_correct_balances(self):
        log = self._waste(Decimal('200.000'))
        self.assertEqual(log.on_hand_before, Decimal('2000.000'))
        self.assertEqual(log.on_hand_after, Decimal('1800.000'))
        self.assertEqual(log.reserved_before, Decimal('300.000'))
        self.assertEqual(log.reserved_after, Decimal('300.000'))

    def test_wastage_with_reason_and_note(self):
        log = self._waste(
            Decimal('100.000'),
            reason_code='expired',
            note='Past use-by date',
        )
        self.assertEqual(log.reason_code, 'expired')
        self.assertEqual(log.note, 'Past use-by date')

    def test_wastage_records_performer(self):
        user = User.objects.create_user(username='waste_user', password=PASSWORD, role='admin')
        log = self._waste(Decimal('50.000'), performed_by=user)
        self.assertEqual(log.performed_by, user)

    def test_wastage_exactly_available_stock_succeeds(self):
        """Wasting all available stock (but not touching reserved) succeeds."""
        # available = 2000 - 300 = 1700
        self._waste(Decimal('1700.000'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('300.000'))
        self.assertEqual(self.item.reserved_quantity, Decimal('300.000'))

    def test_wastage_idempotency_key(self):
        log1 = self._waste(Decimal('100.000'), idempotency_key='waste-001')
        with transaction.atomic():
            log2 = record_wastage(
                inventory_item_id=self.item.pk,
                quantity=Decimal('100.000'),
                idempotency_key='waste-001',
            )
        self.assertEqual(log1.pk, log2.pk)
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    # ── Rejection paths ───────────────────────────────────────────────────────

    def test_wastage_zero_quantity_raises_error(self):
        with self.assertRaises(InventoryServiceError):
            self._waste(Decimal('0.000'))

    def test_wastage_negative_quantity_raises_error(self):
        with self.assertRaises(InventoryServiceError):
            self._waste(Decimal('-10.000'))

    def test_wastage_exceeds_available_raises_error(self):
        """Wasting more than available (on_hand - reserved) is rejected."""
        # available = 2000 - 300 = 1700; requesting 1701
        with self.assertRaises(ExceedsAvailableError):
            self._waste(Decimal('1701.000'))

    def test_wastage_does_not_touch_reserved_stock(self):
        """Reserved stock is protected — this would exceed available."""
        with self.assertRaises(ExceedsAvailableError):
            # Trying to waste everything including reserved portion
            self._waste(Decimal('2000.000'))

    def test_no_stock_change_on_error(self):
        try:
            self._waste(Decimal('0.000'))
        except InventoryServiceError:
            pass
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('2000.000'))

    def test_no_movement_log_on_error(self):
        try:
            self._waste(Decimal('1701.000'))
        except ExceedsAvailableError:
            pass
        self.assertEqual(IngredientMovementLog.objects.count(), 0)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Atomicity tests
# ═════════════════════════════════════════════════════════════════════════════

class AtomicRollbackTests(TestCase):
    """
    Verify that a failure inside the service rolls back both the quantity
    change and the movement log row. Uses monkeypatching to inject a failure
    after the UPDATE but before the movement log INSERT.
    """

    def setUp(self):
        self.item = _item(name='RollbackItem', base_unit='g',
                          on_hand=Decimal('500.000'))

    def test_rollback_on_log_write_failure_in_adjustment(self):
        """
        If writing the IngredientMovementLog fails, the on_hand update must
        also roll back — the database must stay unchanged.
        """
        original_record = IngredientMovementLog.record

        def failing_record(*args, **kwargs):
            raise RuntimeError('Simulated log write failure')

        IngredientMovementLog.record = staticmethod(failing_record)
        try:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    record_adjustment(
                        inventory_item_id=self.item.pk,
                        new_on_hand=Decimal('600.000'),
                        expected_on_hand=Decimal('500.000'),
                    )
        finally:
            IngredientMovementLog.record = staticmethod(original_record)

        # Neither the stock change nor any log row survived.
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('500.000'))
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_rollback_on_log_write_failure_in_wastage(self):
        original_record = IngredientMovementLog.record

        def failing_record(*args, **kwargs):
            raise RuntimeError('Simulated log write failure')

        IngredientMovementLog.record = staticmethod(failing_record)
        try:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    record_wastage(
                        inventory_item_id=self.item.pk,
                        quantity=Decimal('100.000'),
                    )
        finally:
            IngredientMovementLog.record = staticmethod(original_record)

        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('500.000'))
        self.assertEqual(IngredientMovementLog.objects.count(), 0)


# ═════════════════════════════════════════════════════════════════════════════
# 7. Legacy compatibility — existing Product.stock_quantity and InventoryLog
# ═════════════════════════════════════════════════════════════════════════════

class LegacyCompatibilityTests(TestCase):
    """
    Confirm that Phase 2A does not alter any existing legacy inventory
    behaviour. Product.stock_quantity, InventoryLog, and all existing
    stock-change paths must work exactly as before.
    """

    def setUp(self):
        self.category = Category.objects.create(name='Drinks', slug='drinks-legacy')
        self.product = Product.objects.create(
            category=self.category,
            name='Iced Coffee',
            price=Decimal('65.00'),
            stock_quantity=100,
        )
        self.admin = User.objects.create_user(
            username='legacy_admin', password=PASSWORD, role='admin',
        )

    def test_product_stock_quantity_field_still_exists(self):
        self.assertEqual(self.product.stock_quantity, 100)

    def test_reduce_stock_still_works(self):
        self.product.reduce_stock(10)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 90)

    def test_restore_stock_still_works(self):
        self.product.reduce_stock(10)
        self.product.restore_stock(10)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 100)

    def test_inventory_log_record_classmethod_still_works(self):
        old_qty = self.product.stock_quantity
        new_qty = old_qty + 50
        self.product.stock_quantity = new_qty
        self.product.save(update_fields=['stock_quantity'])
        InventoryLog.record(
            product=self.product,
            action='restock',
            source='manual_adjustment',
            reason='Restock',
            quantity_change=50,
            quantity_before=old_qty,
            quantity_after=new_qty,
            performed_by=self.admin,
        )
        self.assertEqual(InventoryLog.objects.count(), 1)
        log = InventoryLog.objects.first()
        self.assertEqual(log.product, self.product)
        self.assertEqual(log.quantity_change, 50)
        self.assertEqual(log.product_name, 'Iced Coffee')

    def test_inventory_log_and_ingredient_log_are_independent(self):
        """Writing to one log table must not affect the other."""
        # Write legacy log
        InventoryLog.record(
            product=self.product,
            action='restock',
            source='manual_adjustment',
            reason='Test',
            quantity_change=10,
            quantity_before=100,
            quantity_after=110,
        )
        # Write ingredient log
        ing_item = _item(name='Oat Milk', base_unit='ml',
                         on_hand=Decimal('500.000'))
        with transaction.atomic():
            record_wastage(inventory_item_id=ing_item.pk, quantity=Decimal('50.000'))

        # Each table has exactly one row.
        self.assertEqual(InventoryLog.objects.count(), 1)
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

        # Product stock_quantity is untouched by the ingredient wastage.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 100)

    def test_product_non_negative_check_constraint_still_enforced(self):
        """The original product_stock_non_negative CHECK constraint is intact."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                Product.objects.filter(pk=self.product.pk).update(
                    stock_quantity=-1
                )
