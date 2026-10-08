from django.urls import path
from . import views

app_name = 'realtime'

urlpatterns = [
    path('stream/', views.event_stream, name='event_stream'),
    path('track/', views.customer_order_stream, name='customer_order_stream'),
    path('kitchen-stream/', views.kitchen_event_stream, name='kitchen_event_stream'),
]
