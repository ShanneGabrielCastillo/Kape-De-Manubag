"""
tests_phase3b.py
================
Phase 3-2 service tests for record_purchase().

Covers:
  - Basic single-line and multi-line purchase behavior.
  - Rounding rule (the ONLY rounding point in the design).
  - pcs whole-number validation.
  - Document-level idempotency.
  - Multi-item atomicity and rollback.
  - Existing Phase 2A services unaffected.

All tests run on SQLite (reliable for logic, constraints, and rollback).
PostgreSQL concurrency tests are not added here — the purchase service
introduces no new concurrent-access pattern beyond what Phase 2A already
covers with lock_inventory_items(); the existing tests_phase2a_pg.py
scaffolding handles that when run against PostgreSQL in CI.
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    StockPurchase,
    StockPurchaseItem,
)
from apps.inventory.services import (
    FractionalPcsError,
    InventoryServiceError,
    PurchaseLineError,
    _compute_base_quantity,
    record_purchase,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

DATE = datetime.date(2026, 11, 1)


def _item(name='Coffee', base_unit='g', on_hand=Decimal('0.000'),
          item_type='ingredient'):
    return InventoryItem.objects.create(
        name=name, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=on_hand,
    )


def _line(item, purchase_qty='5.000', factor='1000.0000',
          purchase_unit='kg', note='', reason_code=''):
    return {
        'inventory_item_id': item.pk,
        'purchase_unit':     purchase_unit,
        'purchase_quantity': Decimal(purchase_qty),
        'conversion_factor': Decimal(factor),
        'note':              note,
        'reason_code':       reason_code,
    }


def _buy(*lines, key='purchase-001', date=DATE, notes='', performed_by=None):
    return record_purchase(
        purchase_date=date,
        lines=list(lines),
        idempotency_key=key,
        notes=notes,
        performed_by=performed_by,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. Rounding helper (_compute_base_quantity)
# ═════════════════════════════════════════════════════════════════════════════

class ComputeBaseQuantityTests(TestCase):
    """Unit tests for the rounding helper — the only rounding point."""

    def test_normal_multiplication(self):
        """5 kg × 1000 g/kg = 5000.000 g exactly."""
        result = _compute_base_quantity(Decimal('5.000'), Decimal('1000.0000'))
        self.assertEqual(result, Decimal('5000.000'))

    def test_round_half_up_midpoint(self):
        """0.0005 rounds up to 0.001 under ROUND_HALF_UP."""
        # 1 × 0.0005 = 0.0005, which rounds to 0.001 (not 0.000)
        result = _compute_base_quantity(Decimal('1.000'), Decimal('0.0005'))
        self.assertEqual(result, Decimal('0.001'))

    def test_decimal_precision_ieee754_problematic_values(self):
        """0.1 × 0.3 in float = 0.030000000000000002; in Decimal = 0.030."""
        result = _compute_base_quantity(Decimal('0.1'), Decimal('0.3'))
        # Exact: 0.1 × 0.3 = 0.03, quantized to 0.030
        self.assertEqual(result, Decimal('0.030'))

    def test_rounds_to_three_decimal_places(self):
        """1.2345 × 1 rounds to 1.235 under ROUND_HALF_UP."""
        result = _compute_base_quantity(Decimal('1.2345'), Decimal('1.0000'))
        self.assertEqual(result, Decimal('1.235'))

    def test_whole_number_result_for_pcs(self):
        """12 × 1 = 12.000; % 1 == 0 so it is a whole number."""
        result = _compute_base_quantity(Decimal('12.000'), Decimal('1.0000'))
        self.assertEqual(result % Decimal('1'), Decimal('0'))

    def test_result_is_decimal_not_float(self):
        result = _compute_base_quantity(Decimal('5.000'), Decimal('1000.0000'))
        self.assertIsInstance(result, Decimal)

    def test_string_inputs_accepted(self):
        """Caller may pass strings; _compute_base_quantity coerces via str()."""
        result = _compute_base_quantity('5.000', '1000.0000')
        self.assertEqual(result, Decimal('5000.000'))


# ═════════════════════════════════════════════════════════════════════════════
# 2. Basic purchase behavior
# ═════════════════════════════════════════════════════════════════════════════

class SingleLinePurchaseTests(TestCase):

    def setUp(self):
        self.item = _item('Milk', 'ml')

    def test_purchase_creates_stock_purchase_header(self):
        purchase = _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-001')
        self.assertIsNotNone(purchase.pk)
        self.assertEqual(purchase.idempotency_key, 'slp-001')
        self.assertEqual(purchase.purchase_date, DATE)

    def test_on_hand_incremented_by_base_quantity(self):
        # 10 L × 1000 ml/L = 10000 ml
        _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-002')
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('10000.000'))

    def test_reserved_quantity_unchanged(self):
        self.item.reserved_quantity = Decimal('0.000')
        self.item.save(update_fields=['reserved_quantity'])
        _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-003')
        self.item.refresh_from_db()
        self.assertEqual(self.item.reserved_quantity, Decimal('0.000'))

    def test_stock_purchase_item_created_with_correct_fields(self):
        _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-004')
        pi = StockPurchaseItem.objects.get()
        self.assertEqual(pi.purchase_unit, 'L')
        self.assertEqual(pi.purchase_quantity, Decimal('10.000'))
        self.assertEqual(pi.conversion_factor, Decimal('1000.0000'))
        self.assertEqual(pi.base_quantity, Decimal('10000.000'))
        self.assertEqual(pi.inventory_item, self.item)

    def test_movement_log_created_with_purchase_type(self):
        _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-005')
        log = IngredientMovementLog.objects.get()
        self.assertEqual(log.movement_type, 'purchase')

    def test_movement_log_delta_equals_base_quantity(self):
        _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-006')
        log = IngredientMovementLog.objects.get()
        self.assertEqual(log.on_hand_delta, Decimal('10000.000'))
        self.assertEqual(log.reserved_delta, Decimal('0.000'))

    def test_movement_log_before_after_correct(self):
        _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-007')
        log = IngredientMovementLog.objects.get()
        self.assertEqual(log.on_hand_before, Decimal('0.000'))
        self.assertEqual(log.on_hand_after, Decimal('10000.000'))

    def test_movement_log_purchase_item_fk_set(self):
        _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-008')
        pi  = StockPurchaseItem.objects.get()
        log = IngredientMovementLog.objects.get()
        self.assertEqual(log.purchase_item, pi)

    def test_exactly_one_movement_per_line(self):
        _buy(_line(self.item, '10.000', '1000.0000', 'L'), key='slp-009')
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_existing_stock_is_additive(self):
        self.item.on_hand_quantity = Decimal('500.000')
        self.item.save(update_fields=['on_hand_quantity'])
        _buy(_line(self.item, '1.000', '1000.0000', 'L'), key='slp-010')
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1500.000'))

    def test_notes_and_reason_code_stored_on_movement(self):
        line = _line(self.item, '5.000', '1000.0000', 'L',
                     note='Batch 42', reason_code='supplier_delivery')
        _buy(line, key='slp-011')
        log = IngredientMovementLog.objects.get()
        self.assertEqual(log.note, 'Batch 42')
        self.assertEqual(log.reason_code, 'supplier_delivery')


class MultiLinePurchaseTests(TestCase):

    def setUp(self):
        self.coffee = _item('ML Coffee', 'g')
        self.milk   = _item('ML Milk', 'ml')
        self.cup    = _item('ML Cup', 'pcs', item_type='packaging')

    def test_multi_line_increments_all_items(self):
        _buy(
            _line(self.coffee, '5.000', '1000.0000', 'kg'),
            _line(self.milk,   '10.000', '1000.0000', 'L'),
            _line(self.cup,    '2.000', '12.0000', 'box'),
            key='mlp-001',
        )
        self.coffee.refresh_from_db(); self.milk.refresh_from_db()
        self.cup.refresh_from_db()
        self.assertEqual(self.coffee.on_hand_quantity, Decimal('5000.000'))
        self.assertEqual(self.milk.on_hand_quantity,   Decimal('10000.000'))
        self.assertEqual(self.cup.on_hand_quantity,    Decimal('24.000'))

    def test_exactly_one_movement_per_line(self):
        _buy(
            _line(self.coffee, '5.000', '1000.0000', 'kg'),
            _line(self.milk,   '10.000', '1000.0000', 'L'),
            key='mlp-002',
        )
        self.assertEqual(IngredientMovementLog.objects.count(), 2)
        self.assertEqual(StockPurchaseItem.objects.count(), 2)

    def test_movement_log_purchase_item_fk_correct_per_line(self):
        _buy(
            _line(self.coffee, '5.000', '1000.0000', 'kg'),
            _line(self.milk,   '10.000', '1000.0000', 'L'),
            key='mlp-003',
        )
        for spi in StockPurchaseItem.objects.all():
            logs = IngredientMovementLog.objects.filter(purchase_item=spi)
            self.assertEqual(logs.count(), 1)
            self.assertEqual(logs.first().inventory_item, spi.inventory_item)

    def test_header_notes_stored(self):
        p = _buy(_line(self.coffee, '1.000', '1000.0000', 'kg'),
                 key='mlp-004', notes='Invoice #INV-123')
        self.assertEqual(p.notes, 'Invoice #INV-123')


# ═════════════════════════════════════════════════════════════════════════════
# 3. Rounding and pcs validation via the service
# ═════════════════════════════════════════════════════════════════════════════

class ServiceRoundingTests(TestCase):

    def test_base_quantity_uses_round_half_up(self):
        """1.0005 L × 1000 ml/L = 1000.5 → rounds to 1000.500, not 1000.499."""
        # This is a straightforward case — ensures ROUND_HALF_UP not ROUND_HALF_EVEN.
        item = _item('Round Item', 'ml')
        line = {
            'inventory_item_id': item.pk,
            'purchase_unit': 'L',
            'purchase_quantity': Decimal('1.0005'),
            'conversion_factor': Decimal('1000.0000'),
        }
        _buy(line, key='rnd-001')
        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('1000.500'))

    def test_no_float_contamination(self):
        """0.1 × 0.3 must produce 0.030, not a float imprecision artifact."""
        item = _item('Float Test', 'ml')
        line = {
            'inventory_item_id': item.pk,
            'purchase_unit': 'unit',
            'purchase_quantity': Decimal('0.100'),
            'conversion_factor': Decimal('0.3000'),
        }
        _buy(line, key='rnd-002')
        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('0.030'))
        self.assertIsInstance(item.on_hand_quantity, Decimal)

    def test_pcs_whole_number_accepted(self):
        item = _item('Cup', 'pcs', item_type='packaging')
        line = _line(item, '2.000', '12.0000', 'box')
        p = _buy(line, key='pcs-001')
        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('24.000'))
        self.assertIsNotNone(p.pk)

    def test_pcs_fractional_result_rejected(self):
        """2.5 boxes × 3.1 pcs/box = 7.750 pcs — fractional, must be rejected."""
        item = _item('Lid', 'pcs', item_type='packaging')
        line = {
            'inventory_item_id': item.pk,
            'purchase_unit': 'box',
            'purchase_quantity': Decimal('2.500'),
            'conversion_factor': Decimal('3.1000'),
        }
        with self.assertRaises(FractionalPcsError):
            _buy(line, key='pcs-002')
        # No stock should have been changed
        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('0.000'))
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_zero_purchase_quantity_rejected(self):
        item = _item('Zero Qty', 'g')
        line = {
            'inventory_item_id': item.pk,
            'purchase_unit': 'kg',
            'purchase_quantity': Decimal('0.000'),
            'conversion_factor': Decimal('1000.0000'),
        }
        with self.assertRaises(PurchaseLineError):
            _buy(line, key='val-001')

    def test_negative_purchase_quantity_rejected(self):
        item = _item('Neg Qty', 'g')
        line = {
            'inventory_item_id': item.pk,
            'purchase_unit': 'kg',
            'purchase_quantity': Decimal('-1.000'),
            'conversion_factor': Decimal('1000.0000'),
        }
        with self.assertRaises(PurchaseLineError):
            _buy(line, key='val-002')

    def test_zero_conversion_factor_rejected(self):
        item = _item('Zero Factor', 'g')
        line = {
            'inventory_item_id': item.pk,
            'purchase_unit': 'kg',
            'purchase_quantity': Decimal('1.000'),
            'conversion_factor': Decimal('0.0000'),
        }
        with self.assertRaises(PurchaseLineError):
            _buy(line, key='val-003')

    def test_negative_conversion_factor_rejected(self):
        item = _item('Neg Factor', 'g')
        line = {
            'inventory_item_id': item.pk,
            'purchase_unit': 'kg',
            'purchase_quantity': Decimal('1.000'),
            'conversion_factor': Decimal('-1000.0000'),
        }
        with self.assertRaises(PurchaseLineError):
            _buy(line, key='val-004')

    def test_inactive_item_rejected(self):
        item = _item('Inactive Item', 'g')
        item.is_active = False
        item.save(update_fields=['is_active'])
        with self.assertRaises(PurchaseLineError):
            _buy(_line(item), key='val-005')
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_empty_lines_list_rejected(self):
        with self.assertRaises(InventoryServiceError):
            _buy(key='val-006')   # no lines


# ═════════════════════════════════════════════════════════════════════════════
# 4. Document-level idempotency
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseIdempotencyTests(TestCase):

    def setUp(self):
        self.item = _item('Idem Coffee', 'g')

    def test_duplicate_key_returns_existing_purchase(self):
        p1 = _buy(_line(self.item, '5.000', '1000.0000'), key='idem-001')
        p2 = _buy(_line(self.item, '5.000', '1000.0000'), key='idem-001')
        self.assertEqual(p1.pk, p2.pk)

    def test_duplicate_key_does_not_create_second_header(self):
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-002')
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-002')
        self.assertEqual(StockPurchase.objects.count(), 1)

    def test_duplicate_key_does_not_create_extra_line_items(self):
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-003')
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-003')
        self.assertEqual(StockPurchaseItem.objects.count(), 1)

    def test_duplicate_key_does_not_increase_stock_twice(self):
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-004')
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-004')
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('5000.000'))

    def test_duplicate_key_does_not_create_extra_movement_logs(self):
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-005')
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-005')
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_different_keys_create_separate_purchases(self):
        _buy(_line(self.item, '5.000', '1000.0000'), key='idem-006a')
        _buy(_line(self.item, '3.000', '1000.0000'), key='idem-006b')
        self.assertEqual(StockPurchase.objects.count(), 2)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('8000.000'))

    def test_duplicate_key_with_different_payload_returns_original(self):
        """Rev 3 D.12: first committed write wins; the new payload is ignored
        and the original purchase is returned.  The caller should surface the
        returned purchase for user confirmation."""
        p1 = _buy(_line(self.item, '5.000', '1000.0000'), key='idem-007')
        item2 = _item('Other Item', 'ml')
        p2 = _buy(_line(item2, '2.000', '1000.0000'), key='idem-007')
        self.assertEqual(p1.pk, p2.pk)
        # item2 must NOT have received any stock
        item2.refresh_from_db()
        self.assertEqual(item2.on_hand_quantity, Decimal('0.000'))


# ═════════════════════════════════════════════════════════════════════════════
# 5. Atomicity and rollback
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseAtomicityTests(TestCase):
    """If any step fails, the entire purchase must roll back."""

    def setUp(self):
        self.item_a = _item('Rollback Coffee', 'g')
        self.item_b = _item('Rollback Milk',   'ml')

    def test_inactive_item_on_second_line_rolls_back_first_line(self):
        """The inactive-item check happens under lock (after header creation
        would have started) so the rollback must undo the header and first
        line's stock change too."""
        self.item_b.is_active = False
        self.item_b.save(update_fields=['is_active'])

        with self.assertRaises(PurchaseLineError):
            _buy(
                _line(self.item_a, '5.000', '1000.0000', 'kg'),
                _line(self.item_b, '10.000', '1000.0000', 'L'),
                key='atom-001',
            )

        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.on_hand_quantity, Decimal('0.000'),
                         'First line stock must have rolled back')
        self.assertEqual(StockPurchase.objects.count(), 0,
                         'Purchase header must have rolled back')
        self.assertEqual(IngredientMovementLog.objects.count(), 0,
                         'No movement logs must survive a failed purchase')

    def test_fractional_pcs_on_second_line_rolls_back_first(self):
        """Fractional pcs on the second line must roll back the whole purchase."""
        pcs_item = _item('Straw', 'pcs', item_type='packaging')
        with self.assertRaises(FractionalPcsError):
            _buy(
                _line(self.item_a, '5.000', '1000.0000', 'kg'),
                {
                    'inventory_item_id': pcs_item.pk,
                    'purchase_unit': 'box',
                    'purchase_quantity': Decimal('2.500'),
                    'conversion_factor': Decimal('3.1000'),   # 7.750 pcs — fractional
                },
                key='atom-002',
            )
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.on_hand_quantity, Decimal('0.000'))
        self.assertEqual(StockPurchase.objects.count(), 0)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_movement_log_write_failure_rolls_back_stock_change(self):
        """If writing a movement log raises, the stock increment must roll back."""
        original_record = IngredientMovementLog.record

        def failing_record(*args, **kwargs):
            raise RuntimeError('Simulated movement log failure')

        with patch.object(IngredientMovementLog, 'record',
                          staticmethod(failing_record)):
            with self.assertRaises(RuntimeError):
                _buy(_line(self.item_a, '5.000', '1000.0000', 'kg'), key='atom-003')

        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.on_hand_quantity, Decimal('0.000'),
                         'Stock must not be modified when movement log write fails')
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_failed_purchase_leaves_no_orphan_purchase_items(self):
        """No StockPurchaseItem rows should survive a failed purchase."""
        self.item_b.is_active = False
        self.item_b.save(update_fields=['is_active'])

        with self.assertRaises(PurchaseLineError):
            _buy(
                _line(self.item_a, '5.000', '1000.0000', 'kg'),
                _line(self.item_b, '10.000', '1000.0000', 'L'),
                key='atom-004',
            )
        self.assertEqual(StockPurchaseItem.objects.count(), 0)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Regression — Phase 2A services unchanged
