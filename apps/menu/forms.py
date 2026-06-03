from django import forms
from .models import Product, Category


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ['category', 'name', 'description', 'image', 'price', 'price_medium',
                  'price_large', 'price_hot', 'has_sizes', 'is_available', 'is_featured',
                  'stock_quantity', 'low_stock_threshold']
        widgets = {
            'category': forms.Select(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Product name'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'inputmode': 'decimal'}),
            'price_medium': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'inputmode': 'decimal'}),
            'price_large': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'inputmode': 'decimal'}),
            'price_hot': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'inputmode': 'decimal'}),
            'stock_quantity': forms.NumberInput(attrs={'class': 'form-control', 'inputmode': 'numeric'}),
            'low_stock_threshold': forms.NumberInput(attrs={'class': 'form-control', 'inputmode': 'numeric'}),
        }


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'icon', 'description', 'image', 'is_active', 'is_packaging_required', 'order']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'icon': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Emoji icon'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
        }
