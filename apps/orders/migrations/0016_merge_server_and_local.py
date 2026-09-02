"""
Merge migration: reconciles the server-side branch (0007_merge_20260730_1109)
with the local branch that ends at 0015_order_finance_sales_index.

The server had a merge migration at 0007 that was never in the local repo.
This migration makes both branches converge so 'manage.py migrate' can run
without a ConflictingMigrations error.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0007_merge_20260730_1109'),
        ('orders', '0015_order_finance_sales_index'),
    ]

    operations = [
    ]
