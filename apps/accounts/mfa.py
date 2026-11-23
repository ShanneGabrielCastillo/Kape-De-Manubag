"""Multi-factor authentication services (Phase 5D).

Design
------
* **TOTP only**, verified with :mod:`django_otp.oath` (the maintained
  django-otp library) rather than a hand-rolled RFC 6238 implementation.
  Authenticator apps (Google, Microsoft, Aegis, 1Password, …) all support it.
* **Secrets encrypted at rest** with Fernet (``cryptography``). The key comes
  from ``MFA_ENCRYPTION_KEY``. When that is unset, production
  (``DEBUG=False``) refuses to start via system check ``accounts.E001``; local
  development and tests may opt into a dedicated key derived from
  ``SECRET_KEY`` with HKDF (``MFA_ALLOW_SECRET_KEY_FALLBACK``), so no plaintext
  secret ever reaches the database in any environment.
* **No half-enabled devices**: the secret lives only in the (server-side)
  session until the member proves possession by entering a valid code. Only
  then is a ``StaffMFADevice`` row created.
* **Replay-proof**: the accepted TOTP time step is persisted and passed as
  ``min_t`` on the next verification, so a code cannot be reused.
* **Recovery codes** are high-entropy, single-use, and stored only as Django
  password hashes.
* **Pending login challenges** carry their user, nonce and expiry in the
  session, HMAC-signed with ``SECRET_KEY``. They are validated for signature,
  expiry, account state and staff role before any code is checked. The view
  never accepts a user id from the request, so the pending identity cannot be
  switched by the client.
* **Throttling** uses a dedicated ``MFAVerificationAttempt`` table (per user
  and per IP, sliding 15-minute window, same shape as Phases 5B/5C) so MFA
  brute force can neither lock nor be cleared by password-login counters.
* **Enrollment requires the current password**: a short-lived, session-bound
  proof (``MFA_ENROLL_REAUTH_SECONDS``) must be present before a secret is
  generated or displayed. Replacing an enrolled device is never possible with
  the password alone -- it requires the existing disable flow (TOTP/recovery
  code plus recent verification).

Settings (all optional):

``MFA_ENCRYPTION_KEY``            Fernet key (44-char urlsafe base64); required
                                 when ``DEBUG=False``.
``MFA_ALLOW_SECRET_KEY_FALLBACK`` allow the SECRET_KEY-derived dev/test key.
``MFA_MAX_ATTEMPTS_PER_USER``     default 5.
``MFA_MAX_ATTEMPTS_PER_IP``       default 10.
``MFA_LOCKOUT_MINUTES``           default 15.
``MFA_CHALLENGE_TIMEOUT_SECONDS`` default 300 (5 minutes).
``MFA_SETUP_SECRET_TIMEOUT_SECONDS`` default 900 (15 minutes).
``MFA_SESSION_MAX_AGE_SECONDS``   default 43200 (12 hours).
``MFA_RECOVERY_CODE_COUNT``       default 10.

Nothing in this module logs or returns a TOTP secret, provisioning URI or
verification code.
"""
import base64
import hashlib
import hmac
import io
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import quote, urlencode

import qrcode
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django_otp.oath import TOTP

from .models import MFARecoveryCode, MFAVerificationAttempt, StaffMFADevice

STAFF_ROLES = ('admin', 'cashier', 'kitchen_staff')
ISSUER = 'Kape De Manubag'

SESSION_PENDING = 'mfa_pending'
SESSION_SETUP = 'mfa_setup_secret'
SESSION_ENROLL = 'mfa_enroll_verified'
SESSION_VERIFIED = 'mfa_verified'

RECOVERY_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'   # no 0/1/I/O
RECOVERY_CODE_LENGTH = 16                                 # 80 bits of entropy


class MFAUnavailable(Exception):
    """The stored device secret cannot be used (e.g. encryption key rotated)."""


