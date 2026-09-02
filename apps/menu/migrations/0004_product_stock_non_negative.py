"""Add a database-level guarantee that product stock never goes negative."""
import django
from django.db import migrations, models

# CheckConstraint used 'check=' before Django 5.1 and 'condition=' from 5.1+.
_DJANGO_VERSION = django.VERSION[:2]
_check_constraint_kwargs = (
    {'condition': models.Q(('stock_quantity__gte', 0))}
    if _DJANGO_VERSION >= (5, 1)
    else {'check': models.Q(('stock_quantity__gte', 0))}
)


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0003_product_deactivated_at_product_is_active'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='product',
            constraint=models.CheckConstraint(
                **_check_constraint_kwargs,
                name='product_stock_non_negative',
            ),
        ),
    ]
