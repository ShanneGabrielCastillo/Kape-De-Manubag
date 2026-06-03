from django.contrib import admin
from .models import Order, OrderItem, Cart, CartItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'customer_name', 'status', 'total', 'is_paid', 'created_at']
    list_filter = ['status', 'is_paid', 'order_type']
    search_fields = ['order_number', 'customer_name']
    inlines = [OrderItemInline]
    readonly_fields = ['order_number', 'created_at']


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ['session_key', 'item_count', 'total', 'created_at']
