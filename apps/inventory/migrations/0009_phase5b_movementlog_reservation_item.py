# Generated for Phase 5B Stage 5B-1
# Rev 3 §D.8 — Add reservation_item nullable FK to IngredientMovementLog.
#
# Dependency: must come after orders/0025 (which creates
# OrderIngredientReservationItem) and after inventory/0008 (the previous
# inventory migration).
#
# Circular-dependency analysis:
#   orders/0025 depends on inventory/0008  (uses InventoryItem, Recipe,
#                                            PackagingProfile from inventory)
#   inventory/0009 depends on orders/0025  (uses OrderIngredientReservationItem
#                                            from orders)
#
# This is a ONE-WAY chain:
#   inventory/0008 → orders/0025 → inventory/0009
#
# There is NO cycle: inventory/0009 does not go back to orders.
# Django handles cross-app FK dependencies like this correctly.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0008_phase4b2_packaging_models'),
        ('orders', '0025_phase5b_ingredient_reservation'),
    ]

    operations = [
        migrations.AddField(
            model_name='ingredientmovementlog',
            name='reservation_item',
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    'The reservation line item that caused this movement, if any. '
                    'Set for reservation and reservation_release movements; null for all others.'
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='movements',
                to='orders.orderingredientreservationitem',
            ),
        ),
    ]
