"""
Order Models for Kape De Manubag System
Handles cart, orders, order items, and payments
"""
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.conf import settings
from apps.menu.models import Product


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

    # Duplicate-order protection: the client sends a request_token with every
    # submission (POS and customer checkout). Replaying the same token -- a
    # double-click, a retry after a lost response -- returns the original
    # order instead of creating a duplicate. Null for legacy requests that
    # do not send a token.
    request_token = models.CharField(
        max_length=64, unique=True, null=True, blank=True,
        help_text=(
            'Client-generated idempotency key: replaying a request with the '
            'same key returns the original order instead of creating a '
            'duplicate.'
        ),
    )

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
    cancelled_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp when the order was cancelled. Null for non-cancelled orders.",
    )
    queue_number = models.PositiveIntegerField(null=True, blank=True)
    queued_at    = models.DateTimeField(null=True, blank=True)
    ready_at     = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.order_number or not self.queue_number:
            # Generate order_number and queue_number via the DailyOrderSequence
            # atomic counter, which serialises all concurrent writes for the
            # same calendar day — including the very first order of a new day.
            with transaction.atomic():
                today = timezone.localdate()
                seq = DailyOrderSequence.get_next_sequence(today)
                if not self.order_number:
                    self.order_number = f"KDM-{today:%Y%m%d}-{seq:04d}"
                if not self.queue_number:
                    self.queue_number = seq
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.order_number} - {self.customer_name}"

    def calculate_total(self):
        # Local import to avoid circular dependency: services imports from models.
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
            'pending':   '\U0001f550',
            'preparing': '\U0001f373',
            'ready':     '\u2705',
            'completed': '\U0001f389',
            'cancelled': '\u274c',
        }.get(self.status, '\U0001f550')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Covers: order_list (filter by status + ORDER BY -created_at),
            # queue_board (filter status + date), get_queue_position
            # (filter status__in + created_at__lt), dashboard status counts.
            models.Index(
                fields=['status', '-created_at'],
                name='idx_order_status_created',
            ),
            # Covers: finance and dashboard sales queries
            # (filter is_paid=True, status='completed', created_at date range),
            # reports date-range filter.
            models.Index(
                fields=['is_paid', 'status', '-created_at'],
                name='idx_order_paid_status_created',
            ),
        ]


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
    product_name = models.CharField(max_length=200)  # Snapshot: name at order time
    # Category snapshot: preserved so packaging-fee recalculations on historical
    # orders use the category that was in effect when the order was placed, not
    # whatever the product's category is today.
    category_name = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Snapshot of the product's category name at order time.",
    )
    packaging_eligible = models.BooleanField(
        default=False,
        help_text=(
            "Snapshot: True when this item's category had is_packaging_required=True "
            "at order time. Used to recompute packaging fees for historical orders "
            "without reading live product/category data."
        ),
    )
    size = models.CharField(max_length=20, choices=SIZE_CHOICES, default='none')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.CharField(max_length=200, blank=True)

    def save(self, *args, **kwargs):
        if self.product and not self.product_name:
            self.product_name = self.product.name
        # Populate category snapshot fields on first save if not already set.
        # This handles direct ORM creation paths that don't go through the
        # view layer — the view layer explicitly passes these values, so this
        # acts as a safety net rather than the primary population path.
        if self.product and not self.category_name:
            try:
                self.category_name = self.product.category.name
                self.packaging_eligible = self.product.category.is_packaging_required
            except Exception:
                pass  # product.category unavailable — leave defaults
        self.subtotal = self.unit_price * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.quantity}x {self.product_name} ({self.get_size_display()})"

    class Meta:
        # Prevent the same product/size from appearing as two separate rows
        # on one order. The constraint is partial: it only applies when
        # product is not NULL (a SET_NULL FK after deletion leaves product=None,
        # which is a legitimate historical state and must not be blocked).
        # The view layer consolidates duplicates before inserting, so in normal
        # operation this constraint is never hit -- it is a last-resort DB-level
        # guard against bugs or direct API abuse.
        constraints = [
            models.UniqueConstraint(
                fields=['order', 'product', 'size'],
                condition=models.Q(product__isnull=False),
                name='unique_orderitem_order_product_size',
            ),
        ]


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


