"""
apps/orders/admin_views.py
==========================
Phase 6A Stage 6A-4 — Django Admin intermediate cancellation page.

This module provides a single custom admin view:

    admin_cancel_order(request, pk)

The view:
  - Is accessible only to authenticated admin (superuser) users.
  - Displays the order items with their quantities so the admin can declare
    the correct disposition.
  - For ``awaiting_payment`` orders: no item-level inputs are required; the
    system generates system_unpaid dispositions automatically.
  - For ``preparing`` orders: requires ``prepared_<pk>`` and
    ``discarded_<pk>`` inputs for every order item.
  - For ``ready`` orders: requires ``discarded_<pk>`` inputs for every
    applicable legacy order item; ingredient items need no staff input.
  - On GET: renders the disposition form.
  - On POST: validates the form, then calls the shared ``on_status_changed()``
    service inside a single ``transaction.atomic()`` block.  All of:
    disposition creation, inventory restoration, InventoryLog entries,
    order status update, and audit log are committed or rolled back together.

The view deliberately does NOT depend on ``OrderAdmin.save_model`` to
complete cancellation — it calls the service directly.
"""

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.audit.services import log_action
from apps.orders.models import Order, OrderCancellationDisposition, OrderItem
from apps.orders.services import (
    DispositionRequired,
    on_status_changed,
    validate_status_transition,
)
from apps.orders.forms import CancellationDispositionForm


# ── Authorization ─────────────────────────────────────────────────────────────
# This view is served via admin.site.admin_view(), which wraps it in the
# admin's login_required and superuser checks.  The wrapper is applied in
# OrderAdmin.get_urls().


def admin_cancel_order(request, pk):
    """
    Intermediate cancellation page for the Django Admin.

    GET  — show the disposition form for the order.
    POST — validate, call on_status_changed(), redirect on success.
    """
    order = get_object_or_404(Order, pk=pk)

    # Guard: can only cancel active orders.
    if order.status in ('cancelled', 'completed'):
        from django.contrib import messages
        messages.error(
            request,
            f'Order #{order.order_number} is already {order.get_status_display().lower()} '
            'and cannot be cancelled.',
        )
        return redirect(f'/admin/orders/order/{pk}/change/')

    # Validate that cancellation is a legal transition from the current status.
    try:
        validate_status_transition(order.status, 'cancelled', order=order)
    except ValueError as e:
        from django.contrib import messages
        messages.error(request, f'Cannot cancel: {e}')
        return redirect(f'/admin/orders/order/{pk}/change/')

    order_items = list(order.items.select_related('product').order_by('pk'))
    status = order.status

    if request.method == 'POST':
        error_message, disposition_input = _parse_admin_disposition(
            request, order_items, status
        )
        if error_message:
            return render(request, 'orders/admin_cancel_order.html', {
                'order': order,
                'order_items': order_items,
                'status': status,
                'error': error_message,
                'title': f'Cancel Order #{order.order_number}',
            })

        # All inventory and status changes in one atomic block.
        # on_status_changed() exceptions propagate out so the block rolls back.
        previous_status = order.status  # capture before coordinator may change things
        cancellation_error = None
        try:
            with transaction.atomic():
                on_status_changed(
                    order.pk, 'cancelled',
                    actor=request.user,
                    disposition=disposition_input,
                )
                # Refresh to pick up stock_deducted change from coordinator.
                order.refresh_from_db()
                # Update order status, timestamp, and audit log in the same block.
                order.status = 'cancelled'
                if not order.cancelled_at:
                    order.cancelled_at = timezone.now()
                order.save(update_fields=['status', 'cancelled_at'])
                log_action(
                    request.user, 'order.cancel', order,
                    detail=f'Cancelled — was {previous_status} (via admin cancellation page)',
                )
        except (DispositionRequired, ValueError) as e:
            cancellation_error = str(e)

        if cancellation_error:
            return render(request, 'orders/admin_cancel_order.html', {
                'order': order,
                'order_items': order_items,
                'status': status,
                'error': cancellation_error,
                'title': f'Cancel Order #{order.order_number}',
            })

        from django.contrib import messages
        messages.success(
            request,
            f'Order #{order.order_number} has been cancelled.',
        )
        return redirect(f'/admin/orders/order/{pk}/change/')

    # GET: render the form.
    return render(request, 'orders/admin_cancel_order.html', {
        'order': order,
        'order_items': order_items,
        'status': status,
        'error': None,
        'title': f'Cancel Order #{order.order_number}',
    })


