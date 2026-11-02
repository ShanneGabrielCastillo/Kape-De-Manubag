from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q
from django.http import JsonResponse
from django.core.paginator import Paginator
from .models import InventoryItem, InventoryLog
from .forms import InventoryItemForm, AdjustmentForm, WastageForm, PackagingItemForm
from .services import (
    record_adjustment,
    record_wastage,
    InventoryServiceError,
    StaleExpectedQuantityError,
    ReservedConflictError,
    ExceedsAvailableError,
)
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


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2B-A — Ingredient Inventory Listing and Creation
# ═══════════════════════════════════════════════════════════════════════════════


@login_required
@cashier_or_admin_required
def ingredient_list(request):
    """
    List all InventoryItem records (ingredients and packaging).

    Summary cards reflect the full catalog before any filter is applied.
    Supports text search (name icontains) and item_type filter.
    """
    base_qs = InventoryItem.objects.order_by('item_type', 'name')

    # Summary counts computed on the full queryset before filtering.
    counts = base_qs.aggregate(
        total=Count('id'),
        ingredients=Count('id', filter=Q(item_type='ingredient')),
        packaging=Count('id', filter=Q(item_type='packaging')),
        inactive=Count('id', filter=Q(is_active=False)),
    )

    # Low-stock: available_quantity <= low_stock_threshold and is_active.
    # available_quantity is derived (on_hand - reserved), computed in Python
    # because it is not a stored column.  For the count card we compute it
    # via annotation.
    annotated = base_qs.filter(is_active=True).annotate(
        available=ExpressionWrapper(
            F('on_hand_quantity') - F('reserved_quantity'),
            output_field=DecimalField(max_digits=14, decimal_places=3),
        )
    )
    low_stock_count = annotated.filter(
        available__lte=F('low_stock_threshold')
    ).count()

    items = base_qs

    # ── Text search ───────────────────────────────────────────────────────────
    q = request.GET.get('q', '').strip()
    if q:
        items = items.filter(name__icontains=q)

    # ── Type filter ───────────────────────────────────────────────────────────
    VALID_TYPES = ('ingredient', 'packaging')
    type_filter = request.GET.get('type', '').strip()
    if type_filter not in VALID_TYPES:
        type_filter = ''
    if type_filter:
        items = items.filter(item_type=type_filter)

    # ── Active/inactive filter ────────────────────────────────────────────────
    VALID_STATUS = ('active', 'inactive')
    status_filter = request.GET.get('status', '').strip()
    if status_filter not in VALID_STATUS:
        status_filter = ''
    if status_filter == 'active':
        items = items.filter(is_active=True)
    elif status_filter == 'inactive':
        items = items.filter(is_active=False)

    return render(request, 'inventory/ingredient_list.html', {
        'items': items,
        'total_count': counts['total'],
        'ingredient_count': counts['ingredients'],
        'packaging_count': counts['packaging'],
        'inactive_count': counts['inactive'],
        'low_stock_count': low_stock_count,
        'q': q,
        'type_filter': type_filter,
        'status_filter': status_filter,
    })


