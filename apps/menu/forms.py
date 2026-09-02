from django import forms
from .models import Product, Category


def _price_input():
    """NumberInput widget shared by the four price fields (one price per
    size variant, all with the same attributes)."""
    return forms.NumberInput(attrs={
        'class': 'form-control', 'step': '0.01', 'inputmode': 'decimal',
    })


def _stock_input():
    """NumberInput widget shared by the stock fields."""
    return forms.NumberInput(attrs={'class': 'form-control', 'inputmode': 'numeric'})


class ProductForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Product creation offers only active categories; inactive (soft-
        # deleted) categories stay hidden from the dropdown. Exception: when
        # editing a product that already belongs to an inactive category, that
        # category remains selectable so the edit doesn't silently move the
        # product -- the admin can keep or change it explicitly.
        categories = Category.objects.active()
        if self.instance.pk and self.instance.category_id:
            categories = categories | Category.objects.filter(
                pk=self.instance.category_id,
            )
        self.fields['category'].queryset = categories

    class Meta:
        model = Product
        fields = ['category', 'name', 'description', 'image', 'price', 'price_medium',
                  'price_large', 'price_hot', 'has_sizes', 'is_available', 'is_featured',
                  'stock_quantity', 'low_stock_threshold']
        widgets = {
            'category': forms.Select(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Product name'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'price': _price_input(),
            'price_medium': _price_input(),
            'price_large': _price_input(),
            'price_hot': _price_input(),
            'stock_quantity': _stock_input(),
            'low_stock_threshold': _stock_input(),
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
