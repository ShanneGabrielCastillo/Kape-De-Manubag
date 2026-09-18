# Restores 'awaiting_payment' to Order.status choices.
#
# Migration 0019 previously removed this status by mistake.
# The django_migrations table already has a 0019 entry, so we
# fake-apply this replacement to make Django treat it as applied.
#
# IMPORTANT: run with --fake if 0019 is already in django_migrations:
#   python manage.py migrate orders 0019 --fake
# Then the app code will correctly set status='awaiting_payment' for
# new customer checkout orders.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0018_order_awaiting_payment_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('awaiting_payment', 'Awaiting Payment'),
                    ('pending',          'Pending'),
                    ('preparing',        'Preparing'),
                    ('ready',            'Ready'),
                    ('completed',        'Completed'),
                    ('cancelled',        'Cancelled'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
