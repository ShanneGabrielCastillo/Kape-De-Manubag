from django.urls import path
from . import views

app_name = 'inventory'

urlpatterns = [
    path('', views.inventory_list, name='list'),
    path('<int:pk>/restock/', views.restock_product, name='restock'),
    path('log/', views.inventory_log, name='log'),
]
