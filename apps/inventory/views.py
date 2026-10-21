from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import JsonResponse
from django.core.paginator import Paginator
from .models import InventoryLog
from apps.menu.models import CRITICAL_STOCK_THRESHOLD, Category, Product
from apps.accounts.decorators import cashier_or_admin_required
from apps.audit.services import log_action


@login_required
@cashier_or_admin_required
def inventory_list(request):
    base_qs = Product.objects.select_related('category').order_by('stock_quantity')

    # Summary cards always reflect the FULL catalog (computed before filtering)
    counts = base_qs.aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(is_active=True)),
        low=Count('id', filter=Q(stock_quantity__lte=F('low_stock_threshold'))),
        critical=Count('id', filter=Q(stock_quantity__lte=CRITICAL_STOCK_THRESHOLD)),
        out_of_stock=Count('id', filter=Q(stock_quantity=0)),
    )

    products = base_qs

    q = request.GET.get('q', '').strip()
    if q:
        products = products.filter(name__icontains=q)

    category_filter = request.GET.get('category', '').strip()
    if category_filter:
        try:
            products = products.filter(category_id=int(category_filter))
        except (ValueError, TypeError):
            category_filter = ''

    stock_status_filter = request.GET.get('stock_status', '').strip()
    VALID_STOCK_STATUSES = ('out_of_stock', 'critical', 'low', 'ok')
    if stock_status_filter in VALID_STOCK_STATUSES:
        if stock_status_filter == 'out_of_stock':
            products = products.filter(stock_quantity=0)
        elif stock_status_filter == 'critical':
            products = products.filter(stock_quantity__lte=CRITICAL_STOCK_THRESHOLD)
        elif stock_status_filter == 'low':
            products = products.filter(stock_quantity__lte=F('low_stock_threshold'))
        elif stock_status_filter == 'ok':
            products = products.filter(stock_quantity__gt=F('low_stock_threshold'))
    else:
        stock_status_filter = ''

    categories = Category.objects.filter(is_active=True).order_by('name')

    return render(request, 'inventory/list.html', {
        'products': products,
        'total_count': counts['total'],
        'active_count': counts['active'],
        'low_stock_count': counts['low'],
        'critical_stock_count': counts['critical'],
        'out_of_stock_count': counts['out_of_stock'],
        'search': q,
        'category_filter': category_filter,
        'stock_status_filter': stock_status_filter,
        'categories': categories,
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

    VALID_ACTIONS = [v for v, _ in InventoryLog.ACTION_CHOICES]
    action_filter = request.GET.get('action', '').strip()
    if action_filter not in VALID_ACTIONS:
        action_filter = ''
    if action_filter:
        logs = logs.filter(action=action_filter)

    VALID_SOURCES = [v for v, _ in InventoryLog.SOURCE_CHOICES]
    source_filter = request.GET.get('source', '').strip()
    if source_filter not in VALID_SOURCES:
        source_filter = ''
    if source_filter:
        logs = logs.filter(source=source_filter)

    paginator = Paginator(logs, 10)
    page = request.GET.get('page', 1)
    logs_page = paginator.get_page(page)
    return render(request, 'inventory/log.html', {
        'logs': logs_page,
        'q': q,
        'action_filter': action_filter,
        'source_filter': source_filter,
        'action_choices': InventoryLog.ACTION_CHOICES,
        'source_choices': InventoryLog.SOURCE_CHOICES,
    })
