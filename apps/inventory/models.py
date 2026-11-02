from decimal import Decimal

import django
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.menu.models import Product

# ── Django version compatibility ──────────────────────────────────────────────
# CheckConstraint switched from check= to condition= in Django 5.1.
_DJANGO_VERSION = django.VERSION[:2]
if _DJANGO_VERSION >= (5, 1):
    def _check(**kwargs):
        return {'condition': kwargs['condition']}
else:
    def _check(**kwargs):
        return {'check': kwargs['condition']}


class InventoryLog(models.Model):
    ACTION_CHOICES = [
        ('restock', 'Restock'),
        ('adjustment', 'Adjustment'),
        ('sale', 'Sale Deduction'),
        ('waste', 'Waste/Spoilage'),
    ]

    SOURCE_CHOICES = [
        ('customer_order', 'Customer Order'),
        ('pos_order', 'POS Order'),
        ('manual_adjustment', 'Manual Adjustment'),
        ('inventory_update', 'Inventory Update'),
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        related_name='inventory_logs',
        help_text=(
            'Product this movement relates to. SET_NULL on product deactivation '
            'so the log row survives even if the product FK is cleared.'
        ),
    )
    # Snapshot of the product name at the time the log row was written.
    # Preserved permanently so the audit trail remains readable even after a
    # product is deactivated, renamed, or its FK is nulled out.
    product_name = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Snapshot of the product's name at the time of this log entry.",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True)
    # Where the movement originated. Every production write site passes an
    # explicit source; the default only backfills legacy rows.
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='manual_adjustment',
        help_text='Where the inventory movement originated.',
    )
    # Short structured reason (e.g. "Order #KDM-...", "Restock",
    # "Product stock update"). Free-form detail lives in ``notes``.
    reason = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Short reason for the movement (e.g. order number).',
    )
    quantity_change = models.IntegerField()  # Positive = added, negative = reduced
    quantity_before = models.PositiveIntegerField()
    quantity_after = models.PositiveIntegerField()
    notes = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    @classmethod
    def record(cls, *, product, action, source, reason, quantity_change,
               quantity_before, quantity_after, performed_by=None, notes=''):
        """Create one audit-trail row for a stock movement.

        Every production write site -- order deductions/restores, manual
        restock, product create/edit stock updates -- goes through this
        single entry point so the trail is recorded consistently.

        ``product_name`` is snapshotted at write time so the log row remains
        readable even if the product is later renamed, deactivated, or its FK
        is nulled out (SET_NULL) due to a future schema relaxation.
        """
        return cls.objects.create(
            product=product,
            product_name=product.name if product is not None else '',
            action=action,
            source=source,
            reason=reason,
            quantity_change=quantity_change,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            notes=notes,
            performed_by=performed_by,
        )

    def __str__(self):
        name = (self.product.name if self.product_id and self.product else None) or self.product_name or '(deleted product)'
        return (
            f"{name} {self.action}: {self.quantity_change:+d} "
            f"({self.get_source_display()})"
        )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Covers inventory log list filtered by action type + ORDER BY -created_at.
            models.Index(
                fields=['action', '-created_at'],
                name='idx_invlog_action_created',
            ),
            # Covers per-product log history (product detail / inventory views).
            models.Index(
                fields=['product', '-created_at'],
                name='idx_invlog_product_created',
            ),
        ]


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2A — Ingredient-Based Inventory Foundation
# ══════════════════════════════════════════════════════════════════════════════
#
# These models are ADDITIVE. They do not replace or alter any existing model.
# InventoryLog, Product.stock_quantity, and all legacy inventory behaviour
# are left exactly as-is.
#
# Quantity shorthand used throughout:
#   Q   = DecimalField(max_digits=14, decimal_places=3)  — stock quantities
#
# All quantity arithmetic uses Decimal; float is never used.
# ══════════════════════════════════════════════════════════════════════════════


# ── Shared quantity field factory ─────────────────────────────────────────────

