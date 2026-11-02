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
