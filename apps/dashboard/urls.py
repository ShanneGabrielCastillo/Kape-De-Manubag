from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard_index, name='index'),
    path('chart-data/', views.chart_data, name='chart_data'),
    path('settings/', views.system_settings, name='system_settings'),
]
