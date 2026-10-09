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
        # Suppress the post_save signal's in-transaction broadcast so we can
        # publish only after the transaction commits successfully.
        order._skip_realtime = True
        order.save()

        log_action(request.user, 'order.mark_ready', order,
                   detail='Kitchen staff marked order as ready.')

        # Capture the values we need inside the closure before the atomic
        # block exits (order.pk is stable; the other fields are already set).
        _order_id    = order.pk
        _order_num   = order.order_number
        _queue_num   = order.queue_number
        _status      = order.status            # 'ready'
        _status_disp = order.get_status_display()
        _is_paid     = order.is_paid

        def _broadcast():
            from apps.realtime.broker import publish
            publish('status_changed', {
                'order_id':           _order_id,
                'order_number':       _order_num,
                'queue_number':       _queue_num,
                'new_status':         _status,
                'new_status_display': _status_disp,
                'is_paid':            _is_paid,
            })

        transaction.on_commit(_broadcast)

    return JsonResponse({
        'success': True,
        'order_number': order.order_number,
        'new_status': 'ready'
    })
