"""
Order views - Cart, Checkout, Order management for cashier/admin
"""
import json
import logging
import secrets

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from django.db import transaction, IntegrityError
from django.db.models import Count, Q
from django.core.paginator import Paginator
from decimal import Decimal
from .models import Order, OrderItem, Cart, CartItem
from .forms import CheckoutForm
from apps.menu.models import Category, Product
from apps.accounts.decorators import cashier_or_admin_required, admin_required
from apps.orders.services import (
    deduct_inventory_for_order,
    restore_inventory_for_order,
    validate_status_transition,
    calculate_packaging_fee_for_items,
    get_packaging_fee_per_item,
    create_order_item,
    VALID_TRANSITIONS,
)

logger = logging.getLogger(__name__)


# ========== CART VIEWS ==========

def get_or_create_cart(request):
    """Get or create cart for current session"""
    if not request.session.session_key:
        request.session.create()
    cart, _ = Cart.objects.get_or_create(session_key=request.session.session_key)
    return cart


def cart_view(request):
    cart = get_or_create_cart(request)
    items = cart.cart_items.select_related('product').all()

    # Refresh every CartItem against live product data on each cart page load.
    # This ensures the cart is self-consistent before the customer reviews it:
    # - Inactive / unavailable products are removed immediately.
    # - Items whose requested quantity exceeds current stock are capped.
    # - unit_price is refreshed so the displayed subtotals are current.
    # None of these changes affect historical orders — they only update the
    # temporary CartItem rows in the session's cart.
    for item in items:
        product = item.product
        if not product.is_active or not product.is_available:
            item.delete()
            continue
        changed = False
        live_price = product.get_price_for_size(item.size)
        if item.unit_price != live_price:
            item.unit_price = live_price
            changed = True
        if item.quantity > product.stock_quantity:
            if product.stock_quantity <= 0:
                item.delete()
                continue
            item.quantity = product.stock_quantity
            changed = True
        if changed:
            item.save(update_fields=['quantity', 'unit_price'])

    # Re-fetch after modifications so the template sees the updated data.
    items = cart.cart_items.select_related('product').all()
    return render(request, 'orders/cart.html', {'cart': cart, 'items': items})


@require_POST
def add_to_cart(request, product_id):
    # Inactive (deactivated) or unavailable products are not sellable.
    product = get_object_or_404(Product.objects.sellable(), pk=product_id)
    cart = get_or_create_cart(request)
    size = request.POST.get('size', 'none')
    try:
        quantity = int(request.POST.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    quantity = max(quantity, 1)
    unit_price = product.get_price_for_size(size)

    def _reject(message):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': message})
        messages.error(request, message)
        return redirect('menu:index')

    # Server-side availability guard (the menu already disables the button,
    # but a stale page or a direct request must never add an unorderable
    # product). The stock check here is a pre-check only -- checkout re-
    # validates against the live stock inside its transaction.
    if product.stock_quantity <= 0:
        return _reject(
            f'{product.name} is out of stock and cannot be added to your cart.'
        )
    if quantity > product.stock_quantity:
        return _reject(
            f'Only {product.stock_quantity} {product.name} left in stock. '
            'Please adjust the quantity.'
        )

    cart_item, created = CartItem.objects.get_or_create(
        cart=cart,
        product=product,
        size=size,
        defaults={'quantity': quantity, 'unit_price': unit_price}
    )

    if not created:
        cart_item.quantity += quantity
        cart_item.save()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'cart_count': cart.item_count,
            'message': f'{product.name} added to cart!'
        })
    messages.success(request, f'{product.name} added to cart!')
    return redirect('orders:cart')


