"""
Order Models for Kape De Manubag System
Handles cart, orders, order items, and payments
"""
import secrets

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.conf import settings
import django
from apps.menu.models import Product

# ── Django version compatibility ──────────────────────────────────────────────
# CheckConstraint used 'check=' before Django 5.1 and 'condition=' from 5.1+.
_ORDERS_DJANGO_VERSION = django.VERSION[:2]
if _ORDERS_DJANGO_VERSION >= (5, 1):
    def _orders_check(**kwargs):
        return {'condition': kwargs['condition']}
else:
    def _orders_check(**kwargs):
        return {'check': kwargs['condition']}


class Order(models.Model):
    STATUS_CHOICES = [
        ('awaiting_payment', 'Awaiting Payment'),
        ('preparing',        'Preparing'),
        ('ready',            'Ready'),
        ('completed',        'Completed'),
        ('cancelled',        'Cancelled'),
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
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='awaiting_payment')
    notes = models.TextField(blank=True)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    packaging_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text="Packaging fee for Take-Out orders.")
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Payment
    is_paid = models.BooleanField(default=False)
    paid_at = models.DateTimeField(
        null=True, blank=True,
        help_text=(
            'Timezone-aware timestamp set at the moment payment is successfully '
            'confirmed by staff (Cash) or verified (GCash).  Null for unpaid '
            'orders and for historical paid orders whose confirmation time was '
            'not recorded.'
        ),
    )
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    change_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # ── Manual GCash payment verification ────────────────────────────────────
    # Populated when the customer submits their GCash payment for verification.
    # CRITICAL: is_paid stays False until staff explicitly confirms the payment.
    # These fields are only relevant when payment_method == 'gcash'.

    GCASH_STATUS_CHOICES = [
        ('none',     'Not Submitted'),     # default — no GCash payment submitted yet
        ('pending',  'Pending Verification'),  # customer submitted, awaiting staff
        ('verified', 'Verified'),          # staff confirmed payment is legitimate
        ('rejected', 'Rejected'),          # staff rejected (invalid ref / not found)
    ]

    gcash_status = models.CharField(
        max_length=20,
        choices=GCASH_STATUS_CHOICES,
        default='none',
        help_text='Verification state of a customer-submitted GCash payment.',
    )
    gcash_reference = models.CharField(
        max_length=50,
        blank=True,
        default='',
        help_text='GCash reference number submitted by the customer.',
    )
    gcash_proof = models.ImageField(
        upload_to='gcash_proofs/',
        blank=True,
        null=True,
        help_text='Optional payment screenshot uploaded by the customer.',
    )
    gcash_submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Timestamp when the customer submitted GCash payment info.',
    )
    gcash_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Timestamp when staff verified (or rejected) the payment.',
    )
    gcash_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='gcash_verifications',
        help_text='Staff member who verified or rejected this GCash payment.',
    )
    gcash_notes = models.TextField(
        blank=True,
        default='',
        help_text='Staff notes, e.g. rejection reason.',
    )

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

    # Secure public order tracking token.  This is a cryptographically random
    # URL-safe string that the customer uses to access their order tracker.
    # Unlike the predictable order_number (KDM-YYYYMMDD-NNNN), this token
    # cannot be guessed or enumerated, so it prevents one customer from
    # reading another customer's order details via the public tracker endpoint.
    # It is null for orders that pre-date this field and is backfilled by the
    # data migration 0017_order_tracking_token.
    tracking_token = models.CharField(
        max_length=64, unique=True, null=True, blank=True,
        db_index=True,
        help_text=(
            'Server-generated cryptographically random token used for the '
            'customer-facing order tracker URL. Cannot be predicted from the '
            'order number, date, or any other public field.'
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
                # Assign a secure random tracking token if one has not been
                # set yet.  This covers all new orders including POS orders,
                # customer checkout orders, and any ORM path that calls save().
                if not self.tracking_token:
                    self.tracking_token = secrets.token_urlsafe(32)
                super().save(*args, **kwargs)
        else:
            # For updates to existing orders, still generate a tracking_token
            # if somehow missing (safety net — the migration backfills these).
            if not self.tracking_token:
                self.tracking_token = secrets.token_urlsafe(32)
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
        active = ['awaiting_payment', 'preparing']
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
            'preparing': 'ready',
            'ready':     'completed',
        }
        return flow.get(self.status)

    @property
    def status_emoji(self):
        return {
            'awaiting_payment': '\U0001f4b3',  # 💳
            'preparing':        '\U0001f373',  # 🍳
            'ready':            '\u2705',      # ✅
            'completed':        '\U0001f389',  # 🎉
            'cancelled':        '\u274c',      # ❌
        }.get(self.status, '\U0001f4b3')

    @property
    def is_customer_order(self):
        """True when the order was placed by an anonymous customer (not via POS)."""
        return self.cashier_id is None

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
            # Covers: Finance cash-sales query pattern
            # (filter is_paid, status, payment_method, created_at date).
            # Added by migration 0015_order_finance_sales_index.
            models.Index(
                fields=['is_paid', 'status', 'payment_method', 'created_at'],
                name='idx_order_finance_sales',
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

    # ── Phase 4A: Per-line inventory ledger snapshot ──────────────────────────
    # Rev 3 §D.1. Set once at order placement (Phase 5A+) from
    # Product.inventory_tracking_mode; never updated after the order line is
    # created. All existing rows receive 'legacy' from the column default,
    # which is correct because every historical line was handled by the legacy
    # ledger. The application does NOT yet read or act on this field — that
    # begins in Phase 5A.
    INVENTORY_MODE_LEGACY     = 'legacy'
    INVENTORY_MODE_INGREDIENT = 'ingredient'
    INVENTORY_MODE_CHOICES = [
        (INVENTORY_MODE_LEGACY,     'Legacy'),
        (INVENTORY_MODE_INGREDIENT, 'Ingredient'),
    ]
    inventory_mode = models.CharField(
        max_length=10,
        choices=INVENTORY_MODE_CHOICES,
        default=INVENTORY_MODE_LEGACY,
        help_text=(
            'Snapshot of the inventory ledger used for this order line, '
            'copied from Product.inventory_tracking_mode at placement time. '
            'Immutable after the line is created. All existing lines default '
            'to "legacy".'
        ),
    )

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
            # Phase 4A: inventory_mode must be one of two defined values.
            # The DB-level backstop ensures no code path can write an invalid
            # mode even if it bypasses form/model validation.
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    inventory_mode__in=['legacy', 'ingredient']
                )),
                name='orderitem_inventory_mode_valid',
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