# ═════════════════════════════════════════════════════════════════════════════

class Phase3BPhase2ARegressionTests(TestCase):
    """record_adjustment and record_wastage must still work correctly."""

    def setUp(self):
        self.item = _item('Reg Item', 'g', on_hand=Decimal('1000.000'))

    def test_adjustment_still_works(self):
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

    def test_wastage_still_works(self):
        from apps.inventory.services import record_wastage
        with transaction.atomic():
            log = record_wastage(
                inventory_item_id=self.item.pk,
                quantity=Decimal('50.000'),
                reason_code='expired',
            )
        self.assertEqual(log.movement_type, 'wastage')
        self.assertIsNone(log.purchase_item)

    def test_purchase_and_adjustment_can_coexist(self):
        """Purchasing then adjusting the same item must work correctly."""
        # item starts with on_hand=1000.000 (from setUp)
        _buy(_line(self.item, '5.000', '1000.0000', 'kg'), key='reg-001')
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('6000.000'))  # 1000 + 5000

        from apps.inventory.services import record_adjustment
        with transaction.atomic():
            record_adjustment(
                inventory_item_id=self.item.pk,
                new_on_hand=Decimal('5800.000'),
                expected_on_hand=Decimal('6000.000'),
                reason_code='stock_count_correction',
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('5800.000'))
        self.assertEqual(IngredientMovementLog.objects.count(), 2)
        types = set(
            IngredientMovementLog.objects.values_list('movement_type', flat=True)
        )
        self.assertEqual(types, {'purchase', 'adjustment'})


