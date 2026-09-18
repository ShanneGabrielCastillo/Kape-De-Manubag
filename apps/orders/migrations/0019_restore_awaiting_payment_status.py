# Restores 'awaiting_payment' and removes 'pending' from Order.status choices.
#
# The django_migrations table already has a 0019 entry so this migration
# was fake-applied via the _fix_migration.py script. Its schema operation
# reflects the final desired state of the field.

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
