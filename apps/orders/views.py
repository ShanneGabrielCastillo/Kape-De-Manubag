"""
Order views - Cart, Checkout, Order management for cashier/admin
"""
import json
import logging
import secrets
import time

from django.conf import settings
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
    calculate_packaging_fee,
    calculate_packaging_fee_for_items,
    get_packaging_fee_per_item,
    create_order_item,
    VALID_TRANSITIONS,
)
from apps.audit.services import log_action

# ── Anonymous order rate limiting ─────────────────────────────────────────────
# Session key under which the sliding-window counter is stored.
# Format: {'timestamps': [epoch_float, ...]}  — list of successful order
# creation times within the current window.  Stored in the Django session so
# it persists across processes (PostgreSQL-backed session store on Render) and
# is automatically scoped to one browser session.
_ORDER_RATE_SESSION_KEY = 'order_rate_timestamps'


def _check_order_rate_limit(request):
    """Return (allowed: bool, wait_seconds: int).

    Reads ORDER_RATE_LIMIT (max orders) and ORDER_RATE_WINDOW (seconds) from
    Django settings.  Only timestamps of *successfully created* orders within
    the current sliding window are counted — failed validation attempts never
    consume quota.

    The counter is session-stored, which is appropriate for anonymous customers
    and compatible with multi-worker deployments (sessions are PostgreSQL-backed
    on Render).  We accept a small race window for truly simultaneous requests
    from the same session (an extremely rare edge case for a physical café); a
    database-level solution would add significant complexity for negligible
    practical benefit at this scale.
    """
    limit = getattr(settings, 'ORDER_RATE_LIMIT', 3)
    window = getattr(settings, 'ORDER_RATE_WINDOW', 600)
    now = time.time()
    cutoff = now - window

    timestamps = request.session.get(_ORDER_RATE_SESSION_KEY, [])
    # Prune timestamps that are outside the current window.
    timestamps = [t for t in timestamps if t > cutoff]

    if len(timestamps) >= limit:
        oldest = min(timestamps)
        wait = int(oldest + window - now) + 1  # +1 so display is never 0
        return False, max(wait, 1)

    return True, 0