# ── Roles ─────────────────────────────────────────────────────────────────────

def is_staff_user(user):
    """True for accounts that may use the staff portal (not customers)."""
    return bool(
        user is not None
        and getattr(user, 'is_authenticated', False)
        and getattr(user, 'role', None) in STAFF_ROLES
    )


def can_enroll(user):
    """Accounts allowed to hold an authenticator: staff or admin-site users."""
    return is_staff_user(user) or requires_admin_mfa(user)


def requires_admin_mfa(user):
    """Accounts that must hold a confirmed MFA device to reach Django Admin."""
    return bool(
        getattr(user, 'is_authenticated', False)
        and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))
    )


# ── Secret encryption ─────────────────────────────────────────────────────────

def _fernet():
    """Return the Fernet instance used to encrypt TOTP secrets.

    A dedicated ``MFA_ENCRYPTION_KEY`` is required in production; a fallback
    key derived from ``SECRET_KEY`` is allowed only when
    ``MFA_ALLOW_SECRET_KEY_FALLBACK`` is on (default: ``DEBUG``). The startup
    system check rejects a missing production key before the first request.
    """
    raw = (getattr(settings, 'MFA_ENCRYPTION_KEY', '') or '').strip()
    if raw:
        try:
            return Fernet(raw.encode('ascii'))
        except (ValueError, TypeError, UnicodeEncodeError):
            raise ImproperlyConfigured(
                'MFA_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: '
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )

    allow_fallback = getattr(
        settings, 'MFA_ALLOW_SECRET_KEY_FALLBACK', settings.DEBUG)
    if not allow_fallback:
        raise ImproperlyConfigured(
            'MFA_ENCRYPTION_KEY must be set when DEBUG=False. Generate one with: '
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b'kape-de-manubag-mfa-v1',
        info=b'staff-totp-secret-encryption',
    )
    derived = hkdf.derive(settings.SECRET_KEY.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(secret):
    return _fernet().encrypt(secret.encode('ascii')).decode('ascii')


def decrypt_secret(token):
    try:
        return _fernet().decrypt(token.encode('ascii')).decode('ascii')
    except (InvalidToken, ValueError, TypeError):
        raise MFAUnavailable('Stored MFA secret cannot be decrypted.')


# ── TOTP primitives ───────────────────────────────────────────────────────────

def generate_secret():
    """A fresh 160-bit TOTP secret, base32 encoded without padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _totp(secret, drift=0):
    padded = secret + '=' * (-len(secret) % 8)
    return TOTP(base64.b32decode(padded), step=30, digits=6, drift=drift)


def provisioning_uri(secret, user):
    """``otpauth://`` URI for authenticator apps (shown once, never logged)."""
    label = quote(f'{ISSUER}:{user.get_username()}')
    query = urlencode({
        'secret': secret,
        'issuer': ISSUER,
        'algorithm': 'SHA1',
        'digits': 6,
        'period': 30,
    })
    return f'otpauth://totp/{label}?{query}'


def qr_data_uri(uri):
    """PNG QR code as a ``data:`` URI (no third-party QR service involved)."""
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color='black', back_color='white')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def verify_code(secret, raw_code):
    """Verify a 6-digit code against ``secret`` (no replay tracking).

    Used for pending enrollment, where there is no device row yet. Returns the
    matched time step, or ``None``. ``tolerance=1`` accepts ±30 s of clock
    skew; ``min_t`` is handled by :func:`verify_device_code`.
    """
    code = (raw_code or '').strip().replace(' ', '')
    if not re.fullmatch(r'\d{6}', code):
        return None
    totp = _totp(secret)
    if not totp.verify(int(code), tolerance=1):
        return None
    return totp.t()


