from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import (
    login, logout, authenticate,
    update_session_auth_hash,
)
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode, url_has_allowed_host_and_scheme
from django.conf import settings
from . import bruteforce
from .forms import (
    LoginForm, StaffCreateForm, ProfileUpdateForm,
    StaffPasswordChangeForm, PasswordResetRequestForm,
)
from .models import CustomUser
from apps.accounts.decorators import admin_required
from apps.audit.services import log_action


def _default_login_redirect(user):
    """Role-appropriate landing page when no safe 'next' target exists."""
    if user.is_admin_user or user.is_cashier:
        return 'dashboard:index'
    return 'menu:index'


def _get_safe_next_url(request):
    """Return a validated, same-host redirect target or '' when unsafe.

    Uses Django's own login-view validation (``url_has_allowed_host_and_scheme``):
    rejects external hosts, protocol-relative URLs (``//evil.com``), non-HTTP
    schemes (``javascript:``, ``data:``), backslash host tricks and URL
    fragments, so the ``next`` parameter can never cause an open redirect.
    """
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return ''


def login_view(request):
    if request.user.is_authenticated:
        return redirect(_default_login_redirect(request.user))

    form = LoginForm(request, data=request.POST or None)

    if request.method == 'POST':
        # Brute-force guard: while a lockout is active, reject before any
        # credential check (the password is never even verified). The message
        # is the same whether or not the username exists, so lockouts never
        # leak account information. It is passed as a dedicated template
        # variable so the lockout notice (not the generic form error) is what
        # the user sees.
        remaining = bruteforce.get_lockout_remaining(request)
        if remaining is not None:
            return render(request, 'accounts/login.html', {
                'form': form,
                'lockout_message': bruteforce.lockout_message(remaining),
            })

        if form.is_valid():
            user = form.get_user()
            login(request, user)
            bruteforce.clear_failed_attempts(request)
            messages.success(request, f'Welcome back, {user.first_name or user.username}!')
            # Only ever follow a validated internal 'next'; anything else falls
            # back to the role-appropriate landing page (prevents open redirects).
            next_url = _get_safe_next_url(request) or _default_login_redirect(user)
            return redirect(next_url)
        else:
            # Record every failed submission -- for unknown usernames too --
            # so attackers cannot distinguish existing accounts from fake ones.
            bruteforce.register_failed_attempt(request)
            messages.error(request, 'Invalid username or password.')

    return render(request, 'accounts/login.html', {'form': form})


def logout_view(request):
    logout(request)
    messages.info(request, 'You have been logged out.')
    return redirect('accounts:login')


@login_required
def profile_view(request):
    if request.method == 'POST':
        form = ProfileUpdateForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully!')
            return redirect('accounts:profile')
    else:
        form = ProfileUpdateForm(instance=request.user)
    return render(request, 'accounts/profile.html', {'form': form})


@login_required
@admin_required
def staff_list(request):
    staff = CustomUser.objects.filter(role__in=['admin', 'cashier']).order_by('-created_at')
    return render(request, 'accounts/staff_list.html', {'staff': staff})


@login_required
@admin_required
def staff_create(request):
    if request.method == 'POST':
        form = StaffCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            log_action(request.user, 'staff.create', user, detail=f'role={user.role}')
            messages.success(request, f'Staff account for {user.username} created successfully!')
            return redirect('accounts:staff_list')
    else:
        form = StaffCreateForm()
    return render(request, 'accounts/staff_form.html', {'form': form, 'title': 'Add Staff'})


@login_required
@admin_required
def staff_toggle(request, pk):
    user = get_object_or_404(CustomUser, pk=pk)
    if user != request.user:
        try:
            if user.is_active:
                user.deactivate()
                log_action(request.user, 'staff.deactivate', user)
                status = 'deactivated'
            else:
                user.activate()
                log_action(request.user, 'staff.activate', user)
                status = 'activated'
        except ValidationError as exc:
            # The model guard blocks deactivating the last administrator;
            # keep the account active and tell the caller why.
            return JsonResponse({'success': False, 'error': ' '.join(exc.messages)})
        return JsonResponse({'success': True, 'status': status})
    return JsonResponse({'success': False, 'error': 'Cannot deactivate yourself'})


# ── Password Management Views ─────────────────────────────────────────────────

