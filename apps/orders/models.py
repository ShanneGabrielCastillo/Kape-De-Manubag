"""
Order Models for Kape De Manubag System
Handles cart, orders, order items, and payments
"""
from django.db import models
from django.conf import settings
from apps.menu.models import Product
import uuid


class Order(models.Model):
    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('preparing', 'Preparing'),
        ('ready',     'Ready'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    ORDER_TYPE_CHOICES = [
        ('dine_in', 'Dine-In'),
        ('takeout', 'Take-Out'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('gcash', 'GCash'),
        ('card', 'Card'),
    ]

    order_number = models.CharField(max_length=20, unique=True, blank=True)
    customer_name = models.CharField(max_length=200, default='Walk-in Customer')
    customer_phone = models.CharField(max_length=15, blank=True)
    table_number = models.CharField(max_length=10, blank=True)
    order_type = models.CharField(max_length=20, choices=ORDER_TYPE_CHOICES, default='dine_in')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    packaging_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text="Packaging fee for Take-Out orders.")
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Payment
    is_paid = models.BooleanField(default=False)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    change_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Inventory
    stock_deducted = models.BooleanField(default=False,
        help_text="True after inventory has been deducted for this order.")

    # Staff
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='orders_handled'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    queue_number = models.PositiveIntegerField(null=True, blank=True)
    queued_at    = models.DateTimeField(null=True, blank=True)
    ready_at     = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.order_number:
            # Generate order number: KDM-YYYYMMDD-XXXX
            from django.utils import timezone
            today = timezone.now().strftime('%Y%m%d')
            count = Order.objects.filter(created_at__date=timezone.now().date()).count() + 1
            self.order_number = f"KDM-{today}-{count:04d}"
        if not self.queue_number:
            from django.utils import timezone
            self.queue_number = Order.objects.filter(
                created_at__date=timezone.now().date()
            ).count() + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.order_number} - {self.customer_name}"

    def calculate_total(self):
        from decimal import Decimal
        from apps.orders.services import calculate_packaging_fee
        self.subtotal = sum(item.subtotal for item in self.items.all())
        self.packaging_fee = calculate_packaging_fee(self)
        self.total = self.subtotal + self.packaging_fee - self.discount
        self.save(update_fields=['subtotal', 'packaging_fee', 'total'])

    def get_queue_position(self):
        active = ['pending', 'preparing']
        if self.status not in active:
            return 0
        ahead = Order.objects.filter(
            status__in=active,
            created_at__lt=self.created_at
        ).count()
        return ahead + 1

    @property
    def next_status(self):
        flow = {
            'pending':   'preparing',
            'preparing': 'ready',
            'ready':     'completed',
        }
        return flow.get(self.status)

    @property
    def status_emoji(self):
        return {
            'pending':   '🕐',
            'preparing': '🍳',
            'ready':     '✅',
            'completed': '🎉',
            'cancelled': '❌',
        }.get(self.status, '🕐')

    class Meta:
        ordering = ['-created_at']


class OrderItem(models.Model):
    SIZE_CHOICES = [
        ('none', 'Regular'),
        ('small', 'Small'),
        ('medium', 'Medium'),
        ('large', 'Large'),
        ('hot', 'Hot'),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    product_name = models.CharField(max_length=200)  # Store name in case product deleted
    size = models.CharField(max_length=20, choices=SIZE_CHOICES, default='none')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.CharField(max_length=200, blank=True)

    def save(self, *args, **kwargs):
        if self.product and not self.product_name:
            self.product_name = self.product.name
        self.subtotal = self.unit_price * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.quantity}x {self.product_name} ({self.get_size_display()})"


class Cart(models.Model):
    """Session-based cart for customers"""
    session_key = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cart {self.session_key}"

    @property
    def total(self):
        return sum(item.subtotal for item in self.cart_items.all())

    @property
    def item_count(self):
        return sum(item.quantity for item in self.cart_items.all())


class CartItem(models.Model):
    SIZE_CHOICES = [
        ('none', 'Regular'),
        ('small', 'Small'),
        ('medium', 'Medium'),
        ('large', 'Large'),
        ('hot', 'Hot'),
    ]

    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='cart_items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    size = models.CharField(max_length=20, choices=SIZE_CHOICES, default='none')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    @property
    def subtotal(self):
        return self.unit_price * self.quantity

    def __str__(self):
        return f"{self.quantity}x {self.product.name}"

    class Meta:
        unique_together = ['cart', 'product', 'size']
