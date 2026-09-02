from django.urls import path
from . import views

app_name = 'menu'

urlpatterns = [
    # Public menu
    path('', views.menu_index, name='index'),
    path('api/product/<int:pk>/price/', views.get_product_price, name='product_price'),
    path('api/product-stock/', views.product_stock, name='product_stock'),

    # Admin - Products
    path('manage/products/', views.product_list, name='product_list'),
    path('manage/products/create/', views.product_create, name='product_create'),
    path('manage/products/<int:pk>/edit/', views.product_edit, name='product_edit'),
    path('manage/products/<int:pk>/toggle-active/', views.product_toggle_active, name='product_toggle_active'),
    path('manage/products/<int:pk>/toggle/', views.product_toggle, name='product_toggle'),

    # Admin - Categories
    path('manage/categories/', views.category_list, name='category_list'),
    path('manage/categories/create/', views.category_create, name='category_create'),
    path('manage/categories/<int:pk>/edit/', views.category_edit, name='category_edit'),
    path('manage/categories/<int:pk>/toggle-active/', views.category_toggle_active, name='category_toggle_active'),
    path('manage/categories/<int:pk>/delete/', views.category_delete, name='category_delete'),
]
