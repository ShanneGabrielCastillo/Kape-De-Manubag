from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('staff/', views.staff_list, name='staff_list'),
    path('staff/create/', views.staff_create, name='staff_create'),
    path('staff/<int:pk>/toggle/', views.staff_toggle, name='staff_toggle'),
    # ── Password management ───────────────────────────────────────────────────
    path('password/change/', views.change_password, name='password_change'),
    path('password/reset/', views.password_reset_request, name='password_reset'),
    path('password/reset/done/', views.password_reset_done, name='password_reset_done'),
    path('password/reset/<uidb64>/<token>/', views.password_reset_confirm, name='password_reset_confirm'),
]
