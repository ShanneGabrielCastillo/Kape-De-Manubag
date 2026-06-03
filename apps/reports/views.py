"""
Reports - Sales analytics, exportable reports
"""
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.db.models import Sum, Count
from django.utils import timezone
from datetime import timedelta
from apps.orders.models import Order, OrderItem
from apps.accounts.decorators import admin_required


@login_required
@admin_required
def reports_index(request):
    today = timezone.now().date()

    # Date range filter
    start_date = request.GET.get('start', (today - timedelta(days=30)).isoformat())
    end_date = request.GET.get('end', today.isoformat())

    orders = Order.objects.filter(
        is_paid=True,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )

    total_revenue = orders.aggregate(total=Sum('total'))['total'] or 0
    total_orders = orders.count()
    avg_order = total_revenue / total_orders if total_orders > 0 else 0

    # Sales by day
    daily_sales = []
    from datetime import date as date_cls
    import datetime
    start = date_cls.fromisoformat(start_date)
    end = date_cls.fromisoformat(end_date)
    delta = end - start
    for i in range(delta.days + 1):
        day = start + timedelta(days=i)
        day_total = orders.filter(created_at__date=day).aggregate(t=Sum('total'))['t'] or 0
        day_count = orders.filter(created_at__date=day).count()
        daily_sales.append({
            'date': day.strftime('%b %d'),
            'total': float(day_total),
            'count': day_count,
        })

    # Top products
    top_products = OrderItem.objects.filter(
        order__in=orders
    ).values('product_name').annotate(
        total_qty=Sum('quantity'),
        total_revenue=Sum('subtotal')
    ).order_by('-total_revenue')[:10]

    # Sales by category
    from apps.menu.models import Category
    category_sales = []
    for cat in Category.objects.all():
        cat_items = OrderItem.objects.filter(
            order__in=orders,
            product__category=cat
        ).aggregate(total=Sum('subtotal'), qty=Sum('quantity'))
        if cat_items['total']:
            category_sales.append({
                'name': cat.name,
                'total': float(cat_items['total'] or 0),
                'qty': cat_items['qty'] or 0,
            })

    context = {
        'start_date': start_date,
        'end_date': end_date,
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'avg_order': avg_order,
        'daily_sales': daily_sales,
        'top_products': top_products,
        'category_sales': category_sales,
    }
    return render(request, 'reports/index.html', context)


@login_required
@admin_required
def export_excel(request):
    """Export sales report to Excel"""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return HttpResponse("openpyxl not installed", status=500)

    today = timezone.now().date()
    start_date = request.GET.get('start', (today - timedelta(days=30)).isoformat())
    end_date = request.GET.get('end', today.isoformat())

    orders = Order.objects.filter(
        is_paid=True,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    ).prefetch_related('items')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales Report"

    # Header styling
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(fill_type="solid", fgColor="4A2C2A")

    headers = ['Order #', 'Date', 'Customer', 'Type', 'Items', 'Total', 'Payment', 'Cashier']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')

    for row, order in enumerate(orders, 2):
        ws.cell(row=row, column=1, value=order.order_number)
        ws.cell(row=row, column=2, value=order.created_at.strftime('%Y-%m-%d %H:%M'))
        ws.cell(row=row, column=3, value=order.customer_name)
        ws.cell(row=row, column=4, value=order.get_order_type_display())
        ws.cell(row=row, column=5, value=order.items.count())
        ws.cell(row=row, column=6, value=float(order.total))
        ws.cell(row=row, column=7, value=order.get_payment_method_display())
        ws.cell(row=row, column=8, value=str(order.cashier) if order.cashier else 'N/A')

    # Auto-fit columns
    for col in ws.columns:
        max_length = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max_length + 4

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="sales_report_{start_date}_to_{end_date}.xlsx"'
    wb.save(response)
    return response