@require_POST
def update_cart(request, item_id):
    # Ownership check: only fetch the CartItem if it belongs to the current
    # session's cart.  A CartItem from another session resolves to 404 so
    # the response is identical to "item not found" — no information leak.
    cart = get_or_create_cart(request)
    item = get_object_or_404(CartItem, pk=item_id, cart=cart)

    def _response(**extra):
        data = {
            'success': True,
            'cart_total': float(cart.total),
            'cart_count': cart.item_count,
        }
        data.update(extra)
        return JsonResponse(data)

    try:
        quantity = int(request.POST.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1

    if quantity <= 0:
        item.delete()
        return _response(removed=True, quantity=0, item_subtotal=0)

    product = item.product
    # Items whose product is no longer sellable (deactivated / marked
    # unavailable) are dropped with a notice, matching checkout.
    if not product.is_active or not product.is_available:
        name = product.name
        item.delete()
        return _response(
            removed=True, quantity=0, item_subtotal=0,
            message=f'{name} is no longer available and was removed from your cart.',
        )
    # Out-of-stock items cannot be ordered; remove them instead of leaving a
    # dead row that would fail checkout.
    if product.stock_quantity <= 0:
        name = product.name
        item.delete()
        return _response(
            removed=True, quantity=0, item_subtotal=0,
            message=f'{name} is out of stock and was removed from your cart.',
        )
    # Cap the quantity at what is actually available, telling the customer
    # instead of silently changing their order.
    capped = min(quantity, product.stock_quantity)
    # Refresh the unit price from the current product price so the cart
    # always displays the up-to-date price and checkout uses the correct
    # value.  The price is authoritative at the DB level, not from the
    # browser.
    item.quantity = capped
    item.unit_price = product.get_price_for_size(item.size)
    item.save()
    message = None
    if capped < quantity:
        message = (
            f'Only {product.stock_quantity} {product.name} left in stock. '
            'Quantity adjusted.'
        )
    return _response(
        quantity=capped,
        item_subtotal=float(item.subtotal),
        message=message,
    )


@require_POST
def remove_from_cart(request, item_id):
    # Ownership check: only delete the CartItem if it belongs to the current
    # session's cart.  Items from other sessions resolve to 404.
    cart = get_or_create_cart(request)
    item = get_object_or_404(CartItem, pk=item_id, cart=cart)
    item.delete()
    return JsonResponse({
        'success': True,
        'cart_total': float(cart.total),
        'cart_count': cart.item_count
    })


@require_POST
def clear_cart(request):
    """Remove all items from the current session's cart in one operation.

    Used by the "Clear cart" button on the cart page.  Returns JSON when
    called via AJAX, otherwise redirects to the menu so the customer can
    start a fresh order.
    """
    cart = get_or_create_cart(request)
    cart.cart_items.all().delete()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'cart_count': 0})
    messages.info(request, 'Your cart has been cleared.')
    return redirect('menu:index')


