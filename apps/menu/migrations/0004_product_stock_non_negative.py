"""Add a database-level guarantee that product stock never goes negative."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0003_product_deactivated_at_product_is_active'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='product',
            constraint=models.CheckConstraint(
                check=models.Q(('stock_quantity__gte', 0)),
                name='product_stock_non_negative',
            ),
        ),
    ]
