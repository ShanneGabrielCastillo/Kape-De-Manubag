"""
Dashboard views - Statistics and analytics for Admin/Cashier
"""
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Sum, Count, Q
from django.utils import timezone
from datetime import timedelta, date
from apps.orders.models import Order, OrderItem
from apps.menu.models import Product, Category
from apps.accounts.decorators import cashier_or_admin_required, admin_required


@login_required
@cashier_or_admin_required
def dashboard_index(request):
    today = timezone.now().date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    # Sales stats
    def get_sales(queryset):
        result = queryset.filter(is_paid=True).aggregate(
            total=Sum('total'), count=Count('id')
        )
        return result['total'] or 0, result['count'] or 0

    total_sales, total_orders = get_sales(Order.objects.all())
    daily_sales, daily_orders = get_sales(Order.objects.filter(created_at__date=today))
    weekly_sales, weekly_orders = get_sales(Order.objects.filter(created_at__date__gte=week_start))
    monthly_sales, monthly_orders = get_sales(Order.objects.filter(created_at__date__gte=month_start))

    # Recent orders
    recent_orders = Order.objects.select_related('cashier').order_by('-created_at')[:10]

    # Top selling products
    top_products = OrderItem.objects.values(
        'product_name'
    ).annotate(
        total_qty=Sum('quantity'),
        total_revenue=Sum('subtotal')
    ).order_by('-total_qty')[:5]

    # Low stock alerts
    low_stock = Product.objects.filter(
        is_available=True,
        stock_quantity__lte=10
    ).order_by('stock_quantity')[:5]

    # Pending orders count
    pending_count = Order.objects.filter(status='pending').count()
    preparing_count = Order.objects.filter(status='preparing').count()

    # Monthly chart data (last 7 days)
    chart_labels = []
    chart_data = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_sales = Order.objects.filter(
            is_paid=True, created_at__date=day
        ).aggregate(total=Sum('total'))['total'] or 0
        chart_labels.append(day.strftime('%b %d'))
        chart_data.append(float(day_sales))

    context = {
        'total_sales': total_sales,
        'total_orders': total_orders,
        'daily_sales': daily_sales,
        'daily_orders': daily_orders,
        'weekly_sales': weekly_sales,
        'weekly_orders': weekly_orders,
        'monthly_sales': monthly_sales,
        'monthly_orders': monthly_orders,
        'recent_orders': recent_orders,
        'top_products': top_products,
        'low_stock': low_stock,
        'pending_count': pending_count,
        'preparing_count': preparing_count,
        'chart_labels': chart_labels,
        'chart_data': chart_data,
        'product_count': Product.objects.filter(is_available=True).count(),
        'category_count': Category.objects.filter(is_active=True).count(),
    }
    return render(request, 'dashboard/index.html', context)


@login_required
@cashier_or_admin_required
def chart_data(request):
    """AJAX endpoint for chart data"""
    period = request.GET.get('period', 'week')
    today = timezone.now().date()
    labels = []
    data = []

    if period == 'week':
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            sales = Order.objects.filter(is_paid=True, created_at__date=day).aggregate(
                total=Sum('total'))['total'] or 0
            labels.append(day.strftime('%a'))
            data.append(float(sales))
    elif period == 'month':
        for i in range(29, -1, -1):
            day = today - timedelta(days=i)
            sales = Order.objects.filter(is_paid=True, created_at__date=day).aggregate(
                total=Sum('total'))['total'] or 0
            labels.append(day.strftime('%d'))
            data.append(float(sales))

    return JsonResponse({'labels': labels, 'data': data})


@login_required
@admin_required
def system_settings(request):
    """Settings page — admin only. Manage configurable system values."""
    from .models import SystemSetting

    # Ensure the packaging fee setting exists with a default
    SystemSetting.objects.get_or_create(
        key='PACKAGING_FEE_PER_ITEM',
        defaults={
            'value': '6.00',
            'description': 'Packaging fee in PHP per eligible meal item for Take-Out orders.',
        }
    )

    settings_list = SystemSetting.objects.all().order_by('key')

    if request.method == 'POST':
        updated = 0
        errors = []
        for setting in settings_list:
            new_value = request.POST.get(f'value_{setting.key}', '').strip()
            if new_value == '':
                errors.append(f'"{setting.key}" cannot be empty.')
                continue
            # Validate numeric fields
            if setting.key == 'PACKAGING_FEE_PER_ITEM':
                try:
                    from decimal import Decimal, InvalidOperation
                    val = Decimal(new_value)
                    if val < 0:
                        errors.append('Packaging fee cannot be negative.')
                        continue
                    new_value = str(round(val, 2))
                except InvalidOperation:
                    errors.append(f'"{setting.key}" must be a valid number (e.g. 6.00).')
                    continue
            if setting.value != new_value:
                setting.value = new_value
                setting.save(update_fields=['value', 'updated_at'])
                updated += 1

        if errors:
            for err in errors:
                messages.error(request, err)
        else:
            if updated:
                messages.success(request, f'{updated} setting{"s" if updated != 1 else ""} updated successfully.')
            else:
                messages.info(request, 'No changes were made.')
        return redirect('dashboard:system_settings')

    return render(request, 'dashboard/settings.html', {
        'settings_list': settings_list,
    })