def checkout_view(request):
    cart = get_or_create_cart(request)

    # Duplicate-order protection: the checkout form carries a request_token
    # (rendered from this view). Replaying an already-placed order -- a
    # double-click, a retry after a lost response -- returns the original
    # order's success page instead of creating a duplicate. Checked before
    # the empty-cart redirect, because the first submission already cleared
    # the cart.
    posted_token = request.POST.get('request_token') if request.method == 'POST' else None
    if posted_token:
        duplicate = Order.objects.filter(request_token=posted_token).first()
        if duplicate:
            request.session['last_order_id'] = duplicate.pk
            return redirect('orders:order_success', pk=duplicate.pk)

    items = cart.cart_items.select_related('product__category').all()

    if not items:
        messages.warning(request, 'Your cart is empty!')
        return redirect('menu:index')

    # Stable per-checkout token: generated once and kept while the form is
    # being filled in or fixed (nothing is created until a successful POST),
    # so a re-submit after a validation error reuses the same key. The
    # session copy is consumed once an order is actually created.
    if 'checkout_request_token' not in request.session:
        request.session['checkout_request_token'] = secrets.token_urlsafe(32)
    request_token = posted_token or request.session['checkout_request_token']

    if request.method == 'POST':
        form = CheckoutForm(request.POST)
        if form.is_valid():
            # Items that are no longer sellable (deactivated, marked
            # unavailable, or out of stock) are dropped with a clear notice
            # so the customer can review the remaining cart before checking
            # out -- an unorderable product must never end up in an order.
            unorderable_items = items.filter(
                Q(product__is_active=False)
                | Q(product__is_available=False)
                | Q(product__stock_quantity=0),
            )
            if unorderable_items.exists():
                unorderable_items.delete()
                messages.warning(
                    request,
                    'Some items in your cart are no longer available '
                    'and were removed.',
                )
                return redirect('orders:cart')

            # Quantity-over-stock check: if any item's requested quantity
            # exceeds the current available stock (but stock is still > 0),
            # cap the CartItem quantity to what is available and bounce the
            # customer back to the cart to review the change before placing
            # the order.  This replaces the previous silent cap inside the
            # atomic block — customers deserve to see a clear message before
            # their order quantity is reduced.
            adjusted_items = []
            for cart_item in items:
                available = cart_item.product.stock_quantity
                if cart_item.quantity > available:
                    cart_item.quantity = available
                    cart_item.unit_price = cart_item.product.get_price_for_size(cart_item.size)
                    cart_item.save(update_fields=['quantity', 'unit_price'])
                    adjusted_items.append(cart_item.product.name)

            if adjusted_items:
                names = ', '.join(adjusted_items)
                messages.warning(
                    request,
                    f'Stock has changed for: {names}. '
                    'Quantities have been updated — please review your cart '
                    'before placing your order.',
                )
                return redirect('orders:cart')

            # Create the order, its items, the totals and the stock deduction
            # in ONE transaction: if any step fails (e.g. insufficient stock)
            # everything rolls back, so a failed checkout can never leave a
            # half-created order, a partial deduction or an inventory log
            # without the matching order.
            try:
                with transaction.atomic():
                    order = Order.objects.create(
                        customer_name=form.cleaned_data['customer_name'],
                        customer_phone=form.cleaned_data.get('customer_phone', ''),
                        table_number=form.cleaned_data.get('table_number', ''),
                        order_type=form.cleaned_data['order_type'],
                        notes=form.cleaned_data.get('notes', ''),
                        request_token=request_token,
                    )

                    # Add items — all prices and quantities are validated
                    # against the live database values inside this transaction
                    # so no browser-supplied or stale value can influence the
                    # final order total.
                    for cart_item in items:
                        product = cart_item.product

                        # ── Final per-item validation (inside the transaction) ──────
                        # These checks run AFTER acquiring the order row so that any
                        # race between the outer pre-flight filter and here is caught
                        # atomically.  A ValueError rolls back the entire transaction:
                        # no order, no items, no stock change, no log row.

                        # 1. Quantity must be positive — guards against a CartItem
                        #    whose quantity was set to 0 by a concurrent update_cart
                        #    call or direct manipulation after the pre-flight checks.
                        if cart_item.quantity <= 0:
                            raise ValueError(
                                f'Invalid quantity ({cart_item.quantity}) for '
                                f'"{product.name}". Please review your cart.'
                            )

                        # 2. Re-validate product status inside the transaction using
                        #    select_for_update() so a deactivation or availability
                        #    change that raced past the outer pre-flight is caught.
                        live_product = (
                            Product.objects
                            .select_for_update()
                            .select_related('category')
                            .get(pk=product.pk)
                        )
                        if not live_product.is_active:
                            raise ValueError(
                                f'"{live_product.name}" is no longer available '
                                '(product deactivated). Please remove it from your cart.'
                            )
                        if not live_product.is_available:
                            raise ValueError(
                                f'"{live_product.name}" is currently unavailable. '
                                'Please remove it from your cart.'
                            )
                        if live_product.stock_quantity <= 0:
                            raise ValueError(
                                f'"{live_product.name}" is out of stock. '
                                'Please remove it from your cart.'
                            )

                        # 3. Validate the size value is a known choice so that a
                        #    tampered CartItem cannot inject an arbitrary string.
                        valid_sizes = {k for k, _ in cart_item.SIZE_CHOICES}
                        if cart_item.size not in valid_sizes:
                            raise ValueError(
                                f'Invalid size "{cart_item.size}" for '
                                f'"{live_product.name}".'
                            )

                        # Authoritative price: always read from the live product
                        # record, never from the CartItem or the POST body.
                        current_price = live_product.get_price_for_size(cart_item.size)
                        if current_price != cart_item.unit_price:
                            cart_item.unit_price = current_price
                            cart_item.save(update_fields=['unit_price'])

                        create_order_item(
                            order=order,
                            product=live_product,
                            size=cart_item.size,
                            quantity=cart_item.quantity,
                        )

                    order.calculate_total()

                    deduct_inventory_for_order(order, performed_by=None)

                    # Clear cart
                    cart.cart_items.all().delete()
            except ValueError as e:
                # The atomic block above was rolled back: no order, no items,
                # no stock change and no inventory log were persisted.
                messages.error(request, str(e))
                return redirect('orders:cart')
            except IntegrityError:
                # A concurrent duplicate slipped past the token lookup: the
                # unique request_token (or order_number) constraint rejected
                # the INSERT. Serve the original order when the token
                # identifies one; otherwise surface a friendly error.
                duplicate = (
                    Order.objects.filter(request_token=request_token).first()
                    if request_token else None
                )
                if duplicate:
                    request.session['last_order_id'] = duplicate.pk
                    return redirect('orders:order_success', pk=duplicate.pk)
                messages.error(
                    request, 'Order could not be created. Please try again.',
                )
                return redirect('orders:cart')
            except Exception:
                # Unexpected server-side failure (e.g. database unreachable,
                # programming error).  The transaction.atomic() context manager
                # already rolled back everything, so no partial order exists.
                # Log the full traceback for the server operator, but show the
                # customer only a generic, friendly message — no internal
                # details are exposed.
                logger.exception(
                    'Unexpected error during checkout for session %s',
                    request.session.session_key or 'unknown',
                )
                messages.error(
                    request,
                    'Something went wrong and your order could not be placed. '
                    'Your cart has been preserved — please try again. '
                    'If the problem continues, please let a staff member know.',
                )
                return redirect('orders:cart')

            request.session['last_order_id'] = order.pk
            request.session.pop('checkout_request_token', None)
            return redirect('orders:order_success', pk=order.pk)
    else:
        form = CheckoutForm()

    return render(request, 'orders/checkout.html', {
        'cart': cart, 'items': items, 'form': form,
        'request_token': request_token,
    })


