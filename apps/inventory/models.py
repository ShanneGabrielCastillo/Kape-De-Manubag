from django.db import models
from django.conf import settings
from apps.menu.models import Product


class InventoryLog(models.Model):
    ACTION_CHOICES = [
        ('restock', 'Restock'),
        ('adjustment', 'Adjustment'),
        ('sale', 'Sale Deduction'),
        ('waste', 'Waste/Spoilage'),
    ]

    SOURCE_CHOICES = [
        ('customer_order', 'Customer Order'),
        ('pos_order', 'POS Order'),
        ('manual_adjustment', 'Manual Adjustment'),
        ('inventory_update', 'Inventory Update'),
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        related_name='inventory_logs',
        help_text=(
            'Product this movement relates to. SET_NULL on product deactivation '
            'so the log row survives even if the product FK is cleared.'
        ),
    )
    # Snapshot of the product name at the time the log row was written.
    # Preserved permanently so the audit trail remains readable even after a
    # product is deactivated, renamed, or its FK is nulled out.
    product_name = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Snapshot of the product's name at the time of this log entry.",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    # Where the movement originated. Every production write site passes an
    # explicit source; the default only backfills legacy rows.
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='manual_adjustment',
        help_text='Where the inventory movement originated.',
    )
    # Short structured reason (e.g. "Order #KDM-...", "Restock",
    # "Product stock update"). Free-form detail lives in ``notes``.
    reason = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Short reason for the movement (e.g. order number).',
    )
    quantity_change = models.IntegerField()  # Positive = added, negative = reduced
    quantity_before = models.PositiveIntegerField()
    quantity_after = models.PositiveIntegerField()
    notes = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @classmethod
    def record(cls, *, product, action, source, reason, quantity_change,
               quantity_before, quantity_after, performed_by=None, notes=''):
        """Create one audit-trail row for a stock movement.

        Every production write site -- order deductions/restores, manual
        restock, product create/edit stock updates -- goes through this
        single entry point so the trail is recorded consistently.

        ``product_name`` is snapshotted at write time so the log row remains
        readable even if the product is later renamed, deactivated, or its FK
        is nulled out (SET_NULL) due to a future schema relaxation.
        """
        return cls.objects.create(
            product=product,
            product_name=product.name if product is not None else '',
            action=action,
            source=source,
            reason=reason,
            quantity_change=quantity_change,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            notes=notes,
            performed_by=performed_by,
        )

    def __str__(self):
        name = (self.product.name if self.product_id and self.product else None) or self.product_name or '(deleted product)'
        return (
            f"{name} {self.action}: {self.quantity_change:+d} "
            f"({self.get_source_display()})"
        )

    class Meta:
        ordering = ['-created_at']
