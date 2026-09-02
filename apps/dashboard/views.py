"""
Dashboard views - Statistics and analytics for Admin/Cashier
"""
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.decorators import admin_required, cashier_or_admin_required
from apps.audit.services import log_action
from apps.menu.models import CRITICAL_STOCK_THRESHOLD, Product
from apps.orders.models import Order, OrderItem

from .models import SystemSetting

logger = logging.getLogger(__name__)


# ── Error-resilient widget loading ─────────────────────────────────────────
# Every dashboard widget is computed independently and guarded by _widget():
# an unexpected failure in one dataset logs the error (with traceback) and
# returns a safe fallback instead of taking down the whole page or the whole
# summary response. The page tracks which widgets failed (``widget_errors``)
# so the template can render a friendly fallback message per widget; the
# summary endpoint returns ``None`` for a failed widget so the client keeps
# updating everything else.


# Zeroed sales snapshot used when the sales-stats query itself fails (a
# missing/empty aggregate result and a failed query are different states).
_ZERO_STATS = {
    'total_sales': 0, 'total_orders': 0,
    'daily_sales': 0, 'daily_orders': 0,
    'weekly_sales': 0, 'weekly_orders': 0,
    'monthly_sales': 0, 'monthly_orders': 0,
}


def _widget(label, fallback, fn):
    """Compute one dashboard widget in isolation.

    Returns ``(value, failed)``. On any unexpected exception the error is
    logged and ``(fallback, True)`` is returned, so a failure in one dataset
    can never break the rest of the dashboard. This wraps existing widget
    logic only -- the computations themselves are unchanged.
    """
    try:
        return fn(), False
    except Exception:
        logger.exception('Dashboard widget failed: %s', label)
        return fallback, True


# ── Shared widget computations ─────────────────────────────────────────────
# The page render and the realtime summary endpoint both build the dashboard
# widgets from the SAME consolidated queries (one per dataset, never one per
# day or per row). Keeping them in one place guarantees the summary endpoint
# cannot drift from what the page shows.


def _sales_stats():
    """All four sales totals/counts (all-time, today, week, month) in a SINGLE
    aggregate query using conditional aggregates.

    Filters: is_paid=True AND status='completed' — consistent with the finance
    module's definition of a completed sale.  Cancelled orders are excluded
    even if is_paid somehow ended up True (e.g. direct DB edit).

    NOTE: the plain aggregate is named ``all_time``/``all_time_orders``, NOT
    ``total``/``total_orders``: on Django >= 5.0 an aggregate output name that
    collides with a model field referenced by a later conditional aggregate
    (``Sum('total', filter=...)``) is resolved as the annotation alias and
    raises "Cannot compute Sum('total'): 'total' is an aggregate".
    """
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    sales = Order.objects.filter(is_paid=True, status='completed').aggregate(
        all_time=Sum('total'),
        all_time_orders=Count('id'),
        daily=Sum('total', filter=Q(created_at__date=today)),
        daily_orders=Count('id', filter=Q(created_at__date=today)),
        weekly=Sum('total', filter=Q(created_at__date__gte=week_start)),
        weekly_orders=Count('id', filter=Q(created_at__date__gte=week_start)),
        monthly=Sum('total', filter=Q(created_at__date__gte=month_start)),
        monthly_orders=Count('id', filter=Q(created_at__date__gte=month_start)),
    )
    return {
        'total_sales': sales['all_time'] or 0,
        'total_orders': sales['all_time_orders'] or 0,
        'daily_sales': sales['daily'] or 0,
        'daily_orders': sales['daily_orders'] or 0,
        'weekly_sales': sales['weekly'] or 0,
        'weekly_orders': sales['weekly_orders'] or 0,
        'monthly_sales': sales['monthly'] or 0,
        'monthly_orders': sales['monthly_orders'] or 0,
    }


def _status_counts():
    """Pending/preparing order counts — one aggregate query."""
    counts = Order.objects.aggregate(
        pending=Count('id', filter=Q(status='pending')),
        preparing=Count('id', filter=Q(status='preparing')),
    )
    return counts['pending'] or 0, counts['preparing'] or 0


