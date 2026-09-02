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
    list_display = ['name', 'icon', 'is_active', 'order']
    list_editable = ['is_active', 'order']
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
    readonly_fields = ['deactivated_at']
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
        for product in queryset.filter(is_active=False):
            product.activate()
            log_action(request.user, 'product.reactivate', product)
        self.message_user(request, 'Selected products have been reactivated.')
