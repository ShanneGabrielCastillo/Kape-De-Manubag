"""
Reports - Sales analytics, exportable reports
"""
from datetime import datetime, time as time_cls, timedelta, date as date_cls
from decimal import Decimal

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.db.models import Sum, Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.accounts.decorators import admin_required
from apps.inventory.models import IngredientMovementLog, InventoryItem
from apps.orders.models import Order, OrderItem, OrderCancellationDisposition


# ── Export / range hardening constants (Phase 7.4, decision record §10) ──────

# H2: one quarter maximum span for the Sales page and the Excel export.
# Larger requested ranges are clamped to the most recent 92 days of the
# window (start = end - 91 days) and the page shows a visible notice.
MAX_SALES_RANGE_DAYS = 92

# H3: hard cap on exported order rows. The export counts matching orders
# first and refuses with HTTP 400 (no workbook bytes) when exceeded —
# there is no silent truncation path.
MAX_EXPORT_ROWS = 5000

# Existing fallback window (kept identical to the previous behavior).
DEFAULT_RANGE_DAYS = 30


# ── Shared helpers ────────────────────────────────────────────────────────────

def _parse_date_range(request, today):
    """
    Parse and validate the ``start`` / ``end`` GET parameters.

    Returns a ``(start_date_str, end_date_str)`` tuple of ISO-format date
    strings ready for use in ORM ``__date__gte`` / ``__date__lte`` filters.

    Validation rules (both views share identical logic):
    - Invalid or missing values fall back to the default 30-day window.
    - An inverted range (start > end) is silently swapped.

    Extracted to eliminate the verbatim duplication between
    ``reports_index`` and ``export_excel``.
    """
    raw_start = request.GET.get('start', '')
    raw_end   = request.GET.get('end',   '')
    try:
        start_date = date_cls.fromisoformat(raw_start).isoformat()
    except (ValueError, TypeError):
        start_date = (today - timedelta(days=30)).isoformat()
    try:
        end_date = date_cls.fromisoformat(raw_end).isoformat()
    except (ValueError, TypeError):
        end_date = today.isoformat()
    # Guard against inverted ranges (start after end) — swap silently.
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _parse_date_range_bounded(request, today, max_days=MAX_SALES_RANGE_DAYS):
    """
    H2 (Phase 7.4): existing ``_parse_date_range`` semantics plus a maximum
    span clamp.

    Returns ``(start_date_str, end_date_str, was_clamped)``. When the parsed
    span reaches ``max_days`` days, ``start`` is clamped to
    ``end - (max_days - 1)`` — the most recent ``max_days`` days of the
    requested window — and ``was_clamped`` is True so the page can show the
    effective-range notice. Invalid/missing inputs fall back to the default
    30-day window exactly as before; inverted ranges are swapped first.
    """
    start_date, end_date = _parse_date_range(request, today)
    start_d = date_cls.fromisoformat(start_date)
    end_d = date_cls.fromisoformat(end_date)
    if (end_d - start_d).days >= max_days:
        start_d = end_d - timedelta(days=max_days - 1)
        return start_d.isoformat(), end_date, True
    return start_date, end_date, False


def _movement_date_bounds(start_date_str, end_date_str):
    """
    Aware datetime bounds ``[start 00:00, end+1day 00:00)`` in the active
    timezone (Asia/Manila), matching the Phase 7.3 dashboard widget pattern.
    New report views filter raw datetimes with these bounds so the DB can use
    plain indexed datetime predicates instead of ``__date`` transforms.
    """
    start_d = parse_date(start_date_str)
    end_d = parse_date(end_date_str)
    start_dt = timezone.make_aware(datetime.combine(start_d, time_cls.min))
    end_dt = timezone.make_aware(
        datetime.combine(end_d + timedelta(days=1), time_cls.min)
    )
    return start_dt, end_dt


