"""
apps/inventory/forms.py
=======================
Forms for the ingredient inventory management screens.

Phase 2B-A: InventoryItemForm for creating and editing InventoryItem records.
Phase 2B-B+: Additional forms (adjustment, wastage) will be added here.

Authorization: all form-bearing views use @cashier_or_admin_required.
"""

from django import forms

from .models import InventoryItem


class InventoryItemForm(forms.ModelForm):
    """
    Create or edit an InventoryItem (ingredient or packaging material).

    Only the fields that staff can set at creation time are exposed.
    Fields managed by the system (reserved_quantity, created_at, updated_at)
    are excluded — they must never be writable through the UI.

    available_quantity is a derived property, not a stored field.
    """

    class Meta:
        model = InventoryItem
        fields = [
            'name',
            'item_type',
            'base_unit',
            'on_hand_quantity',
            'low_stock_threshold',
            'description',
            'is_active',
        ]
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. Coffee Powder, Oat Milk, Medium Cup',
                'autofocus': True,
            }),
            'item_type': forms.Select(attrs={'class': 'form-control'}),
            'base_unit': forms.Select(attrs={'class': 'form-control'}),
            'on_hand_quantity': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': '0.000',
                'step': '0.001',
                'min': '0',
            }),
            'low_stock_threshold': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': '0.000',
                'step': '0.001',
                'min': '0',
            }),
            'description': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Optional notes about this ingredient or packaging material.',
            }),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'name': 'Item Name',
            'item_type': 'Item Type',
            'base_unit': 'Base Unit',
            'on_hand_quantity': 'Initial On-Hand Quantity',
            'low_stock_threshold': 'Low-Stock Alert Threshold',
            'description': 'Description',
            'is_active': 'Active',
        }
        help_texts = {
            'name': 'Unique name. Case-insensitive — "Rice" and "rice" are the same item.',
            'item_type': 'Ingredients are consumed during preparation. Packaging materials are used for serving.',
            'base_unit': (
                'The base unit for all stock quantities. '
                'Once movements are recorded this cannot be changed. '
                'Use g (grams) for solid ingredients, ml for liquids, pcs for discrete items.'
            ),
            'on_hand_quantity': 'Starting stock. Enter 0 to begin tracking without initial stock.',
            'low_stock_threshold': 'You will see a low-stock alert when available quantity falls at or below this value.',
            'is_active': 'Inactive items are hidden from ingredient selection screens.',
        }

    def clean_on_hand_quantity(self):
        """
        Reject negative on-hand values at the form level before the model
        clean() runs. This prevents the model's reserved_quantity cross-check
        from raising a ValidationError on a field that is not in the form.
        """
        from decimal import Decimal
        value = self.cleaned_data.get('on_hand_quantity')
        if value is not None and value < Decimal('0'):
            raise forms.ValidationError('On-hand quantity cannot be negative.')
        return value

    def clean_low_stock_threshold(self):
        """Reject negative threshold values at the form level."""
        from decimal import Decimal
        value = self.cleaned_data.get('low_stock_threshold')
        if value is not None and value < Decimal('0'):
            raise forms.ValidationError('Low-stock threshold cannot be negative.')
        return value


# ── Shared reason choices for adjustment and wastage ──────────────────────────

ADJUSTMENT_REASON_CHOICES = [
    ('', '— Select a reason —'),
    ('stock_count_correction', 'Stock count correction'),
    ('supplier_short_delivery', 'Supplier short delivery'),
    ('returned_to_supplier', 'Returned to supplier'),
    ('found_unrecorded_stock', 'Found unrecorded stock'),
    ('other', 'Other (describe in notes)'),
]

WASTAGE_REASON_CHOICES = [
    ('', '— Select a reason —'),
    ('expired', 'Expired / past use-by date'),
    ('spilled', 'Spilled or dropped'),
    ('contaminated', 'Contaminated / unusable'),
    ('overproduction', 'Overproduction / excess'),
    ('damaged', 'Damaged packaging'),
    ('other', 'Other (describe in notes)'),
]


