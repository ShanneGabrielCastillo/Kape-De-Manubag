"""Add source and reason to InventoryLog for the inventory audit trail."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventorylog',
            name='source',
            field=models.CharField(
                choices=[
                    ('customer_order', 'Customer Order'),
                    ('pos_order', 'POS Order'),
                    ('manual_adjustment', 'Manual Adjustment'),
                    ('inventory_update', 'Inventory Update'),
                ],
                default='manual_adjustment',
                help_text='Where the inventory movement originated.',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='inventorylog',
            name='reason',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Short reason for the movement (e.g. order number).',
                max_length=200,
            ),
        ),
    ]
