# Generated manually — removes 'awaiting_payment' from Order.status choices.
#
# What this migration does
# ────────────────────────
# 1. DATA MIGRATION (RunPython, runs first):
#    Convert any existing orders with status='awaiting_payment' to status='pending'.
#    The is_paid flag is preserved exactly as-is, so:
#      awaiting_payment + is_paid=False  →  pending + is_paid=False
#      awaiting_payment + is_paid=True   →  pending + is_paid=True
#    This is safe because PENDING is now the single pre-preparation status
#    and is_paid is the independent payment flag.
#
# 2. SCHEMA MIGRATION (AlterField):
#    Removes 'awaiting_payment' from the STATUS_CHOICES list.
#    Django stores status as a VARCHAR — no actual DB column type changes,
#    just the choices constraint in application code.  Existing rows with
#    other statuses are untouched.
#
# Safety notes
# ────────────
# - No orders are deleted.
# - No financial, inventory, or audit data is altered.
# - The data migration runs inside Django's transaction so it rolls back
#   cleanly if anything fails.
# - If there are zero awaiting_payment rows (fresh install or already
#   cleaned up), the RunPython step is a no-op.

from django.db import migrations, models


def convert_awaiting_payment_to_pending(apps, schema_editor):
    """Move all awaiting_payment orders to pending, preserving is_paid."""
    Order = apps.get_model('orders', 'Order')
    updated = Order.objects.filter(status='awaiting_payment').update(status='pending')
    if updated:
        print(f'\n  Converted {updated} awaiting_payment order(s) → pending.')


def reverse_convert(apps, schema_editor):
    """Reverse is intentionally a no-op.

    We cannot safely restore the original awaiting_payment status because
    we no longer know which pending+unpaid orders were originally in
    awaiting_payment vs genuinely in pending.  Rolling back this migration
    while data exists is inherently lossy — warn and leave rows as pending.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0018_order_awaiting_payment_status'),
    ]

    operations = [
        # Step 1: convert existing rows before the schema change
        migrations.RunPython(
            convert_awaiting_payment_to_pending,
            reverse_code=reverse_convert,
        ),
        # Step 2: update the field choices (no DB column change — VARCHAR stays)
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending',   'Pending'),
                    ('preparing', 'Preparing'),
                    ('ready',     'Ready'),
                    ('completed', 'Completed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
