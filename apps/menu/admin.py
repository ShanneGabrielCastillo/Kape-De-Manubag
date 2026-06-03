from django.contrib import admin
from .models import Category, Product


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'icon', 'is_active', 'order']
    list_editable = ['is_active', 'order']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'price', 'is_available', 'stock_quantity']
    list_filter = ['category', 'is_available', 'is_featured']
    list_editable = ['is_available']
    search_fields = ['name', 'description']
    prepopulated_fields = {'slug': ('name',)}
