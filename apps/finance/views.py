"""
Finance views — Daily Cash Reconciliation for Kape De Manubag.
"""
import datetime
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db import IntegrityError
from django.db.models import (
    Sum, Count, OuterRef, Subquery, DecimalField, ExpressionWrapper, F, Value,
)
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.utils import timezone

from apps.accounts.decorators import cashier_or_admin_required
from apps.audit.services import log_action
from apps.orders.models import Order
from .models import DailyFinance
from .forms import DailyFinanceForm


# ── Shared annotation helpers ─────────────────────────────────────────────────

def _cash_sales_subquery():
    """
    Returns a Subquery that aggregates completed cash-order totals for the
    date that matches the outer DailyFinance row.  Used in annotate() calls
    so the history tables compute cash_sales in SQL rather than via N Python
    property calls.
    """
    return Subquery(
        Order.objects.filter(
            created_at__date=OuterRef('date'),
            is_paid=True,
            payment_method='cash',
            status='completed',
        )
        .values('created_at__date')
        .annotate(total=Sum('total'))
        .values('total')[:1],
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def _gcash_sales_subquery():
    """
    Returns a Subquery that aggregates completed GCash-order totals for the
    date that matches the outer DailyFinance row.  Mirrors _cash_sales_subquery
    but filters for payment_method='gcash'.  Used in _annotate_history_qs so
    the running total includes both cash and GCash revenue in SQL.
    """
    return Subquery(
        Order.objects.filter(
            created_at__date=OuterRef('date'),
            is_paid=True,
            payment_method='gcash',
            status='completed',
        )
        .values('created_at__date')
        .annotate(total=Sum('total'))
        .values('total')[:1],
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def _annotate_history_qs(qs):
    """
    Attach annotations to a DailyFinance queryset:

    • annotated_cash_sales  — cash sales total from Order table
    • annotated_gcash_sales — GCash sales total from Order table
    • annotated_ending_coh  — ending COH computed entirely in SQL:
                              previous_coh + cash_sales + gcash_sales
                              - all deductions

    Using these annotations in templates avoids the N+1 pattern caused by
    calling the `ending_coh` Python property, which fires a DB query per row.
    """
    cash_sales_sq = _cash_sales_subquery()
    gcash_sales_sq = _gcash_sales_subquery()

    # COALESCE so dates with zero orders contribute 0, not NULL
    cash_sales_expr = Coalesce(
        cash_sales_sq,
        Value(Decimal('0.00')),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )
    gcash_sales_expr = Coalesce(
        gcash_sales_sq,
        Value(Decimal('0.00')),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )
    ending_coh_expr = ExpressionWrapper(
        F('previous_coh')
        + cash_sales_expr
        + gcash_sales_expr
        - F('expenses')
        - F('gcash_payments')
        - F('coins')
        - F('cash_advance')
        - F('floating_cash'),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )
    return qs.annotate(
        annotated_cash_sales=cash_sales_expr,
        annotated_gcash_sales=gcash_sales_expr,
        annotated_ending_coh=ending_coh_expr,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_cash_sales_for_date(date):
    """Return (total Decimal, order count int) for a given date.

    OPT-1: single aggregate query — Sum and Count in one DB round-trip instead
    of two separate queries (.aggregate() then .count()).
    """
    result = Order.objects.filter(
        created_at__date=date,
        is_paid=True,
        payment_method='cash',
        status='completed',
    ).aggregate(
        total=Sum('total'),
        count=Count('id'),
    )
    return result['total'] or Decimal('0.00'), result['count'] or 0


def _get_gcash_sales_for_date(date):
    """Return (total Decimal, order count int) for GCash orders on a given date.

    Mirrors _get_cash_sales_for_date but filters for payment_method='gcash'.
    GCash revenue is shown as a deduction hint on the Finance index page —
    it does NOT contribute to running_total (which counts physical cash only).
    Uses a single aggregate query returning both Sum and Count.
    """
    result = Order.objects.filter(
        created_at__date=date,
        is_paid=True,
        payment_method='gcash',
        status='completed',
    ).aggregate(
        total=Sum('total'),
        count=Count('id'),
    )
    return result['total'] or Decimal('0.00'), result['count'] or 0


def _get_previous_coh_info(selected_date):
    """
    Returns (suggested_value, source_label, is_auto).
    Looks for yesterday's record first, then the most recent past record.
    Uses the model's ending_coh property (single-record lookup, not a list —
    no N+1 concern here).
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
    today = timezone.localdate()

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

    # GCash sales for selected date — OPT-2: single aggregate query
    gcash_sales, gcash_order_count = _get_gcash_sales_for_date(selected_date)

    # ── POST: save / update ───────────────────────────────────────────────────
    if request.method == 'POST':
        if existing_record:
            form = DailyFinanceForm(request.POST, instance=existing_record)
        else:
            form = DailyFinanceForm(request.POST)

        if form.is_valid():
            record = form.save(commit=False)

            # ── BUG-1 guard: date-lock on updates ────────────────────────────
            # DailyFinanceForm drops the date field when updating an existing
            # instance (see forms.py __init__), so the form never writes a new
            # date on the record.  This view-level check is a belt-and-
            # suspenders guard: if the saved record's date diverges from the
            # URL's selected_date (e.g. a crafted POST that somehow bypasses
            # the form), we refuse the save rather than silently moving the
            # record to a different day and corrupting the COH chain.
            if existing_record and record.date != selected_date:
                form.add_error(
                    None,
                    "The date of an existing finance record cannot be changed.",
                )
            else:
                if not existing_record:
                    record.prepared_by = request.user

                # ── BUG-2 fix: only set the manual flag on CREATE ─────────────
                # On an update the flag must reflect the ORIGINAL save context,
                # not the current state of whether yesterday now has a record.
                # Re-evaluating it on every save would flip the flag if a
                # previous-day record was created after this one was first saved.
                if not existing_record:
                    record.previous_coh_is_manual = not previous_coh_is_auto

                try:
                    record.save()
                    log_action(
                        request.user,
                        'finance.update' if existing_record else 'finance.create',
                        record,
                    )
                    messages.success(
                        request,
                        f"Finance record for {record.date} saved successfully."
                    )
                    return redirect(f"{request.path}?date={record.date}")
                except IntegrityError:
                    # Two concurrent requests both reached the INSERT path
                    # (neither saw an existing record when they loaded the page).
                    # The DB unique constraint on DailyFinance.date stopped the
                    # second one from creating a duplicate — no data was lost or
                    # corrupted.  Treat this as a transparent idempotent save:
                    # reload the record the first request created and redirect to
                    # it so the user sees a normal success state instead of 500.
                    saved_record = DailyFinance.objects.filter(
                        date=selected_date
                    ).first()
                    if saved_record:
                        messages.success(
                            request,
                            f"Finance record for {saved_record.date} "
                            "saved successfully."
                        )
                        return redirect(
                            f"{request.path}?date={saved_record.date}"
                        )
                    # Extremely unlikely: constraint fired but record still not
                    # found (e.g. immediately deleted by another process).
                    # Fall through to a generic error rather than crashing.
                    messages.error(
                        request,
                        "The finance record could not be saved. Please try again.",
                    )
                    return redirect(f"{request.path}?date={selected_date}")
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
    # _annotate_history_qs adds annotated_cash_sales and annotated_ending_coh
    # in a single SQL pass, eliminating the N+1 caused by rec.ending_coh in
    # the template calling get_cash_sales() per row.
    finance_history = (
        _annotate_history_qs(
            DailyFinance.objects.select_related('prepared_by')
        )
        .order_by('-date')[:31]
    )

    # Compute running_total for the form display
    prev_coh_display = (
        existing_record.previous_coh
        if existing_record else previous_coh_suggested
    )
    running_total = prev_coh_display + cash_sales + gcash_sales

    context = {
        'form': form,
        'selected_date': selected_date,
        'today': today,
        'existing_record': existing_record,
        'cash_sales': cash_sales,
        'order_count': order_count,
        'gcash_sales': gcash_sales,
        'gcash_order_count': gcash_order_count,
        # previous_coh_suggested: the raw value from yesterday's ending_coh.
        # Used by COH-chain tests to assert carry-forward correctness.
        # Templates use prev_coh_display instead (which equals this when no
        # existing record is present, or the stored previous_coh on edit).
        'previous_coh_suggested': previous_coh_suggested,
        'previous_coh_source': previous_coh_source,
        'previous_coh_is_auto': previous_coh_is_auto,
        # prev_coh_display: the value rendered in the UI.
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

    # GCash sales — OPT-2: single aggregate query via shared helper
    gcash_total, gcash_count = _get_gcash_sales_for_date(date)

    return JsonResponse({
        # String values preserve Decimal precision for JS parseFloat().
        # float() conversion can introduce IEEE 754 imprecision on amounts
        # like ₱123.10 → 123.09999999999999.  parseFloat("123.10") is exact.
        'cash_sales':           str(cash_total),
        'cash_sales_formatted': f'₱{cash_total:.2f}',
        'cash_order_count':     cash_count,
        'gcash_sales':           str(gcash_total),
        'gcash_sales_formatted': f'₱{gcash_total:.2f}',
        'gcash_order_count':     gcash_count,
    })


# ── View 3: Print Report ──────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def finance_print(request, pk):
    record = get_object_or_404(DailyFinance, pk=pk)

    # OPT-3: call _get_cash_sales_for_date exactly ONCE and derive all
    # dependent values from it.  The previous implementation delegated to
    # record.cash_sales / record.running_total / record.ending_coh, which
    # each call get_cash_sales() internally — executing the same DB query
    # three times.  We compute the formula here using the model's field
    # values; the arithmetic is identical to the model properties.
    cash_sales, order_count = _get_cash_sales_for_date(record.date)
    gcash_sales, gcash_order_count = _get_gcash_sales_for_date(record.date)
    running_total    = record.previous_coh + cash_sales + gcash_sales
    total_deductions = record.total_deductions  # pure Python, no DB query
    ending_coh       = running_total - total_deductions

    context = {
        'record': record,
        'cash_sales':         cash_sales,
        'order_count':        order_count,
        'gcash_sales':        gcash_sales,
        'gcash_order_count':  gcash_order_count,
        'running_total':      running_total,
        'total_deductions':   total_deductions,
        'ending_coh':         ending_coh,
        'printed_at':         timezone.now(),
    }
    return render(request, 'finance/print.html', context)


# ── View 4: Finance History ───────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def finance_history(request):
    # _annotate_history_qs adds annotated_cash_sales and annotated_ending_coh
    # in a single SQL pass — no N+1 from rec.ending_coh in the template.
    records = (
        _annotate_history_qs(
            DailyFinance.objects.select_related('prepared_by')
        )
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
