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
    Raised when one or more requested quantities exceed available stock.

    Supports two calling conventions:

    **Single-item form (existing callers — unchanged):**
        raise InsufficientStockError(item=inv_item, requested=qty, available=avail)

    **Multi-item form (Rev3 §H.2 placement coordinator):**
        raise InsufficientStockError(shortages=[
            ('ingredient', inv_pk, name, required, available),
            ('legacy', product_pk, name, required, available),
        ])

    Both forms subclass ValueError so the existing view-level
    ``except ValueError`` handlers catch them without any view changes.
    """

    def __init__(self, item=None, requested=None, available=None, shortages=None):
        if item is not None:
            # ── Single-item form — existing callers unchanged ─────────────────
            self.item      = item
            self.requested = requested
            self.available = available
            self.shortages = None
            msg = (
                f'Insufficient stock for "{item.name}": '
                f'requested {requested} {item.base_unit}, '
                f'available {available} {item.base_unit}.'
            )
        else:
            # ── Multi-item form — placement coordinator (Rev3 §H.2 step 5) ───
            self.item      = None
            self.requested = None
            self.available = None
            self.shortages = shortages or []
            # Legacy shortages use the classic "Available: N, Requested: N"
            # format so that existing error-message tests remain valid.
            # Ingredient shortages use the structured "(required N, available N)"
            # format for clarity.
            parts = []
            for kind, pk, name, req, avail in self.shortages:
                if kind == 'legacy':
                    parts.append(
                        f'Insufficient stock for \'{name}\'. '
                        f'Available: {avail}, Requested: {req}'
                    )
                else:
                    parts.append(
                        f'{name} (required {req}, available {avail})'
                    )
            msg = '; '.join(parts) if parts else 'Insufficient stock'
        super().__init__(msg)


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


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4B-3 — resolve_plan() Service
# ══════════════════════════════════════════════════════════════════════════════
#
# Implements Rev 3 §I.2: the read-only resolver that converts a
# (product, size, order_type) request into a ResolvedPlan containing
# every ingredient and packaging component needed for one unit.
#
# This module is deliberately read-only.  resolve_plan() acquires no locks,
# writes nothing to the database, and changes no stock quantities.
# The caller (place_order_inventory, Phase 5B) holds the Product-row locks
# before calling here, consistent with Rev 3 §H.2 and §H.9.
# ══════════════════════════════════════════════════════════════════════════════

import dataclasses


# ── Configuration-error exception hierarchy ───────────────────────────────────
#
# Rev 3 has two names for configuration errors raised by resolve_plan:
#   §I.2 pseudocode uses the four specific names below.
#   §H.2 comment and §H.7 use "RecipeIncompleteError" as the umbrella.
#   §H.7: "InsufficientStockError, RecipeIncompleteError, and
#           DispositionRequired subclass ValueError"
#
# We reconcile this by making RecipeIncompleteError the shared base class
# for all four, while each specific class adds its own context attributes.
# The existing view-level `except ValueError` handlers catch all of them
# because InventoryServiceError already subclasses ValueError.

class RecipeIncompleteError(InventoryServiceError):
    """
    Base class for all configuration errors raised by resolve_plan().

    Rev 3 §H.7: subclasses ValueError (via InventoryServiceError) so the
    existing view-level except-ValueError handlers catch it without change.

    All four specific resolve_plan exceptions subclass this one so that
    place_order_inventory callers can either catch them all with one
    `except RecipeIncompleteError` or handle each specifically.
    """


class RecipeMissingError(RecipeIncompleteError):
    """
    No approved recipe exists for the requested (product, size, order_type).

    Rev 3 §I.2: "IF none: RAISE RecipeMissingError".

    Raised when:
    - No Recipe row at all for (product, size, scope IN ('all', order_type)).
    - Only draft or retired recipes exist (neither counts as approved).
    """
    def __init__(self, product_id, size, order_type):
        self.product_id = product_id
        self.size       = size
        self.order_type = order_type
        super().__init__(
            f'No approved recipe for product_id={product_id}, '
            f'size={size!r}, order_type={order_type!r}. '
            'A draft or retired recipe exists but only approved recipes '
            'may be used for order placement.'
        )


class AmbiguousRecipeError(RecipeIncompleteError):
    """
    More than one approved recipe matches the request.

    Rev 3 §I.2: "IF many: RAISE AmbiguousRecipeError".

    This indicates a misconfigured state: an 'all'-scope recipe and a
    scope-specific recipe are both approved for the same (product, size).
    Rev 3 §D.4 requires "For one (product, size) the approved set is
    either {all} or {dine_in, takeout}, never both." The DB partial unique
    index prevents duplicates per scope, but it cannot prevent both 'all'
    and 'dine_in' from being approved simultaneously — that is enforced
    at approval time and caught here as a backstop.
    """
    def __init__(self, product_id, size, order_type, matching_ids):
        self.product_id   = product_id
        self.size         = size
        self.order_type   = order_type
        self.matching_ids = matching_ids
        super().__init__(
            f'Ambiguous recipe configuration for product_id={product_id}, '
            f'size={size!r}, order_type={order_type!r}: '
            f'{len(matching_ids)} approved recipes found '
            f'(recipe ids: {matching_ids}). '
            'Retire all but one approved recipe for this combination.'
        )


class PackagingRuleMissingError(RecipeIncompleteError):
    """
    No ProductPackagingRule exists for the requested (product, size, order_type).

    Rev 3 §I.2: "IF rule IS NONE: RAISE PackagingRuleMissingError".
    Silence is never treated as 'no packaging' — an explicit rule is required.
    """
    def __init__(self, product_id, size, order_type):
        self.product_id = product_id
        self.size       = size
        self.order_type = order_type
        super().__init__(
            f'No packaging rule for product_id={product_id}, '
            f'size={size!r}, order_type={order_type!r}. '
            'Create a ProductPackagingRule with an approved profile or '
            'explicitly set no_packaging=True.'
        )


class InactiveItemError(RecipeIncompleteError):
    """
    A referenced InventoryItem is inactive, or its item_type does not match
    the expected type for its context.

    Rev 3 §I.2: "REQUIRE every referenced item is active, recipe items are
    ingredients, packaging items are packaging."
    Rev 3 §I.3: "Orders fail loudly with InactiveItemError."

    Raised in two situations:
    1. item.is_active is False.
    2. A RecipeItem references an item with item_type != 'ingredient'.
    3. A PackagingProfileItem references an item with item_type != 'packaging'.

    Also raised when the PackagingProfile referenced by a rule is not
    'approved' (a rule must point at an approved profile; a retired or
    draft profile fails validation here).
    """
    def __init__(self, message, item=None):
        self.item = item
        super().__init__(message)


# ── ResolvedPlan data structures ──────────────────────────────────────────────

#: Constants for ResolvedPlanComponent.component_source.
#: These match the 'recipe' | 'packaging' vocabulary used in
#: OrderIngredientReservationItem.component_source (Rev 3 §D.10).
COMPONENT_SOURCE_RECIPE    = 'recipe'
COMPONENT_SOURCE_PACKAGING = 'packaging'


@dataclasses.dataclass(frozen=True)
class ResolvedPlanComponent:
    """
    One ingredient or packaging requirement from a resolved plan.

    Rev 3 §D.10 maps directly to OrderIngredientReservationItem fields:
      inventory_item       → inventory_item FK
      component_source     → component_source ('recipe' | 'packaging')
      source_recipe_id     → source_recipe FK (null for packaging)
      source_packaging_profile_id → source_packaging_profile FK (null for ingredients)
      source_revision      → source_revision snapshot
      quantity_per_unit    → quantity_per_unit snapshot (Decimal, base units)

    Frozen so callers cannot mutate snapshot data after resolution.
    """
    inventory_item:               object    # InventoryItem instance
    component_source:             str       # COMPONENT_SOURCE_RECIPE or COMPONENT_SOURCE_PACKAGING
    source_recipe_id:             int | None
    source_packaging_profile_id:  int | None
    source_revision:              int
    quantity_per_unit:            Decimal


@dataclasses.dataclass(frozen=True)
class ResolvedPlan:
    """
    The complete ingredient + packaging plan for one unit of a product at a
    given size and order type.

    Rev 3 §I.2: "RETURN plan(components = recipe items + packaging items,
    with ids, revisions, quantities)".

    Fields
    ------
    product_id : int
        PK of the Product this plan was resolved for.
    size : str
        The size dimension (from Recipe.SIZE_CHOICES vocabulary).
    order_type : str
        'dine_in' or 'takeout'.
    recipe : Recipe
        The one approved Recipe that was selected.
    packaging_rule : ProductPackagingRule
        The rule that determined whether packaging is needed.
    components : tuple[ResolvedPlanComponent, ...]
        All ingredient components (source='recipe') followed by all
        packaging components (source='packaging').
        Empty packaging section when packaging_rule.no_packaging is True.

    Usage by place_order_inventory (Phase 5B)
    -----------------------------------------
    For one order line of quantity Q:
        required[comp.inventory_item.pk] += comp.quantity_per_unit * Q
    """
    product_id:      int
    size:            str
    order_type:      str
    recipe:          object    # Recipe instance
    packaging_rule:  object    # ProductPackagingRule instance
    components:      tuple     # tuple[ResolvedPlanComponent, ...]


# ── resolve_plan() ────────────────────────────────────────────────────────────

def resolve_plan(product_id, size, order_type):
    """
    Resolve the complete ingredient and packaging plan for one unit of a
    product at a given size and order type.

    Rev 3 §I.2 FUNCTION resolve_plan(product, size, order_type).

    This function is read-only.  It acquires no locks, writes nothing to the
    database, and does not modify any stock quantity.  The caller is
    responsible for holding the Product-row lock before calling here
    (Rev 3 §H.9) so that the configuration read here cannot race with a
    configuration write.

    Resolution steps
    ----------------
    1. Find approved Recipe rows whose (product, size, scope) matches:
           scope IN ('all', order_type)
       Exactly one must exist; zero → RecipeMissingError, two+ → AmbiguousRecipeError.

    2. Find the ProductPackagingRule for (product, size, order_type).
       Missing → PackagingRuleMissingError.

    3. If rule.no_packaging is True, packaging components are empty.
       Otherwise, verify rule.profile.status == 'approved'.

    4. Validate every referenced InventoryItem:
       - must be active (is_active=True) → InactiveItemError if not
       - RecipeItem items must have item_type='ingredient' → InactiveItemError
       - PackagingProfileItem items must have item_type='packaging' → InactiveItemError

    5. Return a ResolvedPlan with all components.

    Parameters
    ----------
    product_id : int
        PK of the menu.Product to resolve.
    size : str
        One of the SIZE_CHOICES values: 'none', 'small', 'medium', 'large', 'hot'.
    order_type : str
        Either 'dine_in' or 'takeout'.

    Returns
    -------
    ResolvedPlan
        Frozen dataclass with recipe, packaging_rule, and components list.

    Raises
    ------
    RecipeMissingError
        No approved recipe for (product, size, order_type).
    AmbiguousRecipeError
        More than one approved recipe matches (product, size, order_type).
    PackagingRuleMissingError
        No ProductPackagingRule for (product, size, order_type).
    InactiveItemError
        A referenced InventoryItem is inactive, wrong item_type, or the
        referenced packaging profile is not 'approved'.
    """
    # Deferred imports — avoid circular import risk during startup and keep the
    # top of services.py clean, consistent with the Phase 3 import pattern.
    from apps.inventory.models import (
        Recipe,
        RecipeItem,
        PackagingProfile,
        PackagingProfileItem,
        ProductPackagingRule,
    )

    # ── Step 1: recipe resolution ─────────────────────────────────────────────
    # Rev 3 §I.2: "approved Recipes WHERE product, size, scope IN ('all', order_type)"
    # select_related prefetches the recipe's items and their inventory_item in
    # one trip so the validation loop (step 4) does not issue N+1 queries.
    candidates = list(
        Recipe.objects
        .filter(
            product_id=product_id,
            size=size,
            status=Recipe.STATUS_APPROVED,
            order_type_scope__in=[Recipe.SCOPE_ALL, order_type],
        )
        .prefetch_related('items__inventory_item')
        .order_by('id')
    )

    if len(candidates) == 0:
        raise RecipeMissingError(product_id, size, order_type)

    if len(candidates) > 1:
        raise AmbiguousRecipeError(
            product_id, size, order_type,
            matching_ids=[r.pk for r in candidates],
        )

    recipe = candidates[0]

    # ── Step 2: packaging rule lookup ─────────────────────────────────────────
    # Rev 3 §D.6: ProductPackagingRule has no 'all' order_type; the lookup is
    # always an exact match on (product, size, order_type).
    # ppr_unique_combination guarantees at most one row; .get() is safe but we
    # use filter/first to return None rather than DoesNotExist on a miss.
    try:
        rule = ProductPackagingRule.objects.get(
            product_id=product_id,
            size=size,
            order_type=order_type,
        )
    except ProductPackagingRule.DoesNotExist:
        raise PackagingRuleMissingError(product_id, size, order_type)

    # ── Step 3: packaging profile validation ──────────────────────────────────
    # Rev 3 §I.2: "REQUIRE rule.profile.status = 'approved'"
    # (only checked when no_packaging is False — a profile must be set and live)
    packaging_items = []   # list[PackagingProfileItem]

    if not rule.no_packaging:
        # profile is guaranteed non-null by ppr_profile_xor_none constraint
        # when no_packaging is False, but we access .profile which is a FK —
        # prefetch_related is not used on the rule, so access it directly.
        profile = rule.profile
        if profile.status != PackagingProfile.STATUS_APPROVED:
            raise InactiveItemError(
                f'Packaging profile {profile.pk!r} ({profile.name!r}) '
                f'has status={profile.status!r} but must be "approved". '
                'Repoint the rule to an approved profile.',
                item=None,
            )
        packaging_items = list(
            PackagingProfileItem.objects
            .filter(profile=profile)
            .select_related('inventory_item')
            .order_by('id')
        )

    # ── Step 4: validate all referenced items ─────────────────────────────────
    # Rev 3 §I.2: "REQUIRE every referenced item is active, recipe items are
    # ingredients, packaging items are packaging."
    # We validate here (before building components) so any error fires before
    # the caller wastes time on downstream processing.

    recipe_items = list(recipe.items.select_related('inventory_item').order_by('id'))

    for ri in recipe_items:
        item = ri.inventory_item
        if not item.is_active:
            raise InactiveItemError(
                f'Ingredient "{item.name}" (id={item.pk}) referenced by '
                f'recipe {recipe.pk} is inactive. '
                'Reactivate the item or update the recipe.',
                item=item,
            )
        if item.item_type != 'ingredient':
            raise InactiveItemError(
                f'Item "{item.name}" (id={item.pk}) in recipe {recipe.pk} '
                f'has item_type={item.item_type!r} but must be "ingredient".',
                item=item,
            )

    for ppi in packaging_items:
        item = ppi.inventory_item
        if not item.is_active:
            raise InactiveItemError(
                f'Packaging item "{item.name}" (id={item.pk}) referenced by '
                f'profile {rule.profile_id} is inactive. '
                'Reactivate the item or update the packaging profile.',
                item=item,
            )
        if item.item_type != 'packaging':
            raise InactiveItemError(
                f'Item "{item.name}" (id={item.pk}) in packaging profile '
                f'{rule.profile_id} has item_type={item.item_type!r} '
                'but must be "packaging".',
                item=item,
            )

    # ── Step 5: build the resolved components list ────────────────────────────
    # Rev 3 §I.2: "RETURN plan(components = recipe items + packaging items,
    #                           with ids, revisions, quantities)"
    # Ingredient components come first, then packaging components.

    components = []

    for ri in recipe_items:
        components.append(
            ResolvedPlanComponent(
                inventory_item=ri.inventory_item,
                component_source=COMPONENT_SOURCE_RECIPE,
                source_recipe_id=recipe.pk,
                source_packaging_profile_id=None,
                source_revision=recipe.revision,
                quantity_per_unit=ri.quantity,
            )
        )

    for ppi in packaging_items:
        components.append(
            ResolvedPlanComponent(
                inventory_item=ppi.inventory_item,
                component_source=COMPONENT_SOURCE_PACKAGING,
                source_recipe_id=None,
                source_packaging_profile_id=rule.profile_id,
                source_revision=rule.profile.revision,
                quantity_per_unit=ppi.quantity,
            )
        )

    return ResolvedPlan(
        product_id=product_id,
        size=size,
        order_type=order_type,
        recipe=recipe,
        packaging_rule=rule,
        components=tuple(components),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4B-4 — get_available_units() Service
# ══════════════════════════════════════════════════════════════════════════════
#
# Implements Rev 3 §C.2 and §E.3b: the read-only availability function that
# answers "how many units of this product can still be sold right now?"
#
# For legacy products: returns Product.stock_quantity (unchanged behaviour).
# For ingredient-tracked products: resolves the approved plan and returns the
# minimum of floor(available / quantity_per_unit) over all components.
#
# This function is read-only.  It acquires no locks, writes nothing to the
# database, and changes no stock quantities.  It is a pure informational
# check that call sites (cart, checkout, POS, dashboard — Phase 5B) use
# to gate or cap the quantity a customer may order.
# ══════════════════════════════════════════════════════════════════════════════

import math
import typing


# ── Return structure ──────────────────────────────────────────────────────────

class AvailabilityResult(typing.NamedTuple):
    """
    Return value of get_available_units().

    Rev 3 §E.3b: "If no complete approved plan exists it returns 0 with a
    reason, so the product is not sellable rather than silently under-deducted."

    Fields
    ------
    units : int
        How many product units can be sold right now (>= 0).
        0 means either genuinely out of stock or configuration is incomplete.
    configuration_complete : bool
        True  — units reflects actual stock availability.
        False — plan resolution failed; units is 0 because the configuration
                is broken, not necessarily because stock is exhausted.
                Rev 3 §E.3b: "show a configuration warning for ingredient
                products lacking an approved plan" (Phase 5B POS / dashboard).
    reason : str
        Human-readable explanation.  Empty string when everything is fine.
    """
    units:                  int
    configuration_complete: bool
    reason:                 str


# ── Availability function ─────────────────────────────────────────────────────

def get_available_units(product, size, order_type=None):
    """
    Return how many units of a product at a given size can be sold right now.

    Rev 3 §C.2 and §E.3b.

    This function is read-only.  It acquires no locks and writes nothing to
    the database.  The result is a best-effort estimate: it can be stale by
    the time an order is placed, and the authoritative check is always the
    reservation at placement (Rev 3 §E.3b).  A 1-unit stale over-estimate
    results in a clear InsufficientStockError at placement; it never silently
    under-deducts (Rev 3 §T9).

    Routing by tracking mode (Rev 3 §C.2)
    ──────────────────────────────────────
    Legacy product (inventory_tracking_mode='legacy'):
        Returns product.stock_quantity directly.  size and order_type are
        irrelevant: legacy stock is not split by size or order type.

    Ingredient-tracked product (inventory_tracking_mode='ingredient'):
        Calls resolve_plan() to obtain the complete approved plan, then
        returns min(floor(available / quantity_per_unit)) over all components.
        Both ingredient and packaging components count.

        If resolve_plan() raises RecipeIncompleteError (any subclass —
        RecipeMissingError, AmbiguousRecipeError, PackagingRuleMissingError,
        InactiveItemError), returns 0 with configuration_complete=False so
        the caller can show an admin warning instead of a stock-out message.

    Optional order_type (Rev 3 §E.3b)
    ────────────────────────────────────
    When order_type is None (cart stage, order type not yet known):
        "Use the most restrictive applicable order type."
        Both 'dine_in' and 'takeout' are applicable at the cart stage (the
        customer has not declared a preference yet).  Rev 3 §E.3b also states:
        "If no complete approved plan exists it returns 0 with a reason, so
        the product is not sellable rather than silently under-deducted."
        A broken plan for either order type means the product cannot safely
        be sold through that channel; a cart item could become that channel.
        Therefore both results are computed and the minimum is returned.  An
        incomplete result has units=0, so it is always the minimum when
        configuration is broken — the product is unsellable.
        configuration_complete=True only when BOTH order types have complete
        plans; if either is broken the result carries complete=False so the
        POS/dashboard can show the configuration warning.

    Parameters
    ----------
    product : menu.Product
        The product instance to check.
    size : str
        One of 'none', 'small', 'medium', 'large', 'hot'.
    order_type : str or None
        'dine_in', 'takeout', or None (most-restrictive check).

    Returns
    -------
    AvailabilityResult
        NamedTuple(units: int, configuration_complete: bool, reason: str).
    """
    # Deferred import — avoids circular import risk (same pattern as
    # resolve_plan); Product is from the menu app.
    from apps.menu.models import Product

    # ── Legacy product — return stock_quantity directly ───────────────────────
    # Rev 3 §C.2: "Returns stock_quantity" for legacy lines.
    if product.inventory_tracking_mode != Product.INVENTORY_TRACKING_INGREDIENT:
        return AvailabilityResult(
            units=int(product.stock_quantity),
            configuration_complete=True,
            reason='',
        )

    # ── Ingredient-tracked product ────────────────────────────────────────────
    # Rev 3 §C.2: "Returns the number of units the current approved plan can
    # still make."
    # Rev 3 §E.3b: "resolves the approved plan and returns the minimum over
    # its components of floor(available / quantity_per_unit)."

    if order_type is not None:
        # Single order_type: resolve directly.
        return _units_for_order_type(product.pk, size, order_type)

    # order_type is None — most restrictive of both order types.
    # Rev 3 §E.3b: "use the most restrictive applicable order type."
    # Both dine_in and takeout are applicable at the cart stage.
    # Rev 3 §E.3b also: "If no complete approved plan exists it returns 0 with
    # a reason, so the product is not sellable rather than silently
    # under-deducted." An incomplete plan for either order type means the
    # product is not safely sellable at the cart stage, so an incomplete
    # result (units=0, complete=False) is always the minimum and the
    # configuration_complete flag propagates to the caller.
    result_dine_in = _units_for_order_type(product.pk, size, 'dine_in')
    result_takeout = _units_for_order_type(product.pk, size, 'takeout')

    # Effective units: treat an incomplete result as 0 for the minimum
    # calculation (it already has units=0, so just compare directly).
    if result_dine_in.units <= result_takeout.units:
        most_restrictive = result_dine_in
    else:
        most_restrictive = result_takeout

    # configuration_complete is True only when BOTH plans are complete.
    both_complete = (
        result_dine_in.configuration_complete
        and result_takeout.configuration_complete
    )
    if both_complete:
        return most_restrictive

    # At least one plan is incomplete — return 0 with complete=False.
    # Use the incomplete result's reason when available; prefer whichever
    # is incomplete (both may be; pick dine_in arbitrarily).
    if not result_dine_in.configuration_complete:
        return AvailabilityResult(
            units=0,
            configuration_complete=False,
            reason=result_dine_in.reason,
        )
    return AvailabilityResult(
        units=0,
        configuration_complete=False,
        reason=result_takeout.reason,
    )


def _units_for_order_type(product_id, size, order_type):
    """
    Resolve the plan for (product_id, size, order_type) and compute available
    units.  Returns AvailabilityResult.  Never raises.

    Internal helper for get_available_units().
    """
    try:
        plan = resolve_plan(product_id, size, order_type)
    except RecipeIncompleteError as exc:
        # Rev 3 §E.3b: "If no complete approved plan exists it returns 0 with
        # a reason, so the product is not sellable rather than silently
        # under-deducted."
        return AvailabilityResult(
            units=0,
            configuration_complete=False,
            reason=str(exc),
        )

    # Rev 3 §E.3b: "minimum over its components of floor(available / qty)"
    if not plan.components:
        # Defensively: an approved recipe with zero items is blocked by the
        # approval service rule, but protect against edge cases.
        return AvailabilityResult(
            units=0,
            configuration_complete=False,
            reason=(
                f'No components in resolved plan for product_id={product_id}, '
                f'size={size!r}, order_type={order_type!r}.'
            ),
        )

    min_units = None
    for comp in plan.components:
        available = comp.inventory_item.available_quantity  # Decimal, always >= 0
        qty_per   = comp.quantity_per_unit                  # Decimal, always > 0

        # floor(available / qty_per_unit) — number of whole product units
        # this component can support.  Using math.floor on Decimal is safe
        # in Python (math.floor converts to int directly).
        component_units = math.floor(available / qty_per)

        if min_units is None or component_units < min_units:
            min_units = component_units

    # min_units is always an int (math.floor returns int for finite inputs).
    return AvailabilityResult(
        units=int(min_units),
        configuration_complete=True,
        reason='',
    )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4B-5 — Recipe and Packaging Configuration Services
# ══════════════════════════════════════════════════════════════════════════════
#
# Implements the configuration-writing services for Rev 3 §D.4, §D.5, §D.6
# and §H.9 (configuration concurrency protocol).
#
# All services follow the global lock order:
#   Order → Product (ascending) → PackagingProfile (ascending) → InventoryItem (ascending)
#
# Services here handle only the write side.  read_plan() remains read-only.
# ══════════════════════════════════════════════════════════════════════════════


# ── Configuration-writing domain exceptions ───────────────────────────────────

class RecipeConfigError(InventoryServiceError):
    """Raised by recipe configuration service operations."""


class PackagingConfigError(InventoryServiceError):
    """Raised by packaging configuration service operations."""


# ── Recipe services ───────────────────────────────────────────────────────────

def approve_recipe(recipe_id, approved_by, approval_note):
    """
    Approve a draft Recipe, retiring the previous approved revision if one exists.

    Rev 3 §H.9 lock order: Product → referenced InventoryItems (ascending pk).

    Rev 3 §H.9 explicitly states that Recipe and RecipeItem rows are NOT locked
    by configuration writers.  The Product row is the configuration lock.
    Recipe rows are read without FOR UPDATE; the Product lock serialises all
    concurrent configuration writers for that product, and the partial unique
    index recipe_one_approved_per_scope is the DB-level backstop.

    Validation (all performed after acquiring the Product lock):
      - Recipe must be a draft (ordinary read — not locked).
      - No mixed approved scopes for the same (product, size): Rev 3 §D.4
        "For one (product, size) the approved set is either {all} or
        {dine_in, takeout}, never both."
      - Recipe must have at least one RecipeItem.
      - All RecipeItems must reference active ingredients.
      - Retire the previous approved revision for the same scope in the same
        transaction.

    Parameters
    ----------
    recipe_id : int
    approved_by : CustomUser
    approval_note : str   Rev 3: required confirmation note [C]

    Returns
    -------
    Recipe  The newly approved recipe.

    Raises
    ------
    RecipeConfigError
    """
    from apps.inventory.models import Recipe, RecipeItem
    from apps.menu.models import Product
    from django.utils import timezone

    if not approval_note or not approval_note.strip():
        raise RecipeConfigError(
            'An approval note is required to confirm the client approved this recipe.'
        )

    with transaction.atomic():
        # Read the Recipe WITHOUT select_for_update — Rev 3 §H.9: Recipe rows
        # are not locked.  The Product lock (acquired next) serialises writers.
        recipe = Recipe.objects.select_related('product').get(pk=recipe_id)

        if recipe.status != Recipe.STATUS_DRAFT:
            raise RecipeConfigError(
                f'Only draft recipes can be approved. '
                f'This recipe has status="{recipe.status}".'
            )

        # ── Level 2: Lock the Product row (Rev 3 §H.9) ───────────────────────
        # This is the configuration lock.  Every other configuration writer for
        # this product must also acquire it first, so no two writers can
        # interleave.  Placement already holds it before resolving the plan.
        product = Product.objects.select_for_update().get(pk=recipe.product_id)

        # ── Post-lock TOCTOU guard ─────────────────────────────────────────────
        # The initial status check ran before the Product lock was acquired.
        # Under PostgreSQL READ COMMITTED, another transaction may have approved
        # or retired this recipe between that read and the moment we acquired
        # the lock.  Refresh the recipe status from the DB now that the lock
        # serialises all concurrent writers for this product.
        # No select_for_update() on the Recipe row — Rev 3 §H.9 says Recipe
        # rows are not locked; the Product lock is sufficient.
        recipe.refresh_from_db(fields=['status'])
        if recipe.status != Recipe.STATUS_DRAFT:
            raise RecipeConfigError(
                f'Recipe status changed while waiting for the configuration lock: '
                f'expected "draft", found "{recipe.status}". '
                'Another request may have already approved or retired this recipe.'
            )
        # For one (product, size), the approved set must be either {all} or
        # {dine_in, takeout}, never both.  Check for conflicting complementary
        # scopes while holding the Product lock.
        scope = recipe.order_type_scope
        if scope == Recipe.SCOPE_ALL:
            # Cannot have an 'all' recipe if a specific-scope recipe is approved.
            conflict_scopes = [Recipe.SCOPE_DINE_IN, Recipe.SCOPE_TAKEOUT]
        else:
            # Cannot have a specific-scope recipe if an 'all' recipe is approved.
            conflict_scopes = [Recipe.SCOPE_ALL]

        conflicting = Recipe.objects.filter(
            product=product,
            size=recipe.size,
            order_type_scope__in=conflict_scopes,
            status=Recipe.STATUS_APPROVED,
        ).first()
        if conflicting:
            raise RecipeConfigError(
                f'Cannot approve this recipe (scope="{scope}"): '
                f'an approved recipe with scope="{conflicting.order_type_scope}" '
                f'already exists for the same product and size. '
                f'Rev 3 §D.4: the approved set for one (product, size) must be '
                f'either {{all}} or {{dine_in, takeout}}, never both.'
            )

        # ── Collect RecipeItems (ordinary read, no lock) ──────────────────────
        items_qs = list(
            RecipeItem.objects
            .filter(recipe=recipe)
            .select_related('inventory_item')
        )
        if not items_qs:
            raise RecipeConfigError(
                'A recipe must have at least one ingredient line before it can be approved.'
            )

        # ── Level 3: Lock referenced InventoryItems in ascending pk order ─────
        # Rev 3 §H.9: "Verifies items are active ingredients under the lock;
        # a concurrent approval holds the item lock until it commits."
        item_pks = sorted(ri.inventory_item_id for ri in items_qs)
        locked_items = {
            item.pk: item
            for item in InventoryItem.objects
            .select_for_update()
            .filter(pk__in=item_pks)
            .order_by('pk')
        }

        # Validate under lock: every item must be active and of type ingredient.
        for ri in items_qs:
            inv_item = locked_items[ri.inventory_item_id]
            if not inv_item.is_active:
                raise RecipeConfigError(
                    f'Ingredient "{inv_item.name}" is inactive. '
                    'Reactivate it or remove it from the recipe before approving.'
                )
            if inv_item.item_type != InventoryItem.ITEM_TYPE_INGREDIENT:
                raise RecipeConfigError(
                    f'"{inv_item.name}" is a packaging item, not an ingredient. '
                    'Recipe lines must reference ingredients only.'
                )

        # ── Retire the existing approved revision for this scope (ordinary read)
        # The Product lock guarantees no concurrent writer can change this row's
        # status while we hold the Product lock.  No FOR UPDATE needed here.
        existing_approved = Recipe.objects.filter(
            product=product,
            size=recipe.size,
            order_type_scope=recipe.order_type_scope,
            status=Recipe.STATUS_APPROVED,
        ).first()

        if existing_approved and existing_approved.pk != recipe.pk:
            existing_approved.status = Recipe.STATUS_RETIRED
            existing_approved.save(update_fields=['status', 'updated_at'])

        # ── Approve ───────────────────────────────────────────────────────────
        recipe.status        = Recipe.STATUS_APPROVED
        recipe.approved_by   = approved_by
        recipe.approved_at   = timezone.now()
        recipe.approval_note = approval_note.strip()
        recipe.save(update_fields=['status', 'approved_by', 'approved_at', 'approval_note', 'updated_at'])

    return recipe


def retire_recipe(recipe_id):
    """
    Retire an approved Recipe.

    Rev 3 §H.9 lock order: Product.
    Rev 3 §H.9 explicitly states that Recipe rows are NOT locked.  The Product
    row is the configuration lock; acquiring it serialises concurrent writers.

    Refuses standalone retirement if the product is in ingredient mode and no
    replacement approved recipe exists for the same (product, size, scope).
    For legacy-mode products, retirement is always safe.

    Returns the retired Recipe.
    """
    from apps.inventory.models import Recipe
    from apps.menu.models import Product

    with transaction.atomic():
        # Read the Recipe WITHOUT select_for_update — Rev 3 §H.9: Recipe rows
        # are not locked.  The Product lock (acquired next) serialises writers.
        recipe = Recipe.objects.get(pk=recipe_id)

        if recipe.status == Recipe.STATUS_RETIRED:
            raise RecipeConfigError('This recipe is already retired.')
        if recipe.status == Recipe.STATUS_DRAFT:
            raise RecipeConfigError(
                'Draft recipes are not retired — delete them instead.'
            )

        # ── Level 2: Lock the Product row (Rev 3 §H.9) ───────────────────────
        product = Product.objects.select_for_update().get(pk=recipe.product_id)

        # ── Post-lock TOCTOU guard ─────────────────────────────────────────────
        # Another transaction may have changed the recipe's status between the
        # initial pre-lock read and the moment this transaction acquired the
        # Product lock.  Refresh from DB now that the lock is held.
        # No select_for_update() on Recipe — Rev 3 §H.9: Recipe rows not locked.
        recipe.refresh_from_db(fields=['status'])
        if recipe.status == Recipe.STATUS_RETIRED:
            raise RecipeConfigError('This recipe is already retired.')
        if recipe.status == Recipe.STATUS_DRAFT:
            raise RecipeConfigError(
                'Draft recipes are not retired — delete them instead.'
            )
        if recipe.status != Recipe.STATUS_APPROVED:
            raise RecipeConfigError(
                f'Recipe status changed while waiting for the configuration lock: '
                f'found "{recipe.status}". Cannot retire.'
            )

        # Rev 3 §H.9: "Retire recipe | Product | Refused for an ingredient-mode
        # product unless a replacement is approved in the same transaction."
        if product.inventory_tracking_mode == Product.INVENTORY_TRACKING_INGREDIENT:
            replacement = Recipe.objects.filter(
                product=product,
                size=recipe.size,
                order_type_scope=recipe.order_type_scope,
                status=Recipe.STATUS_APPROVED,
            ).exclude(pk=recipe.pk).exists()
            if not replacement:
                raise RecipeConfigError(
                    'Cannot retire the only approved recipe for an ingredient-tracked product '
                    'without a replacement. Approve a new revision first.'
                )

        recipe.status = Recipe.STATUS_RETIRED
        recipe.save(update_fields=['status', 'updated_at'])

    return recipe


# ── Packaging profile services ────────────────────────────────────────────────

def approve_packaging_profile(profile_id, approved_by, approval_note):
    """
    Approve a draft PackagingProfile.

    Rev 3 §H.9: lock PackagingProfile.

    Concurrent-approval serialisation:
      Each profile has its own PK, so locking one profile row does not block
      a concurrent transaction that is approving a *different* profile row
      with the same name (a different revision).  To serialise concurrent
      approvals of same-name revisions without introducing deadlocks, this
      service:

        1. Reads the target profile's name without a lock.
        2. Within the transaction, locks ALL profiles sharing that name
           (case-insensitive) in a single, ascending-PK-ordered query.
           Both concurrent transactions will contend on the same lowest-PK
           row in the set, so one blocks and the other proceeds first.
        3. Re-validates the target profile's status from the locked result.
           If the target is absent (its name was changed between steps 1 and
           2), raises a retryable PackagingConfigError.  The caller must
           retry the call in a fresh transaction — by that time the new name
           will be returned in step 1 and the correct lock set acquired.
           No additional profile locks are acquired in this error path.
        4. Checks that no sibling revision is already approved.
        5. Wraps the final save in a savepoint so that a concurrent commit
           that slips past the sibling check (an extremely narrow window)
           is caught as an IntegrityError and translated to PackagingConfigError.

    Lock order: PackagingProfile rows (ascending pk) → InventoryItems (ascending pk).
    No Product lock: approving a profile changes no resolution until rules are
    repointed.

    Validation:
      - Profile must be draft.
      - No other revision with the same name may be approved.
      - Profile must have at least one PackagingProfileItem.
      - All items must be active packaging items.

    Returns the approved profile.
    """
    from apps.inventory.models import PackagingProfile, PackagingProfileItem
    from django.db import IntegrityError as _IntegrityError
    from django.utils import timezone

    if not approval_note or not approval_note.strip():
        raise PackagingConfigError(
            'An approval note is required to confirm the client approved this packaging profile.'
        )

    with transaction.atomic():
        # ── Step 1: read the profile name without a lock ──────────────────────
        # A plain read is safe here because we re-validate the profile's status
        # under the ordered lock in step 3.  If the profile's name is changed
        # concurrently between this read and the ordered lock query, the target
        # will be absent from the locked set; step 3 raises a retryable error
        # rather than acquiring any additional locks out of order.
        try:
            profile_name = (
                PackagingProfile.objects
                .values_list('name', flat=True)
                .get(pk=profile_id)
            )
        except PackagingProfile.DoesNotExist:
            raise PackagingConfigError(f'Packaging profile {profile_id} not found.')

        # ── Step 2: lock all same-name profiles in ascending pk order ─────────
        # Both concurrent transactions for the same name will contend on the
        # lowest-pk row in the result, serialising their execution.
        # filter uses iexact to match the pp_one_approved_per_name constraint's
        # case-insensitive uniqueness scope.
        same_name_locked = list(
            PackagingProfile.objects
            .filter(name__iexact=profile_name)
            .select_for_update()
            .order_by('pk')
        )

        # ── Step 3: find the target profile in the locked set ─────────────────
        # If the target is absent, the profile's name changed between step 1
        # and the ordered lock query — a concurrent rename overtook this
        # approval attempt.  We MUST NOT acquire an additional lock outside
        # the ordered set: doing so could invert the lock order and create a
        # deadlock with another transaction that already holds a lock from the
        # new name set.
        #
        # Safe resolution: abort this attempt with a retryable error.  The
        # caller can retry approve_packaging_profile() immediately; the second
        # call will read the profile's new name in step 1 and lock the correct
        # same-name set in step 2.
        #
        # A bounded retry inside this function is not safe while locks from
        # step 2 are still held by the current transaction.  Releasing those
        # locks requires the enclosing transaction.atomic() to commit or
        # roll back, which cannot happen in the middle of this function body.
        # The caller's retry starts a fresh transaction and acquires the right
        # locks from scratch.
        profile = next(
            (p for p in same_name_locked if p.pk == profile_id), None
        )
        if profile is None:
            raise PackagingConfigError(
                f'Packaging profile {profile_id} was not found under the '
                'expected name lock. Its name may have been changed concurrently. '
                'Please retry the approval.'
            )

        # ── Step 4: validate status under lock ────────────────────────────────
        if profile.status != PackagingProfile.STATUS_DRAFT:
            raise PackagingConfigError(
                f'Only draft profiles can be approved. '
                f'This profile has status="{profile.status}".'
            )

        # ── Step 5: check for an already-approved sibling ─────────────────────
        already_approved = next(
            (p for p in same_name_locked
             if p.pk != profile_id
             and p.status == PackagingProfile.STATUS_APPROVED),
            None,
        )
        if already_approved:
            raise PackagingConfigError(
                f'An approved packaging profile named "{profile.name}" '
                f'(revision {already_approved.revision}) already exists. '
                'Retire it before approving a new revision.'
            )

        # ── Step 6: validate items ────────────────────────────────────────────
        items_qs = list(
            PackagingProfileItem.objects
            .filter(profile=profile)
            .select_related('inventory_item')
        )
        if not items_qs:
            raise PackagingConfigError(
                'A packaging profile must have at least one item line before it can be approved.'
            )

        # Lock the referenced InventoryItems in ascending pk order.
        item_pks = sorted(ppi.inventory_item_id for ppi in items_qs)
        locked_items = {
            item.pk: item
            for item in InventoryItem.objects.select_for_update()
            .filter(pk__in=item_pks).order_by('pk')
        }

        for ppi in items_qs:
            inv_item = locked_items[ppi.inventory_item_id]
            if not inv_item.is_active:
                raise PackagingConfigError(
                    f'Packaging item "{inv_item.name}" is inactive. '
                    'Reactivate it or remove it before approving the profile.'
                )
            if inv_item.item_type != InventoryItem.ITEM_TYPE_PACKAGING:
                raise PackagingConfigError(
                    f'"{inv_item.name}" is an ingredient, not a packaging item. '
                    'Packaging profile lines must reference packaging items only.'
                )

        # ── Step 7: approve with savepoint backstop ───────────────────────────
        # The savepoint catches the narrow window where two transactions both
        # pass the sibling check and attempt to commit approved status for the
        # same name.  The pp_one_approved_per_name constraint fires for the
        # second committer; the savepoint rolls back that INSERT/UPDATE only,
        # leaving the outer transaction intact so we can raise cleanly.
        profile.status       = PackagingProfile.STATUS_APPROVED
        profile.approved_by  = approved_by
        profile.approved_at  = timezone.now()
        profile.approval_note = approval_note.strip()
        try:
            with transaction.atomic(savepoint=True):
                profile.save(update_fields=[
                    'status', 'approved_by', 'approved_at', 'approval_note', 'updated_at',
                ])
        except _IntegrityError as exc:
            # Translate only the expected constraint violation.  Any other
            # IntegrityError (e.g. a NOT NULL violation) is re-raised so it
            # surfaces as the real error rather than a misleading PackagingConfigError.
            if 'pp_one_approved_per_name' in str(exc):
                raise PackagingConfigError(
                    f'An approved packaging profile named "{profile.name}" '
                    'already exists. '
                    '(Concurrent approval committed while this transaction was running.)'
                ) from exc
            raise

    return profile


def retire_packaging_profile(profile_id):
    """
    Retire an approved PackagingProfile.

    Rev 3 §H.9: lock PackagingProfile.  Refused while any ProductPackagingRule
    references it (counted under the lock).

    Returns the retired profile.
    """
    from apps.inventory.models import PackagingProfile, ProductPackagingRule

    with transaction.atomic():
        profile = PackagingProfile.objects.select_for_update().get(pk=profile_id)

        if profile.status == PackagingProfile.STATUS_RETIRED:
            raise PackagingConfigError('This profile is already retired.')
        if profile.status == PackagingProfile.STATUS_DRAFT:
            raise PackagingConfigError(
                'Draft profiles are not retired — delete them instead.'
            )

        # Check under the lock that no rule still references this profile.
        rule_count = ProductPackagingRule.objects.filter(profile=profile).count()
        if rule_count:
            raise PackagingConfigError(
                f'Cannot retire this profile: {rule_count} product packaging '
                f'rule(s) still reference it. Repoint those rules first.'
            )

        profile.status = PackagingProfile.STATUS_RETIRED
        profile.save(update_fields=['status', 'updated_at'])

    return profile


def save_packaging_rule(product_id, size, order_type,
                        profile_id=None, no_packaging=False,
                        confirmed_by=None, existing_rule_id=None):
    """
    Create or update a ProductPackagingRule.

    Rev 3 §H.9: lock Product → lock target PackagingProfile (ascending).
    Re-reads the profile's status under its lock; rejects a non-approved profile.

    Parameters
    ----------
    product_id : int
    size : str
    order_type : str   'dine_in' or 'takeout'
    profile_id : int or None   None when no_packaging=True
    no_packaging : bool
    confirmed_by : CustomUser
    existing_rule_id : int or None   If updating an existing rule.

    Returns
    -------
    ProductPackagingRule

    Raises
    ------
    PackagingConfigError
    """
    from apps.inventory.models import PackagingProfile, ProductPackagingRule
    from apps.menu.models import Product
    from django.utils import timezone

    # Validate the XOR invariant before touching the DB.
    if no_packaging and profile_id is not None:
        raise PackagingConfigError(
            'A packaging rule cannot have both a profile and no_packaging=True.'
        )
    if not no_packaging and profile_id is None:
        raise PackagingConfigError(
            'A packaging rule must either reference an approved profile or '
            'explicitly declare no_packaging=True.'
        )

    with transaction.atomic():
        # Lock Product (level 2).
        product = Product.objects.select_for_update().get(pk=product_id)

        # Lock PackagingProfile (level 3 in the config sub-hierarchy).
        profile = None
        if profile_id is not None:
            profile = PackagingProfile.objects.select_for_update().get(pk=profile_id)
            if profile.status != PackagingProfile.STATUS_APPROVED:
                raise PackagingConfigError(
                    f'The packaging profile "{profile.name}" is not approved '
                    f'(status="{profile.status}"). Only approved profiles may be '
                    'referenced by a packaging rule.'
                )

        now = timezone.now()
        if existing_rule_id is not None:
            rule = ProductPackagingRule.objects.select_for_update().get(
                pk=existing_rule_id, product=product,
            )
            rule.size         = size
            rule.order_type   = order_type
            rule.profile      = profile
            rule.no_packaging = no_packaging
            rule.updated_by   = confirmed_by
            rule.updated_at   = now
            rule.save(update_fields=[
                'size', 'order_type', 'profile', 'no_packaging',
                'updated_by', 'updated_at',
            ])
        else:
            # ── TOCTOU guard (create path only) ───────────────────────────────
            # The Product lock ensures no concurrent writer has committed a rule
            # for this combination since we acquired the lock.  A plain check
            # here is sufficient because the Product lock serialises all
            # create() calls for this product.
            if ProductPackagingRule.objects.filter(
                product=product, size=size, order_type=order_type,
            ).exists():
                raise PackagingConfigError(
                    f'A packaging rule for {product.name!r} '
                    f'size={size!r} order_type={order_type!r} already exists.'
                )
            # Savepoint backstop: the TOCTOU check above handles the common case.
            # The savepoint catches the extremely narrow window between the check
            # and the INSERT where a concurrent transaction could commit first
            # (both passed the check under the same Product lock, which cannot
            # happen in practice because the Product lock serialises writers —
            # but the savepoint protects against any future code path that
            # bypasses the lock).
            from django.db import IntegrityError as _IntegrityError
            try:
                with transaction.atomic(savepoint=True):
                    rule = ProductPackagingRule.objects.create(
                        product=product,
                        size=size,
                        order_type=order_type,
                        profile=profile,
                        no_packaging=no_packaging,
                        confirmed_by=confirmed_by,
                        confirmed_at=now,
                        updated_by=confirmed_by,
                        updated_at=now,
                    )
            except _IntegrityError as exc:
                # Translate the expected unique-combination constraint only.
                if 'ppr_unique_combination' in str(exc):
                    raise PackagingConfigError(
                        f'A packaging rule for {product.name!r} '
                        f'size={size!r} order_type={order_type!r} already exists.'
                    ) from exc
                raise

    return rule