def _safe_text_cell(ws, row, col, value):
    """
    Write a display string as a strict text cell (H1, Phase 7.4).

    openpyxl 3.1.5 treats a str starting with '=' as a formula; forcing
    ``data_type='s'`` stores it as a string cell, so formula-leading
    characters ('=', '+', '-', '@') in user-controlled text can never
    execute. Verified by save/reopen probe (decision record §10-H1).
    """
    cell = ws.cell(row=row, column=col, value=str(value))
    cell.data_type = 's'
    return cell


def _base_orders_qs(start_date, end_date):
    """
    Return the base ``Order`` queryset shared by both report views.

    Applies the authoritative sales filter — ``is_paid=True`` AND
    ``status='completed'`` — that is consistent with Finance and Dashboard.
    Only the date range varies between calls; the eligibility criteria are
    defined once here so neither view can silently diverge from the other.

    ``reports_index``  uses the queryset directly for aggregations.
    ``export_excel``   chains ``.select_related('cashier').annotate(...)``
                       on top for the per-row Excel output.
    """
    return Order.objects.filter(
        is_paid=True,
        status='completed',
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )


# ── Views ─────────────────────────────────────────────────────────────────────

@login_required
@admin_required
def reports_index(request):
    today = timezone.localdate()
    # H2 (Phase 7.4): bounded parse — clamped range + notice flag.
    start_date, end_date, was_clamped = _parse_date_range_bounded(request, today)
    orders = _base_orders_qs(start_date, end_date)

    # OPT-1: single aggregate query returns both total_revenue and total_orders
    # in one DB round-trip instead of two (aggregate then count separately).
    agg = orders.aggregate(total=Sum('total'), count=Count('id'))
    total_revenue = agg['total'] or 0
    total_orders  = agg['count'] or 0

    # Sales by day — ONE grouped query for the whole range instead of two
    # queries per day (N+1). Days without sales are filled in below so the
    # chart keeps its zero entries.
    daily_sales = []
    start = date_cls.fromisoformat(start_date)
    end   = date_cls.fromisoformat(end_date)
    delta = end - start
    day_rows = {
        row['day']: row
        for row in orders.annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(total=Sum('total'), count=Count('id'))
    }
    for i in range(delta.days + 1):
        day = start + timedelta(days=i)
        row = day_rows.get(day) or {}
        daily_sales.append({
            'date':  day.strftime('%b %d'),
            'total': float(row.get('total') or 0),
            'count': row.get('count') or 0,
        })

    # Top products — ranked by revenue (existing intended behaviour).
    # Secondary sort on product_name makes ties alphabetically deterministic
    # so the same dataset always produces the same report order.
    # Direct join filters on OrderItem via order__ fields so the DB can use
    # the composite index on Order instead of an IN subquery.
    top_products = OrderItem.objects.filter(
        order__is_paid=True,
        order__status='completed',
        order__created_at__date__gte=start_date,
        order__created_at__date__lte=end_date,
    ).values('product_name').annotate(
        total_qty=Sum('quantity'),
        total_revenue=Sum('subtotal')
    ).order_by('-total_revenue', 'product_name')[:10]

    # Sales by category — groups by the category_name SNAPSHOT stored on each
    # OrderItem at the time the order was placed (migration 0012).
    #
    # Why snapshot instead of product__category__name (live join)?
    #   1. If a product is later deleted (product FK → NULL), the live join
    #      returns NULL and those items are silently dropped from the totals.
    #   2. If a product is moved to a different category after the order,
    #      the live join reclassifies the historical sale to the new category,
    #      making the old category lose revenue it actually earned.
    #
    # Items with a blank category_name are excluded so they don't produce
    # an unnamed row in the report.
    # Direct join (order__ fields) replaces the order__in subquery so the DB
    # can use the composite index on Order directly.
    category_sales = []
    for row in (
        OrderItem.objects
        .filter(
            order__is_paid=True,
            order__status='completed',
            order__created_at__date__gte=start_date,
            order__created_at__date__lte=end_date,
        )
        .exclude(category_name='')
        .values('category_name')
        .annotate(total=Sum('subtotal'), qty=Sum('quantity'))
        .order_by('-total')
    ):
        category_sales.append({
            'name':  row['category_name'],
            'total': float(row['total']),
            'qty':   row['qty'] or 0,
        })

    context = {
        'start_date':   start_date,
        'end_date':     end_date,
        'total_revenue': total_revenue,
        'total_orders':  total_orders,
        'daily_sales':   daily_sales,
        # True when at least one day in the range has a completed paid order.
        # Used by the template to show a "No sales in this period" placeholder
        # instead of a silently empty Daily Breakdown table/cards — the
        # {% empty %} block on the for loop cannot serve this purpose because
        # daily_sales is always non-empty (zero-filled for every day).
        'has_any_sales': any(d['count'] > 0 for d in daily_sales),
        'top_products':  top_products,
        'category_sales': category_sales,
        # Phase 7.4: H2 clamp notice + report tab strip.
        'was_clamped':   was_clamped,
        'max_range_days': MAX_SALES_RANGE_DAYS,
        'tab':           'sales',
    }
    return render(request, 'reports/index.html', context)


