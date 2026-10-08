from django.urls import path
from . import views

app_name = 'kitchen'

urlpatterns = [
    path('', views.kitchen_orders, name='orders'),
    path('orders/<int:pk>/ready/', views.mark_order_ready, name='mark_ready'),
]