def _parse_admin_disposition(request, order_items, status):
    """
    Parse and validate per-item disposition data from an admin POST.

    Returns ``(None, disposition_dict)`` on success,
    ``(error_message, None)`` on failure.

    Uses ``CancellationDispositionForm`` for field-level validation of
    each item, then cross-validates against item quantities.
    """
    if status == 'awaiting_payment':
        # No staff input required; service generates system_unpaid dispositions.
        return None, None

    disposition = {}

    for item in order_items:
        pk = item.pk

        if status == 'preparing':
            # Both prepared_units and discarded_units required.
            data = {
                'prepared_units': request.POST.get(f'prepared_{pk}'),
                'discarded_units': request.POST.get(f'discarded_{pk}'),
            }
            form = CancellationDispositionForm(data)
            # Make both fields required for preparing.
            form.fields['prepared_units'].required = True
            form.fields['discarded_units'].required = True

            if data['prepared_units'] is None:
                return (
                    f'Missing prepared quantity for "{item.product_name}". '
                    'Please enter how many units were prepared.',
                    None,
                )
            if data['discarded_units'] is None:
                return (
                    f'Missing discarded quantity for "{item.product_name}". '
                    'Please enter how many prepared units were discarded.',
                    None,
                )

            if not form.is_valid():
                errors = '; '.join(
                    f'{item.product_name} — {k}: {", ".join(v)}'
                    for k, vs in form.errors.items()
                    for v in [vs]
                )
                return f'Invalid input: {errors}', None

            prepared = form.cleaned_data['prepared_units']
            discarded = form.cleaned_data['discarded_units']

            if prepared < 0 or prepared > item.quantity:
                return (
                    f'Prepared quantity ({prepared}) for "{item.product_name}" '
                    f'must be 0 to {item.quantity}.',
                    None,
                )
            if discarded < 0 or discarded > prepared:
                return (
                    f'Discarded quantity ({discarded}) for "{item.product_name}" '
                    f'must be 0 to prepared ({prepared}).',
                    None,
                )

            disposition[pk] = {
                'prepared_units': prepared,
                'discarded_units': discarded,
            }

        elif status == 'ready':
            # discarded_units required only for legacy items.
            if item.inventory_mode != OrderItem.INVENTORY_MODE_LEGACY:
                disposition[pk] = {'discarded_units': 0}
                continue

            data = {'discarded_units': request.POST.get(f'discarded_{pk}')}
            if data['discarded_units'] is None:
                return (
                    f'Missing discarded quantity for "{item.product_name}". '
                    'Please enter how many prepared units were discarded '
                    '(enter 0 if none were discarded).',
                    None,
                )

            form = CancellationDispositionForm(data)
            form.fields['discarded_units'].required = True

            if not form.is_valid():
                errors = '; '.join(
                    f'{item.product_name} — {k}: {", ".join(v)}'
                    for k, vs in form.errors.items()
                    for v in [vs]
                )
                return f'Invalid input: {errors}', None

            discarded = form.cleaned_data['discarded_units']

            if discarded < 0 or discarded > item.quantity:
                return (
                    f'Discarded quantity ({discarded}) for "{item.product_name}" '
                    f'must be 0 to {item.quantity}.',
                    None,
                )

            disposition[pk] = {'discarded_units': discarded}

    return None, disposition
