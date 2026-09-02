"""
Merge migration: reconciles the server-side branch ending at
0007_merge_20260730_1109 with the local branch ending at
0015_order_finance_sales_index.

Both 0006_alter_order_status and 0007_merge_20260730_1109 were server-only
migrations that are now included in the local repo so the migration graph
is fully consistent across all environments.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0007_merge_20260730_1109'),
        ('orders', '0015_order_finance_sales_index'),
    ]

    operations = [
    ]
