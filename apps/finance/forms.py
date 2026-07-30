from django import forms
from django.utils import timezone
from decimal import Decimal
from .models import DailyFinance


class DailyFinanceForm(forms.ModelForm):
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
        today = timezone.now().date()
        if date and date > today:
            raise forms.ValidationError(
                "Finance records cannot be created for future dates."
            )
        return date

    def clean(self):
        cleaned_data = super().clean()
        decimal_fields = [
            'previous_coh', 'expenses', 'gcash_payments',
            'coins', 'cash_advance', 'floating_cash',
        ]
        for field_name in decimal_fields:
            value = cleaned_data.get(field_name)
            if value is not None and value < Decimal('0.00'):
                self.add_error(field_name, "This value cannot be negative.")
        return cleaned_data
