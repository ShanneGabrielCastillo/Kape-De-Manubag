from django.db import migrations


def convert_card_to_cash(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    card_orders = Order.objects.filter(payment_method='card')
    count = card_orders.count()
    if count > 0:
        for order in card_orders:
            existing_notes = order.notes or ''
            if existing_notes:
                order.notes = (
                    existing_notes +
                    '\n[Payment method updated: card → cash during system update]'
                )
            else:
                order.notes = (
                    '[Payment method updated: card → cash during system update]'
                )
            order.payment_method = 'cash'
            order.save()


def reverse_convert(apps, schema_editor):
    pass  # Cannot safely reverse — leave as no-op


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0005_migrate_queued_to_preparing'),
    ]

    operations = [
        migrations.RunPython(convert_card_to_cash, reverse_convert),
    ]