# ══════════════════════════════════════════════════════════════════════════════
# Phase 6A — Order Cancellation Disposition
# ══════════════════════════════════════════════════════════════════════════════
#
# Rev 3 §D.13.  Append-only record of what was declared prepared when an
# order was cancelled, for both ledgers (legacy and ingredient).
#
# One row per (order, order_item).  Written in the same transaction as
# the cancellation and never edited.
#
# Q6d extension: the `discarded_units` field is an additive extension to
# §D.13 that records how many of the prepared units were discarded as
# waste (Q6d: YES — owner confirmed 2026-11-06).
# ══════════════════════════════════════════════════════════════════════════════


class OrderCancellationDisposition(models.Model):
    """
    Append-only record of what was declared prepared when an order was
    cancelled, for **both** ledgers (legacy and ingredient).

    Rev 3 §D.13 + Q6d extension.

    A disposition belongs to one order and one order item.  The service
    layer must validate that ``order_item.order_id == order.pk`` — the
    database's separate foreign keys and uniqueness constraint do not
    automatically guarantee this relationship.

    Written in the same transaction as the cancellation and never edited.
    Cancelling from ``awaiting_payment`` writes ``system_unpaid`` rows
    (all unprepared); from ``ready`` writes ``system_ready`` rows (all
    prepared); from ``preparing`` writes ``staff_declared`` rows.

    Ingredient consumption and release are derived from these rows
    (prepared units consume, unprepared units release); the legacy
    restore quantity is ``unprepared_units``.  Prepared legacy units
    change no stock and write no ``InventoryLog`` row; the fact lives
    here.

    The ``discarded_units`` field (Q6d extension) records how many of
    the prepared units were discarded as waste.  Not all prepared units
    are necessarily discarded; the disposition captures the distinction.
    """

    LEDGER_LEGACY     = 'legacy'
    LEDGER_INGREDIENT = 'ingredient'
    LEDGER_CHOICES = [
        (LEDGER_LEGACY,     'Legacy'),
        (LEDGER_INGREDIENT, 'Ingredient'),
    ]

    SOURCE_STAFF_DECLARED = 'staff_declared'
    SOURCE_SYSTEM_READY   = 'system_ready'
    SOURCE_SYSTEM_UNPAID  = 'system_unpaid'
    SOURCE_CHOICES = [
        (SOURCE_STAFF_DECLARED, 'Staff declared'),
        (SOURCE_SYSTEM_READY,   'System (ready)'),
        (SOURCE_SYSTEM_UNPAID,  'System (unpaid)'),
    ]

    # ── Foreign keys ──────────────────────────────────────────────────────────
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        related_name='cancellation_dispositions',
        help_text='The order this disposition belongs to.',
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.PROTECT,
        related_name='cancellation_dispositions',
        help_text='The order line this disposition covers.',
    )

    # ── Ledger snapshot (copied from order_item.inventory_mode) ───────────────
    ledger = models.CharField(
        max_length=10,
        choices=LEDGER_CHOICES,
        help_text='Which ledger this line belongs to (copied from the line snapshot).',
    )

    # ── Quantity snapshots ────────────────────────────────────────────────────
    order_item_quantity = models.PositiveIntegerField(
        help_text='Snapshot of order_item.quantity at cancellation time.',
    )
    prepared_units = models.PositiveIntegerField(
        default=0,
        help_text='Units declared prepared by staff or system.',
    )
    unprepared_units = models.PositiveIntegerField(
        default=0,
        help_text='Units not prepared (restored to stock for legacy).',
    )
    discarded_units = models.PositiveIntegerField(
        default=0,
        help_text=(
            'Q6d extension. Of the prepared units, how many were '
            'discarded as waste. Must be <= prepared_units.'
        ),
    )

    # ── Disposition source ────────────────────────────────────────────────────
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        help_text='Who/what determined the disposition.',
    )

    # ── Audit fields ───────────────────────────────────────────────────────────
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='cancellation_dispositions_decided',
        help_text='Staff member who entered the disposition.',
    )
    decided_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When the disposition was recorded.',
    )
    note = models.TextField(
        blank=True, default='',
        help_text='Staff note.',
    )

    class Meta:
        ordering = ['order', 'order_item']
        constraints = [
            # ── ocd_unique_item ───────────────────────────────────────────────
            # One row per (order, order_item). A repeated cancellation
            # cannot declare twice or restore twice.
            models.UniqueConstraint(
                fields=['order', 'order_item'],
                name='ocd_unique_item',
                violation_error_message=(
                    'A cancellation disposition already exists for this order item.'
                ),
            ),
            # ── ocd_units_non_negative ────────────────────────────────────────
            # PositiveIntegerField enforces >= 0 at the form level but the DB
            # column is a plain integer. This CHECK provides the DB backstop.
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    prepared_units__gte=0,
                    unprepared_units__gte=0,
                    discarded_units__gte=0,
                )),
                name='ocd_units_non_negative',
            ),
            # ── ocd_units_sum_to_quantity ────────────────────────────────────
            # prepared + unprepared = order_item_quantity.
            # Expressed as: unprepared = order_item_quantity - prepared.
            # If prepared > quantity, the RHS is negative and the constraint
            # fails (unprepared cannot be negative) — which is correct.
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    unprepared_units=models.F('order_item_quantity') - models.F('prepared_units'),
                )),
                name='ocd_units_sum_to_quantity',
            ),
            # ── ocd_discarded_within_prepared ────────────────────────────────
            # Q6d extension. discarded <= prepared.
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    discarded_units__lte=models.F('prepared_units'),
                )),
                name='ocd_discarded_within_prepared',
            ),
            # ── ocd_source_valid ─────────────────────────────────────────────
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    source__in=['staff_declared', 'system_ready', 'system_unpaid']
                )),
                name='ocd_source_valid',
            ),
            # ── ocd_ledger_valid ──────────────────────────────────────────────
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    ledger__in=['legacy', 'ingredient']
                )),
                name='ocd_ledger_valid',
            ),
        ]

    def __str__(self):
        return (
            f'Disposition(order={self.order_id}, item={self.order_item_id}, '
            f'ledger={self.ledger}, prepared={self.prepared_units}, '
            f'unprepared={self.unprepared_units}, discarded={self.discarded_units})'
        )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5B — Ingredient Reservation Models
