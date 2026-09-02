from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db.models import Count
from .models import Category, Product
from .forms import ProductForm, CategoryForm
from .services import (
    category_active_order_count,
    get_order_reference_counts,
    annotate_order_reference_counts,
)
from apps.accounts.decorators import admin_required
from apps.audit.services import log_action
from apps.inventory.models import InventoryLog


# ── Lifecycle confirmation guard ────────────────────────────────────────────
# Deactivating a product (or marking it unavailable) while it is referenced
# by orders that are still being fulfilled requires an explicit ``confirm``
# flag. The first POST without the flag returns ``requires_confirmation`` so
# the UI can show a clear warning; the action only runs once the flag is
# present. Completed/cancelled orders are history and never require this.
# Soft deactivation is safe for those orders (their line items store a
# snapshot of name/price), but admins must knowingly take the action.


def _require_lifecycle_confirmation(request, product, action_label):
    """Return a JsonResponse when confirmation is required, else None.

    Only active order references matter; historical (completed/cancelled)
    references never block a lifecycle action.
    """
    counts = get_order_reference_counts(product)
    if counts['active'] and request.POST.get('confirm') != '1':
        return counts, JsonResponse({
            'success': False,
            'requires_confirmation': True,
            'active_order_count': counts['active'],
            'message': (
                f'This product is currently in {counts["active"]} active '
                f'order(s) that are still being fulfilled. {action_label} '
                'applies immediately to the menu and POS, but those orders '
                'keep their saved line items and remain fulfillable. '
                'Confirm to proceed.'
            ),
        })
    return counts, None


def menu_index(request):
    """Public-facing menu page.

    Products are rendered grouped by category from the prefetch below, which
    already returns only sellable products (Product.objects.sellable()). The
    page's search box and category tabs filter those cards client-side
    (main.js), so no separate server-side product queryset is needed.
    """
    categories = Category.objects.with_sellable_products()
    return render(request, 'menu/index.html', {'categories': categories})


def product_stock(request):
    """Stock/availability snapshot for the public menu's live polling.

    Returns every active product's stock level and availability so the menu
    page can disable an "Add to cart" button the moment a product runs out
    (or is marked unavailable) without reloading. Only the fields the cards
    already display are exposed; names, prices and images stay server-side.

    Performance:
    - An ETag derived from the data fingerprint is set on every response.
    - If the client sends ``If-None-Match`` matching the current ETag the
      server returns 304 Not Modified with an empty body, saving JSON
      serialisation and transfer on the ~90 % of polls where nothing changed.
    - Cache-Control is set to private, no-store so proxies and CDNs do not
      cache customer-specific data between different browsers.
    """
    import hashlib, json as _json
    products = list(Product.objects.filter(is_active=True).values(
        'pk', 'stock_quantity', 'is_available',
    ).order_by('pk'))

    data = {
        str(p['pk']): {
            'stock_quantity': p['stock_quantity'],
            'is_available':   p['is_available'],
            'is_active':      True,
        }
        for p in products
    }

    # Build a cheap ETag from a hash of the serialised data.
    # Only changes when stock or availability actually changes.
    payload = _json.dumps(data, sort_keys=True, separators=(',', ':'))
    etag = f'"{hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest()}"'

    if request.META.get('HTTP_IF_NONE_MATCH') == etag:
        from django.http import HttpResponse
        resp304 = HttpResponse(status=304)
        resp304['ETag'] = etag
        resp304['Cache-Control'] = 'private, no-store'
        return resp304

    response = JsonResponse({'products': data})
    response['ETag'] = etag
    response['Cache-Control'] = 'private, no-store'
    return response

# --- Admin Menu Management ---


