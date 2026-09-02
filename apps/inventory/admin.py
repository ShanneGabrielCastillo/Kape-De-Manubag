from django.contrib import admin
from .models import InventoryLog

@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = ['product', 'action', 'source', 'reason', 'quantity_change',
                    'quantity_after', 'performed_by', 'created_at']
    list_filter = ['action', 'source']
    search_fields = ['product__name', 'reason', 'notes', 'action', 'source']
    readonly_fields = ['created_at']