def _qty_field(default=Decimal('0.000'), **kwargs):
    """Return a DecimalField(14,3) with the project-standard quantity precision."""
    return models.DecimalField(
        max_digits=14, decimal_places=3, default=default, **kwargs
    )


# ── InventoryItem ─────────────────────────────────────────────────────────────

class InventoryItem(models.Model):
    """
    One ingredient or packaging material, tracked in a single base unit.

    All stock quantities are stored in the item's base_unit (g, ml, or pcs).
    Purchasing in larger units (kg, L, packs) is handled by UnitConversion
    (Phase 3) and converted to base units before being added here.

    on_hand_quantity  — physical units currently in stock
    reserved_quantity — units allocated to open orders (not yet consumed)
    available_quantity — derived property: on_hand − reserved (never stored)

    The DB enforces:
      on_hand   >= 0
      reserved  >= 0
      reserved  <= on_hand   (so available is always >= 0)
    """

    ITEM_TYPE_INGREDIENT = 'ingredient'
    ITEM_TYPE_PACKAGING  = 'packaging'
    ITEM_TYPE_CHOICES = [
        (ITEM_TYPE_INGREDIENT, 'Ingredient'),
        (ITEM_TYPE_PACKAGING,  'Packaging'),
    ]

    BASE_UNIT_G   = 'g'
    BASE_UNIT_ML  = 'ml'
    BASE_UNIT_PCS = 'pcs'
    BASE_UNIT_CHOICES = [
        (BASE_UNIT_G,   'Grams (g)'),
        (BASE_UNIT_ML,  'Millilitres (ml)'),
        (BASE_UNIT_PCS, 'Pieces (pcs)'),
    ]

    name = models.CharField(
        max_length=120,
        help_text='Name of this ingredient or packaging material.',
    )
    item_type = models.CharField(
        max_length=12,
        choices=ITEM_TYPE_CHOICES,
        db_index=True,
        help_text='Whether this item is an ingredient or a packaging material.',
    )
    base_unit = models.CharField(
        max_length=3,
        choices=BASE_UNIT_CHOICES,
        help_text=(
            'Base unit for all stock quantities. '
            'Immutable after the first movement is recorded.'
        ),
    )

    on_hand_quantity = _qty_field(
        help_text='Physical quantity currently in stock, in base units.',
    )
    reserved_quantity = _qty_field(
        help_text=(
            'Quantity allocated to open orders that have not yet been '
            'consumed or released. Never exceeds on_hand_quantity.'
        ),
    )
    low_stock_threshold = _qty_field(
        help_text=(
            'Alert threshold. When available_quantity falls at or below this '
            'value, the item appears in the low-stock dashboard widget.'
        ),
    )

    is_active = models.BooleanField(
        default=True,
        help_text=(
            'Inactive items are hidden from ingredient and packaging '
            'selection screens. Deactivation is blocked while the item is '
            'referenced by an approved recipe or packaging profile or has '
            'reserved_quantity > 0.'
        ),
    )

    description = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['item_type', 'name']
        indexes = [
            models.Index(fields=['item_type', 'is_active'], name='idx_invitem_type_active'),
        ]
        constraints = [
            # Case-insensitive unique name: 'Rice' and 'rice' cannot coexist.
            # Django 4.2 supports UniqueConstraint with expressions (Lower).
            models.UniqueConstraint(
                models.functions.Lower('name'),
                name='item_name_unique_ci',
                violation_error_message='An inventory item with this name already exists.',
            ),
            # Physical stock can never go negative.
            models.CheckConstraint(
                **_check(condition=Q(on_hand_quantity__gte=Decimal('0.000'))),
                name='item_on_hand_non_negative',
            ),
            # Reservation counter can never go negative.
            models.CheckConstraint(
                **_check(condition=Q(reserved_quantity__gte=Decimal('0.000'))),
                name='item_reserved_non_negative',
            ),
            # The core availability invariant: reserved can never exceed on-hand,
            # so available_quantity (on_hand − reserved) is always >= 0.
            models.CheckConstraint(
                **_check(condition=Q(reserved_quantity__lte=models.F('on_hand_quantity'))),
                name='item_reserved_within_on_hand',
            ),
            # Low-stock threshold cannot be negative.
            models.CheckConstraint(
                **_check(condition=Q(low_stock_threshold__gte=Decimal('0.000'))),
                name='item_threshold_non_negative',
            ),
            # Only the two defined item types are accepted.
            models.CheckConstraint(
                **_check(condition=Q(item_type__in=['ingredient', 'packaging'])),
                name='item_type_valid',
            ),
            # Only the three defined base units are accepted.
            models.CheckConstraint(
                **_check(condition=Q(base_unit__in=['g', 'ml', 'pcs'])),
                name='item_unit_valid',
            ),
        ]

    @property
    def available_quantity(self) -> Decimal:
        """Available stock = on_hand − reserved. Always >= 0 (DB-enforced)."""
        return self.on_hand_quantity - self.reserved_quantity

    @property
    def is_low_stock(self) -> bool:
        """True when available quantity is at or below the low-stock threshold."""
        return self.available_quantity <= self.low_stock_threshold

    def clean(self):
        super().clean()
        # Cross-field validation: reserved must not exceed on-hand.
        # The DB CHECK covers this but model-level clean() gives a better
        # form error during admin and form-based saves.
        if self.reserved_quantity is not None and self.on_hand_quantity is not None:
            if self.reserved_quantity > self.on_hand_quantity:
                raise ValidationError({
                    'reserved_quantity': (
                        'Reserved quantity cannot exceed on-hand quantity. '
                        f'On-hand: {self.on_hand_quantity}, '
                        f'Reserved: {self.reserved_quantity}.'
                    )
                })
        if self.on_hand_quantity is not None and self.on_hand_quantity < Decimal('0'):
            raise ValidationError({'on_hand_quantity': 'On-hand quantity cannot be negative.'})
        if self.reserved_quantity is not None and self.reserved_quantity < Decimal('0'):
            raise ValidationError({'reserved_quantity': 'Reserved quantity cannot be negative.'})

    def __str__(self):
        return f'{self.name} ({self.get_base_unit_display()})'


