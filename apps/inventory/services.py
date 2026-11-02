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

from django.db import IntegrityError, transaction

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


# ── Purchase service ──────────────────────────────────────────────────────────

# Phase 3-2 imports — placed here (not at module top) to keep the import
# section minimal for the existing services and avoid circular-import risk
# from the new Phase 3-1 models during early startup.
from decimal import ROUND_HALF_UP


class FractionalPcsError(InventoryServiceError):
    """
    Raised when a purchase of a 'pcs' item yields a fractional base_quantity.

    Rev 3 §D.2: 'pcs items accept whole numbers only'. A purchase of 2.5
    boxes × 3 pcs/box = 7.500 pcs is fine (whole), but 2.5 boxes × 3.1
    pcs/box = 7.750 pcs is not.
    """
    def __init__(self, item, base_quantity):
        self.item         = item
        self.base_quantity = base_quantity
        super().__init__(
            f'Purchase of "{item.name}" would add {base_quantity} pcs, '
            'which is not a whole number. '
            'Adjust the purchase quantity or conversion factor.'
        )


class PurchaseLineError(InventoryServiceError):
    """
    Raised for a validation failure on a specific purchase line (e.g. inactive
    item, non-positive base_quantity after rounding).

    Carries `line_index` (0-based) so the caller can point to the offending
    line in the UI.
    """
    def __init__(self, message, line_index=None):
        self.line_index = line_index
        super().__init__(message)


def _compute_base_quantity(purchase_quantity, conversion_factor):
    """
    Apply the single rounding rule from Rev 3 §D.7.

        base_quantity = quantize(purchase_quantity × conversion_factor,
                                 0.001, ROUND_HALF_UP)

    Both inputs are converted to Decimal via str() to avoid any float
    representation error before the multiplication.  The ROUND_HALF_UP
    mode ensures 0.0005 rounds to 0.001 (not 0.000 as ROUND_HALF_EVEN
    would give in some cases).

    This is the ONLY place rounding occurs in the whole inventory design.
    """
    qty    = Decimal(str(purchase_quantity))
    factor = Decimal(str(conversion_factor))
    return (qty * factor).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


