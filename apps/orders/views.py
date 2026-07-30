"""
Order views - Cart, Checkout, Order management for cashier/admin
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from django.db.models import Q
from decimal import Decimal
from .models import Order, OrderItem, Cart, CartItem
from .forms import CheckoutForm, OrderStatusForm
from apps.menu.models import Product
from apps.accounts.decorators import cashier_or_admin_required, admin_required
import json


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
    return render(request, 'orders/cart.html', {'cart': cart, 'items': items})


@require_POST
def add_to_cart(request, product_id):
    product = get_object_or_404(Product, pk=product_id, is_available=True)
    cart = get_or_create_cart(request)
    size = request.POST.get('size', 'none')
    quantity = int(request.POST.get('quantity', 1))
    unit_price = product.get_price_for_size(size)

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
    item = get_object_or_404(CartItem, pk=item_id)
    quantity = int(request.POST.get('quantity', 1))
    if quantity > 0:
        item.quantity = quantity
        item.save()
    else:
        item.delete()
    cart = get_or_create_cart(request)
    return JsonResponse({
        'success': True,
        'item_subtotal': float(item.subtotal) if quantity > 0 else 0,
        'cart_total': float(cart.total),
        'cart_count': cart.item_count
    })


@require_POST
def remove_from_cart(request, item_id):
    item = get_object_or_404(CartItem, pk=item_id)
    item.delete()
    cart = get_or_create_cart(request)
    return JsonResponse({
        'success': True,
        'cart_total': float(cart.total),
        'cart_count': cart.item_count
    })


def checkout_view(request):
    cart = get_or_create_cart(request)
    items = cart.cart_items.select_related('product').all()

    if not items:
        messages.warning(request, 'Your cart is empty!')
        return redirect('menu:index')

    if request.method == 'POST':
        form = CheckoutForm(request.POST)
        if form.is_valid():
            # Create order
            order = Order.objects.create(
                customer_name=form.cleaned_data['customer_name'],
                customer_phone=form.cleaned_data.get('customer_phone', ''),
                table_number=form.cleaned_data.get('table_number', ''),
                order_type=form.cleaned_data['order_type'],
                notes=form.cleaned_data.get('notes', ''),
            )

            # Add items
            for cart_item in items:
                OrderItem.objects.create(
                    order=order,
                    product=cart_item.product,
                    product_name=cart_item.product.name,
                    size=cart_item.size,
                    quantity=cart_item.quantity,
                    unit_price=cart_item.unit_price,
                    subtotal=cart_item.subtotal,
                )

            order.calculate_total()

            # Deduct inventory via service layer
            from apps.orders.services import deduct_inventory_for_order
            try:
                deduct_inventory_for_order(order, performed_by=None)
            except ValueError as e:
                order.delete()
                messages.error(request, str(e))
                return redirect('orders:cart')

            # Clear cart
            cart.cart_items.all().delete()

            request.session['last_order_id'] = order.pk
            return redirect('orders:order_success', pk=order.pk)
    else:
        form = CheckoutForm()

    return render(request, 'orders/checkout.html', {'cart': cart, 'items': items, 'form': form})


def order_success(request, pk):
    order = get_object_or_404(Order, pk=pk)
    return render(request, 'orders/order_success.html', {'order': order})


# ========== STAFF / CASHIER VIEWS ==========

@login_required
@cashier_or_admin_required
def order_list(request):
    orders = Order.objects.select_related('cashier').prefetch_related('items')
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

    # Paginate
    from django.core.paginator import Paginator
    paginator = Paginator(orders, 20)
    page = request.GET.get('page', 1)
    orders_page = paginator.get_page(page)

    return render(request, 'orders/order_list.html', {
        'orders': orders_page,
        'status_filter': status_filter,
        'search': search,
        'status_choices': Order.STATUS_CHOICES,
        'pending_count': Order.objects.filter(status='pending').count(),
    })


@login_required
@cashier_or_admin_required
def order_detail(request, pk):
    order = get_object_or_404(Order, pk=pk)
    items = order.items.select_related('product').all()
    return render(request, 'orders/order_detail.html', {'order': order, 'items': items})


@login_required
@cashier_or_admin_required
def update_order_status(request, pk):
    order = get_object_or_404(Order, pk=pk)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in dict(Order.STATUS_CHOICES):
            # Restore inventory if cancelling
            if new_status == 'cancelled' and order.status != 'cancelled':
                from apps.orders.services import restore_inventory_for_order
                restore_inventory_for_order(order, performed_by=request.user)

            # Timestamp logic
            now = timezone.now()
            if new_status == 'ready' and not order.ready_at:
                order.ready_at = now
            elif new_status == 'completed':
                order.completed_at = now
                order.cashier = request.user

            order.status = new_status
            order.save()
            return JsonResponse({'success': True, 'status': order.get_status_display()})
    return JsonResponse({'success': False})


@login_required
@cashier_or_admin_required
def process_payment(request, pk):
    order = get_object_or_404(Order, pk=pk)
    if request.method == 'POST':
        amount_paid = float(request.POST.get('amount_paid', 0))
        payment_method = request.POST.get('payment_method', 'cash')

        VALID_PAYMENT_METHODS = ['cash', 'gcash']
        if payment_method not in VALID_PAYMENT_METHODS:
            return JsonResponse({
                'success': False,
                'error': 'Invalid payment method. Only Cash and GCash are accepted.',
            })

        if amount_paid >= float(order.total):
            order.is_paid = True
            order.payment_method = payment_method
            order.amount_paid = amount_paid
            order.change_amount = amount_paid - float(order.total)
            order.status = 'completed'
            order.completed_at = timezone.now()
            order.cashier = request.user
            order.save()
            messages.success(request, f'Payment processed! Change: ₱{order.change_amount:.2f}')
            return JsonResponse({
                'success': True,
                'change': float(order.change_amount),
                'order_number': order.order_number
            })
        else:
            return JsonResponse({'success': False, 'error': 'Insufficient payment amount'})
    return render(request, 'orders/payment.html', {'order': order})


@login_required
@cashier_or_admin_required
def print_receipt(request, pk):
    order = get_object_or_404(Order, pk=pk)
    items = order.items.all()
    return render(request, 'orders/receipt.html', {'order': order, 'items': items})


@login_required
@cashier_or_admin_required
def cashier_pos(request):
    """POS interface for cashier to create orders directly"""
    from apps.menu.models import Category
    categories = Category.objects.filter(is_active=True).prefetch_related('products')
    return render(request, 'orders/pos.html', {'categories': categories})


@login_required
@cashier_or_admin_required
def create_pos_order(request):
    """Create order from POS terminal"""
    if request.method == 'POST':
        data = json.loads(request.body)
        items_data = data.get('items', [])
        if not items_data:
            return JsonResponse({'success': False, 'error': 'No items in order'})

        order = Order.objects.create(
            customer_name=data.get('customer_name', 'Walk-in Customer'),
            table_number=data.get('table_number', ''),
            order_type=data.get('order_type', 'dine_in'),
            notes=data.get('notes', ''),
            cashier=request.user,
        )

        for item_data in items_data:
            product = get_object_or_404(Product, pk=item_data['product_id'])
            size = item_data.get('size', 'none')
            quantity = item_data['quantity']
            unit_price = product.get_price_for_size(size)

            OrderItem.objects.create(
                order=order,
                product=product,
                product_name=product.name,
                size=size,
                quantity=quantity,
                unit_price=unit_price,
                subtotal=unit_price * quantity,
            )

        order.calculate_total()

        # Deduct inventory via service layer
        from apps.orders.services import deduct_inventory_for_order
        try:
            deduct_inventory_for_order(order, performed_by=request.user)
        except ValueError as e:
            order.delete()
            return JsonResponse({'success': False, 'error': str(e)})

        return JsonResponse({'success': True, 'order_id': order.pk, 'order_number': order.order_number})

    return JsonResponse({'success': False})


@require_GET
def packaging_fee_preview(request):
    """
    Public API endpoint to preview packaging fee for a given order type and items.
    No login required — used by checkout and POS JS.
    """
    from apps.orders.services import get_packaging_fee_per_item

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

    total_fee = Decimal('0.00')
    eligible_count = 0

    for item_data in items_data:
        try:
            product = Product.objects.select_related('category').get(pk=item_data['product_id'])
            qty = int(item_data.get('quantity', 1))
            if product.category.is_packaging_required:
                total_fee += fee_per_item * qty
                eligible_count += qty
        except (Product.DoesNotExist, KeyError, ValueError):
            continue

    return JsonResponse({
        'packaging_fee': float(total_fee),
        'packaging_fee_formatted': f'₱{total_fee:.2f}',
        'fee_per_item': float(fee_per_item),
        'eligible_item_count': eligible_count,
    })


# ========== QUEUE / TRACKER VIEWS ==========

def order_tracker(request, order_number):
    """Customer-facing live order tracker. No login required."""
    order = get_object_or_404(Order, order_number=order_number)
    return render(request, 'orders/order_tracker.html', {
        'order': order,
        'queue_position': order.get_queue_position(),
    })


def queue_board(request):
    """Public queue display board for in-store screens. No login required."""
    today = timezone.now().date()
    preparing_orders = Order.objects.filter(
        status='preparing', created_at__date=today
    ).order_by('created_at')[:20]
    ready_orders = Order.objects.filter(
        status='ready', created_at__date=today
    ).order_by('created_at')[:20]
    return render(request, 'orders/queue_board.html', {
        'preparing_orders': preparing_orders,
        'ready_orders': ready_orders,
    })


def api_track_order(request, order_number):
    """AJAX polling endpoint for order tracker. No login required."""
    try:
        order = Order.objects.get(order_number=order_number)
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
    today = timezone.now().date()

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

    order = get_object_or_404(Order, pk=pk)
    next_status = order.next_status

    if not next_status:
        return JsonResponse({'success': False, 'error': 'Already final state'})

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