class AdjustmentForm(forms.Form):
    """
    Stock adjustment form — sets on_hand_quantity to a new absolute value.

    record_adjustment() uses optimistic concurrency: it compares the value
    the user loaded against the current DB value and rejects the write if
    they differ. The hidden `expected_on_hand` field carries that snapshot
    from GET to POST.

    The form does not perform the adjustment itself — it only collects and
    validates the inputs. The view calls record_adjustment() inside
    transaction.atomic().
    """

    new_on_hand = forms.DecimalField(
        label='New On-Hand Quantity',
        min_value=0,
        decimal_places=3,
        max_digits=14,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.001',
            'min': '0',
            'autofocus': True,
        }),
        help_text=(
            'Enter the corrected physical count in the item\'s base unit. '
            'This replaces the current recorded quantity.'
        ),
    )

    # Hidden snapshot of on_hand_quantity at page-load time.
    # Passed back by the browser so the service can detect concurrent changes.
    expected_on_hand = forms.DecimalField(
        widget=forms.HiddenInput(),
        required=True,
    )

    reason_code = forms.ChoiceField(
        label='Reason',
        choices=ADJUSTMENT_REASON_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
        required=True,
    )

    note = forms.CharField(
        label='Notes',
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Additional context (required if reason is "Other").',
        }),
        max_length=500,
    )

    def __init__(self, *args, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._item = item
        # On GET, pre-fill expected_on_hand with the current DB value so it
        # travels to the browser and comes back in the POST unchanged.
        if item is not None and not args and 'data' not in kwargs:
            self.fields['expected_on_hand'].initial = item.on_hand_quantity
            self.fields['new_on_hand'].initial      = item.on_hand_quantity

    def clean(self):
        cleaned = super().clean()
        reason_code = cleaned.get('reason_code', '')
        note        = cleaned.get('note', '').strip()
        if reason_code == 'other' and not note:
            self.add_error(
                'note',
                'Please describe the reason in the Notes field when selecting "Other".',
            )
        if not reason_code:
            self.add_error('reason_code', 'Please select a reason for the adjustment.')
        return cleaned


class WastageForm(forms.Form):
    """
    Wastage recording form — deducts a positive quantity from available stock.

    record_wastage() rejects quantities > (on_hand - reserved), so the form
    validates against the item's current available quantity to give the user
    a clear error before the service is called.
    """

    quantity = forms.DecimalField(
        label='Wastage Quantity',
        min_value=0,
        decimal_places=3,
        max_digits=14,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.001',
            'min': '0.001',
            'autofocus': True,
        }),
        help_text='How much was wasted, in the item\'s base unit. Must be greater than 0.',
    )

    reason_code = forms.ChoiceField(
        label='Reason',
        choices=WASTAGE_REASON_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
        required=True,
    )

    note = forms.CharField(
        label='Notes',
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Additional context (required if reason is "Other").',
        }),
        max_length=500,
    )

    def __init__(self, *args, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._item = item

    def clean_quantity(self):
        from decimal import Decimal
        value = self.cleaned_data.get('quantity')
        if value is None:
            return value
        if value <= Decimal('0'):
            raise forms.ValidationError('Wastage quantity must be greater than zero.')
        # Early validation against available stock so the user sees a clear
        # message without waiting for the service call.
        if self._item is not None:
            available = self._item.on_hand_quantity - self._item.reserved_quantity
            if value > available:
                raise forms.ValidationError(
                    f'Cannot waste {value} {self._item.base_unit}: '
                    f'only {available} {self._item.base_unit} is available '
                    f'(not reserved). The service will also enforce this.'
                )
        return value

    def clean(self):
        cleaned = super().clean()
        reason_code = cleaned.get('reason_code', '')
        note        = cleaned.get('note', '').strip()
        if reason_code == 'other' and not note:
            self.add_error(
                'note',
                'Please describe the reason in the Notes field when selecting "Other".',
            )
        if not reason_code:
            self.add_error('reason_code', 'Please select a reason.')
        return cleaned


# ── Phase 2B-C: Packaging creation form ──────────────────────────────────────

class PackagingItemForm(forms.Form):
    """
    Create a new InventoryItem whose item_type is always 'packaging'.

    item_type is NOT exposed to the user — it is forced to 'packaging' in the
    view. Accepting a user-submitted item_type would allow creating ingredients
    through the packaging endpoint, which must not be possible.

    Mirrors InventoryItemForm but is a plain Form (not ModelForm) so we can
    control which fields are exposed and enforce the type server-side.
    """

    name = forms.CharField(
        label='Item Name',
        max_length=120,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. Medium Cup, Lid, Straw, Takeout Box',
            'autofocus': True,
        }),
        help_text='Unique name. Case-insensitive — "Cup" and "cup" are treated as the same item.',
    )

    base_unit = forms.ChoiceField(
        label='Base Unit',
        choices=InventoryItem.BASE_UNIT_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
        help_text=(
            'The unit for all stock quantities. '
            'Once movements are recorded this cannot be changed. '
            'Packaging materials are typically counted in pcs.'
        ),
    )

    on_hand_quantity = forms.DecimalField(
        label='Initial On-Hand Quantity',
        min_value=0,
        decimal_places=3,
        max_digits=14,
        initial=0,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'placeholder': '0.000',
            'step': '0.001',
            'min': '0',
        }),
        help_text='Starting stock. Enter 0 to begin tracking without initial stock.',
    )

    low_stock_threshold = forms.DecimalField(
        label='Low-Stock Alert Threshold',
        min_value=0,
        decimal_places=3,
        max_digits=14,
        initial=0,
        required=False,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'placeholder': '0.000',
            'step': '0.001',
            'min': '0',
        }),
        help_text='You will see a low-stock alert when available quantity falls at or below this value.',
    )

    description = forms.CharField(
        label='Description',
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Optional notes about this packaging material.',
        }),
        max_length=500,
    )

    is_active = forms.BooleanField(
        label='Active',
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        help_text='Inactive items are hidden from packaging selection screens.',
    )

    def clean_on_hand_quantity(self):
        from decimal import Decimal
        value = self.cleaned_data.get('on_hand_quantity')
        if value is not None and value < Decimal('0'):
            raise forms.ValidationError('On-hand quantity cannot be negative.')
        return value

    def clean_low_stock_threshold(self):
        from decimal import Decimal
        value = self.cleaned_data.get('low_stock_threshold')
        if value is None:
            return Decimal('0')
        if value < Decimal('0'):
            raise forms.ValidationError('Low-stock threshold cannot be negative.')
        return value