# ══════════════════════════════════════════════════════════════════════════════
#
# Rev 3 §D.9 (OrderIngredientReservation) and §D.10
# (OrderIngredientReservationItem).
#
# These two models record the reserved ingredient and packaging quantities for
# orders that contain at least one ingredient-tracked product line.  They are
# additive — legacy-only orders have no reservation row.
#
# The reservation lifecycle mirrors the order lifecycle:
#   reserved  → order placed, ingredients reserved
#   consumed  → all reserved quantities consumed on preparing → ready
#   released  → all reserved quantities released on cancel before consumption
#   settled   → partial — mix of consumed and released lines
# ══════════════════════════════════════════════════════════════════════════════


class OrderIngredientReservation(models.Model):
    """
    Header record for the ingredient reservation of one order.

    Rev 3 §D.9.  Exists only when the order has at least one
    ingredient-tracked line.  Legacy-only orders have none.

    Status lifecycle:
      reserved  — order placed; all lines fully outstanding.
      consumed  — all lines fully consumed (order went through ready).
      released  — all lines fully released (cancelled before consumption).
      settled   — partial: some lines consumed, some released.
    """

    STATUS_RESERVED = 'reserved'
    STATUS_CONSUMED = 'consumed'
    STATUS_RELEASED = 'released'
    STATUS_SETTLED  = 'settled'
    STATUS_CHOICES = [
        (STATUS_RESERVED, 'Reserved'),
        (STATUS_CONSUMED, 'Consumed'),
        (STATUS_RELEASED, 'Released'),
        (STATUS_SETTLED,  'Settled'),
    ]

    # Rev 3 §D.9: OneToOneField enforces res_one_per_order (one reservation
    # per order — DB unique constraint).
    order = models.OneToOneField(
        Order,
        on_delete=models.PROTECT,
        related_name='ingredient_reservation',
        help_text='The order this reservation belongs to. One reservation per order.',
    )
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default=STATUS_RESERVED,
        help_text=(
            'Lifecycle state of this reservation. '
            '"reserved" = fully outstanding; "consumed" = all consumed; '
            '"released" = all released; "settled" = mix of consumed and released.'
        ),
    )
    # Snapshot of Order.order_type at reservation creation time.
    # Immutable: the order type is fixed at placement and the resolution
    # (resolve_plan) depends on it, so we record it here for traceability.
    order_type_snapshot = models.CharField(
        max_length=20,
        help_text='Snapshot of Order.order_type at reservation creation time.',
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When this reservation was created.',
    )
    settled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            'When this reservation was fully settled (all lines consumed or '
            'released). Null while status is "reserved".'
        ),
    )

    class Meta:
        # res_one_per_order is provided by the OneToOneField itself.
        constraints = [
            # ── res_status_valid ─────────────────────────────────────────────
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    status__in=[
                        'reserved', 'consumed', 'released', 'settled',
                    ]
                )),
                name='res_status_valid',
            ),
            # ── res_settled_at_consistent ────────────────────────────────────
            # status='reserved' <=> settled_at IS NULL.
            # An open reservation has no close time; every closed reservation
            # (consumed/released/settled) must have one.
            models.CheckConstraint(
                **_orders_check(condition=(
                    # Open: status=reserved AND settled_at IS NULL
                    models.Q(status='reserved', settled_at__isnull=True)
                    |
                    # Closed: status != reserved AND settled_at IS NOT NULL
                    models.Q(settled_at__isnull=False) & ~models.Q(status='reserved')
                )),
                name='res_settled_at_consistent',
            ),
        ]

    def __str__(self):
        return (
            f'Reservation(order={self.order_id}, status={self.status})'
        )


