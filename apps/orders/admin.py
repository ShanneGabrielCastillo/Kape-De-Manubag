from django.contrib import admin
from django.db import transaction
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from .models import Order, OrderItem, Cart, CartItem
from apps.orders.services import validate_status_transition

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
    def get_urls(self):
        """Add the admin intermediate cancellation page URL."""
        from apps.orders import admin_views
        urls = super().get_urls()
        custom = [
            path(
                '<int:pk>/cancel/',
                self.admin_site.admin_view(admin_views.admin_cancel_order),
                name='orders_order_cancel',
            ),
        ]
        return custom + urls

    def cancel_order_link(self, obj):
        """Render a 'Cancel Order' action link for non-terminal active orders."""
        if obj and obj.status not in _TERMINAL_STATUSES:
            url = reverse('admin:orders_order_cancel', args=[obj.pk])
            return format_html(
                '<a class="button" href="{}" style="'
                'background:#dc3545;color:#fff;padding:4px 10px;'
                'border-radius:4px;text-decoration:none;font-size:0.85rem">'
                'Cancel Order</a>',
                url,
            )
        return '—'

    cancel_order_link.short_description = 'Cancel'

    list_display = ['order_number', 'customer_name', 'status', 'total', 'is_paid', 'created_at', 'cancel_order_link']
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
        """Block direct status changes to 'cancelled' through the ordinary
        Django Admin change form.  Cancellation must go through the dedicated
        intermediate cancellation page, which collects the required disposition
        data and calls the shared ``on_status_changed()`` service.

        Without this override, an admin could bypass the disposition workflow
        entirely by changing the status dropdown on the change form.

        For all other valid status transitions (e.g. preparing → ready), the
        override validates the transition, calls the inventory coordinator
        (Phase 5B-3: no-op for non-cancel; Phase 6B will add consumption), and
        saves normally.

        Phase 5B-3: wrapped in transaction.atomic() with a locked read of the
        Order row so the coordinator runs under the same lock hierarchy as the
        view-based paths (Rev 7 §10.3).
        """
        if not change:
            super().save_model(request, obj, form, change)
            return

        new_status = obj.status
        error_message = None

        try:
            with transaction.atomic():
                # Lock the existing Order row so we read the authoritative
                # status and no concurrent transition can race us.
                try:
                    locked = Order.objects.select_for_update().get(pk=obj.pk)
                except Order.DoesNotExist:
                    super().save_model(request, obj, form, change)
                    return

                original_status = locked.status

                # No status change — just save the non-status edits normally.
                if original_status == new_status:
                    super().save_model(request, obj, form, change)
                    return

                # ── Block direct cancellation ─────────────────────────────
                if new_status == 'cancelled' and original_status != 'cancelled':
                    # Revert the in-memory change so super().save_model() below
                    # writes back the original status, not 'cancelled'.
                    obj.status = original_status
                    error_message = (
                        'Direct cancellation is not permitted from the Admin change '
                        'form.  Use the "Cancel Order" button to open the cancellation '
                        'workflow, which collects the required disposition data.'
                    )
                    super().save_model(request, obj, form, change)
                    return

                # ── Validate non-cancel transition ────────────────────────
                try:
                    validate_status_transition(original_status, new_status, order=locked)
                except ValueError as e:
                    obj.status = original_status
                    error_message = (
                        f'Status change blocked: {e} The status was not changed.'
                    )
                    super().save_model(request, obj, form, change)
                    return

                # ── Call coordinator before saving ────────────────────────
                # Phase 5B-3: no-op for non-cancel.  Phase 6B will add
                # consumption on preparing → ready.
                from apps.orders.services import on_status_changed
                on_status_changed(obj.pk, new_status, actor=request.user)

                super().save_model(request, obj, form, change)

        except Exception as e:
            error_message = (
                f'Status change failed due to an inventory error: {e} '
                'The status was not changed.'
            )

        if error_message:
            self.message_user(request, error_message, level='error')


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ['session_key', 'item_count', 'total', 'created_at']