def record_purchase(
    *,
    purchase_date,
    lines,
    idempotency_key,
    notes='',
    performed_by=None,
):
    """
    Record a stock-purchase event: create the purchase header, purchase lines,
    update on-hand stock, and write movement logs.

    Rev 3 §D.7, §H.1, §H.5.

    This service opens its own ``transaction.atomic()`` block because a purchase
    is a top-level write operation (unlike adjustment/wastage, which are called
    from views that supply their own outer atomic context).  The entire purchase —
    header, all lines, all stock updates, all movement rows — commits or rolls
    back as one unit.

    Idempotency
    -----------
    If ``idempotency_key`` already exists in ``StockPurchase``, the existing
    record is returned immediately without touching inventory or writing
    movement logs.  This allows the purchase form to safely retry after a
    network timeout.

    If the key is submitted with *different* line data, the already-committed
    purchase is returned unchanged (the new payload is silently ignored).
    Rev 3 §D.12 records this as the correct safe behavior: the first committed
    write wins; retries must be payload-identical in practice.  The caller
    should surface the returned purchase to the user for confirmation.

    Parameters
    ----------
    purchase_date : datetime.date
        The date the stock was received.
    lines : list[dict]
        Each element must contain:
          ``inventory_item_id`` : int   — PK of the InventoryItem
          ``purchase_unit``     : str   — purchase unit name (snapshot)
          ``purchase_quantity`` : Decimal or str — quantity in purchase_unit
          ``conversion_factor`` : Decimal or str — factor from UnitConversion
        Optional per-line keys:
          ``note``              : str   — staff note on this line
          ``reason_code``       : str   — machine-readable code
    idempotency_key : str
        Unique key for this purchase document.  Duplicate keys return the
        existing document.
    notes : str, optional
        Header-level notes (supplier name, invoice number, etc.).
    performed_by : CustomUser, optional
        Staff member recording the purchase.

    Returns
    -------
    StockPurchase
        The newly created (or pre-existing idempotent) purchase record.

    Raises
    ------
    PurchaseLineError
        A line references an inactive item, yields a non-positive
        base_quantity, or contains an invalid purchase_quantity /
        conversion_factor.
    FractionalPcsError
        A 'pcs' item yields a fractional base_quantity after rounding.
    InventoryServiceError
        Any other validation failure (e.g. empty lines list).
    """
    from apps.inventory.models import StockPurchase, StockPurchaseItem

    if not lines:
        raise InventoryServiceError('A purchase must have at least one line.')

    # ── Idempotency check (outside the write transaction) ────────────────────
    # Using get_or_create here would be a read-modify-write race; instead we
    # attempt a plain fetch first.  If the key already exists we return early
    # before acquiring any locks.  If it doesn't exist yet, we proceed into
    # the atomic block where the unique constraint is the final guard.
    existing = StockPurchase.objects.filter(
        idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        return existing

    # ── Pre-compute base_quantity for every line ──────────────────────────────
    # Done before the transaction so validation errors surface cheaply.
    prepared_lines = []
    for idx, line in enumerate(lines):
        try:
            purchase_qty = Decimal(str(line['purchase_quantity']))
            factor       = Decimal(str(line['conversion_factor']))
        except Exception as exc:
            raise PurchaseLineError(
                f'Line {idx}: invalid purchase_quantity or conversion_factor — {exc}',
                line_index=idx,
            ) from exc

        if purchase_qty <= Decimal('0'):
            raise PurchaseLineError(
                f'Line {idx}: purchase_quantity must be greater than zero '
                f'(got {purchase_qty}).',
                line_index=idx,
            )
        if factor <= Decimal('0'):
            raise PurchaseLineError(
                f'Line {idx}: conversion_factor must be greater than zero '
                f'(got {factor}).',
                line_index=idx,
            )

        base_qty = _compute_base_quantity(purchase_qty, factor)

        if base_qty <= Decimal('0'):
            raise PurchaseLineError(
                f'Line {idx}: computed base_quantity is {base_qty}, '
                'which is not positive. Check purchase_quantity and factor.',
                line_index=idx,
            )

        prepared_lines.append({
            'inventory_item_id': int(line['inventory_item_id']),
            'purchase_unit':     str(line['purchase_unit']),
            'purchase_quantity': purchase_qty,
            'conversion_factor': factor,
            'base_quantity':     base_qty,
            'note':              line.get('note', ''),
            'reason_code':       line.get('reason_code', ''),
        })

    # ── Atomic write block ────────────────────────────────────────────────────
    with transaction.atomic():

        # Re-check idempotency inside the transaction.  A concurrent request
        # with the same key may have committed between our outer check and
        # here.  The unique constraint on idempotency_key would also raise an
        # IntegrityError if we tried to create a duplicate, but catching that
        # inside an active atomic block would leave the transaction in a broken
        # savepoint state.  Re-reading is the clean approach.
        existing = StockPurchase.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return existing

        # ── Lock all affected InventoryItem rows in ascending PK order ────────
        # Rev 3 §H.1 level 3: one ordered SELECT ... FOR UPDATE.
        # Manual purchase operations take only level 3 locks (no Order or
        # Product locks), so they never invert the hierarchy.
        item_ids = [pl['inventory_item_id'] for pl in prepared_lines]
        locked   = lock_inventory_items(item_ids)

        # ── Validate items under lock ─────────────────────────────────────────
        for idx, pl in enumerate(prepared_lines):
            item = locked[pl['inventory_item_id']]
            if not item.is_active:
                raise PurchaseLineError(
                    f'Line {idx}: inventory item "{item.name}" is inactive '
                    'and cannot receive stock.',
                    line_index=idx,
                )
            # pcs items must receive whole-number quantities (Rev 3 §D.2).
            if item.base_unit == InventoryItem.BASE_UNIT_PCS:
                if pl['base_quantity'] % Decimal('1') != Decimal('0'):
                    raise FractionalPcsError(item, pl['base_quantity'])

        # ── Create the purchase header with savepoint-based idempotency guard ───
        #
        # The outer and inner re-reads above handle the common sequential case
        # (a user clicking Submit twice with a gap between clicks).
        #
        # For true concurrent requests arriving with the *same unused key*:
        #   1. Both pass the outer read — the key does not exist yet.
        #   2. Both enter this transaction.
        #   3. Both pass the inner re-read under READ COMMITTED (neither has
        #      committed yet, so neither sees the other's uncommitted row).
        #   4. One of them calls objects.create() first and commits.
        #   5. The second one's create() raises IntegrityError on the unique
        #      constraint.
        #
        # If the IntegrityError escapes the transaction, Django marks the
        # whole transaction as needing rollback and the caller gets an
        # unhandled exception — leaving no partial state but giving no useful
        # result either.
        #
        # The safe pattern on PostgreSQL is a SAVEPOINT: we try the create
        # inside a nested atomic block (savepoint).  If it raises IntegrityError
        # the savepoint is rolled back (only the failed create is undone; the
        # outer transaction stays alive) and we re-read to obtain the
        # committed row that won the race.
        #
        # On SQLite the savepoint is accepted and the rollback works
        # identically (SQLite serialises all writes so the race cannot occur,
        # but the code path is still exercised and safe).
        try:
            with transaction.atomic(savepoint=True):
                purchase = StockPurchase.objects.create(
                    purchase_date=purchase_date,
                    notes=notes,
                    performed_by=performed_by,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            # Another transaction committed the same key while we were inside
            # this one.  Fetch the winner and return it immediately — no stock
            # changes, no lines, no movement logs.
            committed = StockPurchase.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if committed is None:
                # Should be unreachable: the constraint fired so the row must
                # exist.  Raise so the caller can investigate.
                raise InventoryServiceError(
                    f'Concurrent purchase with key "{idempotency_key}" '
                    'raised IntegrityError but the committed row was not found.'
                )
            return committed

        # ── Process each line in ascending item PK order ──────────────────────
        # Rev 3 §H.5: "FOR line IN lines ascending by item id"
        sorted_lines = sorted(prepared_lines, key=lambda pl: pl['inventory_item_id'])

        for pl in sorted_lines:
            item         = locked[pl['inventory_item_id']]
            base_qty     = pl['base_quantity']
            on_hand_before  = item.on_hand_quantity
            reserved_before = item.reserved_quantity
            new_on_hand     = on_hand_before + base_qty

            # ── Create the purchase line (snapshot) ───────────────────────────
            purchase_item = StockPurchaseItem.objects.create(
                purchase=purchase,
                inventory_item=item,
                purchase_unit=pl['purchase_unit'],
                purchase_quantity=pl['purchase_quantity'],
                conversion_factor=pl['conversion_factor'],
                base_quantity=base_qty,
            )

            # ── Apply stock update: conditional UPDATE as backstop ────────────
            # On PostgreSQL, SELECT FOR UPDATE above serialises access.
            # The WHERE clause is an additional safety net (same as Phase 2A).
            # Stock must only increase on a purchase; never decrease.
            updated = InventoryItem.objects.filter(
                pk=item.pk,
                on_hand_quantity=on_hand_before,   # backstop: reject stale writes
            ).update(on_hand_quantity=new_on_hand)

            if not updated:
                raise InventoryServiceError(
                    f'Stock for "{item.name}" was modified by a concurrent '
                    'request. Please try the purchase again.'
                )

            # Refresh the in-memory instance so the next iteration (if this
            # item appears in multiple lines — disallowed by schema but
            # defensive) reads the updated value.
            item.refresh_from_db(fields=['on_hand_quantity'])

            # ── Write the movement log ────────────────────────────────────────
            IngredientMovementLog.record(
                inventory_item=item,
                movement_type=IngredientMovementLog.MOVEMENT_PURCHASE,
                on_hand_delta=base_qty,
                reserved_delta=Decimal('0'),
                on_hand_before=on_hand_before,
                reserved_before=reserved_before,
                reason_code=pl['reason_code'],
                note=pl['note'],
                purchase_item=purchase_item,
                performed_by=performed_by,
            )

    return purchase
