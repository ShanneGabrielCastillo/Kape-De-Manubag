from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import JsonResponse
from .models import InventoryLog
from apps.menu.models import CRITICAL_STOCK_THRESHOLD, Product
from apps.accounts.decorators import cashier_or_admin_required
from apps.audit.services import log_action


@login_required
@cashier_or_admin_required
def inventory_list(request):
    products = Product.objects.select_related('category').order_by('stock_quantity')
    # All four summary-card counts come from ONE conditional-aggregate query.
    # "Low" uses the exact same definition as Product.objects.low_stock()
    # (at or below each product's own restock threshold); "critical" is the
    # shared threshold; out-of-stock is stock at exactly zero.
    counts = products.aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(is_active=True)),
        low=Count('id', filter=Q(stock_quantity__lte=F('low_stock_threshold'))),
        critical=Count('id', filter=Q(stock_quantity__lte=CRITICAL_STOCK_THRESHOLD)),
        out_of_stock=Count('id', filter=Q(stock_quantity=0)),
    )
    return render(request, 'inventory/list.html', {
        'products': products,
        'total_count': counts['total'],
        'active_count': counts['active'],
        'low_stock_count': counts['low'],
        'critical_stock_count': counts['critical'],
        'out_of_stock_count': counts['out_of_stock'],
    })


@login_required
@cashier_or_admin_required
def restock_product(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        # Non-numeric input falls through to the same 'Invalid quantity'
        # response used for zero/negative values (a raw int() would 500).
        try:
            quantity = int(request.POST.get('quantity', 0))
        except (TypeError, ValueError):
            quantity = 0
        notes = request.POST.get('notes', '')
        if quantity > 0:
            # Stock change, inventory log and audit log commit or roll back
            # together -- a failed log write can never leave stock increased
            # without a trace.
            with transaction.atomic():
                old_qty = product.stock_quantity
                product.stock_quantity += quantity
                product.save()
                InventoryLog.record(
                    product=product,
                    action='restock',
                    source='manual_adjustment',
                    reason='Restock',
                    quantity_change=quantity,
                    quantity_before=old_qty,
                    quantity_after=product.stock_quantity,
                    notes=notes,
                    performed_by=request.user,
                )
                log_action(
                    request.user, 'inventory.restock', product,
                    detail=f'{quantity:+d} units ({old_qty} -> {product.stock_quantity})',
                )
            messages.success(request, f'Restocked {product.name} with {quantity} units.')
            return JsonResponse({'success': True, 'new_qty': product.stock_quantity})
        return JsonResponse({'success': False, 'error': 'Invalid quantity'})
    return redirect('inventory:list')


@login_required
@cashier_or_admin_required
def inventory_log(request):
    logs = InventoryLog.objects.select_related('product', 'performed_by').order_by('-created_at')
    q = request.GET.get('q', '').strip()
    if q:
        logs = logs.filter(
            Q(product__name__icontains=q) |
            Q(reason__icontains=q) |
            Q(notes__icontains=q)
        )
    return render(request, 'inventory/log.html', {'logs': logs, 'q': q})
