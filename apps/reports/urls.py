from django.urls import path
from . import views

app_name = 'reports'

urlpatterns = [
    path('', views.reports_index, name='index'),
    path('export/excel/', views.export_excel, name='export_excel'),
    path('ingredient-usage/', views.ingredient_usage, name='ingredient_usage'),
    path('cancellation-dispositions/', views.cancellation_dispositions,
         name='cancellation_dispositions'),
]