# ── IngredientMovementLog ─────────────────────────────────────────────────────

class IngredientMovementLog(models.Model):
    """
    Append-only audit trail for every change to InventoryItem quantities.

    This is the ingredient-inventory counterpart to InventoryLog (which logs
    changes to Product.stock_quantity). The two logs coexist permanently and
    are never merged.

    Each row records:
      - What item moved and its name/unit at the time of movement (snapshots)
      - The type of movement (purchase, reservation, etc.)
      - The quantity delta applied to on_hand and/or reserved
      - The balances before and after
      - Optional link to the order, purchase item, or reservation item that
        caused the movement
      - Who performed the action and when

    DB constraints ensure every row is internally consistent:
      on_hand_after  = on_hand_before  + on_hand_delta
      reserved_after = reserved_before + reserved_delta
      The movement_type shape rules enforce that each type only changes the
      fields it is allowed to change.
    """

    MOVEMENT_PURCHASE            = 'purchase'
    MOVEMENT_RESERVATION         = 'reservation'
    MOVEMENT_RESERVATION_RELEASE = 'reservation_release'
    MOVEMENT_CONSUMPTION         = 'consumption'
    MOVEMENT_WASTAGE             = 'wastage'
    MOVEMENT_ADJUSTMENT          = 'adjustment'

    MOVEMENT_TYPE_CHOICES = [
        (MOVEMENT_PURCHASE,            'Purchase / Restock'),
        (MOVEMENT_RESERVATION,         'Reservation'),
        (MOVEMENT_RESERVATION_RELEASE, 'Reservation Release'),
        (MOVEMENT_CONSUMPTION,         'Consumption'),
        (MOVEMENT_WASTAGE,             'Wastage'),
        (MOVEMENT_ADJUSTMENT,          'Manual Adjustment'),
    ]

    # ── Item reference and snapshots ──────────────────────────────────────────
    inventory_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='movements',
        help_text='The ingredient or packaging item that moved.',
    )
    # Name and unit are snapshotted at write time so the log remains readable
    # even if the item is later renamed (or if base_unit ever changes via
    # admin correction).
    item_name = models.CharField(
        max_length=120,
        help_text="Snapshot of the item's name at the time of this movement.",
    )
    base_unit = models.CharField(
        max_length=3,
        choices=InventoryItem.BASE_UNIT_CHOICES,
        help_text="Snapshot of the item's base unit at the time of this movement.",
    )

    # ── Movement type and quantity deltas ─────────────────────────────────────
    movement_type = models.CharField(
        max_length=24,
        choices=MOVEMENT_TYPE_CHOICES,
        db_index=True,
        help_text='Category of stock movement.',
    )
    on_hand_delta = models.DecimalField(
        max_digits=14, decimal_places=3,
        help_text=(
            'Change applied to on_hand_quantity. '
            'Positive = stock added; negative = stock consumed or wasted.'
        ),
    )
    reserved_delta = models.DecimalField(
        max_digits=14, decimal_places=3,
        help_text=(
            'Change applied to reserved_quantity. '
            'Positive = quantity newly reserved; negative = released or consumed.'
        ),
    )

    # ── Balances before and after (redundant but immutable audit record) ──────
    on_hand_before  = _qty_field(help_text='on_hand_quantity before this movement.')
    on_hand_after   = _qty_field(help_text='on_hand_quantity after this movement.')
    reserved_before = _qty_field(help_text='reserved_quantity before this movement.')
    reserved_after  = _qty_field(help_text='reserved_quantity after this movement.')

    # ── Optional order link ───────────────────────────────────────────────────
    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ingredient_movements',
        help_text='The order that caused this movement, if any.',
    )
    # Snapshot of the order number so the log is readable even if the FK
    # is nulled in a future schema relaxation.
    order_number = models.CharField(
        max_length=30, blank=True, default='',
        help_text="Snapshot of order.order_number at write time.",
    )

    # ── Optional purchase link (Phase 3) ──────────────────────────────────────
    # Links purchase movements to the specific StockPurchaseItem that caused
    # them. SET_NULL so movement rows survive if a purchase record is corrected
    # via adjustment (purchase rows themselves are never deleted per Rev 3 D.7,
    # but the FK is nullable for safety and for all non-purchase movement types).
    purchase_item = models.ForeignKey(
        'inventory.StockPurchaseItem',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='movements',
        help_text='The purchase line item that caused this movement, if any.',
    )

    # ── Idempotency key ───────────────────────────────────────────────────────
    # Unique when set; null for movements that have no natural idempotency key
    # (e.g. initial stock counts). Prevents duplicate movements when a service
    # is called twice due to a retry.
    idempotency_key = models.CharField(
        max_length=120, unique=True, null=True, blank=True,
        help_text=(
            'Optional idempotency key. When set, a second movement with the '
            'same key is silently ignored. Unique constraint enforced by DB.'
        ),
    )

    # ── Context fields ────────────────────────────────────────────────────────
    reason_code = models.CharField(
        max_length=50, blank=True, default='',
        help_text='Short machine-readable reason code (e.g. "cancelled_prepared").',
    )
    note = models.TextField(
        blank=True, default='',
        help_text='Human-readable context or staff note.',
    )

    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ingredient_movements',
        help_text='Staff member who performed or triggered this movement.',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['inventory_item', '-created_at'],
                name='idx_ingmv_item_created',
            ),
            models.Index(
                fields=['order'],
                name='idx_ingmv_order',
            ),
            models.Index(
                fields=['movement_type', '-created_at'],
                name='idx_ingmv_type_created',
            ),
        ]
        constraints = [
            # on_hand_after = on_hand_before + on_hand_delta
            models.CheckConstraint(
                **_check(condition=Q(
                    on_hand_after=models.F('on_hand_before') + models.F('on_hand_delta')
                )),
                name='mv_on_hand_arithmetic',
            ),
            # reserved_after = reserved_before + reserved_delta
            models.CheckConstraint(
                **_check(condition=Q(
                    reserved_after=models.F('reserved_before') + models.F('reserved_delta')
                )),
                name='mv_reserved_arithmetic',
            ),
            # Post-movement state must be valid: 0 <= reserved_after <= on_hand_after
            models.CheckConstraint(
                **_check(condition=Q(
                    on_hand_after__gte=Decimal('0.000'),
                    reserved_after__gte=Decimal('0.000'),
                    reserved_after__lte=models.F('on_hand_after'),
                )),
                name='mv_after_valid',
            ),
            # Only defined movement types accepted.
            models.CheckConstraint(
                **_check(condition=Q(movement_type__in=[
                    'purchase',
                    'reservation',
                    'reservation_release',
                    'consumption',
                    'wastage',
                    'adjustment',
                ])),
                name='mv_type_valid',
            ),
            # Movement type shape rules (Rev 3 §D.8):
            #   reservation         → on_hand_delta = 0, reserved_delta > 0
            #   reservation_release → on_hand_delta = 0, reserved_delta < 0
            #   consumption         → on_hand_delta < 0, reserved_delta = on_hand_delta
            #   purchase            → on_hand_delta > 0, reserved_delta = 0
            #   wastage             → on_hand_delta < 0, reserved_delta = 0
            #   adjustment          → on_hand_delta ≠ 0, reserved_delta = 0
            #
            # These are encoded as six separate CHECK constraints (one per type)
            # because a single constraint combining all types would be extremely
            # long and unreadable, and individual constraints give clearer
            # violation messages.
            models.CheckConstraint(
                **_check(condition=~Q(movement_type='reservation') | Q(
                    on_hand_delta=Decimal('0'),
                    reserved_delta__gt=Decimal('0'),
                )),
                name='mv_shape_reservation',
            ),
            models.CheckConstraint(
                **_check(condition=~Q(movement_type='reservation_release') | Q(
                    on_hand_delta=Decimal('0'),
                    reserved_delta__lt=Decimal('0'),
                )),
                name='mv_shape_release',
            ),
            models.CheckConstraint(
                **_check(condition=~Q(movement_type='consumption') | Q(
                    on_hand_delta__lt=Decimal('0'),
                    reserved_delta=models.F('on_hand_delta'),
                )),
                name='mv_shape_consumption',
            ),
            models.CheckConstraint(
                **_check(condition=~Q(movement_type='purchase') | Q(
                    on_hand_delta__gt=Decimal('0'),
                    reserved_delta=Decimal('0'),
                )),
                name='mv_shape_purchase',
            ),
            models.CheckConstraint(
                **_check(condition=~Q(movement_type='wastage') | Q(
                    on_hand_delta__lt=Decimal('0'),
                    reserved_delta=Decimal('0'),
                )),
                name='mv_shape_wastage',
            ),
            models.CheckConstraint(
                **_check(condition=~Q(movement_type='adjustment') | (
                    ~Q(on_hand_delta=Decimal('0')) & Q(reserved_delta=Decimal('0'))
                )),
                name='mv_shape_adjustment',
            ),
        ]

    def __str__(self):
        return (
            f'{self.item_name}: {self.get_movement_type_display()} '
            f'{self.on_hand_delta:+.3f} {self.base_unit} '
            f'@ {self.created_at:%Y-%m-%d %H:%M}'
        )

    @classmethod
    def record(
        cls,
        *,
        inventory_item,
        movement_type,
        on_hand_delta,
        reserved_delta,
        on_hand_before,
        reserved_before,
        reason_code='',
        note='',
        order=None,
        purchase_item=None,
        idempotency_key=None,
        performed_by=None,
    ):
        """Create one audit-trail row for an ingredient stock movement.

        Computes on_hand_after and reserved_after from the before values and
        deltas so the caller does not have to repeat the arithmetic.

        Idempotency: if idempotency_key is set and a row with that key already
        exists, the existing row is returned without creating a duplicate.
        """
        if idempotency_key:
            existing = cls.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                return existing

        on_hand_delta   = Decimal(str(on_hand_delta))
        reserved_delta  = Decimal(str(reserved_delta))
        on_hand_before  = Decimal(str(on_hand_before))
        reserved_before = Decimal(str(reserved_before))

        return cls.objects.create(
            inventory_item=inventory_item,
            item_name=inventory_item.name,
            base_unit=inventory_item.base_unit,
            movement_type=movement_type,
            on_hand_delta=on_hand_delta,
            reserved_delta=reserved_delta,
            on_hand_before=on_hand_before,
            on_hand_after=on_hand_before + on_hand_delta,
            reserved_before=reserved_before,
            reserved_after=reserved_before + reserved_delta,
            reason_code=reason_code,
            note=note,
            order=order,
            order_number=order.order_number if order is not None else '',
            purchase_item=purchase_item,
            idempotency_key=idempotency_key,
            performed_by=performed_by,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Purchase Foundation
# ══════════════════════════════════════════════════════════════════════════════
#
# These models are ADDITIVE. They do not alter any Phase 2A or legacy model.
#
# Quantity precision shorthand (mirroring Phase 2A):
#   Q = DecimalField(max_digits=14, decimal_places=3)  — stock quantities
#   F = DecimalField(max_digits=14, decimal_places=4)  — conversion factors
#
# Rounding rule (Rev 3 §D.7): the ONLY place rounding occurs is
#   base_quantity = quantize(purchase_quantity × conversion_factor, 0.001, ROUND_HALF_UP)
# This is enforced by the service layer (Phase 3-2), not by the model.
# ══════════════════════════════════════════════════════════════════════════════


def _factor_field(**kwargs):
    """Return a DecimalField(14,4) with the project-standard conversion-factor precision."""
    return models.DecimalField(max_digits=14, decimal_places=4, **kwargs)


# ── UnitConversion ────────────────────────────────────────────────────────────

class UnitConversion(models.Model):
    """
    Defines how a purchase unit (e.g. kg, L, box-of-12) relates to the
    InventoryItem's base unit (g, ml, pcs).

    One row per (item, purchase_unit) pair. The conversion_factor is the
    multiplier that converts purchase_unit to base_unit:

        base_quantity = purchase_quantity × conversion_factor

    Example: 1 kg = 1000 g → conversion_factor = 1000.0000.

    Rev 3 §D.3: editing a factor affects future purchases only, because each
    StockPurchaseItem row snapshots its own factor at creation time.
    """

    inventory_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='unit_conversions',
        help_text='The ingredient or packaging material this conversion applies to.',
    )
    purchase_unit = models.CharField(
        max_length=40,
        help_text=(
            'Name of the purchase unit (e.g. "kg", "L", "box-of-12"). '
            'Case-insensitive unique per item.'
        ),
    )
    conversion_factor = _factor_field(
        help_text=(
            'Multiplier to convert one purchase_unit to the item\'s base unit. '
            'Must be > 0. Example: 1 kg → 1000 g, so factor = 1000.0000.'
        ),
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive conversions are hidden from the purchase screen.',
    )
    notes = models.TextField(
        blank=True, default='',
        help_text='Optional staff note (e.g. supplier-specific pack sizes).',
    )

    class Meta:
        ordering = ['inventory_item', 'purchase_unit']
        constraints = [
            # A conversion factor of zero or less is meaningless and would
            # corrupt the base_quantity calculation.
            models.CheckConstraint(
                **_check(condition=Q(conversion_factor__gt=Decimal('0'))),
                name='conv_factor_positive',
            ),
            # Only one factor per (item, purchase_unit) pair, case-insensitive.
            # Mirrors item_name_unique_ci using Lower().
            models.UniqueConstraint(
                'inventory_item',
                models.functions.Lower('purchase_unit'),
                name='conv_unique_unit_per_item',
                violation_error_message=(
                    'A conversion for this purchase unit already exists for this item.'
                ),
            ),
        ]

    def __str__(self):
        return (
            f'{self.inventory_item.name}: '
            f'1 {self.purchase_unit} = {self.conversion_factor} '
            f'{self.inventory_item.base_unit}'
        )


