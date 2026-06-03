from django.db import migrations


def migrate_queued_to_preparing(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    Order.objects.filter(status='queued').update(status='preparing')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0004_order_queue_number_order_queued_at_order_ready_at_and_more'),
    ]

    operations = [
        migrations.RunPython(migrate_queued_to_preparing, noop),
    ]