def order_success(request, pk):
    # Ownership check: only the session that placed this order may view its
    # success page.  The checkout view stores the pk in session['last_order_id']
    # immediately before redirecting here; staff views that redirect to a
    # success page do the same.  Any request whose session does not contain
    # the matching pk receives a 404 — identical to "order not found" so the
    # response leaks no information about whether that pk exists at all.
    if request.session.get('last_order_id') != pk:
        raise Http404
    order = get_object_or_404(
        Order.objects.only(
            'order_number', 'customer_name', 'table_number',
            'order_type', 'total', 'subtotal', 'packaging_fee',
             'discount', 'queue_number', 'status',
        ),
        pk=pk,
    )
    # Pre-fetch items using only the snapshot fields the template reads.
    # order.items.all() in the template would hit the DB every time;
    # passing a pre-evaluated queryset avoids that extra round-trip.
    items = order.items.only(
        'product_name', 'size', 'quantity', 'unit_price', 'subtotal',
    )
    return render(request, 'orders/order_success.html', {
        'order': order,
        'items': items,
    })


# ========== STAFF / CASHIER VIEWS ==========

@login_required
@cashier_or_admin_required
def order_list(request):
    orders = (
        Order.objects
        .select_related('cashier')
        # Annotate the item count directly so the template reads
        # order.item_count (an integer) instead of triggering
        # order.items.count() — one extra query per row.
        .annotate(item_count=Count('items', distinct=True))
        # Defer large/unused fields that are never rendered on the list page.
        .defer('notes', 'customer_phone', 'discount',
               'amount_paid', 'change_amount', 'stock_deducted',
               'request_token', 'queued_at', 'ready_at',
               'completed_at', 'cancelled_at')
    )
    status_filter = request.GET.get('status', '')
    search = request.GET.get('q', '')

    if status_filter:
        orders = orders.filter(status=status_filter)
    if search:
        orders = orders.filter(
            Q(order_number__icontains=search) |
            Q(customer_name__icontains=search) |
            Q(table_number__icontains=search)
        )

    orders = orders.order_by('-created_at')

    paginator = Paginator(orders, 20)
    page = request.GET.get('page', 1)
    orders_page = paginator.get_page(page)

    pending_count = Order.objects.filter(status='pending').count()

    # Serialize VALID_TRANSITIONS and STATUS_LABELS as JSON for the template's
    # real-time JS handlers.  Injecting from Python keeps the JS literals in
    # sync with services.py automatically — no manual duplication required.
    valid_transitions_json = json.dumps(
        {k: sorted(v) for k, v in VALID_TRANSITIONS.items()}
    )
    status_labels_json = json.dumps(dict(Order.STATUS_CHOICES))

    return render(request, 'orders/order_list.html', {
        'orders': orders_page,
        'status_filter': status_filter,
        'search': search,
        'status_choices': Order.STATUS_CHOICES,
        'pending_count': pending_count,
        'valid_transitions_json': valid_transitions_json,
        'status_labels_json': status_labels_json,
    })


