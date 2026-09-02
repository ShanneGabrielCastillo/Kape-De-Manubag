"""
URL Configuration for Kape De Manubag System
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from kape_de_manubag import health

urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health.health_check, name='health'),
    path('', include('apps.menu.urls')),
    path('accounts/', include('apps.accounts.urls')),
    path('orders/', include('apps.orders.urls')),
    path('dashboard/', include('apps.dashboard.urls')),
    path('inventory/', include('apps.inventory.urls')),
    path('finance/', include('apps.finance.urls')),
    path('realtime/', include('apps.realtime.urls')),
    path('reports/', include('apps.reports.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
# NOTE: /health/ is intentionally public and registered above; it must never
# require authentication or expose sensitive information (see health.py).

# Customize admin site
admin.site.site_header = "Kape De Manubag Admin"
admin.site.site_title = "KDM Admin"
admin.site.index_title = "Welcome to Kape De Manubag Administration"
