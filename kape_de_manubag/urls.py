"""
URL Configuration for Kape De Manubag System
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('apps.menu.urls')),
    path('accounts/', include('apps.accounts.urls')),
    path('orders/', include('apps.orders.urls')),
    path('dashboard/', include('apps.dashboard.urls')),
    path('inventory/', include('apps.inventory.urls')),
    path('finance/', include('apps.finance.urls')),
    path('realtime/', include('apps.realtime.urls')),
    path('reports/', include('apps.reports.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Customize admin site
admin.site.site_header = "Kape De Manubag Admin"
admin.site.site_title = "KDM Admin"
admin.site.index_title = "Welcome to Kape De Manubag Administration"