def _chart_series(period='week', label_fmt=None):
    """Chart labels+data for the requested period — one grouped query for the
    whole range instead of one per day.

    Filters: is_paid=True AND status='completed' — matches _sales_stats() and
    the finance module so the chart bars agree with the stats tiles.
    """
    today = timezone.localdate()
    if period == 'month':
        start = today - timedelta(days=29)      # last 30 days
        if label_fmt is None:
            label_fmt = '%d'
    else:
        start = today - timedelta(days=6)       # last 7 days (default)
        if label_fmt is None:
            label_fmt = '%a'

    day_sales = (
        Order.objects.filter(is_paid=True, status='completed', created_at__date__gte=start)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(total=Sum('total'))
    )
    sales_by_day = {row['day']: row['total'] for row in day_sales}

    labels = []
    data = []
    for i in range((today - start).days + 1):
        day = start + timedelta(days=i)
        labels.append(day.strftime(label_fmt))
        data.append(float(sales_by_day.get(day, 0) or 0))
    return labels, data


def _top_products():
    """Top selling products — one grouped query, top 5.

    Filters to completed orders only so cancelled order items do not
    inflate product counts or revenue totals.
    """
    return list(
        OrderItem.objects
        .filter(order__status='completed')
        .values('product_name')
        .annotate(
            total_qty=Sum('quantity'),
            total_revenue=Sum('subtotal'),
        )
        .order_by('-total_qty')[:5]
    )


def _low_stock():
    """Low stock alerts — sellable products at or below their own restock
    threshold (Product.objects.sellable().low_stock()).
    select_related('category') avoids an N+1 when the template renders each
    product's category name. Deactivated products are not sellable, so they
    are excluded from restock alerts."""
    return list(
        Product.objects.sellable().low_stock()
        .select_related('category')
        .order_by('stock_quantity')[:5]
    )


def _recent_orders():
    """The ten most recent orders, with the cashier preloaded (no N+1)."""
    return list(Order.objects.select_related('cashier').order_by('-created_at')[:10])


_VALID_CHART_PERIODS = ('week', 'month')


def _validated_period(request):
    """Return the requested chart period, defaulting to 'week' for anything
    that is not a known period."""
    period = request.GET.get('period', 'week')
    return period if period in _VALID_CHART_PERIODS else 'week'


# ── Consolidated widget loader ──────────────────────────────────────────────
# _load_widgets() is the single place that computes the dashboard datasets.
# Both the page render and the realtime summary endpoint call it, so the
# summary can never drift from what the page shows (the original motivation
# for the shared ``_sales_stats`` style helpers). Each widget stays isolated:
# a failure is logged by _widget(), the widget keeps its fallback value, and
# its label is recorded in the ``failed`` set for the caller to surface.


def _load_widgets(period='week', chart_label_fmt=None, *, include_recent=False):
    """Compute every dashboard widget in one pass.

    Returns ``(widgets, failed)`` where ``widgets`` is a flat dict keyed by
    the exact names the page context / summary JSON use (e.g. ``total_sales``,
    ``pending_count``, ``chart_data``) and ``failed`` is the set of widget
    labels that raised (see _widget). ``include_recent`` adds the recent
    orders widget — used only by the page render, since the summary endpoint
    refreshes recent orders from realtime events instead.
    """
    failed = set()
    widgets = {}

    stats, stats_failed = _widget(
        'sales stats', dict(_ZERO_STATS), _sales_stats,
    )
    if stats_failed:
        failed.add('stats')
    widgets.update(stats)

    counts, counts_failed = _widget('status counts', (0, 0), _status_counts)
    if counts_failed:
        failed.add('status_counts')
    widgets['pending_count'], widgets['preparing_count'] = counts

    chart, chart_failed = _widget(
        'chart series', ([], []),
        lambda: _chart_series(period, label_fmt=chart_label_fmt),
    )
    if chart_failed:
        failed.add('chart')
    widgets['chart_labels'], widgets['chart_data'] = chart

    if include_recent:
        recent, recent_failed = _widget('recent orders', [], _recent_orders)
        if recent_failed:
            failed.add('recent_orders')
        widgets['recent_orders'] = recent

    top_products, top_failed = _widget('top products', [], _top_products)
    if top_failed:
        failed.add('top_products')
    widgets['top_products'] = top_products

    low_stock, low_failed = _widget('low stock', [], _low_stock)
    if low_failed:
        failed.add('low_stock')
    widgets['low_stock'] = low_stock

    return widgets, failed


