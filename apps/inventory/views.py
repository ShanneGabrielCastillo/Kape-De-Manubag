from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q
from django.http import JsonResponse
from django.core.paginator import Paginator
from .models import InventoryItem, InventoryLog, UnitConversion, StockPurchase, StockPurchaseItem
from .forms import (
    InventoryItemForm,
    AdjustmentForm,
    WastageForm,
    PackagingItemForm,
    PurchaseHeaderForm,
    PurchaseLineForm,
)
from .services import (
    record_adjustment,
    record_wastage,
    InventoryServiceError,
    StaleExpectedQuantityError,
    ReservedConflictError,
    ExceedsAvailableError,
    FractionalPcsError,
    PurchaseLineError,
    record_purchase,
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


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3-3 — Purchase Management UI
# ═══════════════════════════════════════════════════════════════════════════════


@login_required
@cashier_or_admin_required
def purchase_create(request):
    """
    Record a new stock purchase with one or more purchase lines.

    GET:  Renders the purchase form with empty header and no lines.
          Generates a fresh UUID idempotency key embedded in the header form.

    POST: Validates header + line data.  For each line, looks up the
          UnitConversion server-side (never trusts a client-submitted factor).
          Calls record_purchase() with the resolved data.  On success, redirects
          to the purchase detail page.

    Idempotency key design:
    - The UUID is a field on PurchaseHeaderForm (hidden), generated on GET and
      preserved across POST re-renders via the form's own POST data.
    - It is validated as a real UUID4 by the form's clean_idempotency_key().
    - A network retry that re-POSTs the same form body will carry the same key;
      record_purchase() handles that safely.
    - A browser refresh of a successful redirect lands on the detail page (302),
      so there is no automatic re-submission.
    - A deliberate manual reload and re-submit on the create page would re-POST
      the same key. record_purchase() idempotency ensures this is safe: the
      service returns the already-committed purchase unchanged.
    - Disabling the submit button is a convenience, not a security boundary.
      The service is the authoritative safeguard.

    Form data preservation on validation failure:
    - Header form fields (date, notes) are preserved via bound form data.
    - Line data is passed back through 'line_initial_json' so the JS can
      reinstate item, unit, and quantity selections.
    - Errors are displayed per-line using messages.
    """
    import uuid
    import json

    if request.method == 'POST':
        header_form = PurchaseHeaderForm(request.POST)

        # Parse line data from POST.  Lines are submitted as:
        #   lines[0][inventory_item_id], lines[0][purchase_unit_id],
        #   lines[0][purchase_quantity], lines[0][note], …
        line_forms = []
        idx = 0
        while f'lines[{idx}][inventory_item_id]' in request.POST:
            prefix_data = {
                'inventory_item_id': request.POST.get(f'lines[{idx}][inventory_item_id]', ''),
                'purchase_unit_id':  request.POST.get(f'lines[{idx}][purchase_unit_id]', ''),
                'purchase_quantity': request.POST.get(f'lines[{idx}][purchase_quantity]', ''),
                'note':              request.POST.get(f'lines[{idx}][note]', ''),
            }
            line_forms.append(PurchaseLineForm(prefix_data))
            idx += 1

        # Idempotency key comes from the validated header form.
        # We peek at the raw POST value before full validation so we can
        # return it on re-render even if other header fields failed.
        raw_key = request.POST.get('idempotency_key', '').strip()
        try:
            uuid.UUID(raw_key, version=4)
            idempotency_key = raw_key
        except (ValueError, AttributeError):
            # Invalid key in POST — generate a new one for the re-render.
            idempotency_key = str(uuid.uuid4())

        if not line_forms:
            messages.error(request, 'Add at least one purchase line before saving.')
            header_form.is_valid()  # populate cleaned_data even on error
            return _render_purchase_create(request, header_form, [], idempotency_key)

        header_valid = header_form.is_valid()
        lines_valid  = all(lf.is_valid() for lf in line_forms)

        # Resolve conversions server-side for each valid line.
        resolved_lines     = []
        conversion_errors  = []
        if lines_valid:
            for i, lf in enumerate(line_forms):
                try:
                    uc = lf.resolve_conversion()
                    resolved_lines.append({
                        'inventory_item_id': lf.cleaned_data['inventory_item_id'],
                        'purchase_unit':     uc.purchase_unit,
                        'purchase_quantity': lf.cleaned_data['purchase_quantity'],
                        'conversion_factor': uc.conversion_factor,
                        'note':              lf.cleaned_data.get('note', ''),
                    })
                except Exception as exc:
                    conversion_errors.append(f'Line {i + 1}: {exc}')

        if not header_valid or not lines_valid or conversion_errors:
            for err in conversion_errors:
                messages.error(request, err)
            return _render_purchase_create(
                request, header_form, line_forms, idempotency_key,
            )

        # Validate item activity and existence explicitly before calling the
        # service — gives a friendly per-line message.
        item_errors = []
        for i, rl in enumerate(resolved_lines):
            try:
                item = InventoryItem.objects.get(pk=rl['inventory_item_id'])
                if not item.is_active:
                    item_errors.append(
                        f'Line {i + 1}: "{item.name}" is inactive and cannot receive stock.'
                    )
            except InventoryItem.DoesNotExist:
                item_errors.append(f'Line {i + 1}: inventory item not found.')

        if item_errors:
            for err in item_errors:
                messages.error(request, err)
            return _render_purchase_create(
                request, header_form, line_forms, idempotency_key,
            )

        # Call the service — this is the only place stock is mutated.
        try:
            purchase = record_purchase(
                purchase_date=header_form.cleaned_data['purchase_date'],
                lines=resolved_lines,
                idempotency_key=idempotency_key,
                notes=header_form.cleaned_data.get('notes', ''),
                performed_by=request.user,
            )
            log_action(
                request.user,
                'inventory.purchase_create',
                purchase,
                detail=(
                    f'{len(resolved_lines)} line(s) on '
                    f'{header_form.cleaned_data["purchase_date"]}'
                ),
            )
        except (FractionalPcsError, PurchaseLineError, InventoryServiceError) as exc:
            messages.error(request, str(exc))
            return _render_purchase_create(
                request, header_form, line_forms, idempotency_key,
            )

        messages.success(
            request,
            f'Purchase #{purchase.pk} recorded — '
            f'{len(resolved_lines)} line(s) added to stock.',
        )
        return redirect('inventory:purchase_detail', pk=purchase.pk)

    # GET — generate a fresh idempotency key.
    import uuid
    idempotency_key = str(uuid.uuid4())
    initial_data    = {'idempotency_key': idempotency_key}
    header_form     = PurchaseHeaderForm(initial=initial_data)
    return _render_purchase_create(request, header_form, [], idempotency_key)


def _render_purchase_create(request, header_form, line_forms, idempotency_key):
    """Shared render helper for the purchase creation page."""
    import json

    # Build the items+conversions data for the JS to populate dropdowns.
    items = (
        InventoryItem.objects
        .filter(is_active=True)
        .prefetch_related('unit_conversions')
        .order_by('item_type', 'name')
    )
    items_data = []
    for item in items:
        conversions = [
            {
                'id':              uc.pk,
                'purchase_unit':   uc.purchase_unit,
                'factor':          str(uc.conversion_factor),
                'base_unit':       item.base_unit,
                'base_unit_label': item.get_base_unit_display(),
            }
            for uc in item.unit_conversions.filter(is_active=True)
        ]
        if conversions:
            items_data.append({
                'id':              item.pk,
                'name':            item.name,
                'base_unit':       item.base_unit,
                'base_unit_label': item.get_base_unit_display(),
                'conversions':     conversions,
            })

    # Build initial data for the line forms so JS can pre-populate values on
    # a validation-failure re-render.  We only expose the subset of fields
    # the JS needs: item id, unit conversion id, quantity, and note.  The
    # conversion_factor is NOT included here; it is always re-resolved from
    # the DB on submission.
    line_initial = []
    for lf in line_forms:
        line_initial.append({
            'inventory_item_id': lf.data.get('inventory_item_id', ''),
            'purchase_unit_id':  lf.data.get('purchase_unit_id', ''),
            'purchase_quantity': lf.data.get('purchase_quantity', ''),
            'note':              lf.data.get('note', ''),
            'errors':            [str(e) for e in lf.non_field_errors()] +
                                 [f'{f}: {e}'
                                  for f, errs in lf.errors.items()
                                  if f != '__all__'
                                  for e in errs],
        })

    return render(request, 'inventory/purchase_create.html', {
        'header_form':       header_form,
        'idempotency_key':   idempotency_key,
        'items_json':        json.dumps(items_data),
        'line_initial_json': json.dumps(line_initial),
    })


@login_required
@cashier_or_admin_required
def purchase_list(request):
    """
    Purchase history: paginated list of all StockPurchase records, most recent
    first.  Annotates each purchase with its line count.
    """
    purchases = (
        StockPurchase.objects
        .select_related('performed_by')
        .annotate(line_count=Count('items'))
        .order_by('-purchase_date', '-created_at')
    )

    paginator = Paginator(purchases, 20)
    page_num  = request.GET.get('page', 1)
    page_obj  = paginator.get_page(page_num)

    return render(request, 'inventory/purchase_list.html', {
        'page_obj': page_obj,
    })


@login_required
@cashier_or_admin_required
def purchase_detail(request, pk):
    """
    Purchase detail: shows the header and all StockPurchaseItem snapshots.
    Snapshots are historical; they do not change when UnitConversion settings
    change later.
    """
    purchase = get_object_or_404(
        StockPurchase.objects.select_related('performed_by'),
        pk=pk,
    )
    items = (
        purchase.items
        .select_related('inventory_item')
        .order_by('inventory_item__name')
    )
    return render(request, 'inventory/purchase_detail.html', {
        'purchase': purchase,
        'items':    items,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4B-5 — Recipe and Packaging Configuration Management
# ═══════════════════════════════════════════════════════════════════════════════

from .models import (
    Recipe, RecipeItem,
    PackagingProfile, PackagingProfileItem, ProductPackagingRule,
)
from .forms import (
    RecipeForm, RecipeApprovalForm, RecipeItemForm,
    PackagingProfileForm, PackagingProfileApprovalForm, PackagingProfileItemForm,
    ProductPackagingRuleForm,
)
from .services import (
    approve_recipe, retire_recipe,
    approve_packaging_profile, retire_packaging_profile, save_packaging_rule,
    RecipeConfigError, PackagingConfigError,
)


# ── Recipe list ───────────────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def recipe_list(request):
    """List all Recipes, ordered by product / size / scope / revision."""
    recipes = (
        Recipe.objects
        .select_related('product', 'approved_by')
        .order_by('product__name', 'size', 'order_type_scope', '-revision')
    )
    q = request.GET.get('q', '').strip()
    if q:
        recipes = recipes.filter(product__name__icontains=q)
    status_filter = request.GET.get('status', '').strip()
    if status_filter in ('draft', 'approved', 'retired'):
        recipes = recipes.filter(status=status_filter)
    else:
        status_filter = ''
    return render(request, 'inventory/recipe_list.html', {
        'recipes':       recipes,
        'q':             q,
        'status_filter': status_filter,
    })


# ── Recipe create ─────────────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def recipe_create(request):
    """Create a new draft Recipe header."""
    if request.method == 'POST':
        form = RecipeForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    recipe = form.save(commit=False)
                    recipe.status = Recipe.STATUS_DRAFT
                    recipe.save()
                    log_action(request.user, 'inventory.recipe_create', recipe,
                               detail=str(recipe))
            except IntegrityError:
                messages.error(request, 'Could not save recipe — a conflict occurred. Check revision number.')
                return render(request, 'inventory/recipe_form.html', {'form': form, 'title': 'Add Recipe'})
            messages.success(request, f'Draft recipe created for {recipe.product.name}.')
            return redirect('inventory:recipe_detail', pk=recipe.pk)
    else:
        form = RecipeForm()
    return render(request, 'inventory/recipe_form.html', {'form': form, 'title': 'Add Recipe'})


# ── Recipe detail ─────────────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def recipe_detail(request, pk):
    """View a Recipe and its items. Dispatch add-item POST from here."""
    recipe = get_object_or_404(Recipe.objects.select_related('product', 'approved_by'), pk=pk)
    items  = RecipeItem.objects.filter(recipe=recipe).select_related('inventory_item').order_by('id')

    # Add-item form (only shown / processed for drafts).
    item_form = None
    if recipe.status == Recipe.STATUS_DRAFT:
        if request.method == 'POST' and 'add_item' in request.POST:
            item_form = RecipeItemForm(request.POST, recipe=recipe)
            if item_form.is_valid():
                try:
                    with transaction.atomic():
                        ri = item_form.save(commit=False)
                        ri.recipe = recipe
                        ri.save()
                        log_action(request.user, 'inventory.recipe_item_add', recipe,
                                   detail=f'{ri.inventory_item.name} × {ri.quantity}')
                    messages.success(request, f'Added {ri.inventory_item.name} to recipe.')
                    return redirect('inventory:recipe_detail', pk=pk)
                except IntegrityError:
                    messages.error(request, 'Could not add ingredient — a conflict occurred.')
        else:
            item_form = RecipeItemForm(recipe=recipe)

    return render(request, 'inventory/recipe_detail.html', {
        'recipe':    recipe,
        'items':     items,
        'item_form': item_form,
    })


# ── Recipe edit ───────────────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def recipe_edit(request, pk):
    """Edit a draft Recipe header. Approved/retired recipes are immutable."""
    recipe = get_object_or_404(Recipe, pk=pk)
    if recipe.status != Recipe.STATUS_DRAFT:
        messages.error(request, 'Only draft recipes can be edited.')
        return redirect('inventory:recipe_detail', pk=pk)
    if request.method == 'POST':
        form = RecipeForm(request.POST, instance=recipe)
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
                    log_action(request.user, 'inventory.recipe_edit', recipe, detail=str(recipe))
            except IntegrityError:
                messages.error(request, 'Could not save recipe — revision number conflict.')
                return render(request, 'inventory/recipe_form.html',
                              {'form': form, 'recipe': recipe, 'title': 'Edit Recipe'})
            messages.success(request, 'Recipe updated.')
            return redirect('inventory:recipe_detail', pk=pk)
    else:
        form = RecipeForm(instance=recipe)
    return render(request, 'inventory/recipe_form.html',
                  {'form': form, 'recipe': recipe, 'title': 'Edit Recipe'})


# ── Recipe item delete ────────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def recipe_item_delete(request, pk, item_pk):
    """Remove a RecipeItem from a draft Recipe."""
    recipe = get_object_or_404(Recipe, pk=pk)
    if recipe.status != Recipe.STATUS_DRAFT:
        messages.error(request, 'Cannot remove items from a non-draft recipe.')
        return redirect('inventory:recipe_detail', pk=pk)
    ri = get_object_or_404(RecipeItem, pk=item_pk, recipe=recipe)
    if request.method == 'POST':
        with transaction.atomic():
            name = ri.inventory_item.name
            ri.delete()
            log_action(request.user, 'inventory.recipe_item_delete', recipe, detail=name)
        messages.success(request, f'Removed {name} from recipe.')
    return redirect('inventory:recipe_detail', pk=pk)


# ── Recipe approve ────────────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def recipe_approve(request, pk):
    """Approve a draft Recipe (POST with approval note)."""
    recipe = get_object_or_404(Recipe, pk=pk)
    if request.method == 'POST':
        form = RecipeApprovalForm(request.POST)
        if form.is_valid():
            try:
                recipe = approve_recipe(
                    recipe_id=pk,
                    approved_by=request.user,
                    approval_note=form.cleaned_data['approval_note'],
                )
                log_action(request.user, 'inventory.recipe_approve', recipe, detail=str(recipe))
                messages.success(request, f'Recipe approved (rev {recipe.revision}).')
                return redirect('inventory:recipe_detail', pk=pk)
            except RecipeConfigError as exc:
                messages.error(request, str(exc))
                return render(request, 'inventory/recipe_approve.html',
                              {'recipe': recipe, 'form': form})
    else:
        form = RecipeApprovalForm()
    return render(request, 'inventory/recipe_approve.html', {'recipe': recipe, 'form': form})


# ── Recipe retire ─────────────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def recipe_retire(request, pk):
    """Retire an approved Recipe (POST confirmation)."""
    recipe = get_object_or_404(Recipe, pk=pk)
    if request.method == 'POST':
        try:
            recipe = retire_recipe(recipe_id=pk)
            log_action(request.user, 'inventory.recipe_retire', recipe, detail=str(recipe))
            messages.success(request, f'Recipe retired (rev {recipe.revision}).')
        except RecipeConfigError as exc:
            messages.error(request, str(exc))
        return redirect('inventory:recipe_detail', pk=pk)
    return render(request, 'inventory/recipe_retire_confirm.html', {'recipe': recipe})


# ── Packaging profile list ────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_profile_list(request):
    """List all PackagingProfiles."""
    profiles = (
        PackagingProfile.objects
        .select_related('approved_by')
        .order_by('name', '-revision')
    )
    q = request.GET.get('q', '').strip()
    if q:
        profiles = profiles.filter(name__icontains=q)
    status_filter = request.GET.get('status', '').strip()
    if status_filter in ('draft', 'approved', 'retired'):
        profiles = profiles.filter(status=status_filter)
    else:
        status_filter = ''
    return render(request, 'inventory/packaging_profile_list.html', {
        'profiles':      profiles,
        'q':             q,
        'status_filter': status_filter,
    })


# ── Packaging profile create ──────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_profile_create(request):
    """Create a new draft PackagingProfile."""
    if request.method == 'POST':
        form = PackagingProfileForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    profile = form.save(commit=False)
                    profile.status = PackagingProfile.STATUS_DRAFT
                    profile.save()
                    log_action(request.user, 'inventory.packaging_profile_create',
                               profile, detail=str(profile))
            except IntegrityError:
                messages.error(request, 'Could not save profile — a conflict occurred.')
                return render(request, 'inventory/packaging_profile_form.html',
                              {'form': form, 'title': 'Add Packaging Profile'})
            messages.success(request, f'Draft packaging profile "{profile.name}" created.')
            return redirect('inventory:packaging_profile_detail', pk=profile.pk)
    else:
        form = PackagingProfileForm()
    return render(request, 'inventory/packaging_profile_form.html',
                  {'form': form, 'title': 'Add Packaging Profile'})


# ── Packaging profile detail ──────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_profile_detail(request, pk):
    """View a PackagingProfile and manage its items."""
    profile = get_object_or_404(
        PackagingProfile.objects.select_related('approved_by'), pk=pk
    )
    items = PackagingProfileItem.objects.filter(profile=profile).select_related('inventory_item').order_by('id')

    item_form = None
    if profile.status == PackagingProfile.STATUS_DRAFT:
        if request.method == 'POST' and 'add_item' in request.POST:
            item_form = PackagingProfileItemForm(request.POST, profile=profile)
            if item_form.is_valid():
                try:
                    with transaction.atomic():
                        ppi = item_form.save(commit=False)
                        ppi.profile = profile
                        ppi.save()
                        log_action(request.user, 'inventory.packaging_profile_item_add',
                                   profile, detail=f'{ppi.inventory_item.name} × {ppi.quantity}')
                    messages.success(request, f'Added {ppi.inventory_item.name} to profile.')
                    return redirect('inventory:packaging_profile_detail', pk=pk)
                except IntegrityError:
                    messages.error(request, 'Could not add item — a conflict occurred.')
        else:
            item_form = PackagingProfileItemForm(profile=profile)

    return render(request, 'inventory/packaging_profile_detail.html', {
        'profile':   profile,
        'items':     items,
        'item_form': item_form,
    })


# ── Packaging profile edit ────────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_profile_edit(request, pk):
    """Edit a draft PackagingProfile header."""
    profile = get_object_or_404(PackagingProfile, pk=pk)
    if profile.status != PackagingProfile.STATUS_DRAFT:
        messages.error(request, 'Only draft packaging profiles can be edited.')
        return redirect('inventory:packaging_profile_detail', pk=pk)
    if request.method == 'POST':
        form = PackagingProfileForm(request.POST, instance=profile)
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
                    log_action(request.user, 'inventory.packaging_profile_edit',
                               profile, detail=str(profile))
            except IntegrityError:
                messages.error(request, 'Could not save profile — revision conflict.')
                return render(request, 'inventory/packaging_profile_form.html',
                              {'form': form, 'profile': profile, 'title': 'Edit Packaging Profile'})
            messages.success(request, 'Packaging profile updated.')
            return redirect('inventory:packaging_profile_detail', pk=pk)
    else:
        form = PackagingProfileForm(instance=profile)
    return render(request, 'inventory/packaging_profile_form.html',
                  {'form': form, 'profile': profile, 'title': 'Edit Packaging Profile'})


# ── Packaging profile item delete ─────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_profile_item_delete(request, pk, item_pk):
    """Remove a PackagingProfileItem from a draft PackagingProfile."""
    profile = get_object_or_404(PackagingProfile, pk=pk)
    if profile.status != PackagingProfile.STATUS_DRAFT:
        messages.error(request, 'Cannot remove items from a non-draft packaging profile.')
        return redirect('inventory:packaging_profile_detail', pk=pk)
    ppi = get_object_or_404(PackagingProfileItem, pk=item_pk, profile=profile)
    if request.method == 'POST':
        with transaction.atomic():
            name = ppi.inventory_item.name
            ppi.delete()
            log_action(request.user, 'inventory.packaging_profile_item_delete',
                       profile, detail=name)
        messages.success(request, f'Removed {name} from profile.')
    return redirect('inventory:packaging_profile_detail', pk=pk)


# ── Packaging profile approve ─────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_profile_approve(request, pk):
    """Approve a draft PackagingProfile."""
    profile = get_object_or_404(PackagingProfile, pk=pk)
    if request.method == 'POST':
        form = PackagingProfileApprovalForm(request.POST)
        if form.is_valid():
            try:
                profile = approve_packaging_profile(
                    profile_id=pk,
                    approved_by=request.user,
                    approval_note=form.cleaned_data['approval_note'],
                )
                log_action(request.user, 'inventory.packaging_profile_approve',
                           profile, detail=str(profile))
                messages.success(request, f'Packaging profile "{profile.name}" approved.')
                return redirect('inventory:packaging_profile_detail', pk=pk)
            except PackagingConfigError as exc:
                messages.error(request, str(exc))
                return render(request, 'inventory/packaging_profile_approve.html',
                              {'profile': profile, 'form': form})
    else:
        form = PackagingProfileApprovalForm()
    return render(request, 'inventory/packaging_profile_approve.html',
                  {'profile': profile, 'form': form})


# ── Packaging profile retire ──────────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_profile_retire(request, pk):
    """Retire an approved PackagingProfile."""
    profile = get_object_or_404(PackagingProfile, pk=pk)
    if request.method == 'POST':
        try:
            profile = retire_packaging_profile(profile_id=pk)
            log_action(request.user, 'inventory.packaging_profile_retire',
                       profile, detail=str(profile))
            messages.success(request, f'Packaging profile "{profile.name}" retired.')
        except PackagingConfigError as exc:
            messages.error(request, str(exc))
        return redirect('inventory:packaging_profile_detail', pk=pk)
    return render(request, 'inventory/packaging_profile_retire_confirm.html',
                  {'profile': profile})


# ── Product packaging rules list (per product) ────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_rule_list(request):
    """List all ProductPackagingRules across all products."""
    from apps.menu.models import Product as _Product
    rules = (
        ProductPackagingRule.objects
        .select_related('product', 'profile', 'confirmed_by', 'updated_by')
        .order_by('product__name', 'size', 'order_type')
    )
    products = _Product.objects.filter(is_active=True).order_by('name')
    product_filter = request.GET.get('product', '').strip()
    if product_filter:
        try:
            rules = rules.filter(product_id=int(product_filter))
        except (ValueError, TypeError):
            product_filter = ''
    return render(request, 'inventory/packaging_rule_list.html', {
        'rules':          rules,
        'products':       products,
        'product_filter': product_filter,
    })


# ── Product packaging rule create ─────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_rule_create(request):
    """Create a new ProductPackagingRule."""
    from apps.menu.models import Product as _Product
    if request.method == 'POST':
        form = ProductPackagingRuleForm(request.POST)
        if form.is_valid():
            try:
                rule = save_packaging_rule(
                    product_id=form.cleaned_data['product_id'],
                    size=form.cleaned_data['size'],
                    order_type=form.cleaned_data['order_type'],
                    profile_id=form.cleaned_data.get('profile_id') or None,
                    no_packaging=form.cleaned_data.get('no_packaging', False),
                    confirmed_by=request.user,
                )
                log_action(request.user, 'inventory.packaging_rule_create',
                           rule, detail=str(rule))
                messages.success(request, 'Packaging rule saved.')
                return redirect('inventory:packaging_rule_list')
            except (PackagingConfigError, IntegrityError) as exc:
                messages.error(request, str(exc))
    else:
        # Pre-populate product_id from GET parameter.
        product_id = request.GET.get('product', '')
        initial = {}
        if product_id:
            try:
                initial['product_id'] = int(product_id)
            except (ValueError, TypeError):
                pass
        form = ProductPackagingRuleForm(initial=initial)
    products = _Product.objects.filter(is_active=True).order_by('name')
    return render(request, 'inventory/packaging_rule_form.html', {
        'form':     form,
        'title':    'Add Packaging Rule',
        'products': products,
    })


# ── Product packaging rule edit ───────────────────────────────────────────────

@login_required
@cashier_or_admin_required
def packaging_rule_edit(request, pk):
    """Edit an existing ProductPackagingRule."""
    from apps.menu.models import Product as _Product
    rule = get_object_or_404(ProductPackagingRule.objects.select_related('product', 'profile'), pk=pk)
    if request.method == 'POST':
        form = ProductPackagingRuleForm(request.POST)
        if form.is_valid():
            # Ensure the submitted product_id matches the rule's product.
            if form.cleaned_data['product_id'] != rule.product_id:
                messages.error(request, 'Product cannot be changed when editing a rule.')
                return redirect('inventory:packaging_rule_edit', pk=pk)
            try:
                rule = save_packaging_rule(
                    product_id=rule.product_id,
                    size=form.cleaned_data['size'],
                    order_type=form.cleaned_data['order_type'],
                    profile_id=form.cleaned_data.get('profile_id') or None,
                    no_packaging=form.cleaned_data.get('no_packaging', False),
                    confirmed_by=request.user,
                    existing_rule_id=pk,
                )
                log_action(request.user, 'inventory.packaging_rule_edit',
                           rule, detail=str(rule))
                messages.success(request, 'Packaging rule updated.')
                return redirect('inventory:packaging_rule_list')
            except (PackagingConfigError, IntegrityError) as exc:
                messages.error(request, str(exc))
    else:
        form = ProductPackagingRuleForm(initial={
            'product_id':   rule.product_id,
            'size':         rule.size,
            'order_type':   rule.order_type,
            'no_packaging': rule.no_packaging,
            'profile_id':   rule.profile_id or '',
        })
    products = _Product.objects.filter(is_active=True).order_by('name')
    return render(request, 'inventory/packaging_rule_form.html', {
        'form':     form,
        'rule':     rule,
        'title':    'Edit Packaging Rule',
        'products': products,
    })


# ── Configuration completeness check ─────────────────────────────────────────

@login_required
@cashier_or_admin_required
def recipe_config_status(request, product_pk):
    """
    Show the configuration completeness matrix for an ingredient-tracked product.

    For each (size, order_type) combination that the product currently has
    recipes or rules for, calls resolve_plan() and displays the result.

    This is read-only and does not call any write service.
    """
    from apps.menu.models import Product as _Product
    from .services import resolve_plan, RecipeIncompleteError

    product = get_object_or_404(
        _Product.objects.filter(inventory_tracking_mode='ingredient'),
        pk=product_pk,
    )

    # Collect all sizes and order_types referenced by any Recipe or Rule for
    # this product — the union of what has been configured so far.
    sizes_from_recipes = list(
        Recipe.objects.filter(product=product)
        .values_list('size', flat=True).distinct()
    )
    sizes_from_rules = list(
        ProductPackagingRule.objects.filter(product=product)
        .values_list('size', flat=True).distinct()
    )
    all_sizes = sorted(set(sizes_from_recipes + sizes_from_rules)) or ['none']
    order_types = ['dine_in', 'takeout']

    matrix = []
    for size in all_sizes:
        row = {'size': size, 'cells': []}
        for ot in order_types:
            try:
                plan = resolve_plan(product.pk, size, ot)
                row['cells'].append({
                    'order_type': ot,
                    'ok': True,
                    'reason': '',
                    'recipe': plan.recipe,
                    'packaging_rule': plan.packaging_rule,
                })
            except RecipeIncompleteError as exc:
                row['cells'].append({
                    'order_type': ot,
                    'ok': False,
                    'reason': str(exc),
                    'recipe': None,
                    'packaging_rule': None,
                })
        matrix.append(row)

    return render(request, 'inventory/recipe_config_status.html', {
        'product': product,
        'matrix':  matrix,
        'order_types': order_types,
    })