def verify_device_code(device, raw_code):
    """Verify a code for ``device``, rejecting replayed/earlier time steps.

    The accepted time step is consumed with a conditional ``UPDATE`` that only
    succeeds while the stored step is still lower than the matched one. Two
    concurrent submissions of the same code therefore race on the database:
    exactly one wins, and only that request returns a step. Returns the step
    or ``None``.
    """
    code = (raw_code or '').strip().replace(' ', '')
    if not re.fullmatch(r'\d{6}', code):
        return None
    secret = decrypt_secret(device.secret_encrypted)
    min_t = (device.last_used_timestep + 1) if device.last_used_timestep else None
    totp = _totp(secret)
    if not totp.verify(int(code), tolerance=1, min_t=min_t):
        return None
    step = totp.t()
    consumed = StaffMFADevice.objects.filter(
        pk=device.pk, last_used_timestep__lt=step,
    ).update(last_used_timestep=step, updated_at=timezone.now())
    if not consumed:
        # Another request already consumed this (or a later) time step.
        return None
    device.last_used_timestep = step
    return step


# ── Devices ───────────────────────────────────────────────────────────────────

def get_device(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    try:
        return user.mfa_device
    except StaffMFADevice.DoesNotExist:
        return None


def has_confirmed_device(user):
    device = get_device(user)
    return bool(device and device.confirmed_at)


def activate_device(user, secret):
    """Create/confirm the member's device and issue fresh recovery codes."""
    now = timezone.now()
    device, _ = StaffMFADevice.objects.update_or_create(
        user=user,
        defaults={
            'secret_encrypted': encrypt_secret(secret),
            'confirmed_at': now,
            'last_used_timestep': 0,
        },
    )
    codes = regenerate_recovery_codes(user)
    return device, codes


def delete_device(user):
    """Remove the authenticator and every recovery code for ``user``."""
    StaffMFADevice.objects.filter(user=user).delete()
    MFARecoveryCode.objects.filter(user=user).delete()


# ── Recovery codes ────────────────────────────────────────────────────────────

def _new_recovery_code():
    raw = ''.join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
    return '-'.join(raw[i:i + 4] for i in range(0, len(raw), 4))


def normalize_recovery_code(raw):
    return re.sub(r'[^A-Z0-9]', '', (raw or '').upper())


def regenerate_recovery_codes(user):
    """Replace all recovery codes for ``user``; returns the plaintext once.

    Deletion and creation happen in one transaction, so a failure while
    creating the replacements leaves the previous codes intact.
    """
    count = int(getattr(settings, 'MFA_RECOVERY_CODE_COUNT', 10))
    codes = [_new_recovery_code() for _ in range(count)]
    with transaction.atomic():
        MFARecoveryCode.objects.filter(user=user).delete()
        MFARecoveryCode.objects.bulk_create([
            MFARecoveryCode(user=user, code_hash=make_password(normalize_recovery_code(code)))
            for code in codes
        ])
    return codes


def unused_recovery_code_count(user):
    return MFARecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


def consume_recovery_code(user, raw_code):
    """Consume one unused recovery code. Returns True only on success."""
    normalized = normalize_recovery_code(raw_code)
    if len(normalized) != RECOVERY_CODE_LENGTH:
        return False
    for code in MFARecoveryCode.objects.filter(user=user, used_at__isnull=True).order_by('id'):
        if check_password(normalized, code.code_hash):
            # update() is conditional so two concurrent submissions of the
            # same code cannot both succeed (single-use guarantee).
            updated = MFARecoveryCode.objects.filter(
                pk=code.pk, used_at__isnull=True,
            ).update(used_at=timezone.now())
            return bool(updated)
    return False


# ── Throttling ────────────────────────────────────────────────────────────────

def _window():
    return timedelta(minutes=getattr(settings, 'MFA_LOCKOUT_MINUTES', 15))


def _threshold(scope):
    if scope == MFAVerificationAttempt.SCOPE_USER:
        return getattr(settings, 'MFA_MAX_ATTEMPTS_PER_USER', 5)
    return getattr(settings, 'MFA_MAX_ATTEMPTS_PER_IP', 10)


def _client_ip(request):
    """Direct peer address; ``X-Forwarded-For`` is ignored (see Phase 5B)."""
    return (request.META.get('REMOTE_ADDR') or '').strip()


def _attempt_pairs(request, user):
    return (
        (MFAVerificationAttempt.SCOPE_USER, str(user.pk) if user else None),
        (MFAVerificationAttempt.SCOPE_IP, _client_ip(request)),
    )


def throttle_remaining(request, user):
    """Seconds until another code may be tried, or ``None`` when allowed."""
    now = timezone.now()
    duration = _window()
    remaining = None
    for scope, value in _attempt_pairs(request, user):
        if not value:
            continue
        row = MFAVerificationAttempt.objects.filter(scope=scope, value=value).first()
        if row is None or row.attempts < _threshold(scope):
            continue
        seconds = int((duration - (now - row.last_attempt_at)).total_seconds())
        if seconds > 0:
            remaining = max(remaining or 0, seconds)
    return remaining


def register_failure(request, user):
    """Count one failed code/recovery attempt for the member and the IP."""
    now = timezone.now()
    for scope, value in _attempt_pairs(request, user):
        if not value:
            continue
        row, _ = MFAVerificationAttempt.objects.get_or_create(
            scope=scope, value=value,
            defaults={'attempts': 0, 'last_attempt_at': now},
        )
        if now - row.last_attempt_at >= _window():
            row.attempts = 1
        else:
            row.attempts += 1
        row.last_attempt_at = now
        row.save(update_fields=['attempts', 'last_attempt_at'])


def clear_failures(request, user):
    """Forget MFA failures after a successful verification."""
    for scope, value in _attempt_pairs(request, user):
        if value:
            MFAVerificationAttempt.objects.filter(scope=scope, value=value).delete()


# ── Safe redirects ────────────────────────────────────────────────────────────

def safe_next_url(request, raw, default='', *, prefix=None):
    """Validate a redirect target (same host, http/https) for MFA round trips."""
    from django.utils.http import url_has_allowed_host_and_scheme
    candidate = (raw or '').strip()
    if not candidate:
        return default
    if not url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return default
    if prefix and not candidate.startswith(prefix):
        return default
    return candidate


# ── Pending login challenge ──────────────────────────────────────────────────

def _sign_pending(uid, nonce, issued, mode):
    payload = f'{uid}:{nonce}:{issued}:{mode}'.encode('utf-8')
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'), payload, hashlib.sha256,
    ).hexdigest()