def _record_order_created(request):
    """Append the current timestamp to the session's order rate counter.

    Called exactly once per *successfully created* order so that failed
    checkouts (validation errors, stock issues, etc.) do not penalise the
    customer.
    """
    window = getattr(settings, 'ORDER_RATE_WINDOW', 600)
    now = time.time()
    cutoff = now - window

    timestamps = request.session.get(_ORDER_RATE_SESSION_KEY, [])
    timestamps = [t for t in timestamps if t > cutoff]  # prune expired
    timestamps.append(now)
    request.session[_ORDER_RATE_SESSION_KEY] = timestamps

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
            # Payment-first: awaiting_payment orders → payment waiting page.
            if duplicate.status == 'awaiting_payment':
                return redirect('orders:payment_waiting', tracking_token=duplicate.tracking_token)
            # Already past payment → tracker.
            if duplicate.status not in ('cancelled',):
                return redirect('orders:order_tracker', tracking_token=duplicate.tracking_token)
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
            #
            # Rate-limit check: enforce the per-session order limit BEFORE
            # attempting to create anything.  This sits here (after all
            # cart-validation bounces) so that failed validation attempts
            # (empty cart, unavailable products, quantity adjustments) never
            # consume quota — only a genuine new-order attempt does.
            # The idempotency check above already short-circuits replays of
            # an already-created order, so those never reach this point.
            rate_allowed, rate_wait = _check_order_rate_limit(request)
            if not rate_allowed:
                wait_minutes = (rate_wait + 59) // 60  # ceil to whole minutes
                messages.error(
                    request,
                    f"You've reached the ordering limit. "
                    f"Please wait about {wait_minutes} minute"
                    f"{'s' if wait_minutes != 1 else ''} before placing "
                    "another order.",
                )
                # Do not clear the cart — the customer's items are preserved
                # so they can retry once the window expires.
                return render(request, 'orders/checkout.html', {
                    'cart': cart,
                    'items': items,
                    'form': form,
                    'request_token': request_token,
                })

            try:
                with transaction.atomic():
                    order = Order.objects.create(
                        customer_name=form.cleaned_data['customer_name'],
                        customer_phone=form.cleaned_data.get('customer_phone', ''),
                        table_number=form.cleaned_data.get('table_number', ''),
                        order_type=form.cleaned_data['order_type'],
                        notes=form.cleaned_data.get('notes', ''),
                        request_token=request_token,
                        # Payment-first flow: customer orders start in
                        # awaiting_payment. Confirming payment moves them
                        # straight to preparing in one step.
                        status='awaiting_payment',
                        # Store the chosen payment method at order creation so
                        # the payment_waiting page can show the correct flow.
                        payment_method=form.cleaned_data.get('payment_method', 'cash'),
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

                    # Broadcast new_order AFTER totals are set and the
                    # transaction is fully committed, so the cashier dashboard
                    # always sees the correct total (not 0.00).
                    # on_commit() defers the call until the outer
                    # transaction.atomic() block commits successfully — if
                    # anything rolls back, the broadcast is never sent.
                    def _broadcast_new_order():
                        from apps.realtime.broker import publish as rt_publish
                        rt_publish('new_order', {
                            'order_id':       order.pk,
                            'order_number':   order.order_number,
                            'queue_number':   order.queue_number,
                            'customer_name':  order.customer_name,
                            'order_type':     order.get_order_type_display(),
                            'total':          float(order.total),
                            'item_count':     order.items.count(),
                            'status':         order.status,
                            'status_display': order.get_status_display(),
                            'is_paid':        order.is_paid,
                            'created_at':     order.created_at.isoformat(),
                            'request_token':  order.request_token,
                        })
                    transaction.on_commit(_broadcast_new_order)
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
                    if duplicate.status == 'awaiting_payment':
                        return redirect('orders:payment_waiting', tracking_token=duplicate.tracking_token)
                    if duplicate.status not in ('cancelled',):
                        return redirect('orders:order_tracker', tracking_token=duplicate.tracking_token)
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
            # Record this successful order creation in the rate-limit counter.
            # This is called only on the genuine success path — failed
            # checkouts, stock rejections, and idempotency replays never
            # reach this line, so they never consume quota.
            _record_order_created(request)
            request.session.pop('checkout_request_token', None)
            # Payment-first flow: send the customer to the payment-waiting
            # page rather than the "Order Received" success page.  The
            # success page is only shown after staff confirms payment AND
            # accepts the order.
            return redirect('orders:payment_waiting', tracking_token=order.tracking_token)
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
             'discount', 'queue_number', 'status', 'tracking_token',
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


def payment_waiting(request, tracking_token):
    """Customer-facing payment-waiting page.

    Shown immediately after checkout.  The page covers:
      - Cash orders: "Go to the cashier" instructions
      - GCash orders: owner GCash info + reference submission form
      - After payment confirmed: "Order Received" overlay

    Access is gated by the cryptographically random tracking_token.
    """
    order = get_object_or_404(
        Order.objects.only(
            'order_number', 'queue_number', 'customer_name',
            'order_type', 'table_number', 'total', 'subtotal',
            'packaging_fee', 'status', 'is_paid', 'tracking_token',
            'payment_method', 'gcash_status', 'created_at',
        ),
        tracking_token=tracking_token,
    )
    items = order.items.only(
        'product_name', 'size', 'quantity', 'unit_price', 'subtotal',
    )
    from apps.dashboard.models import GCashSettings
    gcash_settings = GCashSettings.get_settings() if order.payment_method == 'gcash' else None
    from .forms import GCashSubmissionForm
    gcash_form = GCashSubmissionForm() if (
        order.payment_method == 'gcash'
        and order.gcash_status in ('none', 'rejected')
        and not order.is_paid
        and order.status == 'awaiting_payment'
    ) else None
    return render(request, 'orders/payment_waiting.html', {
        'order': order,
        'items': items,
        'gcash_settings': gcash_settings,
        'gcash_form': gcash_form,
    })


def api_payment_waiting_status(request, tracking_token):
    """AJAX/polling endpoint for the payment-waiting page.

    Returns the order's current status and payment state so the waiting
    page can update without a full page reload.  Only the fields needed
    by the waiting page are returned — no payment amounts or cashier info.
    """
    try:
        order = Order.objects.only(
            'order_number', 'queue_number', 'customer_name',
            'order_type', 'status', 'is_paid', 'tracking_token',
            'payment_method', 'gcash_status',
        ).get(tracking_token=tracking_token)
    except Order.DoesNotExist:
        return JsonResponse({'error': 'Order not found'}, status=404)

    return JsonResponse({
        'order_number':      order.order_number,
        'queue_number':      order.queue_number,
        'status':            order.status,
        'status_display':    order.get_status_display(),
        'is_paid':           order.is_paid,
        'payment_method':    order.payment_method,
        'gcash_status':      order.gcash_status,
        # True once the cashier has confirmed payment (order moved to
        # preparing or beyond). The waiting page uses this to show the
        # "Order Received" overlay and redirect to the tracker.
        'order_accepted':    order.status not in ('awaiting_payment', 'cancelled'),
        'is_cancelled':      order.status == 'cancelled',
        'tracker_url':       f'/orders/track/{order.tracking_token}/',
    })


# ── GCash customer submission ─────────────────────────────────────────────────

@require_POST
def submit_gcash_payment(request, tracking_token):
    """Customer submits their GCash reference number and optional proof.

    Access is gated by the cryptographically random tracking_token — the same
    mechanism used for the payment_waiting page and api_track_order.  No login
    required (anonymous customers).

    CRITICAL: This view NEVER sets is_paid=True.  It only stores the customer's
    submission data and updates gcash_status to 'pending'.  Staff must explicitly
    verify the payment via verify_gcash_payment.
    """
    # ── Fast read-only guard checks (no lock, no transaction) ─────────────
    # Validate the token and basic eligibility before doing any form parsing
    # or file processing, so invalid requests are rejected cheaply.
    order = get_object_or_404(Order, tracking_token=tracking_token)

    if order.payment_method != 'gcash':
        return JsonResponse({'success': False, 'error': 'This order is not a GCash order.'}, status=400)

    if order.is_paid:
        return JsonResponse({'success': False, 'error': 'This order has already been paid.'}, status=400)

    if order.status in ('cancelled', 'completed'):
        return JsonResponse({
            'success': False,
            'error': f'Cannot submit payment: this order has been {order.get_status_display().lower()}.',
        }, status=400)

    if order.status != 'awaiting_payment':
        return JsonResponse({
            'success': False,
            'error': 'Payment cannot be submitted for this order at this stage.',
        }, status=400)

    # Idempotent: already submitted and pending — tell the customer without re-writing.
    if order.gcash_status == 'pending':
        return JsonResponse({
            'success': True,
            'already_submitted': True,
            'message': 'Your payment is already submitted and waiting for verification.',
            'gcash_status': 'pending',
        })

    if order.gcash_status == 'verified':
        return JsonResponse({'success': False, 'error': 'This payment has already been verified.'}, status=400)

    # ── Form validation (before acquiring any lock) ───────────────────────
    from .forms import GCashSubmissionForm
    form = GCashSubmissionForm(request.POST, request.FILES)
    if not form.is_valid():
        errors = {field: errs[0] for field, errs in form.errors.items()}
        return JsonResponse({'success': False, 'errors': errors}, status=400)

    reference = form.cleaned_data['gcash_reference']
    proof_file = form.cleaned_data.get('gcash_proof')

    # ── Duplicate reference check (read-only, before lock) ────────────────
    duplicate_qs = Order.objects.filter(
        gcash_reference=reference,
        gcash_status__in=('pending', 'verified'),
    ).exclude(pk=order.pk)
    if duplicate_qs.exists():
        return JsonResponse({
            'success': False,
            'errors': {
                'gcash_reference': (
                    'This GCash reference number has already been submitted '
                    'for another order. Please check the reference and try again.'
                ),
            },
        }, status=400)

    # ── Atomic write — lock only for the actual update ────────────────────
    with transaction.atomic():
        # Re-read with lock inside the transaction to guard against a race
        # where two concurrent submissions arrive simultaneously.
        order = get_object_or_404(Order.objects.select_for_update(), pk=order.pk)

        # Re-check inside the lock (state may have changed since the read above).
        if order.is_paid or order.gcash_status not in ('none', 'rejected'):
            return JsonResponse({
                'success': True,
                'already_submitted': True,
                'message': 'Your payment has already been submitted.',
                'gcash_status': order.gcash_status,
            })

        order.gcash_reference = reference
        order.gcash_status = 'pending'
        order.gcash_submitted_at = timezone.now()
        if proof_file:
            order.gcash_proof = proof_file
        order.save(update_fields=[
            'gcash_reference', 'gcash_status', 'gcash_submitted_at', 'gcash_proof',
        ])

        log_action(
            None,
            'order.gcash_submitted',
            order,
            detail=f'GCash ref: {reference} — proof: {"yes" if proof_file else "no"}',
        )

        from apps.realtime.broker import publish as rt_publish
        rt_publish('gcash_submitted', {
            'order_id':        order.pk,
            'order_number':    order.order_number,
            'customer_name':   order.customer_name,
            'total':           float(order.total),
            'gcash_reference': reference,
        })

    return JsonResponse({
        'success': True,
        'already_submitted': False,
        'message': 'Your payment has been submitted for verification. Please wait for staff confirmation.',
        'gcash_status': 'pending',
    })


# ========== STAFF / CASHIER VIEWS ==========

@login_required
@cashier_or_admin_required
def order_list(request):
    status_filter = request.GET.get('status', '')
    search = request.GET.get('q', '')

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
               'completed_at', 'cancelled_at',
               'gcash_reference', 'gcash_notes',
               'gcash_submitted_at', 'gcash_verified_at')
        .order_by('-created_at')
    )

    if status_filter:
        orders = orders.filter(status=status_filter)
    if search:
        orders = orders.filter(
            Q(order_number__icontains=search) |
            Q(customer_name__icontains=search) |
            Q(table_number__icontains=search)
        )

    paginator = Paginator(orders, 10)
    page = request.GET.get('page', 1)
    orders_page = paginator.get_page(page)

    pending_count = Order.objects.filter(status='awaiting_payment').count()

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
    # select_related preloads FK staff members shown on the page — one query.
    order = get_object_or_404(
        Order.objects.select_related('cashier', 'gcash_verified_by'), pk=pk
    )
    items = order.items.select_related('product').all()
    return render(request, 'orders/order_detail.html', {
        'order': order,
        'items': items,
        'status_choices': Order.STATUS_CHOICES,
    })


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

                # Capture the previous status now, after the lock is acquired,
                # so the audit entry records the accurate before/after transition.
                previous_status = order.status

                # Re-validate inside the lock: the status may have changed
                # between the pre-check above and acquiring the lock.
                try:
                    validate_status_transition(order.status, new_status, order=order)
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

                # ── Audit log ────────────────────────────────────────────────
                # Placed inside the transaction so a rolled-back save never
                # leaves a false "success" entry in the audit trail.
                # Cancellation gets its own distinct action so admins can
                # filter/search for cancellations specifically.
                if new_status == 'cancelled':
                    log_action(
                        request.user, 'order.cancel', order,
                        detail=f'Cancelled — was {previous_status}',
                    )
                else:
                    log_action(
                        request.user, 'order.status_changed', order,
                        detail=f'{previous_status} → {new_status}',
                    )

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

        # ── GCash orders must use the manual verification flow ───────────────
        # A customer-placed GCash order (awaiting_payment + payment_method='gcash')
        # should NOT be confirmed via this endpoint — the customer submits their
        # reference number via submit_gcash_payment and staff verifies via
        # verify_gcash_payment.  This guard prevents staff from accidentally
        # bypassing the verification step by using the Cash "Pay" modal on a
        # GCash order.
        #
        # POS staff may still use this endpoint for any order they create
        # directly (cashier is set) regardless of payment method, since POS
        # is an in-person transaction that doesn't need remote verification.
        if (order.status == 'awaiting_payment'
                and order.payment_method == 'gcash'
                and order.cashier_id is None):
            return JsonResponse({
                'success': False,
                'error': (
                    'This is a GCash order. Please use the GCash Verify button '
                    'to confirm the customer\'s payment after checking your GCash account.'
                ),
            })

        # ── Customer order vs POS order ──────────────────────────────────────
        # Customer orders (placed anonymously via checkout, cashier is None)
        # now auto-accept on payment: confirming payment moves the order
        # straight from awaiting_payment → pending so the kitchen can start
        # immediately.  No separate "Accept Order" step is needed.
        #
        # POS orders (placed by staff, cashier is set) complete immediately
        # on payment — the legacy single-step POS workflow is unchanged.
        if order.status == 'awaiting_payment':
            # Payment confirmed → automatically move to preparing.
            # Applies to both customer checkout orders and POS orders.
            order.status = 'preparing'
            order.cashier = request.user
            order.save()
        else:
            # Order is already past awaiting_payment (e.g. a manual reprocess
            # attempt on a preparing/ready order). Just record the payment
            # without changing the status.
            order.cashier = request.user
            order.save()

        # ── Audit log ────────────────────────────────────────────────────────
        log_action(
            request.user, 'order.payment', order,
            detail=(
                f'{order.get_payment_method_display()} '
                f'₱{order.amount_paid} — Change ₱{order.change_amount}'
            ),
        )
        if order.status == 'preparing':
            # Auto-accepted and moved to preparing.
            log_action(
                request.user, 'order.accepted', order,
                detail='Payment confirmed — order auto-accepted and moved to Preparing.',
            )

        # Broadcast payment_confirmed + order_accepted so the customer's
        # waiting page transitions straight to "Order Received".
        from apps.realtime.broker import publish as rt_publish
        rt_publish('payment_confirmed', {
            'order_id':     order.pk,
            'order_number': order.order_number,
            'is_paid':      True,
            'status':       order.status,
        })
        if order.status == 'preparing':
            rt_publish('order_accepted', {
                'order_id':           order.pk,
                'order_number':       order.order_number,
                'new_status':         order.status,
                'new_status_display': order.get_status_display(),
            })

    return JsonResponse({
        'success': True,
        'change': float(order.change_amount),
        'order_number': order.order_number,
    })