# ── StockPurchase ─────────────────────────────────────────────────────────────

class StockPurchase(models.Model):
    """
    Header record for one stock-purchase event (e.g. a supplier delivery).

    Rev 3 §D.7: append-only. A mistaken purchase is corrected by recording
    an adjustment movement, never by editing or deleting the purchase row.

    The idempotency_key allows the purchase screen to safely retry a failed
    submission without creating duplicate records.
    """

    purchase_date = models.DateField(
        help_text='The date the stock was actually received.',
    )
    notes = models.TextField(
        blank=True, default='',
        help_text='Optional staff note (e.g. supplier, invoice number).',
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='stock_purchases',
        help_text='Staff member who recorded this purchase.',
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When this purchase record was created.',
    )
    idempotency_key = models.CharField(
        max_length=120,
        unique=True,
        help_text=(
            'Unique key to prevent duplicate purchase records on retried '
            'form submissions.'
        ),
    )

    class Meta:
        ordering = ['-purchase_date', '-created_at']

    def __str__(self):
        return f'Purchase {self.pk} on {self.purchase_date}'


# ── StockPurchaseItem ─────────────────────────────────────────────────────────

class StockPurchaseItem(models.Model):
    """
    One line of a StockPurchase: how much of one InventoryItem was purchased,
    in what purchase unit, at what conversion factor.

    All three quantity fields (purchase_quantity, conversion_factor,
    base_quantity) are snapshotted at creation. Editing the UnitConversion
    record later has no effect on existing purchase lines.

    Rev 3 §D.7 rounding rule (enforced by the service, not here):
        base_quantity = quantize(purchase_quantity × conversion_factor, 0.001, ROUND_HALF_UP)
    This is the ONLY place rounding occurs in the whole design.
    """

    purchase = models.ForeignKey(
        StockPurchase,
        on_delete=models.CASCADE,
        related_name='items',
        help_text='The parent purchase record.',
    )
    inventory_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='purchase_items',
        help_text='The ingredient or packaging material that was purchased.',
    )

    # ── Snapshots (immutable after creation) ─────────────────────────────────
    purchase_unit = models.CharField(
        max_length=40,
        help_text='Snapshot of the purchase unit at the time of this purchase.',
    )
    purchase_quantity = _qty_field(
        help_text='Quantity purchased in the purchase_unit. Must be > 0.',
    )
    conversion_factor = _factor_field(
        help_text='Snapshot of the conversion factor at the time of this purchase. Must be > 0.',
    )
    base_quantity = _qty_field(
        help_text=(
            'Computed base-unit quantity added to stock: '
            'quantize(purchase_quantity × conversion_factor, 0.001, ROUND_HALF_UP). '
            'Must be > 0. Set by the service before saving.'
        ),
    )

    class Meta:
        ordering = ['purchase', 'inventory_item']
        constraints = [
            models.CheckConstraint(
                **_check(condition=Q(purchase_quantity__gt=Decimal('0.000'))),
                name='spi_purchase_quantity_positive',
            ),
            models.CheckConstraint(
                **_check(condition=Q(base_quantity__gt=Decimal('0.000'))),
                name='spi_base_quantity_positive',
            ),
            models.CheckConstraint(
                **_check(condition=Q(conversion_factor__gt=Decimal('0'))),
                name='spi_factor_positive',
            ),
        ]

    def __str__(self):
        return (
            f'{self.purchase}: {self.purchase_quantity} {self.purchase_unit} '
            f'of {self.inventory_item.name} → {self.base_quantity} '
            f'{self.inventory_item.base_unit}'
        )