class DailyOrderSequence(models.Model):
    """One row per calendar day — the single authoritative counter for
    order_number and queue_number generation.

    Why this exists
    ---------------
    The previous approach counted today's orders with COUNT(*) + 1 inside a
    SELECT FOR UPDATE block.  That lock only works when at least one order row
    already exists for the day; the very first order of each new day races past
    an empty queryset (SELECT FOR UPDATE on zero rows is a no-op) and two
    concurrent "first order of the day" requests both read count=1, produce the
    same KDM-YYYYMMDD-0001 number, and one of them fails with an IntegrityError.

    This model solves the problem with a proper atomic sequence:

        row = DailyOrderSequence.objects.select_for_update().get_or_create(date=today)
        row.last_sequence += 1
        row.save()
        return row.last_sequence

    get_or_create with select_for_update serialises ALL concurrent calls for the
    same date — including the very first one — through the DB's row-level lock.
    Two requests that arrive simultaneously for a fresh date both try to insert;
    one wins the INSERT (gets sequence 1) and the other's get_or_create retries
    the SELECT, finds the newly inserted row, locks it, increments to 2, and
    succeeds.  No duplicates, no IntegrityError, no silent failures.
    """

    date = models.DateField(unique=True)
    last_sequence = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'Daily Order Sequence'
        verbose_name_plural = 'Daily Order Sequences'

    def __str__(self):
        return f"Sequence {self.date}: {self.last_sequence}"

    @classmethod
    def get_next_sequence(cls, date) -> int:
        """Return the next sequence number for ``date``, atomically.

        Must be called inside an existing ``transaction.atomic()`` block so
        the lock is released the moment the surrounding transaction commits
        (keeping contention windows as short as possible).

        The select_for_update() on the get-or-create pattern works as follows:

        - If a row exists for ``date``:  SELECT FOR UPDATE locks it, we
          increment and save -- one atomic read-modify-write.
        - If no row exists yet:  two concurrent callers both attempt INSERT;
          one succeeds, the other hits the unique constraint, retries as a
          SELECT FOR UPDATE, and increments from 1.  Django's get_or_create
          handles this retry automatically via the IntegrityError/select
          fallback path.
        """
        seq_obj, _ = cls.objects.select_for_update().get_or_create(
            date=date,
            defaults={'last_sequence': 0},
        )
        seq_obj.last_sequence += 1
        seq_obj.save(update_fields=['last_sequence'])
        return seq_obj.last_sequence


# ── Historical-data protection ────────────────────────────────────────────────
# Orders are permanent financial records.  Hard-deleting an order would corrupt
# finance totals, inventory audit trails, and customer order history.  The same
# soft-delete pattern used for Products and Users is applied here: Order.delete()
# is a no-op (raises), and the pre_delete signal blocks bulk QuerySet.delete()
# calls that bypass the instance method (e.g. from the Django admin "delete
# selected" action or any ORM expression that cascades into orders).

ORDER_DELETE_ERROR = (
    'Orders cannot be deleted from the database. '
    'Cancel the order instead — cancelled orders remain fully traceable in '
    'finance reports, inventory logs, and order history.'
)


@receiver(pre_delete, sender=Order)
def _block_order_hard_delete(sender, instance, **kwargs):
    """Block *any* hard delete of an Order row.

    Covers:
    - ``order.delete()`` instance calls
    - ``Order.objects.filter(...).delete()`` bulk queryset deletes
    - Cascades triggered by deleting a related model (none exist in the
      current schema, but this guard future-proofs against new FKs)
    - The Django admin "delete selected" list action
    - ``OrderAdmin.delete_model()`` (separately blocked in admin.py)

    Raising ``ValidationError`` inside a ``transaction.atomic()`` block marks
    the connection for rollback; callers that catch and continue querying must
    use a savepoint.
    """
    raise ValidationError(ORDER_DELETE_ERROR)