def _challenge_timeout():
    return int(getattr(settings, 'MFA_CHALLENGE_TIMEOUT_SECONDS', 300))


def start_pending(request, user, *, next_url='', mode='login'):
    """Begin the MFA step after a correct password (no session login yet)."""
    now = int(time.time())
    nonce = secrets.token_hex(16)
    request.session[SESSION_PENDING] = {
        'uid': user.pk,
        'nonce': nonce,
        'issued': now,
        'mode': mode,
        'next': next_url or '',
        'sig': _sign_pending(user.pk, nonce, now, mode),
    }


def clear_pending(request):
    request.session.pop(SESSION_PENDING, None)


def get_pending(request):
    """Return the validated pending challenge (with ``user``) or ``None``.

    Rejects: missing/malformed data, bad signature (tampering), expiry,
    unknown/inactive accounts, and accounts that are no longer staff.
    """
    from django.contrib.auth import get_user_model

    data = request.session.get(SESSION_PENDING)
    if not isinstance(data, dict):
        return None
    try:
        uid = int(data['uid'])
        nonce = str(data['nonce'])
        issued = int(data['issued'])
        mode = str(data['mode'])
        signature = str(data['sig'])
    except (KeyError, TypeError, ValueError):
        return None
    if mode not in ('login', 'admin'):
        return None
    if not constant_time_compare(signature, _sign_pending(uid, nonce, issued, mode)):
        return None
    if issued > time.time() + 60 or time.time() - issued > _challenge_timeout():
        return None

    user = get_user_model().objects.filter(pk=uid, is_active=True).first()
    if user is None or not can_enroll(user):
        return None
    return {
        'user': user,
        'uid': uid,
        'nonce': nonce,
        'issued': issued,
        'mode': mode,
        'next': str(data.get('next') or ''),
    }


