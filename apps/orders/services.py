"""
Order Services - Centralized inventory management for orders.
Call these functions from views only, never from models.
"""
import logging

from django.db import transaction
from decimal import Decimal

from apps.inventory.models import InventoryLog

_logger = logging.getLogger(__name__)


def create_order_item(order, product, size, quantity):
    """Create one ``OrderItem`` for *order*, snapshotting all product fields.

    Centralises the full set of keyword arguments so neither
    ``checkout_view`` nor ``create_pos_order`` need to repeat the snapshot
    logic.  The caller is responsible for providing a valid, active
    ``product`` (availability gates live in the view layer).

    ``unit_price`` is always read from ``product.get_price_for_size(size)``
    so that no caller can ever pass a browser-supplied or stale price.
    ``subtotal`` is derived from the authoritative unit_price × quantity
    and is never accepted from outside this function.

    Phase 5A: ``inventory_mode`` is snapshotted from
    ``product.inventory_tracking_mode`` at creation time so the correct
    ledger path is permanently recorded on the line regardless of any
    future change to the product's tracking mode.
    """
    # Local import to avoid a circular dependency at module load time
    # (models → services → models).  Both modules are fully initialised by
    # the time any view calls this function.
    from apps.orders.models import OrderItem
    # Always fetch the authoritative price from the product record.
    # This is the single place where the customer-order price is locked in,
    # so price changes, discounts, or any future pricing logic only need to
    # be handled here.
    unit_price = product.get_price_for_size(size)
    return OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        category_name=product.category.name,
        packaging_eligible=product.category.is_packaging_required,
        size=size,
        quantity=quantity,
        unit_price=unit_price,
        subtotal=unit_price * quantity,
        # Phase 5A: snapshot the inventory ledger mode at placement time.
        # Immutable after this point — the ledger path is fixed for the
        # lifetime of this order line.
        inventory_mode=product.inventory_tracking_mode,
    )


# ── Status Transition Validation ──────────────────────────────────────────────
# This map is the single authoritative definition of every allowed status
# transition in the business workflow.
#
# Standard preparation flow:
#   pending   → preparing  (cashier accepts the paid customer order, or kitchen
#                           starts a POS order)
#   pending   → cancelled  (cancelled before prep starts)
#   preparing → ready      (kitchen finishes preparation)
#   preparing → cancelled  (cancelled mid-prep)
#   ready     → completed  (customer collects / cashier completes)
#   ready     → cancelled  (rare: customer no-show)
#   completed → (none)     terminal — order is done
#   cancelled → (none)     terminal — order is void
#
# Any transition not listed above is explicitly forbidden.  All views that
# change order status must call validate_status_transition() before writing.
#
# Payment is tracked separately via Order.is_paid (BooleanField).  An order
# starts as PENDING + is_paid=False.  When the cashier confirms payment the
# is_paid flag is set to True while the order remains PENDING.  The cashier
# then accepts the order, which moves it from PENDING → PREPARING.
VALID_TRANSITIONS: dict[str, set[str]] = {
    'awaiting_payment': {'preparing', 'cancelled'},   # payment confirmed → preparing
    'preparing':        {'ready',     'cancelled'},
    'ready':            {'completed', 'cancelled'},
    'completed': set(),   # terminal state — no further transitions
    'cancelled': set(),   # terminal state — no further transitions
}


def validate_status_transition(current_status: str, new_status: str, order=None) -> None:
    """Raise ValueError if current_status → new_status is not a valid transition.

    Called by every view that changes order status.  The error message is
    user-readable so it can be surfaced directly in the API response.

    Args:
        current_status: The order's current status string.
        new_status:     The requested new status string.
        order:          Optional Order instance.  When supplied, business-rule
                        guards that require the full order (e.g. payment check)
                        are also enforced.

    Raises:
        ValueError: with a descriptive message when the transition is invalid
                    or a business rule blocks it.
    """
    # Same-status "change" is a no-op — treat it as valid so idempotent
    # saves (e.g. re-saving with no change) are not accidentally blocked.
    if current_status == new_status:
        return
    allowed = VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        from apps.orders.models import Order
        label = dict(Order.STATUS_CHOICES)
        current_label = label.get(current_status, current_status)
        new_label     = label.get(new_status,     new_status)
        raise ValueError(
            f"Cannot change order status from \"{current_label}\" "
            f"to \"{new_label}\"."
        )

    # ── Business-rule: pending → preparing requires payment for customer orders.
    # ── Business-rule: awaiting_payment → preparing requires is_paid=True.
    # process_payment sets is_paid=True and transitions to preparing in one
    # atomic step, so this gate only fires if something tries to manually
    # advance an unpaid awaiting_payment order to preparing.
    if new_status == 'preparing' and current_status == 'awaiting_payment':
        if order is not None and not order.is_paid:
            raise ValueError(
                "Cannot start preparation: payment has not been confirmed yet. "
                "Please process the payment first."
            )

    # ── Business-rule: an order can only be completed once payment is
    # confirmed.  This prevents unpaid orders from appearing in finance
    # totals and keeps sales reports accurate.
    if new_status == 'completed' and order is not None and not order.is_paid:
        raise ValueError(
            "Cannot mark as Completed: this order is not paid yet. "
            "Please collect payment first."
        )


def get_packaging_fee_per_item():
    """Return the configured packaging fee per eligible meal item."""
    from apps.dashboard.models import SystemSetting
    raw = SystemSetting.get('PACKAGING_FEE_PER_ITEM', '6.00')
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal('6.00')


def calculate_packaging_fee_for_items(items, fee_per_item=None):
    """Compute the packaging fee and eligible item count for a sequence of
    ``(product, quantity)`` pairs.

    Business rule (Kape De Manubag take-out packaging policy):
    - ELIGIBLE (charged ₱6 per unit): food categories with
      ``Category.is_packaging_required = True``.
      Examples: Combo Meals, Pastil Meals, Burgers, Snacks, Ala Carte.
    - NOT ELIGIBLE (no charge): drink categories with
      ``Category.is_packaging_required = False``.
      Examples: Coffee, Milk Tea, Non-Coffee Drinks.
    - Dine-in orders: always ₱0 (callers must guard with order_type check).
    - Quantity is respected — 3 eligible items × ₱6 = ₱18.

    The fee_per_item rate is stored in SystemSetting('PACKAGING_FEE_PER_ITEM')
    (default ₱6.00) and applied uniformly; there is no per-product rate.

    Shared by :func:`calculate_packaging_fee` (uses stored snapshots for
    placed orders) and the packaging-fee preview API (uses live product data
    for pre-order previews) so the eligibility logic lives in exactly one place.
    """
    if fee_per_item is None:
        fee_per_item = get_packaging_fee_per_item()

    total_fee = Decimal('0.00')
    eligible_count = 0
    for product, quantity in items:
        if product is None:
            continue
        if product.category.is_packaging_required:
            total_fee += fee_per_item * quantity
            eligible_count += quantity
    return total_fee, eligible_count


def calculate_packaging_fee(order):
    """
    Calculate total packaging fee for a Take-Out order.
    Only items whose category had is_packaging_required=True at order time
    are charged.  Returns Decimal('0.00') for Dine-In orders.

    Uses the ``packaging_eligible`` snapshot stored on each OrderItem so
    that historical orders always produce the same fee regardless of any
    subsequent changes to the product's category or the category's
    is_packaging_required flag.

    For items that pre-date the snapshot field (packaging_eligible=False
    with category_name=''), falls back to reading the live product category
    so legacy orders are not silently under-charged.
    """
    if order.order_type != 'takeout':
        return Decimal('0.00')

    fee_per_item = get_packaging_fee_per_item()
    total_fee = Decimal('0.00')

    items = order.items.all()
    has_legacy_items = False

    for item in items:
        if item.category_name:
            # Snapshot present — use it; no live data access needed.
            if item.packaging_eligible:
                total_fee += fee_per_item * item.quantity
        else:
            # Legacy item created before snapshot fields existed; fall back
            # to live product data so the fee is computed correctly.
            has_legacy_items = True

    if has_legacy_items:
        # Re-fetch only the legacy items with live category data in one query.
        legacy_items = order.items.filter(
            category_name=''
        ).select_related('product__category')
        legacy_fee, _ = calculate_packaging_fee_for_items(
            (item.product, item.quantity) for item in legacy_items
        )
        total_fee += legacy_fee

    return total_fee


def _order_inventory_source(order):
    """Audit-trail source for an order's stock movements.

    Customer checkout orders have no cashier; POS orders always do.
    """
    return 'pos_order' if order.cashier_id else 'customer_order'


