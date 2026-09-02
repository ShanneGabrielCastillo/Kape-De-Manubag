"""
Add a partial UniqueConstraint on OrderItem(order, product, size) WHERE
product IS NOT NULL.

This is a last-resort database-level guard that prevents the same product/size
from appearing as two separate line items on a single order — which would cause
a double stock deduction for that product. The view layer consolidates such
duplicates before inserting, so in normal operation this constraint is never
triggered. It blocks bugs or direct API abuse that bypass the view.

The condition ``product__isnull=False`` (rendered as ``WHERE product_id IS NOT
NULL``) excludes historical rows where the FK was nulled out by SET_NULL after
a product was deactivated; those rows must remain visible in order history even
though they no longer point to a live product.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0008_order_request_token'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='orderitem',
            constraint=models.UniqueConstraint(
                fields=['order', 'product', 'size'],
                condition=models.Q(product__isnull=False),
                name='unique_orderitem_order_product_size',
            ),
        ),
    ]