@login_required
@cashier_or_admin_required
def accept_order(request, pk):
    """Accept a paid customer order, moving it from PENDING → PREPARING.

    This is the staff-side "Accept Order" action in the payment-first flow.
    Server-side enforcement:
      - The order must be PENDING (the only valid pre-preparation status).
      - The order must be a customer order (cashier is None).
      - The order must already be marked is_paid=True.
      - Only authenticated cashier/admin staff can call this endpoint.
    Attempting to accept an unpaid order returns a 400 JSON error regardless
    of UI state — the client-side button being disabled is defence-in-depth only.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)

        # ── Guard: must be in awaiting_payment ───────────────────────────
        if order.status != 'awaiting_payment':
            return JsonResponse({
                'success': False,
                'error': (
                    f'Cannot accept order: current status is '
                    f'"{order.get_status_display()}", not Awaiting Payment.'
                ),
            }, status=400)

        if order.cashier_id is not None:
            return JsonResponse({
                'success': False,
                'error': (
                    'Cannot accept order: this is a POS order and does not '
                    'require staff acceptance.'
                ),
            }, status=400)

        # ── Server-side payment gate — NOT just a UI guard ────────────────
        # Even if the button is hidden for unpaid orders in the UI, a direct
        # API call must be rejected here so the invariant cannot be bypassed.
        if not order.is_paid:
            return JsonResponse({
                'success': False,
                'error': (
                    'Cannot accept order: payment has not been confirmed yet. '
                    'Please process the payment first.'
                ),
            }, status=400)

        # All checks pass — accept the order and move it into the prep queue.
        order.status = 'preparing'
        order.cashier = request.user
        order.queued_at = timezone.now()
        order.save()

        log_action(
            request.user, 'order.accepted', order,
            detail='Order accepted after payment confirmation — moved to Preparing.',
        )

        # Broadcast a dedicated order_accepted event.  The customer's waiting
        # page listens for this to show the "Order Received" overlay and
        # redirect to the tracker.
        from apps.realtime.broker import publish as rt_publish
        rt_publish('order_accepted', {
            'order_id':           order.pk,
            'order_number':       order.order_number,
            'new_status':         order.status,
            'new_status_display': order.get_status_display(),
        })

    return JsonResponse({
        'success':            True,
        'order_number':       order.order_number,
        'new_status':         order.status,
        'new_status_display': order.get_status_display(),
    })


@login_required
@cashier_or_admin_required
def verify_gcash_payment(request, pk):
    """Staff confirms a customer's GCash payment is legitimate.

    After verification:
    - gcash_status → 'verified'
    - is_paid → True
    - payment fields filled in (amount = order.total, change = 0)
    - gcash_verified_at and gcash_verified_by recorded
    - order status advances awaiting_payment → preparing
    - realtime: payment_confirmed + order_accepted sent to customer
    - audit log entry created

    CRITICAL security checks (all server-side):
    - Requires authenticated cashier/admin
    - Order must exist
    - payment_method must be 'gcash'
    - gcash_status must be 'pending' (customer must have submitted)
    - is_paid must still be False (idempotency guard)
    - Order must be in 'awaiting_payment' status
    - Not cancelled / completed
    - Uses SELECT FOR UPDATE to prevent concurrent double-verification
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)

        if order.payment_method != 'gcash':
            return JsonResponse({
                'success': False,
                'error': 'This is not a GCash order.',
            }, status=400)

        if order.is_paid:
            return JsonResponse({
                'success': False,
                'error': 'This order has already been marked as paid.',
            }, status=400)

        if order.gcash_status != 'pending':
            return JsonResponse({
                'success': False,
                'error': (
                    'Cannot verify: the customer has not submitted a GCash '
                    'reference number yet, or the payment was already processed.'
                ),
            }, status=400)

        if order.status in ('cancelled', 'completed'):
            return JsonResponse({
                'success': False,
                'error': f'Cannot verify: order is {order.get_status_display().lower()}.',
            }, status=400)

        if order.status != 'awaiting_payment':
            return JsonResponse({
                'success': False,
                'error': f'Cannot verify: order status is "{order.get_status_display()}".',
            }, status=400)

        now = timezone.now()

        # Mark payment as verified + paid.
        order.gcash_status = 'verified'
        order.gcash_verified_at = now
        order.gcash_verified_by = request.user
        order.is_paid = True
        order.amount_paid = order.total
        order.change_amount = Decimal('0.00')
        order.cashier = request.user

        # Advance order into preparation queue (same as process_payment for Cash).
        order.status = 'preparing'

        order.save()

        log_action(
            request.user, 'order.gcash_verified', order,
            detail=f'GCash ref {order.gcash_reference} verified — order moved to Preparing.',
        )
        log_action(
            request.user, 'order.payment', order,
            detail=f'GCash ₱{order.amount_paid} — verified by staff',
        )

        from apps.realtime.broker import publish as rt_publish
        rt_publish('payment_confirmed', {
            'order_id':      order.pk,
            'order_number':  order.order_number,
            'is_paid':       True,
            'status':        order.status,
        })
        rt_publish('order_accepted', {
            'order_id':           order.pk,
            'order_number':       order.order_number,
            'new_status':         order.status,
            'new_status_display': order.get_status_display(),
        })

    return JsonResponse({
        'success':           True,
        'order_number':      order.order_number,
        'new_status':        order.status,
        'new_status_display': order.get_status_display(),
    })


