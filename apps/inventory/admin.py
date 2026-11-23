from django.contrib import admin
from .models import InventoryLog, InventoryItem, IngredientMovementLog


# ══════════════════════════════════════════════════════════════════════════════
# Inventory ledgers — append-only in the admin
# ══════════════════════════════════════════════════════════════════════════════
#
# Both ledgers are written exclusively by the inventory services
# (record_adjustment / record_wastage / record_purchase and the order
# reservation/consumption services) in the same transaction that changes the
# balance.  Every field is a snapshot of the state at write time, so letting
# the admin add, edit or delete a row would fabricate or erase stock history.
#
# These overrides follow the same convention as AuditLogAdmin: the admin is a
# read-only review surface.  Django renders the change page view-only for a
# user who can view but not change an object, and rejects a crafted POST (403).
#
# Limits: this protects the admin interface only.  It is not database-level
# immutability — code writing through the ORM (shell, scripts, services) can
# still create or delete rows.

@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    """Read-only review interface for the legacy product-stock ledger."""

    list_display = ['product', 'action', 'source', 'reason', 'quantity_change',
                    'quantity_after', 'performed_by', 'created_at']
    list_filter = ['action', 'source']
    search_fields = ['product__name', 'reason', 'notes', 'action', 'source']
    readonly_fields = ['created_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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

    def get_readonly_fields(self, request, obj=None):
        """Lock the fields that represent authoritative stock.

        - ``reserved_quantity`` is owned by the order-reservation services and
          is never a staff-entered value, on add or on change.
        - ``on_hand_quantity`` may be set once, when an item is created (the
          ingredient/packaging create screens also accept a starting stock and
          record no movement).  Afterwards it changes only through
          record_adjustment / record_wastage / record_purchase, each of which
          writes an IngredientMovementLog row in the same transaction.
        - ``base_unit`` is immutable once changing it would reinterpret a
          recorded or existing balance: any movement exists, the item already
          carries stock, or any configuration or historical record references
          the item and was written in its current base unit.  Dependency
          records are counted regardless of their status (draft, retired,
          inactive, consumed, released), because they still express quantities
          in the item's base unit.  A genuinely unreferenced, zero-stock item
          with no history can still have a typo corrected.

        Descriptive metadata (name, description, item type, low-stock
        threshold, active flag) stays editable.
        """
        fields = list(super().get_readonly_fields(request, obj))
        fields.append('reserved_quantity')
        if obj is not None:
            fields.append('on_hand_quantity')
            has_dependents = (
                obj.movements.exists()
                or obj.recipe_items.exists()
                or obj.packaging_profile_items.exists()
                or obj.unit_conversions.exists()
                or obj.purchase_items.exists()
                or obj.reservation_items.exists()
            )
            if (has_dependents or obj.on_hand_quantity
                    or obj.reserved_quantity):
                fields.append('base_unit')
        return fields


@admin.register(IngredientMovementLog)
class IngredientMovementLogAdmin(admin.ModelAdmin):
    """Read-only review interface for the append-only ingredient ledger."""

    list_display = ['item_name', 'movement_type', 'on_hand_delta', 'reserved_delta',
                    'on_hand_after', 'reserved_after', 'order_number',
                    'performed_by', 'created_at']
    list_filter = ['movement_type']
    search_fields = ['item_name', 'reason_code', 'note', 'order_number']
    readonly_fields = ['created_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
