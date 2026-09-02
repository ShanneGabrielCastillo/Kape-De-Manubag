from django.urls import path
from . import views

app_name = 'orders'

urlpatterns = [
    # Customer-facing
    path('cart/', views.cart_view, name='cart'),
    path('cart/add/<int:product_id>/', views.add_to_cart, name='add_to_cart'),
    path('cart/update/<int:item_id>/', views.update_cart, name='update_cart'),
    path('cart/remove/<int:item_id>/', views.remove_from_cart, name='remove_from_cart'),
    path('cart/clear/', views.clear_cart, name='clear_cart'),
    path('checkout/', views.checkout_view, name='checkout'),
    path('success/<int:pk>/', views.order_success, name='order_success'),

    # Staff/Cashier
    path('manage/', views.order_list, name='order_list'),
    path('manage/<int:pk>/', views.order_detail, name='order_detail'),
    path('manage/<int:pk>/status/', views.update_order_status, name='update_status'),
    path('manage/<int:pk>/payment/', views.process_payment, name='process_payment'),
    path('manage/<int:pk>/receipt/', views.print_receipt, name='print_receipt'),
    path('pos/', views.cashier_pos, name='pos'),
    path('pos/create/', views.create_pos_order, name='create_pos_order'),
    path('pos/draft-status/', views.pos_draft_status, name='pos_draft_status'),
    path('api/packaging-fee/', views.packaging_fee_preview, name='packaging_fee_preview'),
    # Queue / Tracker
    path('track/<str:order_number>/', views.order_tracker, name='order_tracker'),
    path('queue-board/', views.queue_board, name='queue_board'),
    path('api/track/<str:order_number>/', views.api_track_order, name='api_track_order'),
    path('api/queue-board/', views.api_queue_board, name='api_queue_board'),
    path('manage/<int:pk>/advance/', views.quick_status_advance, name='quick_status_advance'),
]