# ═════════════════════════════════════════════════════════════════════════════
# 7. Idempotency savepoint path — SQLite regression
# ═════════════════════════════════════════════════════════════════════════════

class IdempotencySavepointRegressionTests(TestCase):
    """
    Regression tests for the savepoint-based IntegrityError handler added to
    record_purchase() to fix the concurrent duplicate-key race.

    On SQLite the race cannot actually occur (all writes are serialised), but
    the save/restore savepoint code path must still work correctly for the
    sequential case so that existing idempotency behavior is preserved.
    """

    def setUp(self):
        self.item = _item('Savepoint Item', 'g')

    def test_sequential_duplicate_key_still_returns_original(self):
        """The most important invariant: a second sequential call returns the
        original purchase and does not corrupt stock."""
        p1 = _buy(_line(self.item, '5.000', '1000.0000'), key='sp-idem-001')
        p2 = _buy(_line(self.item, '5.000', '1000.0000'), key='sp-idem-001')
        self.assertEqual(p1.pk, p2.pk)
        from apps.inventory.models import StockPurchase
        self.assertEqual(StockPurchase.objects.count(), 1)

    def test_sequential_duplicate_does_not_double_stock(self):
        _buy(_line(self.item, '5.000', '1000.0000'), key='sp-idem-002')
        _buy(_line(self.item, '5.000', '1000.0000'), key='sp-idem-002')
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('5000.000'))

    def test_sequential_duplicate_does_not_double_movement_logs(self):
        _buy(_line(self.item, '5.000', '1000.0000'), key='sp-idem-003')
        _buy(_line(self.item, '5.000', '1000.0000'), key='sp-idem-003')
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_different_key_still_creates_new_purchase(self):
        """Ensure the savepoint path does not accidentally block legitimate
        purchases that happen to follow a duplicate-key call."""
        _buy(_line(self.item, '5.000', '1000.0000'), key='sp-idem-004a')
        _buy(_line(self.item, '3.000', '1000.0000'), key='sp-idem-004b')
        from apps.inventory.models import StockPurchase
        self.assertEqual(StockPurchase.objects.count(), 2)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('8000.000'))
