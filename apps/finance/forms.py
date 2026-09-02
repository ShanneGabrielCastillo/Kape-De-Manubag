from django import forms
from django.utils import timezone
from decimal import Decimal
from .models import DailyFinance

# Monetary fields that share the same "empty = zero" coercion rule.
# Listed here so the clean loop and per-field methods stay in one place.
_MONETARY_FIELDS = [
    'previous_coh',
    'expenses',
    'gcash_payments',
    'coins',
    'cash_advance',
    'floating_cash',
]


class DailyFinanceForm(forms.ModelForm):
    """
    Finance reconciliation form.

    When bound to an existing instance (update mode) the ``date`` field is
    removed entirely: the date of a finance record must never change after
    it has been created.  Removing it at the form level means the field is
    never parsed, validated, or written on updates — not just hidden in the
    UI.  A direct POST that includes a ``date`` value will have it silently
    ignored because it is not in the form's field list.

    Monetary fields are all optional at the HTML level (``required=False``).
    Submitting an empty value — e.g. the cashier clears the expenses box
    instead of typing 0 — is treated as zero rather than raising the
    unhelpful Django default "Enter a number." validation error.  The server
    still rejects negative values via ``clean()``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # instance is set by ModelForm when an existing object is passed.
        # If we have a pk it is a saved record — lock its date.
        if self.instance and self.instance.pk:
            self.fields.pop('date', None)
        # Mark all monetary fields as not required so an empty submission
        # arrives as None rather than triggering "Enter a number."
        for field_name in _MONETARY_FIELDS:
            if field_name in self.fields:
                self.fields[field_name].required = False

    # ── Per-field empty→zero coercion ─────────────────────────────────────────
    # Each method follows the same pattern:
    #   - If the field was submitted empty Django gives us None.
    #   - Return Decimal('0.00') so the cashier's intent ("no amount") is
    #     treated as zero, which is the model default.
    #   - A real value passes through unchanged; negativity is caught below
    #     in clean().

    def _coerce_decimal(self, field_name):
        value = self.cleaned_data.get(field_name)
        return value if value is not None else Decimal('0.00')

    def clean_previous_coh(self):
        return self._coerce_decimal('previous_coh')

    def clean_expenses(self):
        return self._coerce_decimal('expenses')

    def clean_gcash_payments(self):
        return self._coerce_decimal('gcash_payments')

    def clean_coins(self):
        return self._coerce_decimal('coins')

    def clean_cash_advance(self):
        return self._coerce_decimal('cash_advance')

    def clean_floating_cash(self):
        return self._coerce_decimal('floating_cash')

    class Meta:
        model = DailyFinance
        fields = [
            'date',
            'previous_coh',
            'expenses',
            'expenses_notes',
            'gcash_payments',
            'coins',
            'cash_advance',
            'floating_cash',
        ]
        widgets = {
            'date': forms.DateInput(attrs={
                'class': 'form-control',
                'type': 'date',
            }),
            'previous_coh': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
                'inputmode': 'decimal',
            }),
            'expenses': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
                'inputmode': 'decimal',
                'placeholder': '0.00',
            }),
            'expenses_notes': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 2,
                'placeholder': 'What were the expenses for?',
            }),
            'gcash_payments': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
                'inputmode': 'decimal',
                'placeholder': '0.00',
            }),
            'coins': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
                'inputmode': 'decimal',
                'placeholder': '0.00',
            }),
            'cash_advance': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
                'inputmode': 'decimal',
                'placeholder': '0.00',
            }),
            'floating_cash': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
                'inputmode': 'decimal',
                'placeholder': '0.00',
            }),
        }

    def clean_date(self):
        date = self.cleaned_data.get('date')
        if not date:
            raise forms.ValidationError("Date is required.")
        today = timezone.localdate()
        if date > today:
            raise forms.ValidationError(
                "Finance records cannot be created for future dates."
            )
        return date

    def clean(self):
        cleaned_data = super().clean()
        # Negativity check runs after per-field coercion, so None has already
        # been replaced with Decimal('0.00') and will never trigger this.
        for field_name in _MONETARY_FIELDS:
            value = cleaned_data.get(field_name)
            if value is not None and value < Decimal('0.00'):
                self.add_error(field_name, "This value cannot be negative.")
        return cleaned_data
