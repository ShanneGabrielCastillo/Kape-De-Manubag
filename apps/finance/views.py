"""
Finance views — Daily Cash Reconciliation for Kape De Manubag.
"""
import datetime
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Sum, OuterRef, Subquery, DecimalField
from django.core.paginator import Paginator
from django.utils import timezone

from apps.accounts.decorators import cashier_or_admin_required
from apps.orders.models import Order
from .models import DailyFinance
from .forms import DailyFinanceForm


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_cash_sales_for_date(date):
    """Return (total Decimal, order count int) for a given date."""
    qs = Order.objects.filter(
        created_at__date=date,
        is_paid=True,
        payment_method='cash',
        status='completed',
    )
    total = qs.aggregate(total=Sum('total'))['total'] or Decimal('0.00')
    count = qs.count()
    return total, count


def _get_previous_coh_info(selected_date):
    """
    Returns (suggested_value, source_label, is_auto).
    Looks for yesterday's record first, then the most recent past record.
    """
    yesterday = selected_date - datetime.timedelta(days=1)
    try:
        prev = DailyFinance.objects.get(date=yesterday)
        return prev.ending_coh, f"From {yesterday}", True
    except DailyFinance.DoesNotExist:
        pass

    most_recent = (
        DailyFinance.objects
        .filter(date__lt=selected_date)
        .order_by('-date')
        .first()
    )
    if most_recent:
        return (
            most_recent.ending_coh,
            f"Last record: {most_recent.date}",
            False,
        )
    return Decimal('0.00'), "No previous record found", False


# ── View 1: Finance Index ─────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def finance_index(request):
    today = timezone.now().date()

    # Resolve selected date from GET param
    date_str = request.GET.get('date', '')
    try:
        selected_date = datetime.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        selected_date = today

    # Try to load existing record for this date
    existing_record = DailyFinance.objects.filter(date=selected_date).first()

    # Previous COH suggestion
    previous_coh_suggested, previous_coh_source, previous_coh_is_auto = (
        _get_previous_coh_info(selected_date)
    )

    # Cash sales for selected date
    cash_sales, order_count = _get_cash_sales_for_date(selected_date)

    # GCash sales for selected date (for suggestion only — not in running total)
    gcash_qs = Order.objects.filter(
        created_at__date=selected_date,
        is_paid=True,
        payment_method='gcash',
        status='completed',
    )
    gcash_sales = gcash_qs.aggregate(total=Sum('total'))['total'] or Decimal('0.00')
    gcash_order_count = gcash_qs.count()

    # ── POST: save / update ───────────────────────────────────────────────────
    if request.method == 'POST':
        if existing_record:
            form = DailyFinanceForm(request.POST, instance=existing_record)
        else:
            form = DailyFinanceForm(request.POST)

        if form.is_valid():
            record = form.save(commit=False)
            if not existing_record:
                record.prepared_by = request.user
            record.previous_coh_is_manual = not previous_coh_is_auto
            record.save()
            messages.success(
                request,
                f"Finance record for {record.date} saved successfully."
            )
            return redirect(f"{request.path}?date={record.date}")
        # fall through to render with form errors
    else:
        # ── GET: pre-populate ─────────────────────────────────────────────────
        if existing_record:
            form = DailyFinanceForm(instance=existing_record)
        else:
            form = DailyFinanceForm(initial={
                'date': selected_date,
                'previous_coh': previous_coh_suggested,
            })

    # ── Finance history (last 31 records) ─────────────────────────────────────
    cash_sales_subquery = (
        Order.objects.filter(
            created_at__date=OuterRef('date'),
            is_paid=True,
            payment_method='cash',
            status='completed',
        )
        .values('created_at__date')
        .annotate(total=Sum('total'))
        .values('total')[:1]
    )

    finance_history = (
        DailyFinance.objects
        .annotate(
            annotated_cash_sales=Subquery(
                cash_sales_subquery,
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .select_related('prepared_by')
        .order_by('-date')[:31]
    )

    # Compute running_total for the form display
    prev_coh_display = (
        existing_record.previous_coh
        if existing_record else previous_coh_suggested
    )
    running_total = prev_coh_display + cash_sales

    context = {
        'form': form,
        'selected_date': selected_date,
        'today': today,
        'existing_record': existing_record,
        'cash_sales': cash_sales,
        'order_count': order_count,
        'gcash_sales': gcash_sales,
        'gcash_order_count': gcash_order_count,
        'previous_coh_suggested': previous_coh_suggested,
        'previous_coh_source': previous_coh_source,
        'previous_coh_is_auto': previous_coh_is_auto,
        'prev_coh_display': prev_coh_display,
        'running_total': running_total,
        'finance_history': finance_history,
    }
    return render(request, 'finance/index.html', context)


# ── View 2: AJAX Cash Sales ───────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def finance_api_cash_sales(request):
    date_str = request.GET.get('date', '')
    try:
        date = datetime.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid date format. Use YYYY-MM-DD.'}, status=400)

    cash_total, cash_count = _get_cash_sales_for_date(date)

    # GCash sales for the same date
    gcash_qs = Order.objects.filter(
        created_at__date=date,
        is_paid=True,
        payment_method='gcash',
        status='completed',
    )
    gcash_total = gcash_qs.aggregate(total=Sum('total'))['total'] or Decimal('0.00')
    gcash_count = gcash_qs.count()

    return JsonResponse({
        'cash_sales':           float(cash_total),
        'cash_sales_formatted': f'₱{cash_total:.2f}',
        'cash_order_count':     cash_count,
        'gcash_sales':           float(gcash_total),
        'gcash_sales_formatted': f'₱{gcash_total:.2f}',
        'gcash_order_count':     gcash_count,
    })


# ── View 3: Print Report ──────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def finance_print(request, pk):
    record = get_object_or_404(DailyFinance, pk=pk)
    cash_sales, order_count = _get_cash_sales_for_date(record.date)
    running_total = record.previous_coh + cash_sales
    total_deductions = (
        record.expenses + record.gcash_payments + record.coins +
        record.cash_advance + record.floating_cash
    )
    ending_coh = running_total - total_deductions

    context = {
        'record': record,
        'cash_sales': cash_sales,
        'order_count': order_count,
        'running_total': running_total,
        'total_deductions': total_deductions,
        'ending_coh': ending_coh,
        'printed_at': timezone.now(),
    }
    return render(request, 'finance/print.html', context)


# ── View 4: Finance History ───────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def finance_history(request):
    cash_sales_subquery = (
        Order.objects.filter(
            created_at__date=OuterRef('date'),
            is_paid=True,
            payment_method='cash',
            status='completed',
        )
        .values('created_at__date')
        .annotate(total=Sum('total'))
        .values('total')[:1]
    )

    records = (
        DailyFinance.objects
        .annotate(
            annotated_cash_sales=Subquery(
                cash_sales_subquery,
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .select_related('prepared_by')
        .order_by('-date')
    )

    # Month filter
    month_str = request.GET.get('month', '')
    if month_str:
        try:
            year, month = month_str.split('-')
            records = records.filter(
                date__year=int(year),
                date__month=int(month),
            )
        except (ValueError, AttributeError):
            pass

    # Search by date
    q = request.GET.get('q', '').strip()
    if q:
        records = records.filter(date__icontains=q)

    paginator = Paginator(records, 31)
    page = request.GET.get('page', 1)
    records_page = paginator.get_page(page)

    context = {
        'records': records_page,
        'month_filter': month_str,
        'search': q,
        'show_history_only': True,
    }
    return render(request, 'finance/history.html', context)