def _save_form_or_render(request, form_class, *, template, title, list_url,
                         action, success_message, instance=None,
                         extra_context=None, log_detail='', log_inventory=False):
    """Shared create/edit flow for products and categories.

    Validates the form, saves, audit-logs the action, flashes a success toast
    and redirects to the list page; on GET or an invalid POST it renders the
    form page. Keeping this in one place removes the duplicated boilerplate
    from the four create/edit views below.

    ``log_inventory`` additionally records stock movements made through the
    product create/edit forms (source ``inventory_update``) so the inventory
    audit trail covers manual stock edits too. The stock itself is saved
    exactly as before -- only a log row is added.
    """
    form = form_class(request.POST or None, request.FILES or None, instance=instance)
    # Snapshot the pre-edit stock BEFORE validation. ModelForm applies the
    # cleaned data to the instance during is_valid()/_post_clean(), so reading
    # it afterwards would capture the NEW value instead of the old one.
    old_stock = instance.stock_quantity if (
        log_inventory and instance is not None and instance.pk
        and request.method == 'POST'
    ) else None
    if request.method == 'POST' and form.is_valid():
        obj = form.save()
        if log_inventory:
            _log_inventory_update(request, obj, old_stock)
        log_action(request.user, action, obj, detail=log_detail)
        messages.success(request, success_message)
        return redirect(list_url)
    context = {'form': form, 'title': title}
    if extra_context:
        context.update(extra_context)
    return render(request, template, context)


def _log_inventory_update(request, product, old_stock):
    """Record a stock change made through the product create/edit forms.

    Only actual movements are logged: creating a product with an initial
    stock, or an edit that changes the stock level. Edits that leave stock
    untouched produce no entry.
    """
    new_stock = product.stock_quantity
    if old_stock is None:
        if new_stock <= 0:
            return
        change, before = new_stock, 0
        reason = 'Initial stock'
    else:
        if old_stock == new_stock:
            return
        change, before = new_stock - old_stock, old_stock
        reason = 'Product stock update'
    InventoryLog.record(
        product=product,
        action='adjustment',
        source='inventory_update',
        reason=reason,
        quantity_change=change,
        quantity_before=before,
        quantity_after=new_stock,
        performed_by=request.user,
    )


@login_required
@admin_required
def product_list(request):
    products = Product.objects.select_related('category').order_by('-created_at')
    # Lifecycle visibility: every row carries how many live orders reference
    # it (active_order_count) and how many orders overall (total_order_count),
    # so admins can see at a glance which products are mid-flight.
    products = annotate_order_reference_counts(products)
    categories = Category.objects.all()
    category_filter = request.GET.get('category', '')
    if category_filter:
        products = products.filter(category__slug=category_filter)
    search = request.GET.get('q', '')
    if search:
        products = products.filter(name__icontains=search)
    return render(request, 'menu/product_list.html', {
        'products': products,
        'categories': categories,
        'category_filter': category_filter,
        'search': search,
        'products_in_active_orders': products.filter(
            active_order_count__gt=0,
        ).count(),
    })


@login_required
@admin_required
def product_create(request):
    return _save_form_or_render(
        request, ProductForm,
        template='menu/product_form.html', title='Add Product',
        list_url='menu:product_list', action='product.create',
        success_message='Product created successfully!', log_inventory=True,
    )


@login_required
@admin_required
def product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)
    # Warn the editor when this product is referenced by live orders: price
    # and name changes never rewrite saved line items, so existing orders are
    # unaffected -- the banner just makes that explicit before saving.
    ref = get_order_reference_counts(product)
    return _save_form_or_render(
        request, ProductForm,
        template='menu/product_form.html', title='Edit Product',
        list_url='menu:product_list', action='product.update',
        success_message='Product updated successfully!',
        instance=product,
        extra_context={
            'product': product,
            'active_order_count': ref['active'],
            'total_order_count': ref['total'],
        },
        log_detail=f'active_orders={ref["active"]}',
        log_inventory=True,
    )


