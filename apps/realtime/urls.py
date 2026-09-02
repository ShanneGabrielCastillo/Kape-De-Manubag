from django.urls import path
from . import views

app_name = 'realtime'

urlpatterns = [
    path('stream/', views.event_stream, name='event_stream'),
    path('track/', views.customer_order_stream, name='customer_order_stream'),
]
