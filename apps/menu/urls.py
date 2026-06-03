from django.urls import path
from . import views

app_name = 'menu'

urlpatterns = [
    # Public menu
    path('', views.menu_index, name='index'),
    path('product/<slug:slug>/', views.product_detail, name='product_detail'),
    path('api/product/<int:pk>/price/', views.get_product_price, name='product_price'),

    # Admin - Products
    path('manage/products/', views.product_list, name='product_list'),
    path('manage/products/create/', views.product_create, name='product_create'),
    path('manage/products/<int:pk>/edit/', views.product_edit, name='product_edit'),
    path('manage/products/<int:pk>/delete/', views.product_delete, name='product_delete'),
    path('manage/products/<int:pk>/toggle/', views.product_toggle, name='product_toggle'),

    # Admin - Categories
    path('manage/categories/', views.category_list, name='category_list'),
    path('manage/categories/create/', views.category_create, name='category_create'),
    path('manage/categories/<int:pk>/edit/', views.category_edit, name='category_edit'),
    path('manage/categories/<int:pk>/delete/', views.category_delete, name='category_delete'),
]