@login_required
@admin_required
@require_POST
def product_toggle_active(request, pk):
    """Deactivate or reactivate a product (soft-delete workflow).

    Deactivated products disappear from the customer menu and the POS, but
    keep their row, ID and every historical reference (orders, reports,
    finance records, analytics) intact so they can be reactivated later.
    """
    product = get_object_or_404(Product, pk=pk)
    if product.is_active:
        # Guard: never deactivate silently while live orders reference it.
        counts, pending = _require_lifecycle_confirmation(
            request, product, 'Deactivation',
        )
        if pending:
            return pending
        product.deactivate()
        log_action(request.user, 'product.deactivate', product,
                   detail=f'active_orders={counts["active"]}')
        status = 'deactivated'
    else:
        product.activate()
        log_action(request.user, 'product.reactivate', product)
        status = 'reactivated'
    return JsonResponse({'success': True, 'status': status, 'is_active': product.is_active})


@login_required
@admin_required
def product_toggle(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if product.is_available:
        # Making the product unavailable is a lifecycle change: guard it the
        # same way as deactivation when live orders reference the product.
        counts, pending = _require_lifecycle_confirmation(
            request, product, 'Marking it unavailable',
        )
        if pending:
            return pending
    product.is_available = not product.is_available
    product.save()
    log_action(request.user, 'product.availability', product)
    return JsonResponse({'success': True, 'is_available': product.is_available})


@login_required
@admin_required
def category_list(request):
    categories = Category.objects.annotate(product_count=Count('products')).order_by('order')
    return render(request, 'menu/category_list.html', {'categories': categories})


@login_required
@admin_required
def category_create(request):
    return _save_form_or_render(
        request, CategoryForm,
        template='menu/category_form.html', title='Add Category',
        list_url='menu:category_list', action='category.create',
        success_message='Category created!',
    )


@login_required
@admin_required
def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)
    return _save_form_or_render(
        request, CategoryForm,
        template='menu/category_form.html', title='Edit Category',
        list_url='menu:category_list', action='category.update',
        success_message='Category updated!',
        instance=category,
        extra_context={'category': category},
    )


@login_required
@admin_required
@require_POST
def category_toggle_active(request, pk):
    """Deactivate or reactivate a category (soft-delete workflow).

    Inactive categories disappear from the customer menu, the POS and the
    product-creation dropdown, but keep their row, products and every
    historical reference intact so they can be reactivated anytime.
    """
    category = get_object_or_404(Category, pk=pk)
    if category.is_active:
        category.deactivate()
        log_action(request.user, 'category.deactivate', category)
        status = 'deactivated'
    else:
        category.activate()
        log_action(request.user, 'category.reactivate', category)
        status = 'reactivated'
    return JsonResponse({'success': True, 'status': status, 'is_active': category.is_active})


@login_required
@admin_required
def category_delete(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if request.method == 'POST':
        product_count = category.products.count()
        if product_count:
            # Data-integrity guard: a category with products can never be
            # deleted (its products are never hard-deleted either), so block
            # the attempt and explain how to proceed. Nothing is changed.
            message = (
                f'"{category.name}" cannot be deleted because it still has '
                f'{product_count} product(s). Instead, deactivate it with the '
                'Deactivate button (it can be reactivated anytime), or move '
                'its products to another category first -- a category can '
                'only be deleted once it is empty.'
            )
            active_orders = category_active_order_count(category)
            if active_orders:
                message += (
                    f' Note: {active_orders} active order(s) reference its '
                    'products and are not affected by this.'
                )
            return JsonResponse({
                'success': False,
                'error': 'Category cannot be deleted',
                'message': message,
                'product_count': product_count,
            })
        category.delete()
        # Django nulls obj.pk on delete, so the id is passed explicitly.
        log_action(request.user, 'category.delete', category,
                   object_id=pk, object_repr=str(category))
        messages.success(request, 'Category deleted!')
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})


def get_product_price(request, pk):
    """AJAX endpoint to get product price by size"""
    product = get_object_or_404(Product, pk=pk)
    size = request.GET.get('size', 'none')
    # Return as string (matching the Finance API convention) so JS parseFloat()
    # of '123.10' is exact at 2dp — float('123.10') can produce 123.09999999...
    # which accumulates into the JS price×quantity subtotal calculation.
    price = product.get_price_for_size(size)
    return JsonResponse({'price': str(price), 'formatted': f'₱{price:.2f}'})
