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


class GCashSubmissionForm(forms.Form):
    """Submitted by the customer to provide GCash payment evidence.

    The reference number is required; the proof screenshot is optional.
    Validation reuses the existing production-grade image validator from
    validators.py (same rules as profile / product image uploads).

    IMPORTANT: submitting this form NEVER sets is_paid=True.  Only an
    authorized staff member can confirm the payment via the staff-side
    verify_gcash_payment view.
    """
    gcash_reference = forms.CharField(
        max_length=50,
        min_length=3,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. 1234567890123',
            'autocomplete': 'off',
            'inputmode': 'text',
        }),
        help_text='Enter the GCash reference number from your transaction.',
    )
    gcash_proof = forms.ImageField(
        required=False,
        validators=[validate_payment_proof_upload],
        widget=forms.FileInput(attrs={
            'class': 'form-control',
            'accept': 'image/*',
        }),
        help_text='Optional: upload a screenshot of your GCash payment receipt.',
    )

    def clean_gcash_reference(self):
        ref = self.cleaned_data.get('gcash_reference', '').strip()
        if not ref:
            raise forms.ValidationError('GCash reference number is required.')
        # Normalize: strip surrounding whitespace only.
        # We do NOT enforce a strict numeric format because GCash reference
        # formats may vary and we don't want to reject legitimate references.
        return ref