@login_required
@cashier_or_admin_required
def reject_gcash_payment(request, pk):
    """Staff rejects an unverifiable GCash payment submission.

    After rejection:
    - gcash_status → 'rejected'
    - is_paid stays False
    - order status stays 'awaiting_payment'
    - gcash_verified_at and gcash_verified_by recorded
    - optional rejection note stored
    - realtime: gcash_rejected sent to customer so they can re-submit
    - audit log entry created

    The customer may re-submit corrected payment information (the submit view
    allows re-submission when gcash_status='rejected').
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)

        if order.payment_method != 'gcash':
            return JsonResponse({'success': False, 'error': 'Not a GCash order.'}, status=400)

        if order.is_paid:
            return JsonResponse({'success': False, 'error': 'Order is already paid.'}, status=400)

        if order.gcash_status != 'pending':
            return JsonResponse({
                'success': False,
                'error': 'No pending GCash submission to reject.',
            }, status=400)

        if order.status in ('cancelled', 'completed'):
            return JsonResponse({
                'success': False,
                'error': f'Order is {order.get_status_display().lower()}.',
            }, status=400)

        rejection_note = request.POST.get('rejection_note', '').strip()
        now = timezone.now()

        order.gcash_status = 'rejected'
        order.gcash_verified_at = now
        order.gcash_verified_by = request.user
        order.gcash_notes = rejection_note
        order.save(update_fields=[
            'gcash_status', 'gcash_verified_at', 'gcash_verified_by', 'gcash_notes',
        ])

        log_action(
            request.user, 'order.gcash_rejected', order,
            detail=f'GCash ref {order.gcash_reference} rejected. Note: {rejection_note or "(none)"}',
        )

        from apps.realtime.broker import publish as rt_publish
        rt_publish('gcash_rejected', {
            'order_id':      order.pk,
            'order_number':  order.order_number,
            'rejection_note': rejection_note,
        })

    return JsonResponse({
        'success': True,
        'order_number': order.order_number,
        'message': 'Payment submission rejected. Customer can re-submit.',
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
                # POS orders use the same payment-first flow as customer
                # orders: awaiting_payment → preparing on payment confirmation.
                status='awaiting_payment',
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

            # OPT-3: compute totals into the order object in memory here,
            # then pass the three fields to deduct_inventory_for_order so
            # it can include them in its own final order.save() call.
            # This merges what was previously two separate UPDATEs
            # (calculate_total → save #2, stock_deducted → save #3)
            # into a single UPDATE, saving one DB round-trip per order.
            order.subtotal = sum(
                item.subtotal for item in order.items.all()
            )
            order.packaging_fee = calculate_packaging_fee(order)
            order.total = order.subtotal + order.packaging_fee - order.discount

            deduct_inventory_for_order(
                order,
                performed_by=request.user,
                extra_order_update_fields=['subtotal', 'packaging_fee', 'total'],
            )

            # Broadcast new_order after totals are committed so the
            # cashier list always shows the correct amount.
            def _broadcast_pos_order():
                from apps.realtime.broker import publish as rt_publish
                rt_publish('new_order', {
                    'order_id':       order.pk,
                    'order_number':   order.order_number,
                    'queue_number':   order.queue_number,
                    'customer_name':  order.customer_name,
                    'order_type':     order.get_order_type_display(),
                    'total':          float(order.total),
                    'item_count':     order.items.count(),
                    'status':         order.status,
                    'status_display': order.get_status_display(),
                    'is_paid':        order.is_paid,
                    'created_at':     order.created_at.isoformat(),
                    'request_token':  order.request_token,
                })
            transaction.on_commit(_broadcast_pos_order)
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

# ========== QUEUE / TRACKER VIEWS ==========

def order_tracker(request, tracking_token):
    """Customer-facing live order tracker. No login required.

    Access is gated by the order's cryptographically random tracking_token
    (URL-safe, ~256 bits of entropy) rather than the predictable order_number,
    so one customer cannot enumerate another customer's order details.  The
    order_number is still displayed on the page and used for the queue board
    and staff workflows — it is just not the access key for this endpoint.
    """
    order = get_object_or_404(
        Order.objects.only(
            'order_number', 'queue_number', 'status',
            'customer_name', 'order_type', 'table_number',
            'packaging_fee', 'total', 'created_at', 'tracking_token',
        ),
        tracking_token=tracking_token,
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


def api_track_order(request, tracking_token):
    """AJAX polling endpoint for order tracker. No login required.

    Looks up the order by its cryptographically random tracking_token so
    enumeration of order numbers cannot expose other customers' data.
    Returns only the fields the tracker UI needs — no payment amounts,
    cashier info, or internal flags are included.
    """
    try:
        order = Order.objects.only(
            'order_number', 'queue_number', 'customer_name',
            'order_type', 'status', 'table_number', 'is_paid',
            # created_at is required by get_queue_position() — must be in
            # only() to avoid a deferred-field lazy-load on every poll.
            'created_at', 'queued_at', 'ready_at',
        ).get(tracking_token=tracking_token)
    except Order.DoesNotExist:
        return JsonResponse({'error': 'Order not found'}, status=404)

    queue_position = order.get_queue_position()

    if order.status == 'awaiting_payment':
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
        'is_paid':            order.is_paid,
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
        status__in=['awaiting_payment', 'preparing'], created_at__date=today
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
        # authoritative check for the whole module).  Pass the full order so
        # the payment-gate rule is also enforced here.
        try:
            validate_status_transition(order.status, next_status, order=order)
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)})

        # Capture the current status before advancing so the audit entry
        # records the accurate before/after transition.
        prev_status = order.status

        now = timezone.now()
        if next_status == 'ready':
            order.ready_at = now
        if next_status == 'completed':
            order.completed_at = now
            order.cashier = request.user

        order.status = next_status
        order.save()

        # ── Audit log ────────────────────────────────────────────────────────
        # Placed inside the transaction so the audit entry rolls back if the
        # save fails. quick_status_advance only moves forward (never cancels),
        # so order.status_changed is always the correct action here.
        log_action(
            request.user, 'order.status_changed', order,
            detail=f'{prev_status} → {next_status}',
        )

    return JsonResponse({
        'success':          True,
        'new_status':       next_status,
        'new_status_display': order.get_status_display(),
        'order_number':     order.order_number,
    })