@login_required
@cashier_or_admin_required
def order_detail(request, pk):
    # select_related('cashier') preloads the staff member shown on the page
    # (order.cashier.get_full_name) -- one query instead of a lazy fetch.
    order = get_object_or_404(Order.objects.select_related('cashier'), pk=pk)
    items = order.items.select_related('product').all()
    return render(request, 'orders/order_detail.html', {'order': order, 'items': items})


@login_required
@cashier_or_admin_required
def update_order_status(request, pk):
    order = get_object_or_404(Order, pk=pk)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in dict(Order.STATUS_CHOICES):
            # Validate the transition before acquiring the lock so invalid
            # requests are rejected cheaply without touching the database row.
            try:
                validate_status_transition(order.status, new_status)
            except ValueError as e:
                return JsonResponse({'success': False, 'error': str(e)})

            # The status transition and its inventory side-effect (restoring
            # stock on cancellation) commit or roll back together, so a failed
            # save can never leave stock restored on an order that is still
            # active (which would allow the same stock to be deducted twice).
            with transaction.atomic():
                # Re-read the order inside the transaction with a row-level
                # lock so a concurrent transition cannot race this one.
                order = get_object_or_404(Order.objects.select_for_update(), pk=pk)

                # Re-validate inside the lock: the status may have changed
                # between the pre-check above and acquiring the lock.
                try:
                    validate_status_transition(order.status, new_status)
                except ValueError as e:
                    return JsonResponse({'success': False, 'error': str(e)})

                # Restore inventory if cancelling
                if new_status == 'cancelled':
                    restore_inventory_for_order(order, performed_by=request.user)

                # Timestamp logic
                now = timezone.now()
                if new_status == 'ready' and not order.ready_at:
                    order.ready_at = now
                elif new_status == 'completed':
                    order.completed_at = now
                    order.cashier = request.user
                elif new_status == 'cancelled' and not order.cancelled_at:
                    order.cancelled_at = now

                order.status = new_status
                order.save()
            return JsonResponse({'success': True, 'status': order.get_status_display()})
    return JsonResponse({'success': False})


@login_required
@cashier_or_admin_required
def process_payment(request, pk):
    if request.method != 'POST':
        # Payment is handled via AJAX POST only (see main.js).
        return JsonResponse({'success': False})

    # Parse the submitted amount as Decimal via str() to avoid IEEE 754
    # float imprecision.  float('123.10') → 123.09999999999999; whereas
    # Decimal('123.10') → Decimal('123.10') exactly.  Never convert monetary
    # POST values through float — store and compare as Decimal throughout.
    raw_amount = request.POST.get('amount_paid', '0')
    try:
        amount_paid = Decimal(str(raw_amount))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid payment amount.'})
    payment_method = request.POST.get('payment_method', 'cash')

    VALID_PAYMENT_METHODS = ['cash', 'gcash']
    if payment_method not in VALID_PAYMENT_METHODS:
        return JsonResponse({
            'success': False,
            'error': 'Invalid payment method. Only Cash and GCash are accepted.',
        })

    # Wrap the entire read-check-write in a transaction with a row-level lock
    # so two concurrent payment submissions for the same order (two cashier
    # windows, a double-tap, a network retry) cannot both pass the is_paid
    # check and record two separate payments.
    with transaction.atomic():
        order = get_object_or_404(
            Order.objects.select_for_update(), pk=pk
        )

        # Idempotency guard: if payment was already recorded (by a concurrent
        # request that won the lock, or a previous submission) return the
        # original result instead of writing again.
        if order.is_paid:
            return JsonResponse({
                'success': False,
                'error': 'This order has already been paid.',
            })

        # Terminal-state guard: payment must not be processed on a cancelled
        # order (stock was already restored on cancellation, and completing a
        # void order would corrupt finance totals and queue state) or on a
        # completed order that somehow isn't marked is_paid yet.
        if order.status in ('cancelled', 'completed'):
            return JsonResponse({
                'success': False,
                'error': (
                    'Cannot process payment: this order has been '
                    f'{order.get_status_display().lower()}.'
                ),
            })

        # Decimal comparison — both sides are Decimal so no float imprecision.
        if amount_paid < order.total:
            return JsonResponse({'success': False, 'error': 'Insufficient payment amount'})

        order.is_paid = True
        order.payment_method = payment_method
        order.amount_paid = amount_paid
        # Decimal subtraction — exact peso/centavo arithmetic, no float rounding.
        order.change_amount = amount_paid - order.total
        order.status = 'completed'
        order.completed_at = timezone.now()
        order.cashier = request.user
        order.save()

    messages.success(request, f'Payment processed! Change: ₱{order.change_amount:.2f}')
    return JsonResponse({
        'success': True,
        'change': float(order.change_amount),
        'order_number': order.order_number,
    })


