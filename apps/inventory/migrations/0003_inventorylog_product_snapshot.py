"""
InventoryLog hardening for historical data protection.

Changes
-------
1.  product FK: CASCADE → SET_NULL, null=True
    The previous CASCADE behaviour would silently delete every inventory log
    row for a product if that product were ever hard-deleted.  Products are
    now guarded by a pre_delete signal (PRODUCT_SOFT_DELETE_ERROR), but the
    FK behaviour is changed to SET_NULL as a second line of defence so that
    any future relaxation of the guard can never cascade-delete audit history.

2.  product_name: new CharField snapshot field
    Stores the product's name at the time the log row was written.  Once set,
    this value is never updated — even if the product is later renamed or its
    FK nulled out, the historical record remains readable.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0002_inventorylog_source_reason'),
        ('menu', '0001_initial'),
    ]

    operations = [
        # Step 1: allow NULL on the FK column before altering the constraint.
        migrations.AlterField(
            model_name='inventorylog',
            name='product',
            field=models.ForeignKey(
                help_text=(
                    'Product this movement relates to. SET_NULL on product '
                    'deactivation so the log row survives even if the product '
                    'FK is cleared.'
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='inventory_logs',
                to='menu.product',
            ),
        ),
        # Step 2: add the product_name snapshot column.
        migrations.AddField(
            model_name='inventorylog',
            name='product_name',
            field=models.CharField(
                blank=True,
                default='',
                help_text="Snapshot of the product's name at the time of this log entry.",
                max_length=200,
            ),
        ),
    ]
