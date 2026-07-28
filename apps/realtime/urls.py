from django.urls import path
from . import views

app_name = 'realtime'

urlpatterns = [
    path('stream/', views.event_stream, name='event_stream'),
]