# ── Session MFA state ────────────────────────────────────────────────────────

def mark_session_verified(request, user):
    request.session[SESSION_VERIFIED] = {
        'uid': user.pk,
        'at': int(time.time()),
    }


def clear_session_verification(request):
    request.session.pop(SESSION_VERIFIED, None)


def session_is_verified(request):
    """True when this session has passed MFA recently, for its own user."""
    data = request.session.get(SESSION_VERIFIED)
    if not isinstance(data, dict):
        return False
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return False
    try:
        uid = int(data['uid'])
        at = int(data['at'])
    except (KeyError, TypeError, ValueError):
        return False
    if uid != user.pk:
        return False
    max_age = int(getattr(settings, 'MFA_SESSION_MAX_AGE_SECONDS', 12 * 60 * 60))
    return 0 <= time.time() - at <= max_age


def recently_verified(request, seconds=600):
    """True when MFA was completed within ``seconds`` (for disable/regenerate)."""
    data = request.session.get(SESSION_VERIFIED)
    if not session_is_verified(request) or not isinstance(data, dict):
        return False
    try:
        return time.time() - int(data['at']) <= seconds
    except (KeyError, TypeError, ValueError):
        return False


# ── Enrollment password confirmation ─────────────────────────────────────────

def _enroll_reauth_seconds():
    return int(getattr(settings, 'MFA_ENROLL_REAUTH_SECONDS', 600))


def check_user_password(user, raw_password):
    """True when ``raw_password`` is the current password of ``user``.

    Uses Django's own password hasher check, never logs the value, and
    refuses inactive accounts.
    """
    password = raw_password or ''
    if not password or user is None or not user.is_active:
        return False
    return user.check_password(password)


def mark_enrollment_verified(request, user):
    """Record a short-lived, session-bound password confirmation."""
    request.session[SESSION_ENROLL] = {'uid': user.pk, 'at': int(time.time())}


def clear_enrollment_verification(request):
    request.session.pop(SESSION_ENROLL, None)


def enrollment_verified(request, user):
    """True when this session confirmed ``user``'s password recently."""
    data = request.session.get(SESSION_ENROLL)
    if not isinstance(data, dict) or user is None:
        return False
    try:
        uid = int(data['uid'])
        at = int(data['at'])
    except (KeyError, TypeError, ValueError):
        return False
    if uid != user.pk:
        return False
    return 0 <= time.time() - at <= _enroll_reauth_seconds()


# ── Enrollment secret (session-only until confirmed) ─────────────────────────

def _setup_timeout():
    return int(getattr(settings, 'MFA_SETUP_SECRET_TIMEOUT_SECONDS', 900))


def current_setup_secret(request):
    """Return the unexpired pending enrollment secret, or ``None``."""
    data = request.session.get(SESSION_SETUP)
    if not isinstance(data, dict):
        return None
    try:
        issued = int(data['issued'])
        if time.time() - issued > _setup_timeout():
            return None
        return decrypt_secret(str(data['token']))
    except (KeyError, TypeError, ValueError, MFAUnavailable):
        return None


def ensure_setup_secret(request):
    """Return the current pending enrollment secret, creating one if needed."""
    existing = current_setup_secret(request)
    if existing is not None:
        return existing
    secret = generate_secret()
    request.session[SESSION_SETUP] = {
        'token': encrypt_secret(secret),
        'issued': int(time.time()),
    }
    return secret


def clear_setup_secret(request):
    request.session.pop(SESSION_SETUP, None)
