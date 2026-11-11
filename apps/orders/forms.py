from django import forms
from .models import Order
from apps.accounts.validators import validate_payment_proof_upload


class CheckoutForm(forms.Form):
    customer_name = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Your name',
            'autocomplete': 'name',
            'autocapitalize': 'words',
        })
    )
    order_type = forms.ChoiceField(
        # Reference the model's canonical choices so a future order-type
        # addition only needs updating in one place.
        choices=Order.ORDER_TYPE_CHOICES,
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'})
    )
    # Payment method chosen at checkout.  Cash = pay at counter.
    # GCash = customer pays online and submits a reference for staff verification.
    payment_method = forms.ChoiceField(
        choices=Order.PAYMENT_METHOD_CHOICES,
        initial='cash',
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 2,
            'placeholder': 'Special instructions...',
        })
    )


# ── Phase 6A: Cancellation Disposition Forms ────────────────────────────────


class CancellationDispositionForm(forms.Form):
    """Per-order-item disposition input for cancellation from ``preparing``
    or ``ready`` status.

    For ``preparing``: both ``prepared_units`` and ``discarded_units`` are
    required.  For ``ready``: only ``discarded_units`` is required (the
    service derives ``prepared_units = quantity``).

    An explicitly submitted ``0`` is valid.  A missing field produces a
    validation error — the form does NOT silently default to zero.
    """

    prepared_units = forms.IntegerField(
        required=False,  # required only for preparing; set dynamically
        min_value=0,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'min': '0',
            'style': 'width: 80px',
        }),
    )
    discarded_units = forms.IntegerField(
        required=False,  # required only when staff input is needed; set dynamically
        min_value=0,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'min': '0',
            'style': 'width: 80px',
        }),
    )


class GCashSubmissionForm(forms.Form):
    """Submitted by the customer to provide GCash payment evidence.

    A screenshot of the payment is required.  The reference number field has
    been removed from the customer-facing flow — staff verifies the actual
    payment using the uploaded screenshot.

    IMPORTANT: submitting this form NEVER sets is_paid=True.  Only an
    authorized staff member can confirm the payment via the staff-side
    verify_gcash_payment view.
    """
    gcash_proof = forms.ImageField(
        required=True,
        validators=[validate_payment_proof_upload],
        widget=forms.FileInput(attrs={
            'class': 'form-control',
            'accept': 'image/*',
        }),
        help_text='Upload a screenshot of your GCash payment receipt.',
    )
