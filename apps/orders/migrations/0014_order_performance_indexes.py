"""
Add composite indexes to the Order table for the query patterns that appear
on every page load and every real-time event in the Orders module.

Indexes added
-------------

1. ``idx_order_status_created``  (status, created_at DESC)
   Covers:
   - order_list filter by status + ORDER BY -created_at
   - queue_board / api_queue_board: filter(status=x, created_at__date=today)
   - get_queue_position(): filter(status__in=[...], created_at__lt=self.created_at)
   - dashboard _status_counts(): aggregate(COUNT … filter status='pending' / 'preparing')

2. ``idx_order_paid_status_created``  (is_paid, status, created_at DESC)
   Covers:
   - finance _get_cash_sales_for_date(): filter(is_paid=True, status='completed', created_at__date=…)
   - dashboard _sales_stats(): filter(is_paid=True, status='completed').aggregate(…)
   - dashboard _chart_series(): filter(is_paid=True, status='completed', created_at__date__gte=…)
   - reports reports_index() / export_excel(): filter(is_paid=True, created_at__date__gte=…)

Note: order_number and request_token already have implicit indexes from their
unique=True constraints. OrderItem.order_id has an implicit FK index. No
redundant single-column indexes are added here.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0013_daily_order_sequence'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='order',
            index=models.Index(
                fields=['status', '-created_at'],
                name='idx_order_status_created',
            ),
        ),
        migrations.AddIndex(
            model_name='order',
            index=models.Index(
                fields=['is_paid', 'status', '-created_at'],
                name='idx_order_paid_status_created',
            ),
        ),
    ]
