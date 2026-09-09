from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth import password_validation
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
            'accept': 'image/jpeg,image/png,image/gif,image/webp',
            'id': 'id_profile_image',
        }),
        help_text=f'{SUPPORTED_FORMATS_LABEL}, up to {MAX_SIZE_MB} MB.',
    )

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email', 'profile_image']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
        }


# ── Password Management Forms ─────────────────────────────────────────────────

class StaffPasswordChangeForm(forms.Form):
    """Authenticated password-change form for staff (admin + cashier).

    Uses Django's built-in ``password_validation`` pipeline so every
    configured AUTH_PASSWORD_VALIDATORS rule applies.  The current password
    is verified before the change is committed; neither the current nor the
    new password is ever stored in plaintext or surfaced in logs.
    """
    current_password = forms.CharField(
        label='Current Password',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter your current password',
            'autocomplete': 'current-password',
            'id': 'id_current_password',
        }),
    )
    new_password1 = forms.CharField(
        label='New Password',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter new password',
            'autocomplete': 'new-password',
            'id': 'id_new_password1',
        }),
        help_text=password_validation.password_validators_help_text_html(),
    )
    new_password2 = forms.CharField(
        label='Confirm New Password',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Confirm new password',
            'autocomplete': 'new-password',
            'id': 'id_new_password2',
        }),
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_current_password(self):
        current = self.cleaned_data.get('current_password')
        if not self.user.check_password(current):
            # Generic message — does not confirm whether a specific password
            # is "close" or what the current password is.
            raise forms.ValidationError(
                'Your current password is incorrect.',
                code='password_incorrect',
            )
        return current

    def clean_new_password2(self):
        pw1 = self.cleaned_data.get('new_password1')
        pw2 = self.cleaned_data.get('new_password2')
        if pw1 and pw2 and pw1 != pw2:
            raise forms.ValidationError(
                'The two password fields did not match.',
                code='password_mismatch',
            )
        return pw2

    def clean(self):
        cleaned = super().clean()
        new_pw = cleaned.get('new_password1')
        if new_pw:
            # Run every configured AUTH_PASSWORD_VALIDATORS rule.
            # Pass the user so validators like UserAttributeSimilarityValidator
            # can compare the password against the user's attributes.
            try:
                password_validation.validate_password(new_pw, self.user)
            except forms.ValidationError as exc:
                self.add_error('new_password1', exc)
        return cleaned

    def save(self, commit=True):
        """Set the new password using Django's secure hashing pipeline."""
        self.user.set_password(self.cleaned_data['new_password1'])
        if commit:
            self.user.save(update_fields=['password'])
        return self.user


class PasswordResetRequestForm(forms.Form):
    """Asks for an email address to send a reset link.

    Deliberately produces the same success message whether or not the address
    matches any account (anti-enumeration: see password_reset_request view).
    """
    email = forms.EmailField(
        label='Email Address',
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter your account email address',
            'autocomplete': 'email',
            'autofocus': True,
        }),
    )
