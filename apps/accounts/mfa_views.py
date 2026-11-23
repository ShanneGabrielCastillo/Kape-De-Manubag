"""MFA enrollment, challenge, recovery and management views (Phase 5D).

All views are staff-only (customers are rejected with 403), state-changing
endpoints are POST-only, and CSRF protection is Django's default. None of
these views logs or renders a TOTP secret except the authenticated owner's
own one-time enrollment screen, and verification codes are never echoed back.
"""
import logging
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.audit.services import log_action

from . import bruteforce, mfa

logger = logging.getLogger(__name__)

MANAGE_URL = '/accounts/mfa/manage/'


def _forbidden():
    return HttpResponseForbidden('Two-factor authentication is available to staff accounts only.')


def _default_landing(user):
    if user.is_admin_user or user.is_cashier:
        return reverse('dashboard:index')
    if user.is_kitchen_staff:
        return reverse('kitchen:orders')
    return reverse('menu:index')


def _clear_password_counters(request, user):
    """Clear Phase 5B password counters only after the full MFA step succeeds."""
    bruteforce.clear_failed_attempts_for(
        user.username, request.META.get('REMOTE_ADDR', ''),
    )


def _lockout_message(remaining):
    return bruteforce.lockout_message(remaining)


@login_required
@never_cache
def mfa_setup(request):
    """Start or finish TOTP enrollment.

    Enrollment begins only after the signed-in member confirms their current
    password on a POST, which stores a short-lived, session-bound proof
    (:func:`mfa.enrollment_verified`). Until then no secret is generated or
    rendered. The secret itself stays only in the session until a valid code
    proves the authenticator holds it; no device row is created before that.

    An already-enrolled account is never re-enrolled here: replacing a device
    requires the disable flow, which demands a current TOTP/recovery code plus
    a recent verification.
    """
    user = request.user
    if not user.is_active or not mfa.can_enroll(user):
        return _forbidden()
    if mfa.has_confirmed_device(user):
        return redirect('accounts:mfa_manage')

    error = None
    status = 200
    verified = mfa.enrollment_verified(request, user)

    if request.method == 'POST' and 'password' in request.POST:
        remaining = mfa.throttle_remaining(request, user)
        if remaining is not None:
            error = _lockout_message(remaining)
            status = 429
        elif mfa.check_user_password(user, request.POST.get('password', '')):
            mfa.mark_enrollment_verified(request, user)
            mfa.clear_failures(request, user)
            return redirect('accounts:mfa_setup')
        else:
            mfa.register_failure(request, user)
            error = 'That password is not correct. Enrollment was not started.'
            status = 400

    if not verified:
        # No password confirmation in this session (or it expired): show only
        # the confirmation step. A code POST is ignored, so nothing is
        # generated or enabled without proving the current password first.
        return render(request, 'accounts/mfa_setup.html', {
            'confirm_password': True,
            'error': error,
        }, status=status)

    if request.method == 'POST':
        remaining = mfa.throttle_remaining(request, user)
        if remaining is not None:
            error = _lockout_message(remaining)
            status = 429
        else:
            secret = mfa.current_setup_secret(request)
            if secret is None:
                messages.error(request, 'The setup session expired. Scan the new QR code and try again.')
                return redirect('accounts:mfa_setup')
            if mfa.verify_code(secret, request.POST.get('code', '')) is not None:
                _device, codes = mfa.activate_device(user, secret)
                mfa.clear_setup_secret(request)
                mfa.clear_enrollment_verification(request)
                mfa.clear_failures(request, user)
                mfa.mark_session_verified(request, user)
                _clear_password_counters(request, user)
                log_action(user, 'mfa.enroll', user)
                return render(request, 'accounts/mfa_recovery_codes.html', {
                    'codes': codes,
                })
            mfa.register_failure(request, user)
            error = 'That code is not valid. Check your authenticator app and try again.'
            status = 400

    secret = mfa.current_setup_secret(request) or mfa.ensure_setup_secret(request)
    return render(request, 'accounts/mfa_setup.html', {
        'secret': secret,
        'qr_data_uri': mfa.qr_data_uri(mfa.provisioning_uri(secret, user)),
        'account_name': user.get_username(),
        'error': error,
    }, status=status)


@login_required
@never_cache
def mfa_manage(request):
    """Status page: device state, remaining recovery codes, safe actions."""
    user = request.user
    if not mfa.can_enroll(user):
        return _forbidden()
    device = mfa.get_device(user)
    return render(request, 'accounts/mfa_manage.html', {
        'device': device,
        'mfa_enabled': bool(device and device.confirmed_at),
        'unused_codes': mfa.unused_recovery_code_count(user) if device else 0,
        'recently_verified': mfa.recently_verified(request),
    })