@login_required
@cashier_or_admin_required
def print_receipt(request, pk):
    # select_related('cashier') preloads the staff member printed on the
    # receipt (order.cashier.get_full_name) -- one query instead of a lazy
    # fetch.
    order = get_object_or_404(Order.objects.select_related('cashier'), pk=pk)
    # Receipt uses only snapshot fields (product_name, size, unit_price,
    # subtotal, quantity) — no FK traversal — so select_related('product')
    # is not needed. only() avoids loading unused OrderItem columns.
    items = order.items.only(
        'product_name', 'size', 'quantity', 'unit_price', 'subtotal',
    )
    return render(request, 'orders/receipt.html', {'order': order, 'items': items})


@login_required
@cashier_or_admin_required
def cashier_pos(request):
    """POS interface for cashier to create orders directly"""
    # Only sellable products (Product.objects.sellable()) are offered on the
    # POS; deactivated products stay hidden and keep their order history.
    # Category.objects.with_sellable_products() prefetches them in one query.
    categories = Category.objects.with_sellable_products()
    return render(request, 'orders/pos.html', {'categories': categories})


@login_required
@cashier_or_admin_required
def create_pos_order(request):
    """Create order from POS terminal"""
    if request.method != 'POST':
        return JsonResponse({'success': False})

    data = json.loads(request.body)
    items_data = data.get('items', [])
    if not items_data:
        return JsonResponse({'success': False, 'error': 'No items in order'})

    # Duplicate-order protection: the POS client sends a request_token with
    # every submission. Replaying the same token -- a double-click, a retry
    # after a lost response -- returns the original order instead of
    # creating a duplicate.
    request_token = data.get('request_token') or None
    if request_token:
        existing = Order.objects.filter(request_token=request_token).first()
        if existing:
            return JsonResponse({
                'success': True,
                'order_id': existing.pk,
                'order_number': existing.order_number,
                'duplicate': True,
            })

    # Build the order, its items, the totals and the stock deduction in
    # ONE transaction: any failure (missing/inactive product, insufficient
    # stock) rolls everything back, so a rejected POS order can never
    # leave a half-created order or a partial deduction behind.
    try:
        with transaction.atomic():
            order = Order.objects.create(
                customer_name=data.get('customer_name', 'Walk-in Customer'),
                table_number=data.get('table_number', ''),
                order_type=data.get('order_type', 'dine_in'),
                notes=data.get('notes', ''),
                cashier=request.user,
                request_token=request_token,
            )

            # Fetch every product for the order in one batched query
            # instead of one lookup per line item. category is prefetched
            # here so the packaging_eligible snapshot can be set without
            # an extra query per item.
            products = {
                p.pk: p for p in Product.objects.select_related('category').filter(
                    pk__in=[item_data['product_id'] for item_data in items_data],
                )
            }

            # Consolidate the payload before inserting: a buggy or malicious
            # client could send the same (product_id, size) pair in multiple
            # rows. Two separate OrderItem rows for the same product/size would
            # trigger the DB UniqueConstraint added in migration 0009 AND
            # would cause a double stock deduction for that product. Merge
            # them here so the rest of the loop always works with a clean,
            # deduplicated list.
            consolidated: dict[tuple, dict] = {}
            for item_data in items_data:
                product = products.get(item_data['product_id'])
                if product is None:
                    raise Http404('Product not found')
                if not product.is_active or not product.is_available:
                    raise ValueError(
                        f'{product.name} is no longer available. '
                        'Please refresh the POS and try again.'
                    )
                size = item_data.get('size', 'none')
                key = (product.pk, size)
                if key in consolidated:
                    consolidated[key]['quantity'] += int(item_data['quantity'])
                else:
                    consolidated[key] = {
                        'product': product,
                        'size': size,
                        'quantity': int(item_data['quantity']),
                    }

            for entry in consolidated.values():
                create_order_item(
                    order=order,
                    product=entry['product'],
                    size=entry['size'],
                    quantity=entry['quantity'],
                )

            order.calculate_total()

            deduct_inventory_for_order(order, performed_by=request.user)
    except ValueError as e:
        # The atomic block rolled back: no order, no items and no stock
        # change were persisted. Same friendly error as before.
        return JsonResponse({'success': False, 'error': str(e)})
    except IntegrityError:
        # A concurrent duplicate slipped past the token lookup: the unique
        # request_token (or order_number) constraint rejected the INSERT.
        # Replay the existing order when the token identifies one.
        if request_token:
            existing = Order.objects.filter(request_token=request_token).first()
            if existing:
                return JsonResponse({
                    'success': True,
                    'order_id': existing.pk,
                    'order_number': existing.order_number,
                    'duplicate': True,
                })
        return JsonResponse({
            'success': False,
            'error': 'Order could not be created. Please try again.',
        })

    return JsonResponse({'success': True, 'order_id': order.pk, 'order_number': order.order_number})


