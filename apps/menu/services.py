"""
Menu services — product lifecycle safeguards.

These helpers answer "is this product tied to a live workflow right now?"
without changing how orders behave. An order counts as *active* while it is
still being fulfilled (pending, preparing or ready); completed and cancelled
orders are historical records and never block or delay any product action.

``OrderItem`` is imported lazily because ``apps.orders`` already imports
``apps.menu`` (a module-level import here would be circular).
"""

from django.db.models import Count, Q

# Order statuses in which the product is still part of a live workflow.
# Completed/cancelled orders are history and are always excluded.
ACTIVE_ORDER_STATUSES = ('pending', 'preparing', 'ready')


def annotate_order_reference_counts(queryset):
    """Annotate a ``Product`` queryset with two reference counters:

    * ``active_order_count`` — distinct orders that are still being
      fulfilled (pending/preparing/ready) and reference the product;
    * ``total_order_count``  — distinct orders, active or historical,
      that reference the product.

    Both count distinct *orders* (not line items), so the numbers read as
    "currently in N active orders" / "appears in M orders overall".
    """
    return queryset.annotate(
        active_order_count=Count(
            'orderitem__order',
            filter=Q(orderitem__order__status__in=ACTIVE_ORDER_STATUSES),
            distinct=True,
        ),
        total_order_count=Count('orderitem__order', distinct=True),
    )


def get_order_reference_counts(product):
    """Return ``{'active': n, 'total': m}`` for a single product instance.

    One aggregate query; never touches the order rows themselves.
    """
    from apps.menu.models import Product

    row = (
        annotate_order_reference_counts(
            Product.objects.filter(pk=product.pk),
        )
        .values('active_order_count', 'total_order_count')
        .first()
    ) or {}
    return {
        'active': row.get('active_order_count', 0),
        'total': row.get('total_order_count', 0),
    }


def category_active_order_count(category):
    """Distinct active orders referencing any product in ``category``."""
    from apps.orders.models import Order

    return Order.objects.filter(
        status__in=ACTIVE_ORDER_STATUSES,
        items__product__category=category,
    ).distinct().count()
