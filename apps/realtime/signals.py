"""
Signal receivers that fire real-time events when Order or Product
data changes. These hook into EXISTING model saves — no changes
to existing views required.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.orders.models import Order
from apps.menu.models import Product
from apps.realtime.broker import publish


@receiver(post_save, sender=Order)
def order_saved(sender, instance, created, **kwargs):
    order = instance
    if created:
        publish('new_order', {
            'order_id':     order.pk,
            'order_number': order.order_number,
            'queue_number': order.queue_number,
            'customer_name': order.customer_name,
            'order_type':   order.get_order_type_display(),
            'total':        float(order.total),
            'status':       order.status,
            'created_at':   order.created_at.isoformat(),
            # The idempotency key the ordering terminal submitted. The
            # originating POS knows its token BEFORE the response arrives, so
            # it can ignore the echo of its own order before the broadcast is
            # even delivered (an order_id-only suppression can race).
            'request_token': order.request_token,
        })
    else:
        publish('status_changed', {
            'order_id':          order.pk,
            'order_number':      order.order_number,
            'queue_number':      order.queue_number,
            'new_status':        order.status,
            'new_status_display': order.get_status_display(),
            'is_paid':           order.is_paid,
        })


@receiver(post_save, sender=Product)
def product_saved(sender, instance, created, **kwargs):
    if created:
        return
    # reduce_stock()/restore_stock() set stock_quantity to an F() expression
    # in memory before saving, so post_save sees a CombinedExpression instead
    # of an int. Re-read the committed value before comparing.
    instance.refresh_from_db(fields=[
        'stock_quantity', 'low_stock_threshold', 'is_available', 'is_active',
    ])
    # Live stock/availability snapshot for every open POS terminal: when the
    # last unit of a product is sold (or it is restocked / marked
    # unavailable), the client updates the product card and reconciles the
    # current order without a page reload.
    publish('inventory_changed', {
        'product_id':          instance.pk,
        'product_name':        instance.name,
        'stock_quantity':      instance.stock_quantity,
        'low_stock_threshold': instance.low_stock_threshold,
        'is_available':        instance.is_available,
        'is_active':           instance.is_active,
    })
    if instance.is_low_stock:
        publish('inventory_low', {
            'product_id':     instance.pk,
            'product_name':   instance.name,
            'stock_quantity': instance.stock_quantity,
        })
