import time
import queue
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from apps.accounts.decorators import kitchen_staff_required, kitchen_or_admin_required
from apps.orders.models import Order
from apps.orders.services import validate_status_transition
from apps.audit.services import log_action


@login_required
@kitchen_staff_required
def kitchen_orders(request):
    orders = Order.objects.filter(
        status='preparing'
    ).prefetch_related('items__product').order_by('created_at')
    context = {
        'orders': orders,
        'page_title': 'Kitchen Orders',
    }
    return render(request, 'kitchen/orders.html', context)


@login_required
@kitchen_staff_required
@require_POST
def mark_order_ready(request, pk):
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)
        try:
            validate_status_transition(order.status, 'ready', order=order)
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

        order.status = 'ready'
        order.ready_at = timezone.now()
        order.save()
        # signals.py fires status_changed automatically — no manual publish needed

        log_action(request.user, 'order.mark_ready', order,
                   detail='Kitchen staff marked order as ready.')

    return JsonResponse({
        'success': True,
        'order_number': order.order_number,
        'new_status': 'ready'
    })
