"""Rate limiting for password-reset requests (Phase 5C).

Deliberately separate from the login brute-force limiter
(``apps.accounts.bruteforce``): the login limiter stores raw usernames
and *deletes* counters on successful login — both semantics are wrong
for password-reset throttling, where the reset target must not be
stored in plaintext and a successful login by one person must never
erase another account's reset protection.

Design (mirrors the login limiter's sliding-window pattern):

* Failed/total requests are counted per *keyed email hash* and per
  *client IP* (two independent counters). When either counter reaches
  its threshold, further requests are throttled for a cooldown period.
* The email identifier is a keyed one-way hash — HMAC-SHA256 over the
  casefolded address with the deployment ``SECRET_KEY`` — so throttle
  records never contain raw email addresses and cannot be reversed to
  enumerate staff addresses even if the table leaks.
* The check runs *before* any account lookup or email delivery, so a
  throttled request never touches user rows and never sends mail.
* Throttled requests are deliberately NOT counted: a stale counter can
  never extend its own lockout, so throttling is temporary and a person
  who reaches the limit can request again once the window expires.

Configuration (Django settings, all optional):

* ``RESET_MAX_PER_EMAIL``   (default 3)  — per-address threshold.
* ``RESET_MAX_PER_IP``      (default 10) — per-IP threshold, kept higher
  so several legitimate staff resets from one office IP do not collide.
* ``RESET_THROTTLE_MINUTES`` (default 15) — cooldown length.

Limits are intentionally aligned with the login limiter's shape
(per-username 5 / per-IP 10 / 15 minutes): reset emails are low-volume
operational mail, so a small per-address quota stops mailbox flooding
while the higher per-IP quota tolerates shared office addresses.
"""
import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import PasswordResetAttempt


def _window():
    return timedelta(minutes=getattr(settings, 'RESET_THROTTLE_MINUTES', 15))


def _threshold(scope):
    if scope == PasswordResetAttempt.SCOPE_EMAIL:
        return getattr(settings, 'RESET_MAX_PER_EMAIL', 3)
    return getattr(settings, 'RESET_MAX_PER_IP', 10)


def normalize_email(raw):
    """Normalize a submitted email for hashing (strip + casefold).

    One consistent normalization for the per-email counter regardless of
    how the address is typed ('User@Example.COM ' == 'user@example.com').
    The view's account lookup stays ``email__iexact`` — unchanged."""
    return (raw or '').strip().casefold()


def keyed_email_identifier(email):
    """One-way keyed identifier for the requested email address.

    HMAC-SHA256 keyed with the deployment SECRET_KEY over the normalized
    address: not reversible without the secret, so the throttle table
    never stores (or leaks) raw email addresses."""
    normalized = normalize_email(email)
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        normalized.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def _client_ip(request):
    """The direct peer address (``X-Forwarded-For`` deliberately ignored —
    same rationale as the login limiter)."""
    return (request.META.get('REMOTE_ADDR') or '').strip()


def throttle_remaining(request, email):
    """Seconds until this request may proceed, or ``None`` if allowed.

    Returns the longer of the per-email and per-IP windows. A counter only
    throttles while its most recent request is still inside the window, so
    every throttle expires on its own — no permanent lockout."""
    now = timezone.now()
    duration = _window()
    remaining = None

    pairs = (
        (PasswordResetAttempt.SCOPE_EMAIL,
         keyed_email_identifier(email) if email else None),
        (PasswordResetAttempt.SCOPE_IP, _client_ip(request)),
    )
    for scope, value in pairs:
        if not value:
            continue
        row = PasswordResetAttempt.objects.filter(
            scope=scope, value=value).first()
        if row is None or row.attempts < _threshold(scope):
            continue
        seconds = int((duration - (now - row.last_attempt_at)).total_seconds())
        if seconds > 0:
            remaining = max(remaining or 0, seconds)
    return remaining


def register_reset_request(request, email):
    """Record one request submission for the hashed email + IP counters.

    Called for every accepted submission — eligible or not — so attackers
    probing unknown addresses hit exactly the same counters as real ones.
    The counter write is a read-modify-write like the login limiter's: not
    atomic under concurrency, acceptable at single-store SQLite scale."""
    now = timezone.now()
    email_value = keyed_email_identifier(email) if email else None

    for scope, value in (
        (PasswordResetAttempt.SCOPE_EMAIL, email_value),
        (PasswordResetAttempt.SCOPE_IP, _client_ip(request)),
    ):
        if not value:
            continue
        row, _ = PasswordResetAttempt.objects.get_or_create(
            scope=scope, value=value,
            defaults={'attempts': 0, 'last_attempt_at': now},
        )
        if now - row.last_attempt_at >= _window():
            row.attempts = 1          # fresh window: forget the stale count
        else:
            row.attempts += 1
        row.last_attempt_at = now
        row.save(update_fields=['attempts', 'last_attempt_at'])
