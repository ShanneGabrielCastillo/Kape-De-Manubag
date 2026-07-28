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
    if (not created and
            instance.stock_quantity <= instance.low_stock_threshold):
        publish('inventory_low', {
            'product_id':     instance.pk,
            'product_name':   instance.name,
            'stock_quantity': instance.stock_quantity,
        })
