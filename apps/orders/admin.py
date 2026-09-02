from django.contrib import admin
from django.db import transaction
from django.utils import timezone
from .models import Order, OrderItem, Cart, CartItem
from apps.orders.services import restore_inventory_for_order, validate_status_transition

# Terminal statuses — orders in these states are permanent financial records
# and must never be modified or deleted through the admin interface.
_TERMINAL_STATUSES = frozenset({'completed', 'cancelled'})

# All Order fields that are financial snapshots or audit timestamps.
# Rendered read-only for every order; double-locked for terminal orders.
_ORDER_READONLY_BASE = [
    'order_number', 'created_at', 'cancelled_at',
]
_ORDER_READONLY_TERMINAL = [
    'order_number', 'customer_name', 'customer_phone', 'table_number',
    'order_type', 'status', 'notes',
    'subtotal', 'discount', 'packaging_fee', 'total',
    'is_paid', 'payment_method', 'amount_paid', 'change_amount',
    'stock_deducted', 'request_token', 'cashier',
    'created_at', 'updated_at', 'completed_at', 'cancelled_at',
    'queue_number', 'queued_at', 'ready_at',
]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    # Deletion of individual line items is always blocked: removing a line
    # item from a placed order makes the stored subtotals inconsistent and
    # corrupts finance/inventory history.
    can_delete = False
    # Base set of read-only fields for every order item (snapshot fields that
    # were captured at order time and must never be retroactively altered).
    readonly_fields = [
        'product', 'product_name', 'category_name', 'packaging_eligible',
        'size', 'quantity', 'unit_price', 'subtotal', 'notes',
    ]

    def has_add_permission(self, request, obj=None):
        # Adding items to a placed order would make stored totals wrong.
        # New items can only be added through the POS / checkout flow, which
        # recalculates totals atomically.
        if obj is not None:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        # Line items on terminal orders are immutable records.
        if obj is not None and obj.status in _TERMINAL_STATUSES:
            return False
        return super().has_change_permission(request, obj)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'customer_name', 'status', 'total', 'is_paid', 'created_at']
    list_filter = ['status', 'is_paid', 'order_type']
    search_fields = ['order_number', 'customer_name']
    inlines = [OrderItemInline]
    readonly_fields = _ORDER_READONLY_BASE

    # ── Deletion protection ───────────────────────────────────────────────────
    # The pre_delete signal on Order already raises ValidationError for every
    # hard-delete path (including bulk queryset.delete()).  These overrides
    # provide an earlier, friendlier rejection with a clear admin message
    # before any DB round-trip is attempted.

    def has_delete_permission(self, request, obj=None):
        """Orders cannot be deleted — cancel them instead."""
        return False

    def delete_model(self, request, obj):
        """Safety net: called by the change-page Delete button."""
        self.message_user(
            request,
            'Orders cannot be deleted. Cancel the order instead — '
            'cancelled orders remain fully traceable in finance, inventory, '
            'and order history.',
            level='error',
        )

    def delete_queryset(self, request, queryset):
        """Safety net: called by the list-view "Delete selected" action."""
        self.message_user(
            request,
            f'Cannot delete {queryset.count()} order(s). '
            'Orders are permanent financial records. '
            'Cancel individual orders instead.',
            level='error',
        )

    # ── Field-level write protection for terminal orders ──────────────────────

    def get_readonly_fields(self, request, obj=None):
        """Lock all fields once an order reaches a terminal state.

        Completed and cancelled orders are permanent records — every field
        on them (customer info, payment amounts, status, timestamps) is part
        of the financial audit trail and must not be editable after the fact.
        Active orders still allow status transitions via the normal workflow.
        """
        if obj is not None and obj.status in _TERMINAL_STATUSES:
            return _ORDER_READONLY_TERMINAL
        return _ORDER_READONLY_BASE

    def save_model(self, request, obj, form, change):
        """Run the full cancellation business logic (inventory restore,
        cancelled_at timestamp) when an admin changes status to 'cancelled',
        and block all other status transitions that are invalid per
        VALID_TRANSITIONS.

        Without this override, a superuser editing an Order row in the admin
        could bypass validate_status_transition, skip restore_inventory_for_order,
        and leave stock_deducted=True on a cancelled order — dirty inventory
        forever.
        """
        if not change:
            # New orders are always created via the normal views; admin
            # creation is unusual but falls through to super().save_model().
            super().save_model(request, obj, form, change)
            return

        # Re-fetch the stored status before this edit so we can compare.
        try:
            original = Order.objects.get(pk=obj.pk)
            original_status = original.status
        except Order.DoesNotExist:
            super().save_model(request, obj, form, change)
            return

        new_status = obj.status

        # Validate the transition using the same rules as the view layer.
        # If invalid, revert the status on the in-memory object and save the
        # rest of the fields unchanged — the admin will see the record saved
        # without the bad status change.
        try:
            validate_status_transition(original_status, new_status)
        except ValueError as e:
            self.message_user(
                request,
                f"Status change blocked: {e} The status was not changed.",
                level='error',
            )
            obj.status = original_status
            super().save_model(request, obj, form, change)
            return

        if new_status == 'cancelled' and original_status != 'cancelled':
            with transaction.atomic():
                # Lock the row to prevent a concurrent view-layer cancel
                # racing this admin save.
                locked = Order.objects.select_for_update().get(pk=obj.pk)
                # Re-validate against the freshly locked status.
                try:
                    validate_status_transition(locked.status, new_status)
                except ValueError as e:
                    self.message_user(request, f"Status change blocked: {e}", level='error')
                    obj.status = locked.status
                    super().save_model(request, obj, form, change)
                    return

                # Restore inventory — idempotent, safe to call even if
                # stock_deducted is already False.
                restore_inventory_for_order(locked, performed_by=request.user)

                # Sync the in-memory obj with what the lock read so the
                # subsequent save writes a consistent row.
                obj.stock_deducted = locked.stock_deducted
                if not obj.cancelled_at:
                    obj.cancelled_at = timezone.now()
                super().save_model(request, obj, form, change)
            return

        super().save_model(request, obj, form, change)


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ['session_key', 'item_count', 'total', 'created_at']
