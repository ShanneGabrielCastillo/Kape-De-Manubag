"""
apps/inventory/services.py
==========================
Phase 2A — Ingredient Inventory Services

Provides:
  lock_inventory_items(item_ids)   — ordered-lock helper (Rev 3 §H.1)
  record_adjustment(...)           — manual stock adjustment service
  record_wastage(...)              — wastage / spoilage service

All services are purely functional: they accept parameters, work inside a
caller-provided transaction.atomic() block, and raise domain exceptions on
invalid input. They never open their own outermost transaction.

Business logic lives here, not in views or models. Views catch the domain
exceptions defined at the bottom of this module and translate them into
user-facing messages.

No view, URL, form, or template code belongs in this file.
"""

from decimal import Decimal

from django.db import transaction

from apps.inventory.models import IngredientMovementLog, InventoryItem


# ── Domain exceptions ─────────────────────────────────────────────────────────

class InventoryServiceError(ValueError):
    """
    Base class for all inventory service errors.

    Subclasses ValueError so existing view-level `except ValueError` handlers
    catch them without modification — matching the pattern already established
    in checkout_view and create_pos_order.
    """


class InsufficientStockError(InventoryServiceError):
    """
    Raised when a requested quantity exceeds available stock.

    Carries the item, the requested quantity, and the available quantity so
    callers can construct precise error messages.
    """
    def __init__(self, item, requested, available):
        self.item      = item
        self.requested = requested
        self.available = available
        super().__init__(
            f'Insufficient stock for "{item.name}": '
            f'requested {requested} {item.base_unit}, '
            f'available {available} {item.base_unit}.'
        )


class StaleExpectedQuantityError(InventoryServiceError):
    """
    Raised by record_adjustment when expected_on_hand doesn't match the
    current on_hand_quantity.

    This is an optimistic-concurrency guard: if someone else changed the
    stock between the time the adjustment form was loaded and the time it
    was submitted, we refuse the save rather than silently overwriting the
    intermediate change.
    """
    def __init__(self, item, expected, actual):
        self.item     = item
        self.expected = expected
        self.actual   = actual
        super().__init__(
            f'Stock for "{item.name}" has changed since you loaded the page. '
            f'Expected {expected} {item.base_unit}, '
            f'current value is {actual} {item.base_unit}. '
            'Please reload the page and try again.'
        )


class ReservedConflictError(InventoryServiceError):
    """
    Raised when a downward adjustment would set on_hand below reserved_quantity.

    This would make available_quantity negative, violating the core invariant.
    Staff must cancel the open orders that hold the reservations before
    adjusting stock below the reserved level.
    """
    def __init__(self, item, new_on_hand, reserved):
        self.item        = item
        self.new_on_hand = new_on_hand
        self.reserved    = reserved
        super().__init__(
            f'Cannot adjust "{item.name}" to {new_on_hand} {item.base_unit}: '
            f'{reserved} {item.base_unit} is currently reserved by open orders. '
            'Cancel or complete those orders first.'
        )


class ExceedsAvailableError(InventoryServiceError):
    """
    Raised when a wastage quantity exceeds available (non-reserved) stock.

    Waste of reserved stock must go through the order cancellation path, not
    through the wastage screen.
    """
    def __init__(self, item, requested, available):
        self.item      = item
        self.requested = requested
        self.available = available
        super().__init__(
            f'Cannot record wastage of {requested} {item.base_unit} '
            f'for "{item.name}": only {available} {item.base_unit} is available '
            '(not reserved). Waste of reserved stock must be handled through '
            'order cancellation.'
        )


# ── Ordered-lock helper ───────────────────────────────────────────────────────

