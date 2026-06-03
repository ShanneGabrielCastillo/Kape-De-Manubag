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

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='inventory_logs')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    quantity_change = models.IntegerField()  # Positive = added, negative = reduced
    quantity_before = models.PositiveIntegerField()
    quantity_after = models.PositiveIntegerField()
    notes = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.product.name} {self.action}: {self.quantity_change:+d}"

    class Meta:
        ordering = ['-created_at']
