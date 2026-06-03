"""
Order Services - Centralized inventory management for orders.
Call these functions from views only, never from models.
"""
from django.db import transaction
from decimal import Decimal


def get_packaging_fee_per_item():
    """Return the configured packaging fee per eligible meal item."""
    from apps.dashboard.models import SystemSetting
    raw = SystemSetting.get('PACKAGING_FEE_PER_ITEM', '6.00')
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal('6.00')


def calculate_packaging_fee(order):
    """
    Calculate total packaging fee for a Take-Out order.
    Only items whose product category has is_packaging_required=True are charged.
    Returns Decimal('0.00') for Dine-In orders.
    """
    if order.order_type != 'takeout':
        return Decimal('0.00')

    fee_per_item = get_packaging_fee_per_item()
    total_fee = Decimal('0.00')
    items = order.items.select_related('product__category').all()

    for item in items:
        if item.product is None:
            continue
        if not hasattr(item.product, 'category'):
            continue
        if item.product.category.is_packaging_required:
            total_fee += fee_per_item * item.quantity

    return total_fee


def deduct_inventory_for_order(order, performed_by=None):
    """
    Deduct stock for all items in an order.
    Idempotent: does nothing if order.stock_deducted is already True.
    Raises ValueError if any product has insufficient stock (pre-flight check).
    """
    if order.stock_deducted:
        return

    from apps.inventory.models import InventoryLog

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

    # All checks passed — apply changes atomically
    with transaction.atomic():
        for item in items:
            if item.product is None:
                continue

            old_qty = item.product.stock_quantity
            item.product.reduce_stock(item.quantity)
            new_qty = item.product.stock_quantity

            InventoryLog.objects.create(
                product=item.product,
                action='sale',
                quantity_change=-item.quantity,
                quantity_before=old_qty,
                quantity_after=new_qty,
                notes=f"Order #{order.order_number}",
                performed_by=performed_by,
            )

        order.stock_deducted = True
        order.save(update_fields=['stock_deducted'])


def restore_inventory_for_order(order, performed_by=None):
    """
    Restore stock for all items in a cancelled order.
    Idempotent: does nothing if order.stock_deducted is already False.
    """
    if not order.stock_deducted:
        return

    from apps.inventory.models import InventoryLog

    items = order.items.select_related('product').all()

    with transaction.atomic():
        for item in items:
            if item.product is None:
                continue

            old_qty = item.product.stock_quantity
            item.product.restore_stock(item.quantity)
            new_qty = item.product.stock_quantity

            InventoryLog.objects.create(
                product=item.product,
                action='adjustment',
                quantity_change=+item.quantity,
                quantity_before=old_qty,
                quantity_after=new_qty,
                notes=f"Cancelled Order #{order.order_number}",
                performed_by=performed_by,
            )

        order.stock_deducted = False
        order.save(update_fields=['stock_deducted'])