@login_required
@cashier_or_admin_required
def ingredient_create(request):
    """
    Create a new InventoryItem (ingredient or packaging material).

    GET:  renders an empty InventoryItemForm.
    POST: validates, saves, audits, and redirects to ingredient_list.

    Uses the model's built-in full_clean() validation so all DB constraints
    (case-insensitive name uniqueness, non-negative quantities, valid type/unit)
    surface as user-friendly form errors rather than 500 errors.
    """
    if request.method == 'POST':
        form = InventoryItemForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    item = form.save()
                    log_action(
                        request.user,
                        'inventory.ingredient_create',
                        item,
                        detail=(
                            f'{item.get_item_type_display()} "{item.name}" '
                            f'({item.on_hand_quantity} {item.base_unit} on hand)'
                        ),
                    )
            except IntegrityError:
                # Catches DB-level constraint violations (e.g. a duplicate
                # name that slipped past model validation on a race condition).
                messages.error(
                    request,
                    'Could not save the item due to a database conflict. '
                    'The name may already exist. Please check and try again.',
                )
                return render(request, 'inventory/ingredient_form.html', {
                    'form': form,
                    'title': 'Add Ingredient / Packaging',
                })
            messages.success(
                request,
                f'"{item.name}" has been added to the ingredient inventory.',
            )
            return redirect('inventory:ingredient_list')
    else:
        form = InventoryItemForm()

    return render(request, 'inventory/ingredient_form.html', {
        'form': form,
        'title': 'Add Ingredient / Packaging',
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2B-B — Ingredient Stock Adjustment and Wastage Recording
# ═══════════════════════════════════════════════════════════════════════════════


@login_required
@cashier_or_admin_required
def ingredient_adjust(request, pk):
    """
    Adjust the physical on-hand quantity of an InventoryItem.

    Uses record_adjustment() from services.py, which:
      - Accepts the desired *new absolute* on-hand quantity (not a delta).
      - Requires the value the staff member last read (expected_on_hand) as
        an optimistic-concurrency guard.
      - Raises StaleExpectedQuantityError if someone else changed stock since
        the page was loaded — the user must reload and try again.
      - Raises ReservedConflictError if the new quantity would fall below the
        currently reserved quantity.
      - Raises InventoryServiceError for a no-op or negative target.

    The form carries a hidden `expected_on_hand` field pre-filled at GET time.
    POST reads it back and passes it to the service.
    """
    item = get_object_or_404(InventoryItem, pk=pk,
                            item_type=InventoryItem.ITEM_TYPE_INGREDIENT)

    if request.method == 'POST':
        form = AdjustmentForm(request.POST, item=item)
        if form.is_valid():
            new_on_hand     = form.cleaned_data['new_on_hand']
            expected        = form.cleaned_data['expected_on_hand']
            reason_code     = form.cleaned_data['reason_code']
            note            = form.cleaned_data['note']
            try:
                with transaction.atomic():
                    movement = record_adjustment(
                        inventory_item_id=item.pk,
                        new_on_hand=new_on_hand,
                        expected_on_hand=expected,
                        reason_code=reason_code,
                        note=note,
                        performed_by=request.user,
                    )
                    log_action(
                        request.user,
                        'inventory.ingredient_adjust',
                        item,
                        detail=(
                            f'"{item.name}": {movement.on_hand_before} → '
                            f'{movement.on_hand_after} {item.base_unit} '
                            f'(reason: {reason_code or note or "—"})'
                        ),
                    )
            except StaleExpectedQuantityError as exc:
                messages.warning(request, str(exc))
                return redirect('inventory:ingredient_adjust', pk=pk)
            except (ReservedConflictError, InventoryServiceError) as exc:
                messages.error(request, str(exc))
                return render(request, 'inventory/ingredient_adjust.html', {
                    'item': item,
                    'form': form,
                })
            messages.success(
                request,
                f'Stock for "{item.name}" adjusted to '
                f'{movement.on_hand_after} {item.base_unit}.',
            )
            return redirect('inventory:ingredient_list')
    else:
        form = AdjustmentForm(item=item)

    return render(request, 'inventory/ingredient_adjust.html', {
        'item': item,
        'form': form,
    })


@login_required
@cashier_or_admin_required
def ingredient_record_wastage(request, pk):
    """
    Record a wastage / spoilage event for an InventoryItem.

    Uses record_wastage() from services.py, which:
      - Accepts a positive quantity to write off from available (non-reserved)
        stock.
      - Raises ExceedsAvailableError if the quantity exceeds (on_hand - reserved).
      - Raises InventoryServiceError for zero or negative quantities.

    Wastage only reduces on_hand; reserved_quantity is never touched here.
    Waste of reserved stock must go through order cancellation.
    """
    item = get_object_or_404(InventoryItem, pk=pk,
                            item_type=InventoryItem.ITEM_TYPE_INGREDIENT)

    if request.method == 'POST':
        form = WastageForm(request.POST, item=item)
        if form.is_valid():
            quantity    = form.cleaned_data['quantity']
            reason_code = form.cleaned_data['reason_code']
            note        = form.cleaned_data['note']
            try:
                with transaction.atomic():
                    movement = record_wastage(
                        inventory_item_id=item.pk,
                        quantity=quantity,
                        reason_code=reason_code,
                        note=note,
                        performed_by=request.user,
                    )
                    log_action(
                        request.user,
                        'inventory.ingredient_wastage',
                        item,
                        detail=(
                            f'"{item.name}": −{quantity} {item.base_unit} wasted '
                            f'(reason: {reason_code or note or "—"})'
                        ),
                    )
            except (ExceedsAvailableError, InventoryServiceError) as exc:
                messages.error(request, str(exc))
                return render(request, 'inventory/ingredient_wastage.html', {
                    'item': item,
                    'form': form,
                })
            messages.success(
                request,
                f'{quantity} {item.base_unit} of "{item.name}" recorded as wastage.',
            )
            return redirect('inventory:ingredient_list')
    else:
        form = WastageForm(item=item)

    return render(request, 'inventory/ingredient_wastage.html', {
        'item': item,
        'form': form,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2B-C — Packaging Inventory Management
# ═══════════════════════════════════════════════════════════════════════════════


@login_required
@cashier_or_admin_required
def packaging_list(request):
    """
    List all InventoryItem records whose item_type is 'packaging'.

    Mirrors ingredient_list but is filtered to packaging items only.
    Summary cards, search, and status filters follow the same pattern.
    """
    base_qs = InventoryItem.objects.filter(
        item_type=InventoryItem.ITEM_TYPE_PACKAGING
    ).order_by('name')

    counts = base_qs.aggregate(
        total=Count('id'),
        inactive=Count('id', filter=Q(is_active=False)),
    )

    annotated = base_qs.filter(is_active=True).annotate(
        available=ExpressionWrapper(
            F('on_hand_quantity') - F('reserved_quantity'),
            output_field=DecimalField(max_digits=14, decimal_places=3),
        )
    )
    low_stock_count = annotated.filter(
        available__lte=F('low_stock_threshold')
    ).count()

    items = base_qs

    q = request.GET.get('q', '').strip()
    if q:
        items = items.filter(name__icontains=q)

    VALID_STATUS = ('active', 'inactive')
    status_filter = request.GET.get('status', '').strip()
    if status_filter not in VALID_STATUS:
        status_filter = ''
    if status_filter == 'active':
        items = items.filter(is_active=True)
    elif status_filter == 'inactive':
        items = items.filter(is_active=False)

    return render(request, 'inventory/packaging_list.html', {
        'items': items,
        'total_count': counts['total'],
        'inactive_count': counts['inactive'],
        'low_stock_count': low_stock_count,
        'q': q,
        'status_filter': status_filter,
    })


@login_required
@cashier_or_admin_required
def packaging_create(request):
    """
    Create a new packaging InventoryItem.

    The item_type is forced to 'packaging' server-side regardless of
    any submitted value — the form does not expose item_type at all.
    This prevents creating an ingredient through this endpoint.
    """
    if request.method == 'POST':
        form = PackagingItemForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    item = InventoryItem.objects.create(
                        name=form.cleaned_data['name'],
                        item_type=InventoryItem.ITEM_TYPE_PACKAGING,   # forced, not from form
                        base_unit=form.cleaned_data['base_unit'],
                        on_hand_quantity=form.cleaned_data['on_hand_quantity'],
                        low_stock_threshold=form.cleaned_data['low_stock_threshold'],
                        description=form.cleaned_data.get('description', ''),
                        is_active=form.cleaned_data.get('is_active', True),
                        # reserved_quantity defaults to 0 — never set through this form
                    )
                    log_action(
                        request.user,
                        'inventory.packaging_create',
                        item,
                        detail=(
                            f'Packaging "{item.name}" '
                            f'({item.on_hand_quantity} {item.base_unit} on hand)'
                        ),
                    )
            except IntegrityError:
                messages.error(
                    request,
                    'Could not save the item due to a database conflict. '
                    'The name may already exist. Please check and try again.',
                )
                return render(request, 'inventory/packaging_form.html', {
                    'form': form,
                    'title': 'Add Packaging Item',
                })
            messages.success(
                request,
                f'"{item.name}" has been added to the packaging inventory.',
            )
            return redirect('inventory:packaging_list')
    else:
        form = PackagingItemForm()

    return render(request, 'inventory/packaging_form.html', {
        'form': form,
        'title': 'Add Packaging Item',
    })


@login_required
@cashier_or_admin_required
def packaging_adjust(request, pk):
    """
    Adjust the physical on-hand quantity of a packaging InventoryItem.

    Identical logic to ingredient_adjust, scoped to packaging items.
    Uses record_adjustment() — the only permitted stock-adjustment path.
    """
    item = get_object_or_404(InventoryItem, pk=pk, item_type=InventoryItem.ITEM_TYPE_PACKAGING)

    if request.method == 'POST':
        form = AdjustmentForm(request.POST, item=item)
        if form.is_valid():
            new_on_hand = form.cleaned_data['new_on_hand']
            expected    = form.cleaned_data['expected_on_hand']
            reason_code = form.cleaned_data['reason_code']
            note        = form.cleaned_data['note']
            try:
                with transaction.atomic():
                    movement = record_adjustment(
                        inventory_item_id=item.pk,
                        new_on_hand=new_on_hand,
                        expected_on_hand=expected,
                        reason_code=reason_code,
                        note=note,
                        performed_by=request.user,
                    )
                    log_action(
                        request.user,
                        'inventory.packaging_adjust',
                        item,
                        detail=(
                            f'"{item.name}": {movement.on_hand_before} → '
                            f'{movement.on_hand_after} {item.base_unit} '
                            f'(reason: {reason_code or note or "—"})'
                        ),
                    )
            except StaleExpectedQuantityError as exc:
                messages.warning(request, str(exc))
                return redirect('inventory:packaging_adjust', pk=pk)
            except (ReservedConflictError, InventoryServiceError) as exc:
                messages.error(request, str(exc))
                return render(request, 'inventory/packaging_adjust.html', {
                    'item': item,
                    'form': form,
                })
            messages.success(
                request,
                f'Stock for "{item.name}" adjusted to '
                f'{movement.on_hand_after} {item.base_unit}.',
            )
            return redirect('inventory:packaging_list')
    else:
        form = AdjustmentForm(item=item)

    return render(request, 'inventory/packaging_adjust.html', {
        'item': item,
        'form': form,
    })


@login_required
@cashier_or_admin_required
def packaging_record_wastage(request, pk):
    """
    Record a wastage event for a packaging InventoryItem.

    Identical logic to ingredient_record_wastage, scoped to packaging items.
    Uses record_wastage() — the only permitted wastage path.
    """
    item = get_object_or_404(InventoryItem, pk=pk, item_type=InventoryItem.ITEM_TYPE_PACKAGING)

    if request.method == 'POST':
        form = WastageForm(request.POST, item=item)
        if form.is_valid():
            quantity    = form.cleaned_data['quantity']
            reason_code = form.cleaned_data['reason_code']
            note        = form.cleaned_data['note']
            try:
                with transaction.atomic():
                    movement = record_wastage(
                        inventory_item_id=item.pk,
                        quantity=quantity,
                        reason_code=reason_code,
                        note=note,
                        performed_by=request.user,
                    )
                    log_action(
                        request.user,
                        'inventory.packaging_wastage',
                        item,
                        detail=(
                            f'"{item.name}": −{quantity} {item.base_unit} wasted '
                            f'(reason: {reason_code or note or "—"})'
                        ),
                    )
            except (ExceedsAvailableError, InventoryServiceError) as exc:
                messages.error(request, str(exc))
                return render(request, 'inventory/packaging_wastage.html', {
                    'item': item,
                    'form': form,
                })
            messages.success(
                request,
                f'{quantity} {item.base_unit} of "{item.name}" recorded as wastage.',
            )
            return redirect('inventory:packaging_list')
    else:
        form = WastageForm(item=item)

    return render(request, 'inventory/packaging_wastage.html', {
        'item': item,
        'form': form,
    })