@never_cache
def mfa_challenge(request):
    """Verify a TOTP or recovery code.

    Two contexts share this view:

    * **Pending login** (``mode='login'|'admin'``): password already verified,
      no authenticated session exists yet. On success ``login()`` is called
      (which cycles the session key) and a safe redirect is followed.
    * **Re-authentication** (authenticated staff whose session lacks a recent
      MFA marker): used before MFA disable/regenerate and after the MFA
      session age limit.

    The pending account is read only from the HMAC-signed session record, so
    the client cannot switch identity.
    """
    pending = mfa.get_pending(request)
    if pending is not None:
        user = pending['user']
        mode = pending['mode']
        next_url = pending['next']
    elif (
        request.user.is_authenticated
        and mfa.can_enroll(request.user)
        and mfa.has_confirmed_device(request.user)
    ):
        user = request.user
        mode = 'reauth'
        next_url = mfa.safe_next_url(
            request,
            request.GET.get('next') or request.POST.get('next') or '',
        )
    else:
        return redirect('accounts:login')

    device = mfa.get_device(user)
    if device is None or not device.confirmed_at:
        mfa.clear_pending(request)
        messages.error(request, 'No authenticator is configured for this account.')
        return redirect('accounts:login' if mode != 'reauth' else 'accounts:mfa_manage')

    error = None
    status = 200
    lockout = mfa.throttle_remaining(request, user)

    if request.method == 'POST':
        if lockout is not None:
            error = _lockout_message(lockout)
            status = 429
        else:
            use_recovery = 'recovery_code' in request.POST
            try:
                if use_recovery:
                    ok = mfa.consume_recovery_code(
                        user, request.POST.get('recovery_code', ''))
                else:
                    ok = mfa.verify_device_code(
                        device, request.POST.get('code', '')) is not None
            except mfa.MFAUnavailable:
                # Never leak details (or the ciphertext) to the client.
                logger.error(
                    'MFA device secret unreadable for user pk=%s '
                    '(encryption key rotated?); recovery or admin reset required.',
                    user.pk,
                )
                error = ('Your authenticator cannot be read on this server. '
                         'Use a recovery code or contact an administrator.')
                ok = False

            if ok:
                mfa.clear_failures(request, user)
                mfa.clear_pending(request)
                mfa.clear_setup_secret(request)
                if use_recovery:
                    log_action(user, 'mfa.recovery.used', user)

                if mode == 'reauth':
                    mfa.mark_session_verified(request, user)
                    _clear_password_counters(request, user)
                    return redirect(next_url or MANAGE_URL)

                login(request, user)
                mfa.mark_session_verified(request, user)
                _clear_password_counters(request, user)
                if mode == 'admin':
                    default = reverse('admin:index')
                    return redirect(mfa.safe_next_url(
                        request, next_url or default, default, prefix='/admin/'))
                return redirect(
                    mfa.safe_next_url(request, next_url) or _default_landing(user))

            mfa.register_failure(request, user)
            error = ('That recovery code is not valid.' if use_recovery
                     else 'That code is not valid. Check the time on your device and try again.')
            status = 400

    return render(request, 'accounts/mfa_challenge.html', {
        'mode': mode,
        'error': error,
        'next': next_url,
        'lockout': lockout is not None,
        'remaining_recovery_codes': mfa.unused_recovery_code_count(user),
    }, status=status)


def _require_recent_verification(request):
    """Re-authenticate before sensitive MFA changes; returns a response or None."""
    if not mfa.recently_verified(request):
        messages.error(request, 'Please verify with your authenticator again to continue.')
        return redirect(f"{reverse('accounts:mfa_challenge')}?next={quote(MANAGE_URL)}")
    return None


def _verify_for_change(request, device):
    """Verify a TOTP or recovery code for a sensitive MFA change."""
    if 'recovery_code' in request.POST:
        return mfa.consume_recovery_code(
            request.user, request.POST.get('recovery_code', ''))
    return mfa.verify_device_code(device, request.POST.get('code', '')) is not None


@login_required
@require_POST
def mfa_disable(request):
    """Disable MFA after recent re-authentication plus a valid code."""
    user = request.user
    if not mfa.can_enroll(user):
        return _forbidden()

    gate = _require_recent_verification(request)
    if gate is not None:
        return gate

    device = mfa.get_device(user)
    if device is None or not device.confirmed_at:
        messages.error(request, 'Two-factor authentication is not enabled.')
        return redirect('accounts:mfa_manage')

    remaining = mfa.throttle_remaining(request, user)
    if remaining is not None:
        messages.error(request, _lockout_message(remaining))
        return redirect('accounts:mfa_manage')

    try:
        ok = _verify_for_change(request, device)
    except mfa.MFAUnavailable:
        ok = False

    if not ok:
        mfa.register_failure(request, user)
        messages.error(request, 'The verification code was not valid. MFA is still enabled.')
        return redirect('accounts:mfa_manage')

    mfa.delete_device(user)
    mfa.clear_session_verification(request)
    log_action(user, 'mfa.disable', user)
    messages.success(request, 'Two-factor authentication has been disabled.')
    return redirect('accounts:mfa_manage')


@login_required
@require_POST
@never_cache
def mfa_regenerate_recovery_codes(request):
    """Replace recovery codes after recent re-authentication and a valid code."""
    user = request.user
    if not mfa.can_enroll(user):
        return _forbidden()

    gate = _require_recent_verification(request)
    if gate is not None:
        return gate

    device = mfa.get_device(user)
    if device is None or not device.confirmed_at:
        messages.error(request, 'Two-factor authentication is not enabled.')
        return redirect('accounts:mfa_manage')

    remaining = mfa.throttle_remaining(request, user)
    if remaining is not None:
        messages.error(request, _lockout_message(remaining))
        return redirect('accounts:mfa_manage')

    try:
        ok = _verify_for_change(request, device)
    except mfa.MFAUnavailable:
        ok = False

    if not ok:
        mfa.register_failure(request, user)
        messages.error(request, 'The verification code was not valid. Recovery codes were not changed.')
        return redirect('accounts:mfa_manage')

    codes = mfa.regenerate_recovery_codes(user)
    mfa.clear_failures(request, user)
    log_action(user, 'mfa.recovery.regenerate', user)
    return render(request, 'accounts/mfa_recovery_codes.html', {'codes': codes})
