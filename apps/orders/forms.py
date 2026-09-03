from django import forms
from .models import Order


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
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 2,
            'placeholder': 'Special instructions...',
        })
    )