@login_required
def change_password(request):
    """Authenticated password-change for staff (admin + cashier).

    Uses Django's update_session_auth_hash() after a successful change so the
    user's session remains valid — they are NOT logged out, which is the
    expected UX for a deliberate password change (as opposed to a reset).
    The new password is hashed by Django's pipeline; it is never stored in
    plaintext or surfaced in logs/errors.
    """
    form = StaffPasswordChangeForm(user=request.user, data=request.POST or None)

    if request.method == 'POST' and form.is_valid():
        form.save()
        # Rotate the session auth hash so the current session stays valid
        # after the password change (prevents an immediate self-lockout).
        update_session_auth_hash(request, request.user)
        log_action(request.user, 'account.password_change', request.user,
                   object_repr=str(request.user))
        messages.success(request, 'Your password has been changed successfully.')
        return redirect('accounts:profile')

    return render(request, 'accounts/change_password.html', {'form': form})


def password_reset_request(request):
    """Step 1 — staff member submits their email address.

    Sends a password-reset link to the address if it belongs to an active
    staff account (admin or cashier).  The response is always the same
    success page whether or not a match is found (anti-enumeration: the
    user cannot tell whether an email address is registered).
    """
    form = PasswordResetRequestForm(data=request.POST or None)

    if request.method == 'POST' and form.is_valid():
        email = form.cleaned_data['email']

        # Look up active staff only — customers do not use this portal.
        # Use filter().first() rather than get() to avoid DoesNotExist
        # leaking account existence through exception timing.
        user = CustomUser.objects.filter(
            email__iexact=email,
            is_active=True,
            role__in=['admin', 'cashier'],
        ).first()

        if user:
            # Build the uidb64/token pair using Django's built-in generator.
            # Tokens are HMAC-signed, embed the user's password hash and last-
            # login timestamp, and expire after PASSWORD_RESET_TIMEOUT seconds
            # (default 3 days).  No token is stored in the database.
            uid   = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)

            from django.urls import reverse
            reset_url = request.build_absolute_uri(
                reverse('accounts:password_reset_confirm',
                        kwargs={'uidb64': uid, 'token': token})
            )

            subject = 'Kape De Manubag — Password Reset'
            body = render_to_string('accounts/password_reset_email.html', {
                'user': user,
                'reset_url': reset_url,
                'site_name': 'Kape De Manubag',
            })

            try:
                send_mail(
                    subject=subject,
                    message=body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=False,
                )
            except Exception:
                # Log the failure but do not reveal it to the requester —
                # the same success page is shown so enumeration remains
                # impossible even when email delivery fails.
                import logging
                logging.getLogger(__name__).exception(
                    'Password reset email failed for user pk=%s', user.pk
                )

        # Always redirect to the same "check your email" page regardless of
        # whether a matching account was found.
        return redirect('accounts:password_reset_done')

    return render(request, 'accounts/password_reset_request.html', {'form': form})


def password_reset_done(request):
    """Step 2 — confirmation page shown after the reset email is sent."""
    return render(request, 'accounts/password_reset_done.html')


def password_reset_confirm(request, uidb64, token):
    """Step 3 — staff member clicks the link and sets a new password.

    The uidb64/token pair is validated by Django's built-in token generator.
    An invalid or expired link shows a clear error and never processes a
    password change.  The new password is validated against every configured
    AUTH_PASSWORD_VALIDATORS rule before being stored.
    """
    # Decode and look up the user — treat any error as an invalid link.
    try:
        uid  = force_str(urlsafe_base64_decode(uidb64))
        user = CustomUser.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, CustomUser.DoesNotExist):
        user = None

    # Check the token is valid and unused for this user.
    token_valid = (
        user is not None
        and default_token_generator.check_token(user, token)
    )

    if not token_valid:
        return render(request, 'accounts/password_reset_confirm.html', {
            'invalid_link': True,
        })

    if request.method == 'POST':
        new_password1 = request.POST.get('new_password1', '')
        new_password2 = request.POST.get('new_password2', '')
        errors = []

        if new_password1 != new_password2:
            errors.append('The two passwords did not match.')
        else:
            from django.contrib.auth import password_validation
            from django import forms as django_forms
            try:
                password_validation.validate_password(new_password1, user)
            except django_forms.ValidationError as exc:
                errors.extend(exc.messages)

        if errors:
            return render(request, 'accounts/password_reset_confirm.html', {
                'invalid_link': False,
                'errors': errors,
                'uidb64': uidb64,
                'token': token,
            })

        user.set_password(new_password1)
        user.save(update_fields=['password'])
        log_action(user, 'account.password_reset', user,
                   object_repr=str(user))
        messages.success(request, 'Your password has been reset. You can now log in.')
        return redirect('accounts:login')

    return render(request, 'accounts/password_reset_confirm.html', {
        'invalid_link': False,
        'uidb64': uidb64,
        'token': token,
    })
