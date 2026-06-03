from django.contrib import admin
from .models import InventoryLog

@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = ['product', 'action', 'quantity_change', 'quantity_after', 'performed_by', 'created_at']
    list_filter = ['action']
    readonly_fields = ['created_at']