@login_required
@cashier_or_admin_required
@require_GET
def pos_draft_status(request):
    """Whether a POS draft's request_token already created an order.

    The POS client persists its current order (sessionStorage) so an
    accidental refresh can restore it. If the refresh raced a submission that
    actually succeeded server-side, the token matches an existing order -- the
    client must NOT restore that draft (the order is already placed). This is
    purely a read of the idempotency key; it never creates or changes orders.
    """
    token = request.GET.get('request_token') or ''
    if not token:
        return JsonResponse({'success': True, 'placed': False})
    order = Order.objects.filter(request_token=token).first()
    return JsonResponse({
        'success': True,
        'placed': bool(order),
        'order_number': order.order_number if order else None,
    })


@require_GET
def packaging_fee_preview(request):
    """
    Public API endpoint to preview packaging fee for a given order type and items.
    No login required — used by checkout and POS JS.
    """
    order_type = request.GET.get('order_type', 'dine_in')
    fee_per_item = get_packaging_fee_per_item()

    if order_type != 'takeout':
        return JsonResponse({
            'packaging_fee': 0,
            'packaging_fee_formatted': '₱0.00',
            'fee_per_item': float(fee_per_item),
            'eligible_item_count': 0,
        })

    items_raw = request.GET.get('items', '[]')
    try:
        items_data = json.loads(items_raw)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid items parameter'}, status=400)

    # Collect valid (product_id, quantity) pairs, then resolve every product
    # in a single batched query instead of one lookup per item.
    pairs = []
    for item_data in items_data:
        try:
            pairs.append((
                int(item_data['product_id']),
                int(item_data.get('quantity', 1)),
            ))
        except (KeyError, ValueError):
            continue

    products_by_id = {
        product.pk: product
        for product in Product.objects.select_related('category').filter(
            pk__in=[pid for pid, _ in pairs], is_active=True,
        )
    }
    # Pairs whose product is missing or inactive map to None; the shared
    # service skips them, matching the previous per-item skip behaviour.
    products = [(products_by_id.get(pid), qty) for pid, qty in pairs]

    total_fee, eligible_count = calculate_packaging_fee_for_items(products, fee_per_item)

    return JsonResponse({
        'packaging_fee': float(total_fee),
        'packaging_fee_formatted': f'₱{total_fee:.2f}',
        'fee_per_item': float(fee_per_item),
        'eligible_item_count': eligible_count,
    })


# ========== QUEUE / TRACKER VIEWS ==========

def order_tracker(request, order_number):
    """Customer-facing live order tracker. No login required."""
    # Load only the fields the tracker template and get_queue_position() need.
    # The items sub-query is passed separately so order.items.all in the
    # template does not fire an extra unbounded query.
    order = get_object_or_404(
        Order.objects.only(
            'order_number', 'queue_number', 'status',
            'customer_name', 'order_type', 'table_number',
            'packaging_fee', 'total', 'created_at',
        ),
        order_number=order_number,
    )
    items = order.items.only(
        'product_name', 'size', 'quantity',
    )
    return render(request, 'orders/order_tracker.html', {
        'order': order,
        'items': items,
        'queue_position': order.get_queue_position(),
    })