def lock_inventory_items(item_ids):
    """
    Lock and return InventoryItem rows in ascending primary-key order.

    Rev 3 §H.1 Global lock hierarchy, level 3:
      Always acquire InventoryItem locks with one ordered SELECT ... FOR UPDATE
      statement, never one lock at a time in arbitrary order.

    Acquiring locks in a consistent ascending-pk order prevents deadlocks
    when two concurrent operations need overlapping items:
      - Transaction A needs items {1, 3} and Transaction B needs items {3, 1}.
      - With arbitrary ordering they can deadlock.
      - With ascending pk order, both acquire in the same sequence so one
        simply waits for the other.

    The caller MUST be inside a transaction.atomic() block. On SQLite,
    select_for_update() is accepted but does not produce real row locks;
    the DB-level CHECK constraints and the conditional UPDATE backstop in
    the write helpers maintain correctness there anyway.

    Parameters
    ----------
    item_ids : iterable of int
        Primary keys of the InventoryItem rows to lock. Duplicates are
        deduplicated; order does not matter (the query enforces ascending pk).

    Returns
    -------
    dict[int, InventoryItem]
        Mapping of pk → locked InventoryItem instance, ordered by pk.
        Raises InventoryItem.DoesNotExist (wrapped as InventoryServiceError)
        if any requested pk is not found.
    """
    unique_ids = sorted(set(int(pk) for pk in item_ids))
    if not unique_ids:
        return {}

    rows = (
        InventoryItem.objects
        .select_for_update()
        .filter(pk__in=unique_ids)
        .order_by('pk')
    )
    locked = {item.pk: item for item in rows}

    missing = [pk for pk in unique_ids if pk not in locked]
    if missing:
        raise InventoryServiceError(
            f'InventoryItem pk(s) not found: {missing}. '
            'The item may have been deleted.'
        )

    return locked


# ── Manual adjustment service ─────────────────────────────────────────────────

def record_adjustment(
    *,
    inventory_item_id,
    new_on_hand,
    expected_on_hand,
    reason_code='',
    note='',
    performed_by=None,
    idempotency_key=None,
):
    """
    Set on_hand_quantity to new_on_hand, recording the change in the movement
    log.

    Rev 3 §H.5 adjust_stock pseudocode.

    Validation (in order):
      1. new_on_hand must be >= 0.
      2. new_on_hand must be != current on_hand (a no-op is rejected; callers
         should check before calling).
      3. expected_on_hand must match the current on_hand_quantity (optimistic
         concurrency guard — someone else may have moved stock since the form
         was loaded).
      4. new_on_hand must be >= current reserved_quantity (cannot create a
         situation where available_quantity would be negative).

    All validation happens after acquiring the row lock so the checks see
    the current committed state.

    Parameters
    ----------
    inventory_item_id : int
        PK of the InventoryItem to adjust.
    new_on_hand : Decimal or str
        The desired new on_hand_quantity value.
    expected_on_hand : Decimal or str
        The value the caller last read for on_hand_quantity. If this differs
        from the current DB value, the adjustment is rejected.
    reason_code : str, optional
        Short machine-readable reason (e.g. 'stock_count_correction').
    note : str, optional
        Human-readable staff note.
    performed_by : CustomUser, optional
        Staff member performing the adjustment.
    idempotency_key : str, optional
        If provided and a movement with this key already exists, the existing
        movement is returned without creating a duplicate.

    Returns
    -------
    IngredientMovementLog
        The newly created (or existing idempotent) movement row.

    Raises
    ------
    InventoryServiceError
        new_on_hand < 0 or is equal to current on_hand (no-op).
    StaleExpectedQuantityError
        expected_on_hand != current on_hand_quantity.
    ReservedConflictError
        new_on_hand < current reserved_quantity.
    """
    new_on_hand      = Decimal(str(new_on_hand))
    expected_on_hand = Decimal(str(expected_on_hand))

    if new_on_hand < Decimal('0'):
        raise InventoryServiceError('New on-hand quantity cannot be negative.')

    # Acquire the row lock inside the caller's transaction.
    locked = lock_inventory_items([inventory_item_id])
    item   = locked[inventory_item_id]

    # Optimistic concurrency: reject if someone else changed stock since load.
    if item.on_hand_quantity != expected_on_hand:
        raise StaleExpectedQuantityError(item, expected_on_hand, item.on_hand_quantity)

    on_hand_delta = new_on_hand - item.on_hand_quantity
    if on_hand_delta == Decimal('0'):
        raise InventoryServiceError(
            f'No change: "{item.name}" already has '
            f'{item.on_hand_quantity} {item.base_unit} on hand.'
        )

    # Guard: cannot set on-hand below what is currently reserved.
    if new_on_hand < item.reserved_quantity:
        raise ReservedConflictError(item, new_on_hand, item.reserved_quantity)

    on_hand_before  = item.on_hand_quantity
    reserved_before = item.reserved_quantity

    # Apply the change with a conditional UPDATE as a backstop.
    # On PostgreSQL the SELECT FOR UPDATE above already serialises access;
    # the WHERE clause here is an additional safety net.
    updated = InventoryItem.objects.filter(
        pk=item.pk,
        on_hand_quantity=on_hand_before,   # backstop: reject stale writes
    ).update(on_hand_quantity=new_on_hand)

    if not updated:
        raise InventoryServiceError(
            f'Stock for "{item.name}" was modified by a concurrent request. '
            'Please reload the page and try again.'
        )

    # Refresh the in-memory instance to reflect the DB change.
    item.refresh_from_db(fields=['on_hand_quantity'])

    movement = IngredientMovementLog.record(
        inventory_item=item,
        movement_type=IngredientMovementLog.MOVEMENT_ADJUSTMENT,
        on_hand_delta=on_hand_delta,
        reserved_delta=Decimal('0'),
        on_hand_before=on_hand_before,
        reserved_before=reserved_before,
        reason_code=reason_code,
        note=note,
        idempotency_key=idempotency_key,
        performed_by=performed_by,
    )

    return movement


