# Generated for Phase 5B Stage 5B-1 — Reservation Models
# Rev 3 §D.9 (OrderIngredientReservation) and §D.10 (OrderIngredientReservationItem)
#
# Dependency notes:
#   - depends on orders/0024 (latest orders migration)
#   - depends on inventory/0008 (provides InventoryItem, Recipe, PackagingProfile FKs)
#
# inventory/0009 then depends on THIS migration to add the reservation_item FK
# back to IngredientMovementLog.  Dependency chain:
#   inventory/0008 → orders/0025 → inventory/0009   (no cycle)

from decimal import Decimal

import django.db.models.deletion
import django.db.models.expressions
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0008_phase4b2_packaging_models'),
        ('orders', '0024_phase6a_cancellation_disposition'),
    ]

    operations = [
        # ── OrderIngredientReservation (Rev 3 §D.9) ──────────────────────────
        migrations.CreateModel(
            name='OrderIngredientReservation',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID',
                )),
                ('status', models.CharField(
                    choices=[
                        ('reserved', 'Reserved'),
                        ('consumed', 'Consumed'),
                        ('released', 'Released'),
                        ('settled',  'Settled'),
                    ],
                    default='reserved',
                    max_length=10,
                    help_text=(
                        'Lifecycle state of this reservation. '
                        '"reserved" = fully outstanding; "consumed" = all consumed; '
                        '"released" = all released; "settled" = mix of consumed and released.'
                    ),
                )),
                ('order_type_snapshot', models.CharField(
                    max_length=20,
                    help_text='Snapshot of Order.order_type at reservation creation time.',
                )),
                ('created_at', models.DateTimeField(
                    auto_now_add=True,
                    help_text='When this reservation was created.',
                )),
                ('settled_at', models.DateTimeField(
                    blank=True,
                    null=True,
                    help_text=(
                        'When this reservation was fully settled. '
                        'Null while status is "reserved".'
                    ),
                )),
                ('order', models.OneToOneField(
                    help_text='The order this reservation belongs to.',
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='ingredient_reservation',
                    to='orders.order',
                )),
            ],
        ),
        # res_status_valid
        migrations.AddConstraint(
            model_name='orderingredientreservation',
            constraint=models.CheckConstraint(
                check=models.Q(status__in=['reserved', 'consumed', 'released', 'settled']),
                name='res_status_valid',
            ),
        ),
        # res_settled_at_consistent: status='reserved' <=> settled_at IS NULL
        migrations.AddConstraint(
            model_name='orderingredientreservation',
            constraint=models.CheckConstraint(
                check=(
                    models.Q(status='reserved', settled_at__isnull=True)
                    | (models.Q(settled_at__isnull=False) & ~models.Q(status='reserved'))
                ),
                name='res_settled_at_consistent',
            ),
        ),

        # ── OrderIngredientReservationItem (Rev 3 §D.10) ─────────────────────
        migrations.CreateModel(
            name='OrderIngredientReservationItem',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID',
                )),
                ('item_name', models.CharField(
                    max_length=120,
                    help_text="Snapshot of inventory_item.name at reservation time.",
                )),
                ('base_unit', models.CharField(
                    max_length=3,
                    help_text="Snapshot of inventory_item.base_unit at reservation time.",
                )),
                ('component_source', models.CharField(
                    choices=[
                        ('recipe',    'Recipe ingredient'),
                        ('packaging', 'Packaging material'),
                    ],
                    max_length=10,
                    help_text='Whether this component came from the recipe or the packaging profile.',
                )),
                ('source_revision', models.PositiveIntegerField(
                    help_text='Snapshot of the recipe or packaging profile revision at reservation time.',
                )),
                ('quantity_per_unit', models.DecimalField(
                    decimal_places=3,
                    max_digits=14,
                    help_text='Snapshot: per-unit consumption in base units from the resolved plan.',
                )),
                ('order_item_quantity', models.PositiveIntegerField(
                    help_text='Snapshot of order_item.quantity at reservation time.',
                )),
                ('reserved_quantity', models.DecimalField(
                    decimal_places=3,
                    max_digits=14,
                    help_text='Total reserved = quantity_per_unit × order_item_quantity.',
                )),
                ('consumed_quantity', models.DecimalField(
                    decimal_places=3,
                    default=Decimal('0'),
                    max_digits=14,
                    help_text='Units consumed so far.',
                )),
                ('released_quantity', models.DecimalField(
                    decimal_places=3,
                    default=Decimal('0'),
                    max_digits=14,
                    help_text='Units released so far.',
                )),
                ('consume_reason', models.CharField(
                    blank=True,
                    choices=[
                        ('order_ready',        'Order marked ready'),
                        ('cancelled_prepared', 'Cancelled — prepared units'),
                    ],
                    default='',
                    max_length=25,
                    help_text='Why units were consumed. Must be set when consumed_quantity > 0.',
                )),
                ('release_reason', models.CharField(
                    blank=True,
                    choices=[
                        ('cancelled_not_prepared',   'Cancelled — unprepared units'),
                        ('cancelled_before_payment', 'Cancelled before payment'),
                    ],
                    default='',
                    max_length=30,
                    help_text='Why units were released. Must be set when released_quantity > 0.',
                )),
                ('consumed_at', models.DateTimeField(
                    blank=True,
                    null=True,
                    help_text='When consumption was recorded for this line.',
                )),
                ('released_at', models.DateTimeField(
                    blank=True,
                    null=True,
                    help_text='When release was recorded for this line.',
                )),
                # ── Foreign keys ──────────────────────────────────────────────
                ('reservation', models.ForeignKey(
                    help_text='The parent reservation.',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='items',
                    to='orders.orderingredientreservation',
                )),
                ('order_item', models.ForeignKey(
                    help_text='The order line that caused this reservation.',
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='ingredient_reservation_items',
                    to='orders.orderitem',
                )),
                ('inventory_item', models.ForeignKey(
                    help_text='The ingredient or packaging item that was reserved.',
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='reservation_items',
                    to='inventory.inventoryitem',
                )),
                ('source_recipe', models.ForeignKey(
                    blank=True,
                    help_text='The recipe this line was derived from. Null for packaging components.',
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='reservation_items',
                    to='inventory.recipe',
                )),
                ('source_packaging_profile', models.ForeignKey(
                    blank=True,
                    help_text='The packaging profile this line was derived from. Null for recipe components.',
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='reservation_items',
                    to='inventory.packagingprofile',
                )),
            ],
        ),
        # rli_unique_line_item
        migrations.AddConstraint(
            model_name='orderingredientreservationitem',
            constraint=models.UniqueConstraint(
                fields=['reservation', 'order_item', 'inventory_item'],
                name='rli_unique_line_item',
            ),
        ),
        # rli_reserved_positive
        migrations.AddConstraint(
            model_name='orderingredientreservationitem',
            constraint=models.CheckConstraint(
                check=models.Q(reserved_quantity__gt=0),
                name='rli_reserved_positive',
            ),
        ),
        # rli_consumed_non_negative
        migrations.AddConstraint(
            model_name='orderingredientreservationitem',
            constraint=models.CheckConstraint(
                check=models.Q(consumed_quantity__gte=0),
                name='rli_consumed_non_negative',
            ),
        ),
        # rli_released_non_negative
        migrations.AddConstraint(
            model_name='orderingredientreservationitem',
            constraint=models.CheckConstraint(
                check=models.Q(released_quantity__gte=0),
                name='rli_released_non_negative',
            ),
        ),
        # rli_settled_within_reserved: consumed + released <= reserved
        migrations.AddConstraint(
            model_name='orderingredientreservationitem',
            constraint=models.CheckConstraint(
                check=models.Q(
                    consumed_quantity__lte=(
                        models.F('reserved_quantity') - models.F('released_quantity')
                    ),
                ),
                name='rli_settled_within_reserved',
            ),
        ),
        # rli_reserved_matches_basis: reserved = quantity_per_unit * order_item_quantity
        migrations.AddConstraint(
            model_name='orderingredientreservationitem',
            constraint=models.CheckConstraint(
                check=models.Q(
                    reserved_quantity=django.db.models.expressions.CombinedExpression(
                        models.F('quantity_per_unit'),
                        '*',
                        models.F('order_item_quantity'),
                    ),
                ),
                name='rli_reserved_matches_basis',
            ),
        ),
        # rli_consume_reason_present: consumed > 0 => consume_reason != ''
        migrations.AddConstraint(
            model_name='orderingredientreservationitem',
            constraint=models.CheckConstraint(
                check=(
                    models.Q(consumed_quantity=0)
                    | ~models.Q(consume_reason='')
                ),
                name='rli_consume_reason_present',
            ),
        ),
        # rli_release_reason_present: released > 0 => release_reason != ''
        migrations.AddConstraint(
            model_name='orderingredientreservationitem',
            constraint=models.CheckConstraint(
                check=(
                    models.Q(released_quantity=0)
                    | ~models.Q(release_reason='')
                ),
                name='rli_release_reason_present',
            ),
        ),
        # Partial index: open reservation lines (consumed + released < reserved)
        # Rev 3 §D.10 — fast "open reservations for this inventory item" lookup.
        migrations.RunSQL(
            sql=(
                "CREATE INDEX idx_rli_open_lines "
                "ON orders_orderingredientreservationitem (inventory_item_id) "
                "WHERE consumed_quantity + released_quantity < reserved_quantity"
            ),
            reverse_sql="DROP INDEX IF EXISTS idx_rli_open_lines",
        ),
    ]
