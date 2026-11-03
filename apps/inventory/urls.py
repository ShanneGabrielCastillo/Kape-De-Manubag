from django.urls import path
from . import views

app_name = 'inventory'

urlpatterns = [
    # ── Legacy product inventory ──────────────────────────────────────────────
    path('', views.inventory_list, name='list'),
    path('<int:pk>/restock/', views.restock_product, name='restock'),
    path('log/', views.inventory_log, name='log'),

    # ── Phase 2B-A: ingredient / packaging inventory ──────────────────────────
    path('ingredients/', views.ingredient_list, name='ingredient_list'),
    path('ingredients/add/', views.ingredient_create, name='ingredient_create'),

    # ── Phase 2B-B: adjustment and wastage ────────────────────────────────────
    path('ingredients/<int:pk>/adjust/', views.ingredient_adjust, name='ingredient_adjust'),
    path('ingredients/<int:pk>/wastage/', views.ingredient_record_wastage, name='ingredient_wastage'),

    # ── Phase 2B-C: packaging inventory ──────────────────────────────────────
    path('packaging/', views.packaging_list, name='packaging_list'),
    path('packaging/add/', views.packaging_create, name='packaging_create'),
    path('packaging/<int:pk>/adjust/', views.packaging_adjust, name='packaging_adjust'),
    path('packaging/<int:pk>/wastage/', views.packaging_record_wastage, name='packaging_wastage'),

    # ── Phase 3-3: purchase management ────────────────────────────────────────
    path('purchases/', views.purchase_list, name='purchase_list'),
    path('purchases/new/', views.purchase_create, name='purchase_create'),
    path('purchases/<int:pk>/', views.purchase_detail, name='purchase_detail'),
]