# ── Wastage service ───────────────────────────────────────────────────────────

def record_wastage(
    *,
    inventory_item_id,
    quantity,
    reason_code='',
    note='',
    performed_by=None,
    idempotency_key=None,
):
    """
    Record a wastage / spoilage event, reducing on_hand_quantity.

    Rev 3 §H.5 record_wastage pseudocode.

    Wastage reduces on_hand only (not reserved). Only available stock
    (on_hand − reserved) may be wasted through this service. Waste of
    reserved stock is handled through the order cancellation path.

    Parameters
    ----------
    inventory_item_id : int
        PK of the InventoryItem to mark as wasted.
    quantity : Decimal or str
        Quantity to write off, in the item's base unit. Must be > 0.
    reason_code : str, optional
        Short machine-readable reason (e.g. 'expired', 'spilled', 'damaged').
    note : str, optional
        Human-readable staff note.
    performed_by : CustomUser, optional
        Staff member recording the wastage.
    idempotency_key : str, optional
        Duplicate-submission guard (see record_adjustment).

    Returns
    -------
    IngredientMovementLog
        The newly created (or existing idempotent) movement row.

    Raises
    ------
    InventoryServiceError
        quantity <= 0.
    ExceedsAvailableError
        quantity > available stock (on_hand − reserved).
    """
    quantity = Decimal(str(quantity))

    if quantity <= Decimal('0'):
        raise InventoryServiceError(
            'Wastage quantity must be greater than zero.'
        )

    # Acquire the row lock inside the caller's transaction.
    locked = lock_inventory_items([inventory_item_id])
    item   = locked[inventory_item_id]

    available = item.on_hand_quantity - item.reserved_quantity
    if quantity > available:
        raise ExceedsAvailableError(item, quantity, available)

    on_hand_before  = item.on_hand_quantity
    reserved_before = item.reserved_quantity
    new_on_hand     = on_hand_before - quantity

    # Conditional UPDATE backstop (same pattern as record_adjustment).
    updated = InventoryItem.objects.filter(
        pk=item.pk,
        on_hand_quantity=on_hand_before,
    ).update(on_hand_quantity=new_on_hand)

    if not updated:
        raise InventoryServiceError(
            f'Stock for "{item.name}" was modified by a concurrent request. '
            'Please reload the page and try again.'
        )

    item.refresh_from_db(fields=['on_hand_quantity'])

    movement = IngredientMovementLog.record(
        inventory_item=item,
        movement_type=IngredientMovementLog.MOVEMENT_WASTAGE,
        on_hand_delta=-quantity,
        reserved_delta=Decimal('0'),
        on_hand_before=on_hand_before,
        reserved_before=reserved_before,
        reason_code=reason_code,
        note=note,
        idempotency_key=idempotency_key,
        performed_by=performed_by,
    )

    return movement
