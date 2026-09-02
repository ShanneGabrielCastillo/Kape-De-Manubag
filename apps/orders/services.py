"""
Order Services - Centralized inventory management for orders.
Call these functions from views only, never from models.
"""
from django.db import transaction
from decimal import Decimal

from apps.inventory.models import InventoryLog


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
    )


# ── Status Transition Validation ──────────────────────────────────────────────
# This map is the single authoritative definition of every allowed status
# transition in the business workflow:
#
#   pending  → preparing  (kitchen accepts the order)
#   pending  → cancelled  (cancelled before prep starts)
#   preparing→ ready      (kitchen finishes preparation)
#   preparing→ cancelled  (cancelled mid-prep)
#   ready    → completed  (customer collects / cashier completes)
#   ready    → cancelled  (rare: customer no-show)
#   completed→ (none)     terminal — order is done
#   cancelled→ (none)     terminal — order is void
#
# Any transition not listed above is explicitly forbidden.  All views that
# change order status must call validate_status_transition() before writing.
VALID_TRANSITIONS: dict[str, set[str]] = {
    'pending':   {'preparing', 'cancelled'},
    'preparing': {'ready',     'cancelled'},
    'ready':     {'completed', 'cancelled'},
    'completed': set(),   # terminal state — no further transitions
    'cancelled': set(),   # terminal state — no further transitions
}


def validate_status_transition(current_status: str, new_status: str) -> None:
    """Raise ValueError if current_status → new_status is not a valid transition.

    Called by every view that changes order status.  The error message is
    user-readable so it can be surfaced directly in the API response.

    Raises:
        ValueError: with a descriptive message when the transition is invalid.
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


def _apply_inventory_for_order(order, items, performed_by, *, deduct):
    """Apply one stock movement (deduct or restore) for every order item.

    Shared by :func:`deduct_inventory_for_order` (``deduct=True``) and
    :func:`restore_inventory_for_order` (``deduct=False``): each item's stock
    change, its inventory log row and the order's ``stock_deducted`` flag
    commit or roll back together. ``items`` is passed in so callers fetch the
    queryset exactly once (and, for deductions, run the pre-flight check on
    the same rows that get changed).
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

            old_qty = item.product.stock_quantity
            if deduct:
                item.product.reduce_stock(item.quantity)
            else:
                item.product.restore_stock(item.quantity)
            new_qty = item.product.stock_quantity

            InventoryLog.record(
                product=item.product,
                action=action,
                source=source,
                reason=reason,
                quantity_change=quantity_sign * item.quantity,
                quantity_before=old_qty,
                quantity_after=new_qty,
                performed_by=performed_by,
            )

        order.stock_deducted = deduct
        order.save(update_fields=['stock_deducted'])


def deduct_inventory_for_order(order, performed_by=None):
    """
    Deduct stock for all items in an order.
    Idempotent: does nothing if order.stock_deducted is already True.
    Raises ValueError if any product has insufficient stock (pre-flight check).
    """
    if order.stock_deducted:
        return

    items = order.items.select_related('product').all()

    # Pre-flight validation — check all items before touching any stock
    for item in items:
        if item.product is None:
            continue
        if item.product.stock_quantity < item.quantity:
            raise ValueError(
                f"Insufficient stock for '{item.product.name}'. "
                f"Available: {item.product.stock_quantity}, "
                f"Requested: {item.quantity}"
            )

    # All checks passed — apply the movement atomically.
    _apply_inventory_for_order(order, items, performed_by, deduct=True)


def restore_inventory_for_order(order, performed_by=None):
    """
    Restore stock for all items in a cancelled order.
    Idempotent: does nothing if order.stock_deducted is already False.
    """
    if not order.stock_deducted:
        return

    items = order.items.select_related('product').all()
    _apply_inventory_for_order(order, items, performed_by, deduct=False)
