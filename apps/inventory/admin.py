from django.contrib import admin
from .models import InventoryLog, InventoryItem, IngredientMovementLog


@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = ['product', 'action', 'source', 'reason', 'quantity_change',
                    'quantity_after', 'performed_by', 'created_at']
    list_filter = ['action', 'source']
    search_fields = ['product__name', 'reason', 'notes', 'action', 'source']
    readonly_fields = ['created_at']


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'item_type', 'base_unit', 'on_hand_quantity',
                    'reserved_quantity', 'available_quantity', 'low_stock_threshold',
                    'is_active', 'created_at']
    list_filter = ['item_type', 'base_unit', 'is_active']
    search_fields = ['name', 'description']
    readonly_fields = ['created_at', 'updated_at', 'available_quantity']

    def available_quantity(self, obj):
        return obj.available_quantity
    available_quantity.short_description = 'Available'


@admin.register(IngredientMovementLog)
class IngredientMovementLogAdmin(admin.ModelAdmin):
    list_display = ['item_name', 'movement_type', 'on_hand_delta', 'reserved_delta',
                    'on_hand_after', 'reserved_after', 'order_number',
                    'performed_by', 'created_at']
    list_filter = ['movement_type']
    search_fields = ['item_name', 'reason_code', 'note', 'order_number']
    readonly_fields = ['created_at']
