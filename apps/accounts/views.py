from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from . import bruteforce
from .forms import LoginForm, StaffCreateForm, ProfileUpdateForm
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