@login_required
@admin_required
def export_excel(request):
    """Export sales report to Excel."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return HttpResponse("openpyxl not installed", status=500)

    today = timezone.localdate()
    # H2 (Phase 7.4): same bounded parse as the page, so the workbook always
    # agrees with the clamped range the page shows.
    start_date, end_date, _was_clamped = _parse_date_range_bounded(request, today)

    orders = (
        _base_orders_qs(start_date, end_date)
        .select_related('cashier')
        .annotate(item_count=Count('items', distinct=True))
        .order_by('created_at')
    )

    # H3 (Phase 7.4): hard row cap — refuse with a clear message before any
    # workbook bytes are produced. No silent truncation path exists.
    if orders.count() > MAX_EXPORT_ROWS:
        return HttpResponse(
            f"Export too large: more than {MAX_EXPORT_ROWS:,} orders exceed "
            f"the export limit. Narrow the date range and try again.",
            status=400,
            content_type='text/html',
        )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales Report"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(fill_type="solid", fgColor="4A2C2A")

    headers = ['Order #', 'Date', 'Customer', 'Type',
               'Line items', 'Total', 'Payment', 'Cashier']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font  = header_font
        cell.fill  = header_fill
        cell.alignment = Alignment(horizontal='center')

    for row, order in enumerate(orders, 2):
        # All display-text cells go through _safe_text_cell (H1) so
        # user-controlled strings can never become formula cells.
        _safe_text_cell(ws, row, 1, order.order_number)
        # Convert stored UTC timestamp to Asia/Manila (PHT) before formatting
        # so the Excel "Date" column agrees with the date shown in the filter.
        pht_created = timezone.localtime(order.created_at)
        _safe_text_cell(ws, row, 2, pht_created.strftime('%Y-%m-%d %H:%M'))
        _safe_text_cell(ws, row, 3, order.customer_name)
        _safe_text_cell(ws, row, 4, order.get_order_type_display())
        _safe_text_cell(ws, row, 5, order.item_count)
        # Pass Decimal directly — openpyxl stores it as an exact number.
        # float(order.total) introduces IEEE 754 imprecision that can corrupt
        # Excel SUM formulas for centavo amounts like ₱123.10.
        # (H1: legitimate numeric cells stay numeric — never forced to text.)
        ws.cell(row=row, column=6, value=order.total)
        _safe_text_cell(ws, row, 7, order.get_payment_method_display())
        _safe_text_cell(ws, row, 8,
                        str(order.cashier) if order.cashier else 'N/A')

    # Auto-fit columns
    for col in ws.columns:
        max_length = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max_length + 4

    response = HttpResponse(
        content_type=(
            'application/vnd.openxmlformats-officedocument'
            '.spreadsheetml.sheet'
        )
    )
    response['Content-Disposition'] = (
        f'attachment; filename="sales_report_{start_date}_to_{end_date}.xlsx"'
    )
    wb.save(response)
    return response


# ── Phase 7.4 — read-only ingredient-ledger reports ──────────────────────────
# Decision record: docs/Phase 7.4 - Reports Decision Record.md (A1–A20).
# Both reports are admin-only, read-only aggregations. Neither writes to the
# database, neither feeds the other, and neither is a stock reconciliation.

def _format_quantity(value):
    """Render a 3-decimal quantity without unnecessary trailing zeros
    (Phase 7.3 dashboard convention)."""
    return format(value.normalize(), 'f')


def _humanize_reason(code):
    """Human label for a consumption reason_code (A4/A11).

    order_ready and cancelled_prepared get approved labels; any unexpected
    code renders under a visible, capitalized raw-code label — fail-visible,
    never silently dropped or reclassified (decision record §5.2)."""
    labels = {
        'order_ready': 'Prepared (order ready)',
        'cancelled_prepared': 'Cancelled after prep',
    }
    if code in labels:
        return labels[code]
    if code:
        return code.replace('_', ' ').capitalize()
    return 'Unspecified'


def _group_usage_rows(rows, unit_labels):
    """Group flat consumption aggregates into per-item-type unit sections.

    Input rows are dicts from one values()+Sum query. Output per item type:
    a list of unit blocks ``{base_unit, unit_label, rows, unit_total}`` where
    each row carries per-reason formatted figures and its own total. A row
    never mixes base units (A8); no cross-unit or cross-item-type total
    exists (A7)."""
    sections = []
    units = {}
    for r in rows:
        units.setdefault(r['base_unit'], {})
        units[r['base_unit']].setdefault(r['item_name'], []).append(r)
    for base_unit in sorted(units):
        item_rows = []
        unit_total = Decimal('0.000')
        for item_name in sorted(units[base_unit]):
            reasons = {}
            item_total = Decimal('0.000')
            for r in units[base_unit][item_name]:
                magnitude = -(r['total'] or Decimal('0.000'))  # positive (A3 shape: delta < 0)
                item_total += magnitude
                reasons.setdefault(r['reason_code'], Decimal('0.000'))
                reasons[r['reason_code']] += magnitude
            item_rows.append({
                'item_name': item_name,
                'order_ready': _format_quantity(reasons.get('order_ready', Decimal('0.000'))),
                'cancelled_prepared': _format_quantity(reasons.get('cancelled_prepared', Decimal('0.000'))),
                'other': [
                    {'label': _humanize_reason(code), 'qty': _format_quantity(qty)}
                    for code, qty in sorted(reasons.items())
                    if code not in ('order_ready', 'cancelled_prepared')
                ],
                'total': _format_quantity(item_total),
            })
            unit_total += item_total
        sections.append({
            'base_unit': base_unit,
            'unit_label': unit_labels.get(base_unit, base_unit),
            'rows': item_rows,
            'unit_total': _format_quantity(unit_total),
        })
    return sections


@login_required
@admin_required
def ingredient_usage(request):
    """Report A — physical ingredient and packaging consumption for a period.

    Source of truth: IngredientMovementLog, movement_type='consumption' only
    (A3). Quantity per row is |on_hand_delta| (shape CHECK guarantees the
    delta is negative); reserved_delta is never read (A9). Ingredients and
    packaging are reported as separate sections (A7) classified through the
    PROTECT inventory_item FK (decision record §6.1)."""
    today = timezone.localdate()
    start_date, end_date, _clamped = _parse_date_range_bounded(request, today)
    start_dt, end_dt = _movement_date_bounds(start_date, end_date)

    rows = list(
        IngredientMovementLog.objects
        .filter(
            movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION,  # A3
            created_at__gte=start_dt,   # A5 date basis; indexed range
            created_at__lt=end_dt,
        )
        .values(
            'inventory_item__item_type',  # §6.1 classification join
            'item_name',                  # write-time snapshot (survives renames)
            'base_unit',                  # write-time snapshot; grouping dimension
            'reason_code',                # order_ready / cancelled_prepared / other
        )
        .annotate(total=Sum('on_hand_delta'))
        .order_by('item_name', 'reason_code')
    )

    ingredient_rows = [r for r in rows if r['inventory_item__item_type'] == 'ingredient']
    packaging_rows = [r for r in rows if r['inventory_item__item_type'] == 'packaging']

    unit_labels = dict(InventoryItem.BASE_UNIT_CHOICES)
    context = {
        'start_date': start_date,
        'end_date': end_date,
        'ingredient_sections': _group_usage_rows(ingredient_rows, unit_labels),
        'packaging_sections': _group_usage_rows(packaging_rows, unit_labels),
        'has_any_consumption': bool(rows),
        'tab': 'ingredient',
    }
    return render(request, 'reports/ingredient_usage.html', context)


@login_required
@admin_required
def cancellation_dispositions(request):
    """Report B — product-unit cancellation dispositions for a period.

    Source of truth: OrderCancellationDisposition filtered on decided_at (A6).
    All recorded dispositions are included, including all-zero rows (A13).
    prepared/unprepared/discarded are three separate figures, never summed
    together (A14); ledger classification uses the persisted snapshot (A15);
    quantities stay in product units (A16)."""
    today = timezone.localdate()
    start_date, end_date, _clamped = _parse_date_range_bounded(request, today)
    start_dt, end_dt = _movement_date_bounds(start_date, end_date)

    rows = list(
        OrderCancellationDisposition.objects
        .filter(decided_at__gte=start_dt, decided_at__lt=end_dt)  # A6
        .values(
            'ledger',                   # A15 classification snapshot
            'source',                   # staff_declared / system_ready / system_unpaid
            'order_item__product_name',  # PROTECT join, snapshot name
        )
        .annotate(
            line_count=Count('id'),
            prepared_total=Sum('prepared_units'),
            unprepared_total=Sum('unprepared_units'),
            discarded_total=Sum('discarded_units'),   # A12 — display-only figure
            quantity_total=Sum('order_item_quantity'),
        )
        .order_by('ledger', 'order_item__product_name', 'source')
    )

    source_labels = dict(OrderCancellationDisposition.SOURCE_CHOICES)
    ledger_labels = {
        'legacy': 'Legacy stock ledger',
        'ingredient': 'Ingredient ledger',
    }

    groups = []
    for ledger in ('legacy', 'ingredient'):  # A15: legacy group first
        ledger_rows = [r for r in rows if r['ledger'] == ledger]
        sources = {}
        for r in ledger_rows:
            sources.setdefault(r['source'], []).append({
                'product_name': r['order_item__product_name'],
                'line_count': r['line_count'],
                'prepared': r['prepared_total'] or 0,
                'unprepared': r['unprepared_total'] or 0,
                'discarded': r['discarded_total'] or 0,
                'quantity': r['quantity_total'] or 0,
            })
        groups.append({
            'ledger': ledger,
            'label': ledger_labels.get(ledger, ledger),
            'sources': [
                {
                    'source': source,
                    'label': source_labels.get(source, source),
                    'rows': sorted(sources[source], key=lambda x: x['product_name']),
                }
                for source in sorted(sources)
            ],
            'has_rows': bool(ledger_rows),
        })

    context = {
        'start_date': start_date,
        'end_date': end_date,
        'ledger_groups': groups,
        'has_any_dispositions': bool(rows),
        'tab': 'cancellations',
    }
    return render(request, 'reports/cancellation_dispositions.html', context)