def _apply_inventory_for_order(order, items, performed_by, *, deduct,
                               extra_order_update_fields=None,
                               item_quantities=None):
    """Apply one stock movement (deduct or restore) for every order item.

    Shared by :func:`deduct_inventory_for_order` (``deduct=True``) and
    :func:`restore_inventory_for_order` (``deduct=False``): each item's stock
    change, its inventory log row and the order's ``stock_deducted`` flag
    commit or roll back together. ``items`` is passed in so callers fetch the
    queryset exactly once (and, for deductions, run the pre-flight check on
    the same rows that get changed).

    ``extra_order_update_fields`` — optional list of additional field names to
    include in the final ``order.save(update_fields=...)`` call.  Used by
    ``create_pos_order`` to merge the ``stock_deducted`` update with the
    ``subtotal``/``packaging_fee``/``total`` update into a single DB write,
    saving one round-trip per order.

    ``item_quantities`` — optional ``{item.pk: quantity}`` mapping.  When
    provided, the quantity for each item is taken from this dict rather than
    from ``item.quantity``.  Used by :func:`restore_inventory_for_order` to
    support partial restores (Phase 5A).  Defaults to None (use item.quantity
    for every item).
    """
    source = _order_inventory_source(order)
    action = 'sale' if deduct else 'adjustment'
    if deduct:
        reason = f"Order #{order.order_number}"
        quantity_sign = -1
    else:
        # The restore belongs to the same source as the original order.
        reason = f"Cancelled Order #{order.order_number}"
        quantity_sign = 1

    with transaction.atomic():
        for item in items:
            if item.product is None:
                continue

            # ── Phase 6A: restoration mapping fix ──────────────────────────
            # When item_quantities is not None, the caller is controlling
            # per-item quantities explicitly.  Omitted keys and zero values
            # both mean "do not restore this item" — they must NOT fall back
            # to item.quantity.  This prevents accidental full restoration
            # of intentionally excluded items and avoids zero-quantity
            # InventoryLog rows (§7.4).
            if item_quantities is not None:
                if item.pk not in item_quantities:
                    continue  # explicitly excluded — skip
                qty = item_quantities[item.pk]
                if qty == 0:
                    continue  # zero restoration — skip, no InventoryLog
            else:
                qty = item.quantity  # existing behavior: full per item

            old_qty = item.product.stock_quantity
            if deduct:
                item.product.reduce_stock(qty)
            else:
                item.product.restore_stock(qty)
            new_qty = item.product.stock_quantity

            InventoryLog.record(
                product=item.product,
                action=action,
                source=source,
                reason=reason,
                quantity_change=quantity_sign * qty,
                quantity_before=old_qty,
                quantity_after=new_qty,
                performed_by=performed_by,
            )

        order.stock_deducted = deduct
        # Merge any additional fields from the caller into this single UPDATE
        # so callers can avoid a separate order.save() round-trip.
        update_fields = ['stock_deducted'] + (extra_order_update_fields or [])
        order.save(update_fields=update_fields)


def deduct_inventory_for_order(order, performed_by=None,
                               extra_order_update_fields=None):
    """
    Deduct legacy-ledger stock for all ``inventory_mode='legacy'`` items in
    an order.

    Phase 5A: narrows to legacy lines only.  Ingredient-mode lines are
    handled by :func:`place_order_inventory` (called from Stage B+ views).

    Phase 6A: wraps the entire body in its own ``with transaction.atomic()``
    block so that ``select_for_update()`` is always safe, regardless of
    whether the caller provides a transaction.  A status guard rejects
    non-``awaiting_payment`` orders to prevent re-deduction on cancelled
    orders where ``stock_deducted`` has been set to ``False``.

    Idempotent: does nothing if order.stock_deducted is already True.
    Raises ValueError if any legacy product has insufficient stock (pre-flight
    check aggregated by product to handle multiple same-product lines).
    Raises ValueError if the order is not in ``awaiting_payment`` status.

    ``extra_order_update_fields`` — forwarded to ``_apply_inventory_for_order``
    so the caller can merge additional order fields into the final UPDATE, saving
    a DB round-trip.  See ``create_pos_order`` for usage.
    """
    # Phase 6A: open our own atomic block so select_for_update() is always
    # safe.  Django's atomic() is reentrant: if an outer transaction already
    # exists, this creates a savepoint; if called standalone (as in tests),
    # it creates a new transaction.
    with transaction.atomic():
        from apps.orders.models import Order

        # Retrieve the authoritative, locked Order row.  The caller-supplied
        # instance may be stale — its in-memory status could still read
        # 'awaiting_payment' even though the DB row has been cancelled by
        # another transaction.
        locked_order = Order.objects.select_for_update().get(pk=order.pk)

        # Phase 6A status guard (Rev3 §H.2 "REQUIRE order.status =
        # 'awaiting_payment'").  Prevents re-deduction on cancelled,
        # completed, or ready orders regardless of stock_deducted value.
        if locked_order.status != 'awaiting_payment':
            raise ValueError(
                f'Cannot deduct stock: order status is '
                f'{locked_order.status}, not awaiting_payment.'
            )

        if locked_order.stock_deducted:
            return

        # Phase 6A: carry over caller-supplied extra field values from
        # the (potentially stale) in-memory instance onto the freshly
        # retrieved locked instance.  This preserves the
        # extra_order_update_fields optimization used by create_pos_order:
        # the caller sets subtotal/packaging_fee/total on their in-memory
        # order, and this function saves those values together with
        # stock_deducted in one UPDATE.
        if extra_order_update_fields:
            for field in extra_order_update_fields:
                if hasattr(order, field):
                    setattr(locked_order, field, getattr(order, field))

        # Phase 5A: restrict to legacy-ledger lines only.
        from apps.orders.models import OrderItem
        items = list(
            locked_order.items
            .filter(inventory_mode=OrderItem.INVENTORY_MODE_LEGACY)
            .select_related('product')
        )

        if not items:
            # No legacy lines — nothing to deduct; mark deducted only when
            # there was at least one legacy line (stock_deducted semantics).
            # With no legacy items the flag stays False; place_order_inventory
            # (Stage B) will set it when appropriate.
            return

        # Pre-flight validation — aggregate requested quantities by product pk
        # to handle the (rare) case of the same product appearing in two
        # legacy lines at different sizes.  We check the aggregate demand
        # against the available stock, matching the logic in
        # place_order_inventory.
        aggregated: dict = {}
        for item in items:
            if item.product is None:
                continue
            aggregated[item.product_id] = (
                aggregated.get(item.product_id, 0) + item.quantity
            )

        # Validate each product once (using live stock_quantity).
        checked_products: dict = {}
        for item in items:
            if item.product is None:
                continue
            pid = item.product_id
            if pid in checked_products:
                continue
            checked_products[pid] = item.product
            total_requested = aggregated[pid]
            if item.product.stock_quantity < total_requested:
                raise ValueError(
                    f"Insufficient stock for '{item.product.name}'. "
                    f"Available: {item.product.stock_quantity}, "
                    f"Requested: {total_requested}"
                )

        # All checks passed — apply the movement atomically.
        # Use locked_order so that the stock_deducted flag is written to
        # the same instance that was validated.
        _apply_inventory_for_order(
            locked_order, items, performed_by, deduct=True,
            extra_order_update_fields=extra_order_update_fields,
        )