# ── Phase 3-3: Purchase management forms ──────────────────────────────────────

import uuid as _uuid_module


class PurchaseHeaderForm(forms.Form):
    """
    Header-level fields for a new stock purchase.
    """

    purchase_date = forms.DateField(
        label='Purchase Date',
        widget=forms.DateInput(attrs={
            'class': 'form-control',
            'type': 'date',
        }),
        help_text='The date the stock was actually received.',
    )

    notes = forms.CharField(
        label='Notes',
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 2,
            'placeholder': 'Optional — supplier name, invoice number, delivery notes…',
        }),
        max_length=1000,
    )

    # Hidden UUID — carries the idempotency key across POST re-renders so
    # that a validation-failure re-render preserves the original key.
    idempotency_key = forms.CharField(
        widget=forms.HiddenInput(),
        required=True,
        max_length=36,   # UUID4 canonical string length
    )

    def clean_idempotency_key(self):
        value = self.cleaned_data.get('idempotency_key', '').strip()
        try:
            _uuid_module.UUID(value, version=4)
        except (ValueError, AttributeError):
            raise forms.ValidationError('Invalid idempotency key. Please reload the form.')
        return value


class PurchaseLineForm(forms.Form):
    """
    One purchase line: an inventory item, the purchase unit to use,
    and the quantity received.

    Security: conversion_factor is NOT accepted from the user. The view
    resolves it server-side from the UnitConversion record identified by
    (inventory_item_id, purchase_unit_id). A user-submitted factor would
    allow stock inflation.

    purchase_unit_id: the PK of the UnitConversion row, which carries both
    the purchase_unit name and the conversion_factor to use.
    """

    inventory_item_id = forms.IntegerField(
        label='Inventory Item',
        widget=forms.Select(attrs={'class': 'form-control'}),
        min_value=1,
    )

    purchase_unit_id = forms.IntegerField(
        label='Purchase Unit',
        widget=forms.Select(attrs={'class': 'form-control'}),
        help_text='Select the purchase unit. The conversion factor is looked up automatically.',
        min_value=1,
    )

    purchase_quantity = forms.DecimalField(
        label='Quantity',
        min_value=0,
        decimal_places=3,
        max_digits=14,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.001',
            'min': '0.001',
            'placeholder': '0.000',
        }),
        help_text='Quantity in the selected purchase unit. Must be greater than zero.',
    )

    note = forms.CharField(
        label='Line Note',
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Optional note for this line',
            'maxlength': '200',
        }),
        max_length=200,
    )

    def clean_purchase_quantity(self):
        from decimal import Decimal
        value = self.cleaned_data.get('purchase_quantity')
        if value is not None and value <= Decimal('0'):
            raise forms.ValidationError('Quantity must be greater than zero.')
        return value

    def resolve_conversion(self):
        """
        Look up the UnitConversion row for this line.

        Returns the UnitConversion instance or raises ValidationError if
        the conversion does not exist, belongs to a different item, or is
        inactive. This is the only place the conversion_factor is sourced —
        never from user-submitted data.

        Both item_id and uc_id are validated as positive integers by the form
        fields before this method is called, so we do not need to guard
        against non-integer inputs here.
        """
        from apps.inventory.models import UnitConversion
        item_id = self.cleaned_data.get('inventory_item_id')
        uc_id   = self.cleaned_data.get('purchase_unit_id')
        if not item_id or not uc_id:
            return None
        try:
            uc = UnitConversion.objects.select_related('inventory_item').get(
                pk=uc_id,
                inventory_item_id=item_id,  # ensures uc belongs to the stated item
                is_active=True,
            )
        except UnitConversion.DoesNotExist:
            raise forms.ValidationError(
                'The selected purchase unit is not valid for this item, '
                'or it has been deactivated.'
            )
        return uc


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4B-5 — Recipe and Packaging Configuration Forms
# ══════════════════════════════════════════════════════════════════════════════