def queue_board(request):
    """Public queue display board for in-store screens. No login required."""
    today = timezone.localdate()
    # only() limits each row to the three fields the template renders.
    _board_fields = ('queue_number', 'customer_name', 'order_type')
    preparing_orders = (
        Order.objects
        .filter(status='preparing', created_at__date=today)
        .only(*_board_fields)
        .order_by('created_at')[:20]
    )
    ready_orders = (
        Order.objects
        .filter(status='ready', created_at__date=today)
        .only(*_board_fields)
        .order_by('created_at')[:20]
    )
    return render(request, 'orders/queue_board.html', {
        'preparing_orders': preparing_orders,
        'ready_orders': ready_orders,
    })


def api_track_order(request, order_number):
    """AJAX polling endpoint for order tracker. No login required."""
    try:
        order = Order.objects.only(
            'order_number', 'queue_number', 'customer_name',
            'order_type', 'status', 'table_number',
            # created_at is required by get_queue_position() — must be in
            # only() to avoid a deferred-field lazy-load on every poll.
            'created_at', 'queued_at', 'ready_at',
        ).get(order_number=order_number)
    except Order.DoesNotExist:
        return JsonResponse({'error': 'Order not found'}, status=404)

    queue_position = order.get_queue_position()

    if order.status == 'pending':
        estimated_minutes = max(1, (queue_position - 1) * 3)
    elif order.status == 'preparing':
        estimated_minutes = 3
    else:
        estimated_minutes = None

    return JsonResponse({
        'order_number':       order.order_number,
        'queue_number':       order.queue_number,
        'customer_name':      order.customer_name,
        'order_type':         order.order_type,
        'order_type_display': order.get_order_type_display(),
        'status':             order.status,
        'status_display':     order.get_status_display(),
        'status_emoji':       order.status_emoji,
        'queue_position':     queue_position,
        'estimated_minutes':  estimated_minutes,
        'table_number':       order.table_number or '',
        'created_at':         order.created_at.isoformat(),
        'queued_at':          order.queued_at.isoformat() if order.queued_at else None,
        'ready_at':           order.ready_at.isoformat() if order.ready_at else None,
        'is_final':           order.status in ['completed', 'cancelled'],
    })


def api_queue_board(request):
    """AJAX polling endpoint for queue board. No login required."""
    today = timezone.localdate()

    preparing = list(
        Order.objects.filter(status='preparing', created_at__date=today)
        .order_by('created_at')
        .values('order_number', 'queue_number', 'customer_name', 'order_type', 'created_at')[:20]
    )
    ready = list(
        Order.objects.filter(status='ready', created_at__date=today)
        .order_by('created_at')
        .values('order_number', 'queue_number', 'customer_name', 'order_type', 'ready_at')[:20]
    )
    queued_count = Order.objects.filter(
        status__in=['pending', 'preparing'], created_at__date=today
    ).count()

    # Serialize datetime fields
    for item in preparing:
        item['created_at'] = item['created_at'].isoformat() if item['created_at'] else None
    for item in ready:
        item['ready_at'] = item['ready_at'].isoformat() if item['ready_at'] else None

    return JsonResponse({
        'preparing':     preparing,
        'ready':         ready,
        'waiting_count': queued_count,
        'last_updated':  timezone.now().isoformat(),
    })


@login_required
@cashier_or_admin_required
def quick_status_advance(request, pk):
    """One-click status advance for cashier order list. POST only."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    # Use a row-level lock so two concurrent taps/clicks on the same order
    # cannot both read the same next_status and each advance it once (which
    # would skip a step). The lock serialises the two requests: the first
    # writes the new status, the second re-reads the already-advanced order
    # and either advances it to the correct next step (if one exists) or
    # returns 'Already final state'. Both outcomes are correct; neither
    # applies the same transition twice.
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)
        next_status = order.next_status

        if not next_status:
            return JsonResponse({'success': False, 'error': 'Already final state'})

        # Confirm the computed next step is still a valid transition given the
        # locked status (belt-and-suspenders: next_status already encodes the
        # forward-only flow, but validate_status_transition() is the single
        # authoritative check for the whole module).
        try:
            validate_status_transition(order.status, next_status)
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)})

        now = timezone.now()
        if next_status == 'ready':
            order.ready_at = now
        if next_status == 'completed':
            order.completed_at = now
            order.cashier = request.user

        order.status = next_status
        order.save()

    return JsonResponse({
        'success':          True,
        'new_status':       next_status,
        'new_status_display': order.get_status_display(),
        'order_number':     order.order_number,
    })
