from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from .models import CustomUser
from .validators import (
    MAX_SIZE_MB,
    SUPPORTED_FORMATS_LABEL,
    validate_profile_image_upload,
)


class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={
        'class': 'form-control',
        'placeholder': 'Username',
        'autofocus': True,
        'autocomplete': 'username',
        'autocapitalize': 'none',
        'autocorrect': 'off',
    }))
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        'class': 'form-control',
        'placeholder': 'Password',
        'autocomplete': 'current-password',
    }))


class StaffCreateForm(UserCreationForm):
    """Form for admin to create cashier/staff accounts"""
    first_name = forms.CharField(required=True, widget=forms.TextInput(attrs={'class': 'form-control'}))
    last_name = forms.CharField(required=True, widget=forms.TextInput(attrs={'class': 'form-control'}))
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={'class': 'form-control'}))
    phone = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    role = forms.ChoiceField(choices=[('cashier', 'Cashier'), ('admin', 'Admin')],
                             widget=forms.Select(attrs={'class': 'form-control'}))

    class Meta:
        model = CustomUser
        fields = ['username', 'first_name', 'last_name', 'email', 'phone', 'role', 'password1', 'password2']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields:
            if field not in ['role']:
                self.fields[field].widget.attrs['class'] = 'form-control'


class ProfileUpdateForm(forms.ModelForm):
    # profile_image is declared explicitly to attach the upload validation
    # (see apps/accounts/validators.py). The widget stays the standard
    # ClearableFileInput, so the existing upload workflow is unchanged.
    profile_image = forms.ImageField(
        required=False,
        validators=[validate_profile_image_upload],
        widget=forms.ClearableFileInput(attrs={
            # Client-side picker hint only -- the server always re-validates.
            'accept': 'image/jpeg,image/png,image/gif,image/webp',
        }),
        help_text=f'{SUPPORTED_FORMATS_LABEL}, up to {MAX_SIZE_MB} MB.',
    )

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email', 'phone', 'profile_image']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
        }
