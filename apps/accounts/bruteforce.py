"""
Brute-force login protection for the Kape De Manubag login view.

Failed logins are counted per *username* and per *client IP* (two independent
counters). When either counter reaches its threshold, that username and/or IP
is temporarily locked out for a cooldown period. The checks run *before*
authentication, so during a lockout no credential is ever verified and the
lockout is never extended by continued hammering.

Deliberate anti-enumeration properties:

* Every failed attempt is recorded regardless of whether the username exists,
  so the lockout message and behaviour are identical for known and unknown
  accounts.
* Counters use a sliding window: attempts older than the cooldown period are
  forgotten, so a stale counter can never lock anyone out.

Configuration (Django settings, all optional):

* ``LOGIN_MAX_ATTEMPTS_PER_USERNAME`` (default 5)  — per-account threshold.
* ``LOGIN_MAX_ATTEMPTS_PER_IP`` (default 10)        — per-IP threshold, kept
  higher so staff sharing one office/public IP are not locked out by one
  colleague's mistakes.
* ``LOGIN_LOCKOUT_MINUTES`` (default 15)            — cooldown length.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import FailedLoginAttempt


def _lockout_duration():
    return timedelta(minutes=getattr(settings, 'LOGIN_LOCKOUT_MINUTES', 15))


def _threshold(scope):
    if scope == FailedLoginAttempt.SCOPE_USERNAME:
        return getattr(settings, 'LOGIN_MAX_ATTEMPTS_PER_USERNAME', 5)
    return getattr(settings, 'LOGIN_MAX_ATTEMPTS_PER_IP', 10)


def normalize_username(raw):
    """Normalize a submitted username for counting (strip + casefold)."""
    return (raw or '').strip().casefold()


def _client_ip(request):
    """The direct peer address. ``X-Forwarded-For`` is deliberately ignored:
    trusting it would let attackers spoof their way around the IP lockout.

    Note: behind a reverse proxy that does not pass the real peer through to
    REMOTE_ADDR, every user would share one IP and the (deliberately higher)
    per-IP threshold could block them all. The project's deployment
    (PythonAnywhere, single process) passes the client IP through, so this is
    only relevant if a proxy layer is added later.
    """
    return (request.META.get('REMOTE_ADDR') or '').strip()


def register_failed_attempt(request):
    """Record one failed login for the submitted username and client IP.

    Counters reset automatically once older than the lockout window, so a
    handful of mistakes spread over days never adds up to a lockout.
    """
    username = normalize_username(request.POST.get('username'))
    ip = _client_ip(request)
    now = timezone.now()

    for scope, value in (
        (FailedLoginAttempt.SCOPE_USERNAME, username),
        (FailedLoginAttempt.SCOPE_IP, ip),
    ):
        if not value:
            continue
        # The read-modify-write below is not atomic under concurrency (two
        # simultaneous failures could both read the same count). At single-
        # store scale with SQLite this is acceptable; swapping in F()
        # expressions would harden it if the app ever runs multi-process.
        row, _ = FailedLoginAttempt.objects.get_or_create(
            scope=scope, value=value,
            defaults={'attempts': 0, 'last_attempt_at': now},
        )
        if now - row.last_attempt_at >= _lockout_duration():
            row.attempts = 1          # fresh window: forget the stale count
        else:
            row.attempts += 1
        row.last_attempt_at = now
        row.save(update_fields=['attempts', 'last_attempt_at'])


def get_lockout_remaining(request):
    """Seconds left in any active lockout for this request, or ``None``.

    Returns the longer of the username/IP lockouts. A lockout is active only
    while the most recent attempt is still inside the cooldown window, so it
    always expires on its own.
    """
    username = normalize_username(request.POST.get('username'))
    ip = _client_ip(request)
    now = timezone.now()
    duration = _lockout_duration()

    remaining = None
    for scope, value in (
        (FailedLoginAttempt.SCOPE_USERNAME, username),
        (FailedLoginAttempt.SCOPE_IP, ip),
    ):
        if not value:
            continue
        row = FailedLoginAttempt.objects.filter(scope=scope, value=value).first()
        if row is None or row.attempts < _threshold(scope):
            continue
        seconds = int((duration - (now - row.last_attempt_at)).total_seconds())
        if seconds > 0:
            remaining = max(remaining or 0, seconds)
    return remaining


def clear_failed_attempts(request):
    """Reset counters for a successful login (username and IP rows).

    Deleting the IP row also forgives failed attempts made by *other*
    usernames from that IP. That is intentional: a genuine user successfully
    logging in is good evidence the IP is legitimate.
    """
    values = [normalize_username(request.POST.get('username')), _client_ip(request)]
    FailedLoginAttempt.objects.filter(value__in=[v for v in values if v]).delete()


def lockout_message(remaining_seconds):
    """Friendly, information-safe message shown while a lockout is active."""
    minutes = max(1, -(-remaining_seconds // 60))   # ceil division
    unit = 'minute' if minutes == 1 else 'minutes'
    return (
        f'Too many failed login attempts. Please try again in '
        f'{minutes} {unit}.'
    )