@login_required
@cashier_or_admin_required
def dashboard_index(request):
    # All widgets come from the one consolidated loader (see _load_widgets).
    # Each dataset is guarded, so a failure in one logs the error and falls
    # back to safe values while the rest of the dashboard keeps rendering;
    # the failed labels are passed to the template so it can show a friendly
    # fallback per widget.
    widgets, widget_errors = _load_widgets(
        'week', chart_label_fmt='%b %d', include_recent=True,
    )
    context = dict(widgets)
    context['widget_errors'] = widget_errors
    return render(request, 'dashboard/index.html', context)


@login_required
@cashier_or_admin_required
def dashboard_summary(request):
    """JSON snapshot of every dashboard widget, fetched by the page ONLY when a
    realtime event (new_order / status_changed / inventory_low) indicates the
    data may have changed. Reuses the same consolidated loader as the page
    render, so a refresh is ONE request (no per-widget polling)."""
    period = _validated_period(request)
    widgets, failed = _load_widgets(period)

    # A failed widget is reported as ``None`` so the client can keep updating
    # the healthy ones (each failure is logged by _widget).
    stats_failed = 'stats' in failed
    counts_failed = 'status_counts' in failed
    chart_failed = 'chart' in failed
    top_failed = 'top_products' in failed
    low_failed = 'low_stock' in failed

    return JsonResponse({
        'daily_sales': None if stats_failed else float(widgets['daily_sales']),
        'daily_orders': None if stats_failed else widgets['daily_orders'],
        'weekly_sales': None if stats_failed else float(widgets['weekly_sales']),
        'weekly_orders': None if stats_failed else widgets['weekly_orders'],
        'monthly_sales': None if stats_failed else float(widgets['monthly_sales']),
        'monthly_orders': None if stats_failed else widgets['monthly_orders'],
        'total_sales': None if stats_failed else float(widgets['total_sales']),
        'total_orders': None if stats_failed else widgets['total_orders'],
        'pending_count': None if counts_failed else widgets['pending_count'],
        'preparing_count': None if counts_failed else widgets['preparing_count'],
        'chart_labels': None if chart_failed else widgets['chart_labels'],
        'chart_data': None if chart_failed else widgets['chart_data'],
        'top_products': None if top_failed else [
            {
                'product_name': t['product_name'],
                'total_qty': t['total_qty'],
                'total_revenue': float(t['total_revenue']),
            }
            for t in widgets['top_products']
        ],
        'low_stock': None if low_failed else [
            {
                'pk': p.pk,
                'name': p.name,
                'category': p.category.name,
                'stock_quantity': p.stock_quantity,
                'is_critical': p.stock_quantity <= CRITICAL_STOCK_THRESHOLD,
            }
            for p in widgets['low_stock']
        ],
    })


@login_required
@cashier_or_admin_required
def chart_data(request):
    """AJAX endpoint for chart data"""
    period = _validated_period(request)
    try:
        labels, data = _chart_series(period)
    except Exception:
        logger.exception('Dashboard chart-data widget failed (period=%s)', period)
        # A friendly 503 instead of an unhandled 500: the page keeps the
        # last good chart and the JS shows a fallback message.
        return JsonResponse({'error': 'chart unavailable'}, status=503)
    return JsonResponse({'labels': labels, 'data': data})


@login_required
@admin_required
def system_settings(request):
    """Settings page — admin only. Manage configurable system values."""
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
        updated_keys = []
        for setting in settings_list:
            new_value = request.POST.get(f'value_{setting.key}', '').strip()
            if new_value == '':
                errors.append(f'"{setting.key}" cannot be empty.')
                continue
            # Validate numeric fields
            if setting.key == 'PACKAGING_FEE_PER_ITEM':
                try:
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
                updated_keys.append(setting.key)

        if updated:
            log_action(
                request.user, 'settings.update',
                object_type='SystemSetting', object_repr='System Settings',
                detail='Changed: ' + ', '.join(sorted(updated_keys)),
            )

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