def restore_inventory_for_order(order, performed_by=None, item_quantities=None):
    """
    Restore legacy-ledger stock for all ``inventory_mode='legacy'`` items in
    a cancelled order.

    Phase 5A: narrows to legacy lines only.  Ingredient-mode restores are
    handled by the reservation-release path (Phase 5B+).

    Idempotent: does nothing if order.stock_deducted is already False.

    ``item_quantities`` — optional ``{orderitem_pk: quantity}`` mapping.
    When provided, only the specified quantities are restored (partial
    restore).  Omitted keys use the item's full recorded quantity.  Defaults
    to None (restore full quantity for every legacy line).
    """
    if not order.stock_deducted:
        return

    from apps.orders.models import OrderItem
    items = list(
        order.items
        .filter(inventory_mode=OrderItem.INVENTORY_MODE_LEGACY)
        .select_related('product')
    )
    _apply_inventory_for_order(
        order, items, performed_by, deduct=False,
        item_quantities=item_quantities,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5A — Order Inventory Coordinator
# ══════════════════════════════════════════════════════════════════════════════
#
# Rev 3 §H.2 place_order_inventory pseudocode.
#
# Stage A (this phase): service layer only.
#   - Lock Products in ascending pk order (level 2 in §H.1 hierarchy).
#   - Snapshot inventory_mode on every OrderItem from its Product.
#   - Separate lines into legacy and ingredient buckets.
#   - For ingredient lines: call resolve_plan() read-only and collect the
#     aggregated ingredient requirements.  No reservation writes yet.
#   - For legacy lines: aggregate by product, validate stock, then deduct
#     via the existing _apply_inventory_for_order helper.
#   - stock_deducted=True only when at least one legacy line exists.
#
# Stage B (Phase 5B): add OrderIngredientReservation writes after the
#   ingredient plan is collected.  No changes to this function's signature.
#
# Views (checkout_view, create_pos_order) are NOT changed in Stage A; they
# continue to call deduct_inventory_for_order() directly until Stage B
# wires them to place_order_inventory().
# ══════════════════════════════════════════════════════════════════════════════

def place_order_inventory(order_id, actor=None):
    """
    Coordinate all inventory operations for a single order placement.

    Implements Rev 3 §H.2.  Callers MUST hold an outer ``transaction.atomic()``
    block; this function adds no outermost transaction of its own so it can
    participate in the caller's atomic context.

    Lock hierarchy (Rev 3 §H.1):
      Level 1 — Order row  : caller already holds it (view's select_for_update).
      Level 2 — Product rows: acquired here in ascending pk order.
      Level 3 — InventoryItem rows: acquired inside _apply_inventory_for_order
                via the existing lock_inventory_items helper (Phase 5B+).

    Phase 5A Stage A behaviour
    --------------------------
    - Products are locked in ascending pk order (deterministic, deadlock-safe).
    - ``inventory_mode`` is snapshotted onto every OrderItem from its Product's
      ``inventory_tracking_mode`` (Rev 3 §D.1).  Existing items already carry
      the correct snapshot from create_order_item(); this step is the safety-
      net re-snapshot that runs under the lock.
    - Legacy lines:  validated (aggregate stock check) then deducted via the
      existing legacy path.
    - Ingredient lines: ``resolve_plan()`` is called read-only to verify the
      configuration is valid.  No reservation writes in Stage A.
    - ``order.stock_deducted`` is set to True only when at least one legacy
      line exists (ingredient reservations are not yet implemented).

    Parameters
    ----------
    order_id : int
        PK of the Order to process.  The Order must exist and not yet have
        ``stock_deducted=True`` (idempotency guard).
    actor : CustomUser or None
        Staff member triggering placement.  Forwarded to inventory movement
        logs.

    Returns
    -------
    dict
        Summary keys:
          ``legacy_line_count``     : int   — number of legacy-mode order lines
          ``ingredient_line_count`` : int   — number of ingredient-mode lines
          ``stock_deducted``        : bool  — True iff legacy deduction ran
          ``ingredient_plans``      : list  — ResolvedPlan objects (one per
                                              ingredient line); empty in Stage A
                                              when no ingredient lines exist

    Raises
    ------
    ValueError
        ``order_id`` not found, order already processed, or insufficient
        legacy stock.
    RecipeIncompleteError (subclass of ValueError)
        A resolve_plan() call failed for an ingredient line — missing recipe,
        ambiguous recipe, missing packaging rule, or inactive item.
    """
    from apps.orders.models import Order, OrderItem
    from apps.menu.models import Product
    from apps.inventory.services import resolve_plan, RecipeIncompleteError  # noqa: F401

    # ── Step 1: lock the Order row (level 1) ─────────────────────────────────
    # Rev 3 §H.2: "order := LOCK Order WHERE id = order_id  # level 1"
    # The caller's transaction.atomic() block is already open; acquiring the
    # row lock here serialises concurrent coordinator calls for the same order
    # (e.g. two simultaneous checkout submissions sharing the same
    # request_token that both slipped past the duplicate-token check).
    # For newly created orders (the current Stage B callers) the lock is
    # redundant in practice but satisfies the §H.2 contract and prepares the
    # coordinator for Phase 5B callers that operate on pre-existing orders.
    try:
        order = Order.objects.select_for_update().get(pk=order_id)
    except Order.DoesNotExist:
        raise ValueError(f'Order pk={order_id} does not exist.')

    # ── Phase 6A: status guard (Rev3 §H.2 "REQUIRE order.status = 'awaiting_payment'") ──
    # Prevents re-deduction on cancelled/completed/ready orders.  The order
    # row is already locked above, so this check reads the authoritative
    # committed status.
    if order.status != 'awaiting_payment':
        raise ValueError(
            f'Order pk={order_id} cannot be processed: '
            f'status is {order.status}, not awaiting_payment.'
        )

    if order.stock_deducted:
        # Legacy-only pre-5B early return — still used as the first cut
        # before the full partition-aware guard below runs.
        # For mixed and ingredient-only orders the full guard handles it.
        pass  # fall through to partition + full guard

    # ── Step 2: fetch all order items with their Products ─────────────────────
    all_items = list(
        order.items
        .select_related('product')
        .order_by('pk')
    )

    # Collect the distinct set of product PKs (excluding deleted products).
    product_pks = sorted({
        item.product_id
        for item in all_items
        if item.product_id is not None
    })

    # ── Step 3: lock Products in ascending pk order (level 2) ─────────────────
    # One SELECT ... FOR UPDATE with ORDER BY pk prevents deadlocks between
    # concurrent orders that share the same products.
    locked_products: dict[int, object] = {}
    if product_pks:
        locked_products = {
            p.pk: p
            for p in Product.objects.select_for_update().filter(
                pk__in=product_pks
            ).order_by('pk')
        }

    # ── Step 4: snapshot inventory_mode on every item (under lock) ────────────
    # create_order_item() already set the snapshot at creation time; this
    # re-snapshot under the Product lock ensures correctness even when the
    # product's mode was changed between item creation and this call (rare
    # race), or when items were created via a direct ORM path.
    # The bulk_update persists any corrections inside the caller's atomic block;
    # a later rollback rolls this back atomically too.
    items_to_update_mode = []
    for item in all_items:
        if item.product_id is None:
            continue
        product = locked_products.get(item.product_id)
        if product is None:
            continue
        expected_mode = product.inventory_tracking_mode
        if item.inventory_mode != expected_mode:
            item.inventory_mode = expected_mode
            items_to_update_mode.append(item)

    if items_to_update_mode:
        OrderItem.objects.bulk_update(items_to_update_mode, ['inventory_mode'])
        # Refresh all_items to reflect the updated modes.
        # The list was already fetched; update in-place.
        mode_map = {i.pk: i.inventory_mode for i in items_to_update_mode}
        for item in all_items:
            if item.pk in mode_map:
                item.inventory_mode = mode_map[item.pk]

    # ── Step 5: partition lines into legacy / ingredient buckets ──────────────
    legacy_items = [
        i for i in all_items
        if i.product_id is not None
        and i.inventory_mode == OrderItem.INVENTORY_MODE_LEGACY
    ]
    ingredient_items = [
        i for i in all_items
        if i.product_id is not None
        and i.inventory_mode == OrderItem.INVENTORY_MODE_INGREDIENT
    ]

    # ── Step 6: idempotency / inconsistency guard ─────────────────────────────
    # Runs AFTER the partition (which requires Product locks) but BEFORE any
    # InventoryItem locks or inventory writes.
    #
    # Retrieves the existing reservation (if any) with select_for_update() to
    # prevent a concurrent settle() from changing its status between the check
    # and a potential return-early.  Always unconditional — never gated on
    # ingredient_needed.
    _legacy_needed     = bool(legacy_items)
    _ingredient_needed = bool(ingredient_items)
    _legacy_done       = order.stock_deducted  # authoritative: locked order row

    from apps.orders.models import OrderIngredientReservation as _Res

    _existing_res = (
        _Res.objects
        .filter(order=order)
        .select_for_update()
        .first()
    )
    _reservation_exists = _existing_res is not None
    _reservation_status = _existing_res.status if _existing_res else None

    def _make_summary():
        return {
            'legacy_line_count':     len(legacy_items),
            'ingredient_line_count': len(ingredient_items),
            'stock_deducted':        order.stock_deducted,
            'ingredient_plans':      [],
        }

    # Case A: legacy-only order
    if not _ingredient_needed:
        if _reservation_exists:
            raise InconsistentInventoryStateError(
                f'Order pk={order_id}: no ingredient lines but an '
                f'OrderIngredientReservation (pk={_existing_res.pk}, '
                f'status={_reservation_status!r}) exists. '
                'Manual investigation required.'
            )
        if _legacy_done:
            return _make_summary()   # Row 2: normal second-call idempotency
        # Row 1: first call — fall through

    # Case B: ingredient-only order
    elif not _legacy_needed:
        if _legacy_done:
            # stock_deducted cannot be True for an ingredient-only order
            raise InconsistentInventoryStateError(
                f'Order pk={order_id}: no legacy lines but stock_deducted=True. '
                'Manual investigation required.'
            )
        if _reservation_exists:
            # Row 6: further status check
            if _reservation_status == _Res.STATUS_RESERVED:
                return _make_summary()   # Row 6: normal second-call idempotency
            raise InconsistentInventoryStateError(
                f'Order pk={order_id}: existing reservation has '
                f'status={_reservation_status!r} but order is still '
                'awaiting_payment. Manual investigation required.'
            )
        # Row 5: first call — fall through

    # Case C: mixed order
    else:
        # Row 11: ingredient done, legacy not done
        if _reservation_exists and not _legacy_done:
            raise InconsistentInventoryStateError(
                f'Order pk={order_id}: reservation exists '
                f'(status={_reservation_status!r}) but stock_deducted=False '
                'on a mixed order. Manual investigation required.'
            )
        # Row 12: legacy done, ingredient not done
        if _legacy_done and not _reservation_exists:
            raise InconsistentInventoryStateError(
                f'Order pk={order_id}: stock_deducted=True but no reservation '
                'on a mixed order. Manual investigation required.'
            )
        # Row 10: both done — further status check
        if _legacy_done and _reservation_exists:
            if _reservation_status == _Res.STATUS_RESERVED:
                return _make_summary()   # Row 10: normal second-call idempotency
            raise InconsistentInventoryStateError(
                f'Order pk={order_id}: existing reservation has '
                f'status={_reservation_status!r} but order is still '
                'awaiting_payment on a mixed order. Manual investigation required.'
            )
        # Row 9: first call — fall through

    # ── Step 7: ingredient lines — resolve_plan() read-only validation ────────
    # Called under Product locks (Rev3 §H.9) so configuration writers
    # (approve_recipe, save_packaging_rule) cannot interleave.
    # Raises RecipeIncompleteError / InactiveItemError (both subclass ValueError)
    # if configuration is invalid.
    from apps.inventory.services import InactiveItemError  # noqa: F401
    ingredient_pairs = []   # list[(OrderItem, ResolvedPlan)]
    for item in ingredient_items:
        product = locked_products[item.product_id]
        plan = resolve_plan(
            product_id=product.pk,
            size=item.size,
            order_type=order.order_type,
        )
        ingredient_pairs.append((item, plan))

    # ── Step 8: aggregate required quantities across all ingredient lines ──────
    # Sum quantity_per_unit * line.quantity per inventory_item_id across ALL
    # ingredient lines.  Multiple lines sharing the same ingredient are
    # summed before locking and validating.
    from decimal import Decimal as _Decimal
    required: dict[int, _Decimal] = {}
    for _item, _plan in ingredient_pairs:
        for _comp in _plan.components:
            _inv_pk = _comp.inventory_item.pk
            required[_inv_pk] = (
                required.get(_inv_pk, _Decimal('0'))
                + _comp.quantity_per_unit * _item.quantity
            )
    item_ids = sorted(required.keys())

    # ── Step 9: lock InventoryItem rows (level 3, ascending pk) ──────────────
    # Uses the existing lock_inventory_items() helper from apps/inventory.
    locked_items: dict[int, object] = {}
    if item_ids:
        from apps.inventory.services import (
            lock_inventory_items,
            InactiveItemError,
        )
        locked_items = lock_inventory_items(item_ids)
        # Re-validate activity under the lock.  Item deactivation between
        # resolve_plan (step 7) and here is a rare concurrent race.
        # Inactive items are a configuration error, not a stock shortage.
        inactive = [inv for inv in locked_items.values() if not inv.is_active]
        if inactive:
            raise InactiveItemError(
                f'InventoryItem(s) became inactive between plan resolution and '
                f'reservation: {[inv.name for inv in inactive]}. '
                'Reactivate or update the recipe/packaging profile.',
                item=inactive[0],
            )

    # ── Step 10: validate EVERYTHING before writing ANYTHING ─────────────────
    # Collect all shortages across both ledgers.  Any shortage prevents ALL
    # writes — legacy deduction AND ingredient reservation are both blocked.
    from apps.inventory.services import InsufficientStockError
    shortages = []

    # Ingredient shortages
    for inv_pk in item_ids:
        inv = locked_items[inv_pk]
        if inv.available_quantity < required[inv_pk]:
            shortages.append((
                'ingredient', inv_pk, inv.name,
                required[inv_pk], inv.available_quantity,
            ))

    # Legacy shortages (replaces the old bare ValueError in step 7)
    aggregated_legacy: dict[int, int] = {}
    for item in legacy_items:
        aggregated_legacy[item.product_id] = (
            aggregated_legacy.get(item.product_id, 0) + item.quantity
        )
    for pid, total_req in aggregated_legacy.items():
        product = locked_products[pid]
        if product.stock_quantity < total_req:
            shortages.append((
                'legacy', pid, product.name,
                total_req, product.stock_quantity,
            ))

    if shortages:
        raise InsufficientStockError(shortages=shortages)

    # ── Step 11: legacy write ──────────────────────────────────────────────────
    if legacy_items:
        _apply_inventory_for_order(
            order, legacy_items, actor, deduct=True,
        )
    # _apply_inventory_for_order sets order.stock_deducted = True when
    # legacy lines exist.  For ingredient-only orders it remains False.

    # ── Step 12: ingredient reservation write ─────────────────────────────────
    if ingredient_pairs:
        from apps.orders.models import (
            OrderIngredientReservation,
            OrderIngredientReservationItem,
        )
        from apps.inventory.models import IngredientMovementLog, InventoryItem
        from django.db import models as _dj_models

        # 12a. Create reservation header.
        res = OrderIngredientReservation.objects.create(
            order=order,
            status=OrderIngredientReservation.STATUS_RESERVED,
            order_type_snapshot=order.order_type,
        )

        # 12b. Create reservation items — one per (order_item, component).
        for item, plan in ingredient_pairs:
            for comp in plan.components:
                OrderIngredientReservationItem.objects.create(
                    reservation=res,
                    order_item=item,
                    inventory_item=comp.inventory_item,
                    item_name=comp.inventory_item.name,
                    base_unit=comp.inventory_item.base_unit,
                    component_source=comp.component_source,
                    source_recipe_id=comp.source_recipe_id,
                    source_packaging_profile_id=comp.source_packaging_profile_id,
                    source_revision=comp.source_revision,
                    quantity_per_unit=comp.quantity_per_unit,
                    order_item_quantity=item.quantity,
                    reserved_quantity=(comp.quantity_per_unit * item.quantity),
                )

        # 12c. Update reserved_quantity and write movement logs.
        # Processed in ascending pk order (matching the lock order).
        for inv_pk in item_ids:
            inv_before = locked_items[inv_pk]  # snapshot before the UPDATE
            req = required[inv_pk]

            # Conditional UPDATE backstop (Rev3 §H.2, §H.6).
            # Correct even on SQLite which ignores row locks.
            updated = InventoryItem.objects.filter(
                pk=inv_pk,
                on_hand_quantity__gte=_dj_models.F('reserved_quantity') + req,
            ).update(
                reserved_quantity=_dj_models.F('reserved_quantity') + req
            )

            if updated != 1:
                raise ValueError(
                    f'Concurrent modification detected for InventoryItem '
                    f'pk={inv_pk}; transaction will roll back.'
                )

            IngredientMovementLog.record(
                inventory_item=inv_before,
                movement_type=IngredientMovementLog.MOVEMENT_RESERVATION,
                on_hand_delta=_Decimal('0'),
                reserved_delta=req,
                on_hand_before=inv_before.on_hand_quantity,
                reserved_before=inv_before.reserved_quantity,
                order=order,
                idempotency_key=f'order:{order.pk}:reserve:{inv_pk}',
                performed_by=actor,
                # reservation_item is None for aggregated reservation movements
                # (many-to-one problem — one movement covers multiple reservation
                # items sharing the same InventoryItem).  The field exists on the
                # model but the record() classmethod does not expose it; the FK
                # is more meaningful for settle() consumption/release movements
                # (Phase 6B) where there is a 1:1 correspondence.
            )

    return {
        'legacy_line_count':     len(legacy_items),
        'ingredient_line_count': len(ingredient_items),
        'stock_deducted':        order.stock_deducted,
        'ingredient_plans':      [plan for _, plan in ingredient_pairs],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Phase 6A — Cancellation Coordinator
# ══════════════════════════════════════════════════════════════════════════════
#
# Rev 3 §H.3 on_status_changed pseudocode + §H.8 legacy_restore_by_disposition.
#
# Stage 6A-2: service layer only.  These functions are not yet wired to views
# (Stage 6A-3) or admin (Stage 6A-4).  The caller MUST hold an outer
# transaction.atomic() block.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5B-3 — Reservation Settlement Service
# ══════════════════════════════════════════════════════════════════════════════
#
# Rev 7 of the Phase 5B-3 implementation plan.
#
# settle() is the single entry point for all reservation closure:
#   RELEASE_ALL     — cancel from awaiting_payment
#   CONSUME_ALL     — cancel from ready
#   BY_DISPOSITION  — cancel from preparing
#
# Callers (on_status_changed) pass the already-locked reservation header and
# already-locked reservation items so settle() runs entirely inside the
# caller's transaction.atomic() block.
# ══════════════════════════════════════════════════════════════════════════════

SETTLE_MODE_RELEASE_ALL    = 'RELEASE_ALL'
SETTLE_MODE_CONSUME_ALL    = 'CONSUME_ALL'
SETTLE_MODE_BY_DISPOSITION = 'BY_DISPOSITION'


def settle(reservation, lines, mode, reason, disposition_data=None, actor=None):
    """
    Settle an OrderIngredientReservation.

    Parameters
    ----------
    reservation : OrderIngredientReservation
        Already locked with select_for_update().
    lines : list[OrderIngredientReservationItem]
        Already locked with select_for_update() (ordered by pk).
    mode : str
        One of SETTLE_MODE_RELEASE_ALL, SETTLE_MODE_CONSUME_ALL,
        SETTLE_MODE_BY_DISPOSITION.
    reason : str
        Reason code for consume movements (e.g. 'cancelled_prepared').
        For release movements a release-specific reason is derived from mode.
    disposition_data : dict or None
        Required for BY_DISPOSITION.  Keyed by order_item_id:
        ``{order_item_id: {"prepared_units": int, "unprepared_units": int}}``
    actor : CustomUser or None

    Raises
    ------
    InconsistentInventoryStateError
        BY_DISPOSITION quantity mismatch; conditional UPDATE fails; movement
        conflict; counter mismatch after settlement; header/line inconsistency.
    ValueError
        Conditional UPDATE returns 0 rows (concurrent modification backstop).
    """
    from decimal import Decimal as _D
    from django.db import models as _dj_models
    from django.utils import timezone
    from apps.inventory.models import IngredientMovementLog, InventoryItem

    # Derive release reason from mode
    if mode == SETTLE_MODE_RELEASE_ALL:
        release_reason = 'cancelled_before_payment'
    else:
        # BY_DISPOSITION or CONSUME_ALL — release means unprepared units
        release_reason = 'cancelled_not_prepared'

    order = reservation.order

    # ── State matrix check (§4.4): validate reservation state before processing ─
    res_status = reservation.status
    total_outstanding = sum(
        line.reserved_quantity - line.consumed_quantity - line.released_quantity
        for line in lines
    )

    if res_status != 'reserved':
        # Closed reservation
        if total_outstanding > _D('0'):
            raise InconsistentInventoryStateError(
                f'Reservation pk={reservation.pk} has status={res_status!r} '
                f'(closed) but outstanding={total_outstanding} > 0.'
            )
        # outstanding == 0 — check header/line consistency, then no-op
        _validate_header_line_consistency(reservation, lines)
        return
    else:
        # status == 'reserved'
        if total_outstanding == _D('0'):
            raise InconsistentInventoryStateError(
                f'Reservation pk={reservation.pk} has status=reserved but '
                'all lines have outstanding == 0.'
            )

    for line in lines:
        outstanding    = line.reserved_quantity - line.consumed_quantity - line.released_quantity

        if mode == SETTLE_MODE_RELEASE_ALL:
            consume_amount = _D('0')
            release_amount = outstanding

        elif mode == SETTLE_MODE_CONSUME_ALL:
            consume_amount = outstanding
            release_amount = _D('0')

        else:  # BY_DISPOSITION
            if disposition_data is None:
                raise InconsistentInventoryStateError(
                    'BY_DISPOSITION mode requires disposition_data.'
                )
            order_item_id = line.order_item_id
            disp = disposition_data.get(order_item_id)
            if disp is None:
                raise InconsistentInventoryStateError(
                    f'BY_DISPOSITION: no disposition entry for order_item_id={order_item_id}.'
                )
            prepared_units   = int(disp.get('prepared_units',   0))
            unprepared_units = int(disp.get('unprepared_units', 0))
            full_consume = line.quantity_per_unit * prepared_units
            full_release = line.quantity_per_unit * unprepared_units

            # §8.3: full_consume + full_release must equal the total reserved quantity
            # (not just the remaining outstanding), so that the disposition totals are
            # internally consistent.  For fresh lines outstanding == full_consume + full_release.
            # For partially-done lines (idempotency / partial recovery path) we check
            # idempotency keys first and subtract already-done amounts from the targets.
            consume_key = f'line:{line.pk}:consume'
            release_key = f'line:{line.pk}:release'
            existing_consume = IngredientMovementLog.objects.filter(
                idempotency_key=consume_key
            ).first()
            existing_release = IngredientMovementLog.objects.filter(
                idempotency_key=release_key
            ).first()

            already_consumed = line.consumed_quantity if existing_consume else _D('0')
            already_released = line.released_quantity if existing_release else _D('0')

            # Verify that full_consume + full_release == reserved_quantity
            # (the disposition must account for the entire reserved amount)
            if full_consume + full_release != line.reserved_quantity:
                raise InconsistentInventoryStateError(
                    f'BY_DISPOSITION quantity mismatch for reservation item pk={line.pk}: '
                    f'expected_consume ({full_consume}) + expected_release ({full_release}) '
                    f'= {full_consume + full_release} != reserved_quantity ({line.reserved_quantity}).'
                )

            # Remaining amounts (skipping already-done parts)
            consume_amount = full_consume - already_consumed
            release_amount = full_release - already_released

            if consume_amount < _D('0') or release_amount < _D('0'):
                raise InconsistentInventoryStateError(
                    f'BY_DISPOSITION for reservation item pk={line.pk}: '
                    f'already_consumed ({already_consumed}) > full_consume ({full_consume}) '
                    f'or already_released ({already_released}) > full_release ({full_release}).'
                )

        # ── Process consume operation ──────────────────────────────────────
        if consume_amount > _D('0'):
            _consume_line(line, consume_amount, reason, actor, order)

        # ── Process release operation ──────────────────────────────────────
        if release_amount > _D('0'):
            _release_line(line, release_amount, release_reason, actor, order)

        # ── Post-line counter verification ─────────────────────────────────
        line.refresh_from_db()
        final_outstanding = line.reserved_quantity - line.consumed_quantity - line.released_quantity
        if final_outstanding != _D('0'):
            raise InconsistentInventoryStateError(
                f'Reservation item pk={line.pk} has outstanding={final_outstanding} '
                f'after settlement; expected 0.'
            )

    # ── Derive final header status from aggregate line outcomes (§4.6) ─────
    lines_refreshed = list(reservation.items.all())

    all_consumed = all(
        line.consumed_quantity == line.reserved_quantity and line.released_quantity == _D('0')
        for line in lines_refreshed
    )
    all_released = all(
        line.released_quantity == line.reserved_quantity and line.consumed_quantity == _D('0')
        for line in lines_refreshed
    )
    has_consumed = any(line.consumed_quantity > _D('0') for line in lines_refreshed)
    has_released = any(line.released_quantity > _D('0') for line in lines_refreshed)

    if all_consumed:
        final_status = 'consumed'
    elif all_released:
        final_status = 'released'
    elif has_consumed and has_released:
        final_status = 'settled'
    else:
        raise InconsistentInventoryStateError(
            'Unable to derive reservation status from line outcomes after settlement.'
        )

    reservation.status = final_status
    reservation.settled_at = timezone.now()
    reservation.save(update_fields=['status', 'settled_at'])


def _consume_line(line, consume_amount, reason, actor, order):
    """
    Consume ``consume_amount`` from one reservation line.

    Checks the idempotency key first; if the movement already exists and
    counters are consistent, skips.  If the movement exists but counters
    are inconsistent, raises.

    Raises InconsistentInventoryStateError or ValueError on conflict.
    """
    from decimal import Decimal as _D
    from django.db import models as _dj_models
    from apps.inventory.models import IngredientMovementLog, InventoryItem

    idem_key = f'line:{line.pk}:consume'
    existing = IngredientMovementLog.objects.filter(idempotency_key=idem_key).first()

    if existing is not None:
        # Validate it matches what we expect
        if existing.inventory_item_id != line.inventory_item_id:
            raise InconsistentInventoryStateError(
                f'Existing consume movement (key={idem_key}) references '
                f'inventory_item_id={existing.inventory_item_id} but '
                f'line inventory_item_id={line.inventory_item_id}.'
            )
        if existing.on_hand_delta != -consume_amount or existing.reserved_delta != -consume_amount:
            raise InconsistentInventoryStateError(
                f'Existing consume movement (key={idem_key}) has '
                f'on_hand_delta={existing.on_hand_delta}, '
                f'reserved_delta={existing.reserved_delta}; '
                f'expected both = {-consume_amount}.'
            )
        # Check line counters — consumed must be fully accounted for
        if line.consumed_quantity < consume_amount:
            raise InconsistentInventoryStateError(
                f'Existing consume movement (key={idem_key}) found but '
                f'line.consumed_quantity={line.consumed_quantity} < '
                f'consume_amount={consume_amount}. Counter inconsistency.'
            )
        # Idempotent skip — movement already recorded and counters are consistent
        return

    # ── Conditional UPDATE (Rev 7 §7.1) ──────────────────────────────────
    updated = InventoryItem.objects.filter(
        pk=line.inventory_item_id,
        on_hand_quantity__gte=consume_amount,
        reserved_quantity__gte=consume_amount,
    ).update(
        on_hand_quantity=_dj_models.F('on_hand_quantity') - consume_amount,
        reserved_quantity=_dj_models.F('reserved_quantity') - consume_amount,
    )
    if updated != 1:
        raise ValueError(
            f'Conditional UPDATE failed for InventoryItem pk={line.inventory_item_id} '
            f'during consume of {consume_amount}; insufficient on_hand or reserved.'
        )

    # Read fresh balances after the update for the movement log
    inv = InventoryItem.objects.get(pk=line.inventory_item_id)
    # Derive before-state from fresh after-state
    on_hand_before_val  = inv.on_hand_quantity + consume_amount
    reserved_before_val = inv.reserved_quantity + consume_amount

    IngredientMovementLog.record(
        inventory_item=inv,
        movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION,
        on_hand_delta=-consume_amount,
        reserved_delta=-consume_amount,
        on_hand_before=on_hand_before_val,
        reserved_before=reserved_before_val,
        reason_code=reason,
        order=order,
        reservation_item=line,
        idempotency_key=idem_key,
        performed_by=actor,
    )

    # Update line counters
    from django.utils import timezone as _tz
    line.consumed_quantity += consume_amount
    line.consume_reason = reason
    line.consumed_at = _tz.now()
    line.save(update_fields=['consumed_quantity', 'consume_reason', 'consumed_at'])


def _release_line(line, release_amount, reason, actor, order):
    """
    Release ``release_amount`` from one reservation line.

    Checks the idempotency key first; if the movement already exists and
    counters are consistent, skips.  If the movement exists but counters
    are inconsistent, raises.

    Raises InconsistentInventoryStateError or ValueError on conflict.
    """
    from decimal import Decimal as _D
    from django.db import models as _dj_models
    from apps.inventory.models import IngredientMovementLog, InventoryItem

    idem_key = f'line:{line.pk}:release'
    existing = IngredientMovementLog.objects.filter(idempotency_key=idem_key).first()

    if existing is not None:
        # Validate it matches what we expect
        if existing.inventory_item_id != line.inventory_item_id:
            raise InconsistentInventoryStateError(
                f'Existing release movement (key={idem_key}) references '
                f'inventory_item_id={existing.inventory_item_id} but '
                f'line inventory_item_id={line.inventory_item_id}.'
            )
        if existing.reserved_delta != -release_amount:
            raise InconsistentInventoryStateError(
                f'Existing release movement (key={idem_key}) has '
                f'reserved_delta={existing.reserved_delta}; '
                f'expected {-release_amount}.'
            )
        # Check line counters
        if line.released_quantity < release_amount:
            raise InconsistentInventoryStateError(
                f'Existing release movement (key={idem_key}) found but '
                f'line.released_quantity={line.released_quantity} < '
                f'release_amount={release_amount}. Counter inconsistency.'
            )
        # Idempotent skip
        return

    # ── Conditional UPDATE (Rev 7 §7.2) ──────────────────────────────────
    updated = InventoryItem.objects.filter(
        pk=line.inventory_item_id,
        reserved_quantity__gte=release_amount,
    ).update(
        reserved_quantity=_dj_models.F('reserved_quantity') - release_amount,
    )
    if updated != 1:
        raise ValueError(
            f'Conditional UPDATE failed for InventoryItem pk={line.inventory_item_id} '
            f'during release of {release_amount}; insufficient reserved_quantity.'
        )

    # Read fresh balances after the update for the movement log
    inv = InventoryItem.objects.get(pk=line.inventory_item_id)
    reserved_before_val = inv.reserved_quantity + release_amount

    IngredientMovementLog.record(
        inventory_item=inv,
        movement_type=IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE,
        on_hand_delta=Decimal('0'),
        reserved_delta=-release_amount,
        on_hand_before=inv.on_hand_quantity,
        reserved_before=reserved_before_val,
        reason_code=reason,
        order=order,
        reservation_item=line,
        idempotency_key=idem_key,
        performed_by=actor,
    )

    # Update line counters
    from django.utils import timezone as _tz
    line.released_quantity += release_amount
    line.release_reason = reason
    line.released_at = _tz.now()
    line.save(update_fields=['released_quantity', 'release_reason', 'released_at'])


def _validate_reservation_lines(reservation, order, lines):
    """
    Validate that locked reservation lines are complete and correctly owned.

    Receives the locked reservation items list (after select_for_update).
    Raises InconsistentInventoryStateError for any anomaly.

    Checks (Rev 7 §5.2):
      - Zero items → raise
      - Wrong order_item ownership → raise
      - Wrong reservation ownership → raise
      - Duplicate (order_item_id, inventory_item_id) pair → raise
      - Missing ingredient order items → raise
      - Unexpected non-ingredient order items → raise
    """
    from apps.orders.models import OrderItem

    if not lines:
        raise InconsistentInventoryStateError(
            f'Reservation pk={reservation.pk} has zero reservation items. '
            'Manual investigation required.'
        )

    # Collect valid order_item pks from this order
    order_item_ids_for_order = set(
        order.items.filter(
            inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT
        ).values_list('pk', flat=True)
    )

    seen_pairs = set()
    seen_order_item_ids = set()

    for line in lines:
        # Wrong reservation ownership
        if line.reservation_id != reservation.pk:
            raise InconsistentInventoryStateError(
                f'Reservation item pk={line.pk} has reservation_id={line.reservation_id} '
                f'but expected reservation pk={reservation.pk}.'
            )
        # Wrong order_item ownership
        if line.order_item_id not in order_item_ids_for_order:
            raise InconsistentInventoryStateError(
                f'Reservation item pk={line.pk} has order_item_id={line.order_item_id} '
                f'which does not belong to order pk={order.pk} or is not ingredient-mode.'
            )
        # Duplicate (order_item_id, inventory_item_id) pair
        pair = (line.order_item_id, line.inventory_item_id)
        if pair in seen_pairs:
            raise InconsistentInventoryStateError(
                f'Duplicate (order_item_id={line.order_item_id}, '
                f'inventory_item_id={line.inventory_item_id}) pair in reservation '
                f'pk={reservation.pk}.'
            )
        seen_pairs.add(pair)
        seen_order_item_ids.add(line.order_item_id)

    # Missing ingredient order items
    missing = order_item_ids_for_order - seen_order_item_ids
    if missing:
        raise InconsistentInventoryStateError(
            f'Reservation pk={reservation.pk} is missing lines for '
            f'ingredient order_item pks: {sorted(missing)}.'
        )

    # Unexpected non-ingredient order_items
    unexpected = seen_order_item_ids - order_item_ids_for_order
    if unexpected:
        raise InconsistentInventoryStateError(
            f'Reservation pk={reservation.pk} contains lines for non-ingredient '
            f'order_item pks: {sorted(unexpected)}.'
        )


def _validate_header_line_consistency(reservation, lines):
    """
    Validate that the reservation header status matches its line counters.

    Runs AFTER outstanding quantities are calculated from locked instances.
    Raises InconsistentInventoryStateError if the header status does not
    match the aggregate line outcomes.

    Rules (Rev 7 §4.5):
      consumed  — every line: consumed == reserved AND released == 0
      released  — every line: released == reserved AND consumed == 0
      settled   — genuine mix: at least one consumed > 0 AND at least one released > 0
      reserved  — at least one line has outstanding > 0
    """
    from decimal import Decimal as _D

    status = reservation.status

    if status == 'consumed':
        for line in lines:
            if line.consumed_quantity != line.reserved_quantity or line.released_quantity != _D('0'):
                raise InconsistentInventoryStateError(
                    f'Reservation pk={reservation.pk} has status=consumed but '
                    f'line pk={line.pk} has consumed={line.consumed_quantity}, '
                    f'released={line.released_quantity}, reserved={line.reserved_quantity}. '
                    'Header/line consistency violated.'
                )

    elif status == 'released':
        for line in lines:
            if line.released_quantity != line.reserved_quantity or line.consumed_quantity != _D('0'):
                raise InconsistentInventoryStateError(
                    f'Reservation pk={reservation.pk} has status=released but '
                    f'line pk={line.pk} has released={line.released_quantity}, '
                    f'consumed={line.consumed_quantity}, reserved={line.reserved_quantity}. '
                    'Header/line consistency violated.'
                )

    elif status == 'settled':
        has_consumed = any(line.consumed_quantity > _D('0') for line in lines)
        has_released = any(line.released_quantity > _D('0') for line in lines)
        if not (has_consumed and has_released):
            raise InconsistentInventoryStateError(
                f'Reservation pk={reservation.pk} has status=settled but '
                f'lines do not show a genuine mix (has_consumed={has_consumed}, '
                f'has_released={has_released}). '
                'Header/line consistency violated.'
            )

    elif status == 'reserved':
        # At least one line must have outstanding > 0
        all_zero = all(
            (line.reserved_quantity - line.consumed_quantity - line.released_quantity) == _D('0')
            for line in lines
        )
        if all_zero:
            raise InconsistentInventoryStateError(
                f'Reservation pk={reservation.pk} has status=reserved but '
                'all lines have outstanding == 0. '
                'Header/line consistency violated.'
            )


def _settle_reservation(order, old_status, disp_data, actor):
    """
    Internal dispatcher: decides whether to settle the ingredient reservation
    and with which mode, then calls settle().

    Called from on_status_changed() after legacy operations complete.
    Runs inside the caller's transaction.atomic() block.

    Parameters
    ----------
    order : Order
        Already locked (level-1 lock held by on_status_changed).
    old_status : str
        The order's status before the cancellation.
    disp_data : dict
        The disposition data already derived (keyed by order_item_id).
    actor : CustomUser or None

    Raises
    ------
    InconsistentInventoryStateError
        Any reservation lifecycle anomaly.
    ValueError
        Conditional UPDATE backstop or invalid quantities.
    """
    from apps.orders.models import (
        OrderIngredientReservation,
        OrderIngredientReservationItem,
        OrderItem,
    )
    from apps.inventory.services import lock_inventory_items
    from decimal import Decimal as _D

    # ── Step 6: Fetch reservation with select_for_update ──────────────────
    has_ingredient_items = order.items.filter(
        inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT
    ).exists()

    try:
        reservation = (
            OrderIngredientReservation.objects
            .select_for_update()
            .get(order=order)
        )
    except OrderIngredientReservation.DoesNotExist:
        reservation = None

    # ── Step 7: Check existence ───────────────────────────────────────────
    if reservation is None:
        if has_ingredient_items:
            # Check whether place_order_inventory() was ever called for this
            # order.  If a reservation movement log exists, the reservation
            # SHOULD exist — raise.  If no reservation log exists, this order
            # pre-dates Phase 5B-2 (or is a Phase 6A test fixture where
            # place_order_inventory was not called) — treat as a no-op for
            # backward compatibility.
            from apps.inventory.models import IngredientMovementLog as _IML
            has_reservation_log = _IML.objects.filter(
                order=order,
                movement_type=_IML.MOVEMENT_RESERVATION,
            ).exists()
            if has_reservation_log:
                raise InconsistentInventoryStateError(
                    f'Order pk={order.pk} has ingredient order items and a '
                    'reservation movement log, but no OrderIngredientReservation. '
                    'Manual investigation required.'
                )
            # No reservation log → place_order_inventory was not called →
            # ingredient settlement is a no-op (pre-5B-2 or test fixture).
            return
        # Legacy-only: no reservation to settle
        return

    # ── Step 8: Orphan check (reservation exists but no ingredient items) ─
    if not has_ingredient_items:
        raise InconsistentInventoryStateError(
            f'Order pk={order.pk} has an OrderIngredientReservation '
            f'(pk={reservation.pk}) but no ingredient order items. '
            'Manual investigation required.'
        )

    # ── Step 9: Lock reservation items (no select_related — Rev 7 §9.2) ──
    lines = list(
        reservation.items
        .select_for_update()
        .order_by('pk')
    )

    # ── Step 10: Validate reservation lines ───────────────────────────────
    _validate_reservation_lines(reservation, order, lines)

    # ── Step 11: Calculate outstanding; apply state matrix (§4.4) ─────────
    total_outstanding = sum(
        line.reserved_quantity - line.consumed_quantity - line.released_quantity
        for line in lines
    )
    res_status = reservation.status

    if res_status != 'reserved':
        # Closed reservation
        if total_outstanding > _D('0'):
            raise InconsistentInventoryStateError(
                f'Reservation pk={reservation.pk} has status={res_status!r} '
                f'(closed) but outstanding={total_outstanding} > 0.'
            )
        # outstanding == 0 — check header/line consistency
        _validate_header_line_consistency(reservation, lines)
        # Consistent closed reservation — idempotent no-op
        return
    else:
        # status == 'reserved'
        if total_outstanding == _D('0'):
            raise InconsistentInventoryStateError(
                f'Reservation pk={reservation.pk} has status=reserved but '
                'all lines have outstanding == 0.'
            )
        # Proceed with settlement

    # ── Step 12 & 13: Collect InventoryItem PKs and lock them ─────────────
    item_ids = sorted({line.inventory_item_id for line in lines})
    if item_ids:
        lock_inventory_items(item_ids)

    # ── Step 14 & 15: Determine mode and settle ───────────────────────────
    if old_status == 'awaiting_payment':
        mode   = SETTLE_MODE_RELEASE_ALL
        reason = 'cancelled_before_payment'
    elif old_status == 'ready':
        mode   = SETTLE_MODE_CONSUME_ALL
        reason = 'cancelled_prepared'
    else:
        # old_status == 'preparing'
        mode = SETTLE_MODE_BY_DISPOSITION
        reason = 'cancelled_prepared'   # used for consume lines
        # Build BY_DISPOSITION lookup from disp_data
        # disp_data is keyed by order_item.pk with 'prepared_units' and 'unprepared_units'
        # Pass through — settle() reads disposition_data by order_item_id

    settle(
        reservation=reservation,
        lines=lines,
        mode=mode,
        reason=reason,
        disposition_data=disp_data,
        actor=actor,
    )


# ── Phase 6B: Normal consumption and completion assertion ───────────────────


def _consume_on_ready(order, actor):
    """
    Consume all outstanding ingredient reservations on ``preparing → ready``.

    Rev 3 §F.3 row 5: consume all outstanding; lines ``consumed``
    (``order_ready``); reservation ``consumed``.

    Runs inside the caller's ``transaction.atomic()`` block.  Reuses the
    existing ``settle()`` with ``CONSUME_ALL`` and reason ``'order_ready'``.

    The lock sequence matches ``_settle_reservation()``:
    reservation header → reservation items → inventory items.
    """
    from apps.orders.models import OrderIngredientReservation, OrderItem
    from apps.inventory.services import lock_inventory_items
    from decimal import Decimal as _D

    has_ingredient_items = order.items.filter(
        inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT
    ).exists()

    try:
        reservation = (
            OrderIngredientReservation.objects
            .select_for_update()
            .get(order=order)
        )
    except OrderIngredientReservation.DoesNotExist:
        reservation = None

    if reservation is None:
        if has_ingredient_items:
            from apps.inventory.models import IngredientMovementLog as _IML
            has_reservation_log = _IML.objects.filter(
                order=order,
                movement_type=_IML.MOVEMENT_RESERVATION,
            ).exists()
            if has_reservation_log:
                raise InconsistentInventoryStateError(
                    f'Order pk={order.pk} has ingredient order items and a '
                    'reservation movement log, but no OrderIngredientReservation. '
                    'Manual investigation required.'
                )
            return  # No reservation log → place_order_inventory not called → no-op
        return  # Legacy-only: no reservation to consume

    if not has_ingredient_items:
        raise InconsistentInventoryStateError(
            f'Order pk={order.pk} has an OrderIngredientReservation '
            f'(pk={reservation.pk}) but no ingredient order items. '
            'Manual investigation required.'
        )

    # Lock reservation items (no select_related — Rev 7 §9.2)
    lines = list(
        reservation.items
        .select_for_update()
        .order_by('pk')
    )

    _validate_reservation_lines(reservation, order, lines)

    total_outstanding = sum(
        line.reserved_quantity - line.consumed_quantity - line.released_quantity
        for line in lines
    )
    res_status = reservation.status

    if res_status != 'reserved':
        # Closed reservation
        if total_outstanding > _D('0'):
            raise InconsistentInventoryStateError(
                f'Reservation pk={reservation.pk} has status={res_status!r} '
                f'(closed) but outstanding={total_outstanding} > 0.'
            )
        # Idempotent: already settled (e.g. consumed on a prior ready call)
        _validate_header_line_consistency(reservation, lines)
        return
    else:
        if total_outstanding == _D('0'):
            raise InconsistentInventoryStateError(
                f'Reservation pk={reservation.pk} has status=reserved but '
                'all lines have outstanding == 0.'
            )

    # Lock inventory items (level 3, ascending pk)
    item_ids = sorted({line.inventory_item_id for line in lines})
    if item_ids:
        lock_inventory_items(item_ids)

    settle(
        reservation=reservation,
        lines=lines,
        mode=SETTLE_MODE_CONSUME_ALL,
        reason='order_ready',
        actor=actor,
    )


def _assert_settled_on_complete(order, actor):
    """
    Assert that the ingredient reservation is fully settled on
    ``ready → completed``.

    Rev 3 §F.3 row 6: assert settled; if anything is outstanding, consume
    it and log an anomaly.

    In normal operation, ``preparing → ready`` consumed everything, so
    outstanding is zero and this is a no-op.  The defensive recovery path
    fires only if the ``ready`` consumption was somehow skipped or partially
    failed without rolling back.

    Anomaly logging uses ``transaction.on_commit()`` so the warning is
    emitted only after the recovery transaction commits successfully.
    """
    from apps.orders.models import OrderIngredientReservation, OrderItem
    from apps.inventory.services import lock_inventory_items
    from decimal import Decimal as _D

    has_ingredient_items = order.items.filter(
        inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT
    ).exists()

    try:
        reservation = (
            OrderIngredientReservation.objects
            .select_for_update()
            .get(order=order)
        )
    except OrderIngredientReservation.DoesNotExist:
        reservation = None

    if reservation is None:
        if has_ingredient_items:
            from apps.inventory.models import IngredientMovementLog as _IML
            has_reservation_log = _IML.objects.filter(
                order=order,
                movement_type=_IML.MOVEMENT_RESERVATION,
            ).exists()
            if has_reservation_log:
                raise InconsistentInventoryStateError(
                    f'Order pk={order.pk} has ingredient order items and a '
                    'reservation movement log, but no OrderIngredientReservation. '
                    'Manual investigation required.'
                )
            return  # No reservation log → no-op
        return  # Legacy-only: no reservation

    if not has_ingredient_items:
        raise InconsistentInventoryStateError(
            f'Order pk={order.pk} has an OrderIngredientReservation '
            f'(pk={reservation.pk}) but no ingredient order items. '
            'Manual investigation required.'
        )

    lines = list(
        reservation.items
        .select_for_update()
        .order_by('pk')
    )

    _validate_reservation_lines(reservation, order, lines)

    total_outstanding = sum(
        line.reserved_quantity - line.consumed_quantity - line.released_quantity
        for line in lines
    )

    if total_outstanding == _D('0'):
        # Normal case: fully settled on the preparing → ready transition.
        return

    # ── Defensive recovery: outstanding found at completion ──────────────
    # Lock inventory items and consume the remaining quantity.
    item_ids = sorted({line.inventory_item_id for line in lines})
    if item_ids:
        lock_inventory_items(item_ids)

    # Capture context for anomaly logging before settle() modifies state.
    _order_id       = order.pk
    _reservation_id = reservation.pk
    _outstanding    = total_outstanding

    settle(
        reservation=reservation,
        lines=lines,
        mode=SETTLE_MODE_CONSUME_ALL,
        reason='order_ready',
        actor=actor,
    )

    # Schedule anomaly log after the transaction commits successfully.
    def _log_anomaly():
        _logger.warning(
            'Phase 6B anomaly: order pk=%s completed with outstanding reservation '
            'quantity %s on reservation pk=%s. Defensive consumption applied '
            'with reason=order_ready.',
            _order_id,
            str(_outstanding),
            _reservation_id,
        )

    transaction.on_commit(_log_anomaly)


class DispositionRequired(ValueError):
    """
    Raised when cancelling from ``preparing`` or ``ready`` without the
    required staff-entered disposition input.

    Subclasses ValueError so existing ``except ValueError`` handlers in
    views catch it without modification (Rev3 §H.7).
    """


class InconsistentInventoryStateError(ValueError):
    """
    Raised when ``place_order_inventory`` detects a partial-completion
    state or a reservation lifecycle state that cannot arise from normal
    atomic operation.

    Examples:
    - A legacy-only order that somehow already has a reservation.
    - An ingredient-only order whose ``stock_deducted`` flag is True.
    - A mixed order where only one ledger side is complete.
    - A reservation that exists with status ``consumed``, ``released``,
      or ``settled`` while the order is still ``awaiting_payment``.

    Subclasses ValueError so the existing view-level
    ``except ValueError`` handlers (checkout_view, create_pos_order)
    catch it without any view changes (Rev3 §H.7).

    The human-readable message carries enough detail for staff debugging.
    No inventory write has occurred when this is raised.
    """


def on_status_changed(order_id, new_status, actor=None, disposition=None):
    """
    Coordinate all inventory operations for a status change.

    Implements Rev 3 §H.3.  Callers MUST hold an outer ``transaction.atomic()``
    block; this function adds no outermost transaction of its own.

    Parameters
    ----------
    order_id : int
        PK of the Order.
    new_status : str
        Target status ('preparing', 'ready', 'completed', 'cancelled').
    actor : CustomUser or None
        Staff member performing the change.
    disposition : dict or None
        Staff-entered disposition input, keyed by order_item_id:
        ``{order_item_id: {"prepared_units": int, "discarded_units": int}}``

        Required when cancelling from ``preparing``: both ``prepared_units``
        and ``discarded_units`` must be provided for every applicable order
        item.

        Required when cancelling from ``ready``: ``discarded_units`` must be
        provided for every applicable legacy order item.  The service derives
        ``prepared_units = order_item.quantity`` and ``unprepared_units = 0``.

        ``None`` for ``awaiting_payment`` cancels: the system auto-generates
        ``system_unpaid`` dispositions (prepared=0, unprepared=quantity,
        discarded=0).

        Ignored for non-cancel transitions.

    Raises
    ------
    DispositionRequired (subclass of ValueError)
        Cancelling from ``preparing`` or ``ready`` without the required
        disposition input.
    ValueError
        Invalid transition, invalid disposition quantities, or order_item
        not belonging to this order.
    """
    from apps.orders.models import Order

    # ── Lock the Order row (level 1) ───────────────────────────────────────
    order = Order.objects.select_for_update().get(pk=order_id)
    old_status = order.status

    if old_status == new_status:
        return  # idempotent: same-status is a no-op

    validate_status_transition(old_status, new_status, order=order)

    # ── Non-cancel paths ────────────────────────────────────────────────
    if new_status != 'cancelled':
        # Phase 6B: consume on ready, assert settled on completed.
        if old_status == 'preparing' and new_status == 'ready':
            _consume_on_ready(order, actor)
        elif old_status == 'ready' and new_status == 'completed':
            _assert_settled_on_complete(order, actor)
        # All other non-cancel transitions remain no-ops.
        return

    # ── Cancellation: determine disposition based on old status ──────────
    if old_status == 'awaiting_payment':
        # System generates: prepared=0, unprepared=quantity, discarded=0
        disp_data = _system_unpaid(order)
    elif old_status == 'preparing':
        if disposition is None:
            raise DispositionRequired(
                'Cancelling an order in preparation requires a disposition '
                'declaring how many units were prepared for each order item.'
            )
        disp_data = _validate_and_derive_preparing(order, disposition)
    elif old_status == 'ready':
        if disposition is None:
            raise DispositionRequired(
                'Cancelling a ready order requires a disposition declaring '
                'how many prepared units were discarded for each applicable '
                'legacy order item.'
            )
        disp_data = _validate_and_derive_ready(order, disposition)
    else:
        # Should not reach here — validate_status_transition blocks invalid
        # transitions — but guard defensively.
        raise ValueError(
            f'Cannot cancel from status {old_status!r}.'
        )

    # ── Lock Product rows for legacy items (level 2) ─────────────────────
    from apps.orders.models import OrderItem
    legacy_pids = sorted({
        item.product_id
        for item in order.items.all()
        if item.product_id is not None
        and item.inventory_mode == OrderItem.INVENTORY_MODE_LEGACY
    })
    if legacy_pids:
        from apps.menu.models import Product
        list(
            Product.objects.select_for_update()
            .filter(pk__in=legacy_pids)
            .order_by('pk')
        )

    # ── Create disposition records ──────────────────────────────────────
    _create_dispositions(order, disp_data, actor)

    # ── Restore legacy stock by disposition ──────────────────────────────
    legacy_restore_by_disposition(order, disp_data, actor)

    # ── Ingredient settlement (Phase 5B-3) ───────────────────────────────
    _settle_reservation(order, old_status, disp_data, actor)

    # The caller is responsible for updating order.status, timestamps,
    # and writing the audit log — this function handles only the inventory
    # and disposition side-effects.


# ── Disposition derivation helpers ───────────────────────────────────────────


def _system_unpaid(order):
    """
    Generate system_unpaid dispositions for awaiting_payment cancellation.

    All items are unprepared: prepared=0, unprepared=quantity, discarded=0.
    """
    from apps.orders.models import OrderCancellationDisposition, OrderItem
    result = {}
    for item in order.items.all():
        result[item.pk] = {
            'ledger': item.inventory_mode,
            'order_item_quantity': item.quantity,
            'prepared_units': 0,
            'unprepared_units': item.quantity,
            'discarded_units': 0,
            'source': OrderCancellationDisposition.SOURCE_SYSTEM_UNPAID,
        }
    return result


def _validate_and_derive_preparing(order, staff_disposition):
    """
    Validate staff-entered disposition for preparing cancellation and derive
    the final disposition data.

    Staff submits: prepared_units and discarded_units per order item.
    Service derives: unprepared_units = quantity - prepared.
    """
    from apps.orders.models import OrderCancellationDisposition

    order_items = {item.pk: item for item in order.items.all()}
    result = {}

    for item_pk, item in order_items.items():
        staff_entry = staff_disposition.get(item_pk)
        if staff_entry is None:
            raise DispositionRequired(
                f'Missing disposition for order item pk={item_pk} '
                f'({item.product_name}).'
            )

        prepared = int(staff_entry.get('prepared_units', 0))
        discarded = int(staff_entry.get('discarded_units', 0))

        if prepared < 0 or prepared > item.quantity:
            raise ValueError(
                f'Invalid prepared_units ({prepared}) for order item '
                f'{item.product_name}: must be 0 to {item.quantity}.'
            )
        if discarded < 0 or discarded > prepared:
            raise ValueError(
                f'Invalid discarded_units ({discarded}) for order item '
                f'{item.product_name}: must be 0 to {prepared}.'
            )

        unprepared = item.quantity - prepared

        result[item_pk] = {
            'ledger': item.inventory_mode,
            'order_item_quantity': item.quantity,
            'prepared_units': prepared,
            'unprepared_units': unprepared,
            'discarded_units': discarded,
            'source': OrderCancellationDisposition.SOURCE_STAFF_DECLARED,
        }

    return result


def _validate_and_derive_ready(order, staff_disposition):
    """
    Validate staff-entered disposition for ready cancellation and derive
    the final disposition data.

    Staff submits: discarded_units per applicable legacy order item.
    Service derives: prepared_units = quantity, unprepared_units = 0.
    """
    from apps.orders.models import OrderCancellationDisposition, OrderItem

    order_items = {item.pk: item for item in order.items.all()}
    result = {}

    for item_pk, item in order_items.items():
        staff_entry = staff_disposition.get(item_pk)

        if item.inventory_mode == OrderItem.INVENTORY_MODE_LEGACY:
            # Legacy items require staff-entered discarded_units.
            if staff_entry is None:
                raise DispositionRequired(
                    f'Missing discarded quantity for legacy order item '
                    f'pk={item_pk} ({item.product_name}).'
                )
            discarded = int(staff_entry.get('discarded_units', 0))
            if discarded < 0 or discarded > item.quantity:
                raise ValueError(
                    f'Invalid discarded_units ({discarded}) for order item '
                    f'{item.product_name}: must be 0 to {item.quantity}.'
                )
        else:
            # Ingredient items: no staff input needed in Phase 6A.
            discarded = 0

        result[item_pk] = {
            'ledger': item.inventory_mode,
            'order_item_quantity': item.quantity,
            'prepared_units': item.quantity,  # all prepared
            'unprepared_units': 0,
            'discarded_units': discarded,
            'source': OrderCancellationDisposition.SOURCE_SYSTEM_READY,
        }

    return result


# ── Disposition persistence ──────────────────────────────────────────────────


def _create_dispositions(order, disp_data, actor=None):
    """
    Create OrderCancellationDisposition rows for every order item.

    The ``ocd_unique_item`` unique constraint prevents duplicates; an
    IntegrityError here rolls back the entire transaction.
    """
    from apps.orders.models import OrderCancellationDisposition, OrderItem

    order_items = {item.pk: item for item in order.items.all()}

    for item_pk, data in disp_data.items():
        item = order_items.get(item_pk)
        if item is None:
            raise ValueError(
                f'Order item pk={item_pk} does not belong to '
                f'order pk={order.pk}.'
            )

        # Verify snapshot consistency.
        if data['order_item_quantity'] != item.quantity:
            raise ValueError(
                f'Disposition quantity snapshot ({data["order_item_quantity"]}) '
                f'does not match order item quantity ({item.quantity}) for '
                f'{item.product_name}.'
            )

        OrderCancellationDisposition.objects.create(
            order=order,
            order_item=item,
            ledger=data['ledger'],
            order_item_quantity=data['order_item_quantity'],
            prepared_units=data['prepared_units'],
            unprepared_units=data['unprepared_units'],
            discarded_units=data['discarded_units'],
            source=data['source'],
            decided_by=actor,
            note='',
        )


# ── Legacy restoration by disposition ─────────────────────────────────────────


def legacy_restore_by_disposition(order, dispositions, actor=None):
    """
    Restore only unprepared legacy units.  Rev3 §H.8.

    The service derives ``item_quantities`` from the disposition's
    ``unprepared_units``.  The view never calculates restoration quantities.

    For ready-order cancellations (all prepared, unprepared=0), the function
    sets ``stock_deducted = False`` directly without calling
    ``restore_inventory_for_order`` — no stock is restored, no
    ``InventoryLog`` rows are written.
    """
    if not order.stock_deducted:
        # Legacy stock was never deducted (ingredient-only order) or
        # already restored by a previous cancellation.  Nothing to do.
        # The coordinator still creates dispositions and updates status.
        return

    from apps.orders.models import OrderItem
    legacy_items = list(
        order.items
        .filter(inventory_mode=OrderItem.INVENTORY_MODE_LEGACY)
        .select_related('product')
    )
    if not legacy_items:
        return  # No legacy items on this order

    # Build item_quantities with ALL legacy items, including 0 for prepared
    # ones.  With the _apply_inventory_for_order fix, 0-quantity items are
    # skipped (no restore_stock call, no InventoryLog row).
    item_quantities = {}
    has_any_unprepared = False
    for item in legacy_items:
        disp = dispositions.get(item.pk)
        unprepared = disp['unprepared_units'] if disp else 0
        item_quantities[item.pk] = unprepared
        if unprepared > 0:
            has_any_unprepared = True

    if has_any_unprepared:
        # restore_inventory_for_order sets stock_deducted = False internally.
        # _apply_inventory_for_order skips items with 0 unprepared
        # (no restore_stock, no InventoryLog for those items).
        restore_inventory_for_order(
            order, performed_by=actor,
            item_quantities=item_quantities,
        )
    else:
        # All items are prepared (e.g. ready-order cancellation).
        # No stock to restore, no InventoryLog rows to write.
        # Mark the legacy ledger as settled.
        order.stock_deducted = False
        order.save(update_fields=['stock_deducted'])
