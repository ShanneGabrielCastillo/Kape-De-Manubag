"""
Remove 'pending' from Order.status choices and set the default to
'awaiting_payment'.

DATA SAFETY
-----------
This migration does NOT touch existing database rows.  Historical orders
that still carry status='pending' remain readable — the DB column is a plain
VARCHAR and Django only uses choices for form validation and display labels.
A historical pending order will display its raw value ('pending') in the admin
and any template that falls back to ``order.status`` directly; all templates
that call ``order.get_status_display()`` will return 'pending' (the raw value)
because it is no longer in the choices list, which is acceptable for a
read-only historical record.

If you want to backfill historical pending rows to a new status run:
    Order.objects.filter(status='pending').update(status='preparing')
This is intentionally NOT done automatically here because pending rows are
historical completed/cancelled orders that should not be changed.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0019_restore_awaiting_payment_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('awaiting_payment', 'Awaiting Payment'),
                    ('preparing',        'Preparing'),
                    ('ready',            'Ready'),
                    ('completed',        'Completed'),
                    ('cancelled',        'Cancelled'),
                ],
                default='awaiting_payment',
                max_length=20,
            ),
        ),
    ]
