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

    # ── Phase 4B-5: recipe management ─────────────────────────────────────────
    path('recipes/', views.recipe_list, name='recipe_list'),
    path('recipes/add/', views.recipe_create, name='recipe_create'),
    path('recipes/<int:pk>/', views.recipe_detail, name='recipe_detail'),
    path('recipes/<int:pk>/edit/', views.recipe_edit, name='recipe_edit'),
    path('recipes/<int:pk>/approve/', views.recipe_approve, name='recipe_approve'),
    path('recipes/<int:pk>/retire/', views.recipe_retire, name='recipe_retire'),
    path('recipes/<int:pk>/items/<int:item_pk>/delete/', views.recipe_item_delete, name='recipe_item_delete'),

    # ── Phase 4B-5: packaging profile management ──────────────────────────────
    path('packaging-profiles/', views.packaging_profile_list, name='packaging_profile_list'),
    path('packaging-profiles/add/', views.packaging_profile_create, name='packaging_profile_create'),
    path('packaging-profiles/<int:pk>/', views.packaging_profile_detail, name='packaging_profile_detail'),
    path('packaging-profiles/<int:pk>/edit/', views.packaging_profile_edit, name='packaging_profile_edit'),
    path('packaging-profiles/<int:pk>/approve/', views.packaging_profile_approve, name='packaging_profile_approve'),
    path('packaging-profiles/<int:pk>/retire/', views.packaging_profile_retire, name='packaging_profile_retire'),
    path('packaging-profiles/<int:pk>/items/<int:item_pk>/delete/', views.packaging_profile_item_delete, name='packaging_profile_item_delete'),

    # ── Phase 4B-5: product packaging rules ───────────────────────────────────
    path('packaging-rules/', views.packaging_rule_list, name='packaging_rule_list'),
    path('packaging-rules/add/', views.packaging_rule_create, name='packaging_rule_create'),
    path('packaging-rules/<int:pk>/edit/', views.packaging_rule_edit, name='packaging_rule_edit'),

    # ── Phase 4B-5: configuration completeness ────────────────────────────────
    path('config-status/<int:product_pk>/', views.recipe_config_status, name='recipe_config_status'),
]
