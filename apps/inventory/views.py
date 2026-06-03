from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from .models import InventoryLog
from apps.menu.models import Product
from apps.accounts.decorators import admin_required, cashier_or_admin_required


@login_required
@cashier_or_admin_required
def inventory_list(request):
    products = Product.objects.select_related('category').order_by('stock_quantity')
    low_stock = products.filter(stock_quantity__lte=10)
    return render(request, 'inventory/list.html', {
        'products': products,
        'low_stock_count': low_stock.count()
    })


@login_required
@cashier_or_admin_required
def restock_product(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        quantity = int(request.POST.get('quantity', 0))
        notes = request.POST.get('notes', '')
        if quantity > 0:
            old_qty = product.stock_quantity
            product.stock_quantity += quantity
            product.save()
            InventoryLog.objects.create(
                product=product,
                action='restock',
                quantity_change=quantity,
                quantity_before=old_qty,
                quantity_after=product.stock_quantity,
                notes=notes,
                performed_by=request.user,
            )
            messages.success(request, f'Restocked {product.name} with {quantity} units.')
            return JsonResponse({'success': True, 'new_qty': product.stock_quantity})
        return JsonResponse({'success': False, 'error': 'Invalid quantity'})
    return redirect('inventory:list')


@login_required
@cashier_or_admin_required
def inventory_log(request):
    logs = InventoryLog.objects.select_related('product', 'performed_by').order_by('-created_at')
    return render(request, 'inventory/log.html', {'logs': logs})