from decimal import Decimal as _Decimal

from .models import (
    InventoryItem as _InventoryItem,
    PackagingProfile as _PackagingProfile,
    Recipe as _Recipe,
    RecipeItem as _RecipeItem,
    PackagingProfileItem as _PackagingProfileItem,
    ProductPackagingRule as _ProductPackagingRule,
)


class RecipeForm(forms.ModelForm):
    """Create or edit a draft Recipe header (no items)."""

    class Meta:
        model = _Recipe
        fields = ['product', 'size', 'order_type_scope', 'revision']
        widgets = {
            'product':          forms.Select(attrs={'class': 'form-control'}),
            'size':             forms.Select(attrs={'class': 'form-control'}),
            'order_type_scope': forms.Select(attrs={'class': 'form-control'}),
            'revision':         forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
        }
        labels = {
            'product':          'Product',
            'size':             'Size',
            'order_type_scope': 'Order-Type Scope',
            'revision':         'Revision Number',
        }
        help_texts = {
            'size': 'The product size this recipe covers.',
            'order_type_scope': (
                '"All" applies to both dine-in and takeout. '
                'Use "Dine-in only" or "Takeout only" when ingredient '
                'quantities differ by order type.'
            ),
            'revision': (
                'Monotonically increasing per (product, size, scope). '
                'Revision numbers cannot be reused.'
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Product selector: only active, ingredient-tracked or legacy products.
        # (Any product may have a recipe prepared before cutover.)
        from apps.menu.models import Product
        self.fields['product'].queryset = (
            Product.objects.filter(is_active=True).order_by('name')
        )

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get('product')
        size    = cleaned.get('size')
        scope   = cleaned.get('order_type_scope')
        revision = cleaned.get('revision')

        if product and size and scope and revision:
            # Reject duplicate (product, size, scope, revision).
            qs = _Recipe.objects.filter(
                product=product, size=size,
                order_type_scope=scope, revision=revision,
            )
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    f'Revision {revision} already exists for this product/'
                    f'size/scope combination. Choose a higher revision number.'
                )
        return cleaned


class RecipeApprovalForm(forms.Form):
    """Collect the approval note before approving a Recipe."""

    approval_note = forms.CharField(
        label='Approval Note',
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'How and when did the client confirm this recipe? (required)',
            'autofocus': True,
        }),
        help_text=(
            'Record how and when the client confirmed this recipe is correct. '
            'This note is immutable once the recipe is approved.'
        ),
    )

    def clean_approval_note(self):
        note = self.cleaned_data.get('approval_note', '').strip()
        if not note:
            raise forms.ValidationError('An approval note is required.')
        return note


