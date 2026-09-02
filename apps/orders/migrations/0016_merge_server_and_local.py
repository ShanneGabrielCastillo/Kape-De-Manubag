"""
Merge migration: this was created to reconcile the server-side branch
(0007_merge_20260730_1109) with the local branch ending at
0015_order_finance_sales_index.

The server merge is now complete. This migration retains only the local
dependency so it does not break local environments that never had the
server-only 0007 migration.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0015_order_finance_sales_index'),
    ]

    operations = [
    ]
