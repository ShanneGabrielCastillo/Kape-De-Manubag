from django.urls import path
from . import mfa_views, views

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
    # ── Two-factor authentication (Phase 5D) ────────────────────────────────
    path('mfa/setup/', mfa_views.mfa_setup, name='mfa_setup'),
    path('mfa/challenge/', mfa_views.mfa_challenge, name='mfa_challenge'),
    path('mfa/manage/', mfa_views.mfa_manage, name='mfa_manage'),
    path('mfa/disable/', mfa_views.mfa_disable, name='mfa_disable'),
    path('mfa/recovery-codes/regenerate/', mfa_views.mfa_regenerate_recovery_codes,
         name='mfa_regenerate_recovery_codes'),
]