class RecipeItemForm(forms.ModelForm):
    """Add or edit a single RecipeItem line on a draft Recipe."""

    class Meta:
        model = _RecipeItem
        fields = ['inventory_item', 'quantity']
        widgets = {
            'inventory_item': forms.Select(attrs={'class': 'form-control'}),
            'quantity':       forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.001',
                'min': '0.001',
                'placeholder': '0.000',
            }),
        }
        labels = {
            'inventory_item': 'Ingredient',
            'quantity':       'Quantity (per unit, in base unit)',
        }
        help_texts = {
            'inventory_item': 'Only active ingredients are shown.',
            'quantity':       'How much of this ingredient is consumed per one product unit.',
        }

    def __init__(self, *args, recipe=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._recipe = recipe
        # Only show active ingredient-type items.
        self.fields['inventory_item'].queryset = (
            _InventoryItem.objects
            .filter(item_type=_InventoryItem.ITEM_TYPE_INGREDIENT, is_active=True)
            .order_by('name')
        )

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty is not None and qty <= _Decimal('0'):
            raise forms.ValidationError('Quantity must be greater than zero.')
        return qty

    def clean(self):
        cleaned = super().clean()
        inv_item = cleaned.get('inventory_item')
        if inv_item and inv_item.item_type != _InventoryItem.ITEM_TYPE_INGREDIENT:
            raise forms.ValidationError(
                f'"{inv_item.name}" is a packaging item, not an ingredient. '
                'Only ingredient-type items may appear in a recipe.'
            )
        # Duplicate ingredient check.
        if self._recipe and inv_item:
            qs = _RecipeItem.objects.filter(recipe=self._recipe, inventory_item=inv_item)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    f'"{inv_item.name}" already has a line in this recipe. '
                    'Edit the existing line to change the quantity.'
                )
        return cleaned


class PackagingProfileForm(forms.ModelForm):
    """Create or edit a draft PackagingProfile header (no items)."""

    class Meta:
        model = _PackagingProfile
        fields = ['name', 'revision']
        widgets = {
            'name':     forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. Medium Takeout Cup Set',
                'autofocus': True,
            }),
            'revision': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
        }
        labels = {
            'name':     'Profile Name',
            'revision': 'Revision Number',
        }
        help_texts = {
            'name':     'Human-readable name for this packaging combination. Case-insensitive for the approved-unique check.',
            'revision': 'Revision numbers are never reused for the same profile name.',
        }

    def clean(self):
        cleaned = super().clean()
        name     = cleaned.get('name', '').strip()
        revision = cleaned.get('revision')
        if name and revision:
            qs = _PackagingProfile.objects.filter(name__iexact=name, revision=revision)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    f'Revision {revision} already exists for a profile with '
                    'this name. Choose a higher revision number.'
                )
        return cleaned


