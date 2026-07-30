from django.urls import path
from . import views

app_name = 'finance'

urlpatterns = [
    path('',                    views.finance_index,          name='index'),
    path('api/cash-sales/',     views.finance_api_cash_sales, name='api_cash_sales'),
    path('<int:pk>/print/',     views.finance_print,          name='print'),
    path('history/',            views.finance_history,        name='history'),
]