class OrderIngredientReservationItem(models.Model):
    """
    One row per (reservation, order item, inventory item).

    Rev 3 §D.10.  Snapshots every value needed to settle the line without
    re-reading any configuration.  Counters track how much has been consumed
    and released so that outstanding = reserved − consumed − released.
    """

    COMPONENT_SOURCE_RECIPE    = 'recipe'
    COMPONENT_SOURCE_PACKAGING = 'packaging'
    COMPONENT_SOURCE_CHOICES = [
        (COMPONENT_SOURCE_RECIPE,    'Recipe ingredient'),
        (COMPONENT_SOURCE_PACKAGING, 'Packaging material'),
    ]

    CONSUME_REASON_ORDER_READY       = 'order_ready'
    CONSUME_REASON_CANCELLED_PREP    = 'cancelled_prepared'
    CONSUME_REASON_CHOICES = [
        (CONSUME_REASON_ORDER_READY,    'Order marked ready'),
        (CONSUME_REASON_CANCELLED_PREP, 'Cancelled — prepared units'),
    ]

    RELEASE_REASON_CANCELLED_NOT_PREP   = 'cancelled_not_prepared'
    RELEASE_REASON_CANCELLED_BEFORE_PAY = 'cancelled_before_payment'
    RELEASE_REASON_CHOICES = [
        (RELEASE_REASON_CANCELLED_NOT_PREP,   'Cancelled — unprepared units'),
        (RELEASE_REASON_CANCELLED_BEFORE_PAY, 'Cancelled before payment'),
    ]

    # ── Foreign keys ──────────────────────────────────────────────────────────
    reservation = models.ForeignKey(
        OrderIngredientReservation,
        on_delete=models.CASCADE,
        related_name='items',
        help_text='The parent reservation.',
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.PROTECT,
        related_name='ingredient_reservation_items',
        help_text='The order line that caused this reservation.',
    )
    inventory_item = models.ForeignKey(
        # Deferred string reference to avoid a circular import — InventoryItem
        # is in the inventory app; this model is in the orders app.
        'inventory.InventoryItem',
        on_delete=models.PROTECT,
        related_name='reservation_items',
        help_text='The ingredient or packaging item that was reserved.',
    )

    # ── Snapshots (immutable after creation) ─────────────────────────────────
    item_name = models.CharField(
        max_length=120,
        help_text="Snapshot of inventory_item.name at reservation time.",
    )
    base_unit = models.CharField(
        max_length=3,
        help_text="Snapshot of inventory_item.base_unit at reservation time.",
    )
    component_source = models.CharField(
        max_length=10,
        choices=COMPONENT_SOURCE_CHOICES,
        help_text='Whether this component came from the recipe or the packaging profile.',
    )
    # Source recipe FK — null for packaging components.
    source_recipe = models.ForeignKey(
        'inventory.Recipe',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='reservation_items',
        help_text='The recipe this line was derived from. Null for packaging components.',
    )
    # Source packaging profile FK — null for recipe/ingredient components.
    source_packaging_profile = models.ForeignKey(
        'inventory.PackagingProfile',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='reservation_items',
        help_text=(
            'The packaging profile this line was derived from. '
            'Null for recipe/ingredient components.'
        ),
    )
    source_revision = models.PositiveIntegerField(
        help_text=(
            'Snapshot of the recipe or packaging profile revision at reservation '
            'time. The coordinator never re-reads configuration for an open '
            'reservation.'
        ),
    )
    quantity_per_unit = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        help_text=(
            'Snapshot: how many base units of inventory_item are consumed per '
            'product unit, from the resolved plan at placement time.'
        ),
    )
    order_item_quantity = models.PositiveIntegerField(
        help_text='Snapshot of order_item.quantity at reservation time.',
    )

    # ── Settlement counters (accumulate as lines are settled) ─────────────────
    reserved_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        help_text=(
            'Total reserved = quantity_per_unit × order_item_quantity. '
            'Immutable after creation.'
        ),
    )
    consumed_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=0,
        help_text='Units consumed so far. Increases to reserved_quantity at most.',
    )
    released_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=0,
        help_text='Units released so far. Increases to reserved_quantity at most.',
    )

    # ── Settlement reason and timestamps ─────────────────────────────────────
    consume_reason = models.CharField(
        max_length=25,
        blank=True,
        default='',
        choices=CONSUME_REASON_CHOICES,
        help_text=(
            'Why units were consumed. Must be set when consumed_quantity > 0.'
        ),
    )
    release_reason = models.CharField(
        max_length=30,
        blank=True,
        default='',
        choices=RELEASE_REASON_CHOICES,
        help_text=(
            'Why units were released. Must be set when released_quantity > 0.'
        ),
    )
    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When consumption was recorded for this line.',
    )
    released_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When release was recorded for this line.',
    )

    class Meta:
        constraints = [
            # ── rli_unique_line_item ──────────────────────────────────────────
            # One row per (reservation, order_item, inventory_item).
            models.UniqueConstraint(
                fields=['reservation', 'order_item', 'inventory_item'],
                name='rli_unique_line_item',
            ),
            # ── rli_reserved_positive ─────────────────────────────────────────
            # Reserved quantity must be positive — a zero-reservation line is
            # meaningless and indicates a logic error.
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    reserved_quantity__gt=0,
                )),
                name='rli_reserved_positive',
            ),
            # ── rli_consumed_non_negative ─────────────────────────────────────
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    consumed_quantity__gte=0,
                )),
                name='rli_consumed_non_negative',
            ),
            # ── rli_released_non_negative ─────────────────────────────────────
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    released_quantity__gte=0,
                )),
                name='rli_released_non_negative',
            ),
            # ── rli_settled_within_reserved ───────────────────────────────────
            # consumed + released <= reserved.
            # Ensures outstanding (reserved − consumed − released) is always ≥ 0.
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    consumed_quantity__lte=(
                        models.F('reserved_quantity') - models.F('released_quantity')
                    ),
                )),
                name='rli_settled_within_reserved',
            ),
            # ── rli_reserved_matches_basis ────────────────────────────────────
            # reserved = quantity_per_unit × order_item_quantity.
            # Proves the stored reservation matches the snapshot it was derived
            # from; catches service bugs where the quantity was computed wrong.
            models.CheckConstraint(
                **_orders_check(condition=models.Q(
                    reserved_quantity=(
                        models.F('quantity_per_unit') * models.F('order_item_quantity')
                    ),
                )),
                name='rli_reserved_matches_basis',
            ),
            # ── rli_reason_present ────────────────────────────────────────────
            # consumed > 0 => consume_reason set.
            # released > 0 => release_reason set.
            # Enforced as two separate constraints for clearer error messages.
            models.CheckConstraint(
                **_orders_check(condition=(
                    # consumed_quantity = 0 OR consume_reason != ''
                    models.Q(consumed_quantity=0)
                    | ~models.Q(consume_reason='')
                )),
                name='rli_consume_reason_present',
            ),
            models.CheckConstraint(
                **_orders_check(condition=(
                    # released_quantity = 0 OR release_reason != ''
                    models.Q(released_quantity=0)
                    | ~models.Q(release_reason='')
                )),
                name='rli_release_reason_present',
            ),
        ]
        indexes = [
            # Fast "open reservations for this inventory item" lookup.
            # Partial index: only rows where consumed + released < reserved
            # (i.e. there is still outstanding stock).
            # Rev 3 §D.10: "Partial index on rows where consumed + released < reserved".
            # Django does not support partial indexes via Meta.indexes directly
            # on all backends; we create it as a raw-SQL index in the migration.
        ]

    def __str__(self):
        return (
            f'ReservationItem(res={self.reservation_id}, '
            f'item={self.item_name}, '
            f'reserved={self.reserved_quantity}, '
            f'consumed={self.consumed_quantity}, '
            f'released={self.released_quantity})'
        )


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