class PackagingProfileApprovalForm(forms.Form):
    """Collect the approval note before approving a PackagingProfile."""

    approval_note = forms.CharField(
        label='Approval Note',
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'How and when did the client confirm this packaging configuration? (required)',
            'autofocus': True,
        }),
        help_text=(
            'Record how and when the client confirmed this packaging profile. '
            'This note is immutable once approved.'
        ),
    )

    def clean_approval_note(self):
        note = self.cleaned_data.get('approval_note', '').strip()
        if not note:
            raise forms.ValidationError('An approval note is required.')
        return note


class PackagingProfileItemForm(forms.ModelForm):
    """Add or edit a single PackagingProfileItem line on a draft profile."""

    class Meta:
        model = _PackagingProfileItem
        fields = ['inventory_item', 'quantity']
        widgets = {
            'inventory_item': forms.Select(attrs={'class': 'form-control'}),
            'quantity':       forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.001',
                'min': '0.001',
                'placeholder': '0.000',
            }),
        }
        labels = {
            'inventory_item': 'Packaging Material',
            'quantity':       'Quantity (per unit, in base unit)',
        }
        help_texts = {
            'inventory_item': 'Only active packaging items are shown.',
            'quantity':       'How many of this packaging item are used per one product unit.',
        }

    def __init__(self, *args, profile=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._profile = profile
        self.fields['inventory_item'].queryset = (
            _InventoryItem.objects
            .filter(item_type=_InventoryItem.ITEM_TYPE_PACKAGING, is_active=True)
            .order_by('name')
        )

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty is not None and qty <= _Decimal('0'):
            raise forms.ValidationError('Quantity must be greater than zero.')
        return qty

    def clean(self):
        cleaned = super().clean()
        inv_item = cleaned.get('inventory_item')
        if inv_item and inv_item.item_type != _InventoryItem.ITEM_TYPE_PACKAGING:
            raise forms.ValidationError(
                f'"{inv_item.name}" is an ingredient, not a packaging item. '
                'Only packaging-type items may appear in a packaging profile.'
            )
        if self._profile and inv_item:
            qs = _PackagingProfileItem.objects.filter(
                profile=self._profile, inventory_item=inv_item,
            )
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    f'"{inv_item.name}" already has a line in this profile. '
                    'Edit the existing line to change the quantity.'
                )
        return cleaned


class ProductPackagingRuleForm(forms.Form):
    """Create or update a ProductPackagingRule."""

    product_id = forms.IntegerField(widget=forms.HiddenInput())

    size = forms.ChoiceField(
        label='Size',
        choices=_ProductPackagingRule.SIZE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )

    order_type = forms.ChoiceField(
        label='Order Type',
        choices=_ProductPackagingRule.ORDER_TYPE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )

    no_packaging = forms.BooleanField(
        label='No packaging required',
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        help_text=(
            'Check this if no packaging is used for this combination. '
            'Must not be checked if a profile is selected.'
        ),
    )

    profile_id = forms.IntegerField(
        label='Packaging Profile',
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
        help_text='Select an approved packaging profile, or check "No packaging" above.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only show approved profiles in the dropdown.
        approved = (
            _PackagingProfile.objects
            .filter(status=_PackagingProfile.STATUS_APPROVED)
            .order_by('name', '-revision')
        )
        # Build choices manually; empty option first.
        choices = [('', '— Select an approved profile —')]
        for p in approved:
            choices.append((p.pk, f'{p.name} (rev {p.revision})'))
        self.fields['profile_id'].widget = forms.Select(
            attrs={'class': 'form-control'},
            choices=choices,
        )

    def clean(self):
        cleaned = super().clean()
        no_packaging = cleaned.get('no_packaging', False)
        profile_id   = cleaned.get('profile_id')

        if no_packaging and profile_id:
            raise forms.ValidationError(
                'Cannot set both a profile and "No packaging". Choose one.'
            )
        if not no_packaging and not profile_id:
            raise forms.ValidationError(
                'Either select an approved profile or check "No packaging required".'
            )
        return cleaned
