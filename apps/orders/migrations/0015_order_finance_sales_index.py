"""
Add a composite index on Order(is_paid, status, payment_method, created_at)
to accelerate the Finance module's cash-sales query pattern.

Background
----------
Every Finance page load, the API endpoint, and the print view execute the
same query:

    Order.objects.filter(
        created_at__date=<date>,
        is_paid=True,
        payment_method='cash',
        status='completed',
    ).aggregate(total=Sum('total'), count=Count('id'))

The existing idx_order_paid_status_created index covers (is_paid, status,
-created_at), which allows the DB to pre-filter to completed, paid orders
before scanning by date.  Adding payment_method as the third column narrows
the candidate set by a further ~50% (only cash orders, not gcash) before
the date scan, reducing the work required for the most frequent Finance query.

The existing indexes are unchanged — this index is additive and serves a
distinct query pattern (Finance) that includes payment_method in its filter
while the dashboard/reports queries do not.

Index: (is_paid, status, payment_method, created_at)
Name:  idx_order_finance_sales
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0014_order_performance_indexes'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='order',
            index=models.Index(
                fields=['is_paid', 'status', 'payment_method', 'created_at'],
                name='idx_order_finance_sales',
            ),
        ),
    ]
