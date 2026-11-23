from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from apps.audit.services import log_action
from .models import Category, Product, CATEGORY_DELETE_ERROR
from .services import annotate_order_reference_counts


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display  = ['name', 'icon', 'category_type', 'is_packaging_required', 'is_active', 'order']
    list_editable = ['category_type', 'is_packaging_required', 'is_active', 'order']
    list_filter   = ['category_type', 'is_packaging_required', 'is_active']
    prepopulated_fields = {'slug': ('name',)}

    def delete_view(self, request, object_id, extra_context=None):
        # The admin's built-in delete view calls obj.delete() directly, where
        # the model guard raises for non-empty categories (an unhandled 500).
        # Block here with a clear message instead.
        category = get_object_or_404(Category, pk=object_id)
        # One count() instead of exists() + count(): the number is needed
        # either way, and count() alone tells us whether it is zero.
        product_count = category.products.count()
        if product_count:
            self.message_user(
                request,
                f'"{category.name}" cannot be deleted because it still has '
                f'{product_count} product(s). Move the products '
                'to another category, or deactivate/remove them first.',
                level=messages.ERROR,
            )
            return HttpResponseRedirect(reverse('admin:menu_category_changelist'))
        return super().delete_view(request, object_id, extra_context)

    def delete_queryset(self, request, queryset):
        # Use the model's delete() so categories that still contain products
        # raise the guard instead of being silently cascade-deleted, and tell
        # the admin which ones could not be removed.
        deleted = 0
        kept = []
        for category in list(queryset):
            try:
                category.delete()
                deleted += 1
            except ValidationError:
                kept.append(category.name)
        if kept:
            word = 'category' if len(kept) == 1 else 'categories'
            self.message_user(
                request,
                f'{len(kept)} {word} still contain products and were not '
                f'deleted: {", ".join(kept)}. Move their products to another '
                f'category first. {CATEGORY_DELETE_ERROR}',
                level=messages.ERROR,
            )
        if deleted:
            word = 'category' if deleted == 1 else 'categories'
            self.message_user(
                request, f'{deleted} empty {word} deleted.',
            )


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'price', 'is_active', 'is_available',
                    'stock_quantity']
    list_filter = ['category', 'is_active', 'is_available', 'is_featured']
    list_editable = ['is_available']
    search_fields = ['name', 'description']
    prepopulated_fields = {'slug': ('name',)}
    # deactivated_at is set programmatically by deactivate()/activate(); shown
    # read-only so admins cannot backdate or clear it by hand.
    readonly_fields = ['deactivated_at', 'inventory_tracking_display']
    # Phase 8F-2: inventory_tracking_mode is switched ONLY by the audited
    # cutover service (apps.menu.services.change_inventory_tracking_mode).
    # Phase 7D-2A: that service now supports legacy→ingredient cutovers only
    # (ingredient-to-legacy rollback is retired).
    # Excluding it from the admin form is the server-side guarantee: the field
    # is never part of the bound form, so a crafted POST cannot change it. The
    # read-only display below still shows the current mode for reference.
    exclude = ['inventory_tracking_mode']
    actions = ['deactivate_products', 'activate_products']

    # Permanent deletion is disabled for products (see Product.delete and the
    # pre_delete signal). Without this override the admin's delete button /
    # bulk delete would hard-delete products and orphan their historical
    # records, so it is removed from the interface entirely.
    def has_delete_permission(self, request, obj=None):
        return False

    def delete_queryset(self, request, queryset):
        # Never reached through the UI (has_delete_permission is False), but
        # kept as a guard so a programmatic bulk delete can never hard-delete
        # products either -- it soft-deactivates them instead.
        self.deactivate_products(request, queryset)

    @admin.action(description='Deactivate selected products')
    def deactivate_products(self, request, queryset):
        # Reference counts ride along on the same rows being deactivated, so
        # the active-order warning below costs no extra queries.
        rows = annotate_order_reference_counts(queryset.filter(is_active=True))
        products = list(rows)
        for product in products:
            product.deactivate()
            log_action(request.user, 'product.deactivate', product,
                       detail=f'active_orders={product.active_order_count}')
        in_active_orders = sum(1 for p in products if p.active_order_count)
        message = f'{len(products)} product(s) deactivated.'
        if in_active_orders:
            message += (
                f' Note: {in_active_orders} of them are referenced by active '
                'orders that are still being fulfilled (their saved line '
                'items remain intact).'
            )
        self.message_user(request, message)

    @admin.action(description='Reactivate selected products')
    def activate_products(self, request, queryset):
        # Phase 7D-4: legacy-tracked products cannot be reactivated. Handle
        # them per row so one refusal never blocks eligible ingredient
        # products in the same selection, and audit only real activations.
        activated = 0
        blocked = []
        for product in queryset.filter(is_active=False):
            try:
                product.activate()
            except ValidationError:
                blocked.append(product.name)
                continue
            activated += 1
            log_action(request.user, 'product.reactivate', product)
        if activated:
            self.message_user(request, f'{activated} product(s) reactivated.')
        if blocked:
            self.message_user(
                request,
                f'{len(blocked)} legacy-tracked product(s) could not be '
                f'reactivated: {", ".join(blocked)}. Convert them to '
                'ingredient tracking or leave them deactivated.',
                level=messages.ERROR,
            )

    def get_readonly_fields(self, request, obj=None):
        """Finished-product stock fields are never editable in the admin.

        For ingredient-tracked products ``stock_quantity`` and
        ``low_stock_threshold`` are frozen: stock lives in ``InventoryItem`` and
        ``IngredientMovementLog``. For the remaining legacy-tracked products the
        authorized staff product form is the stock workflow and records an
        ``InventoryLog`` row via ``_log_inventory_update()``; the admin has no
        equivalent logging path, so exposing these fields would change a
        finished-product balance with no ledger entry.

        Read-only on both add and change. New products keep the model defaults
        (they are ingredient-tracked and their stock fields are non-authoritative).
        """
        fields = list(super().get_readonly_fields(request, obj))
        fields.extend(['stock_quantity', 'low_stock_threshold'])
        return fields

    def inventory_tracking_display(self, obj):
        """Current inventory-tracking mode, shown read-only on the change form.

        Display only — the field itself is excluded from the editable form (see
        ``exclude`` above), so this cannot be used to change it.
        """
        return obj.get_inventory_tracking_mode_display()
    inventory_tracking_display.short_description = 'Inventory tracking'
