"""Startup system checks for the MFA configuration (Phase 5D remediation).

These run on every ``manage.py check`` (and therefore on deploy/migrate
commands), so a production deployment with a missing or malformed
``MFA_ENCRYPTION_KEY`` fails early with a clear message instead of silently
deriving a key from ``SECRET_KEY`` or returning a request-time 500.

Messages never include the configured value.
"""
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

KEY_HINT = (
    'Generate one with: python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())" and set it in the environment.'
)


@register(Tags.security)
def mfa_encryption_key_check(app_configs, **kwargs):
    """Require a valid ``MFA_ENCRYPTION_KEY`` outside development."""
    raw = (getattr(settings, 'MFA_ENCRYPTION_KEY', '') or '').strip()
    allow_fallback = getattr(
        settings, 'MFA_ALLOW_SECRET_KEY_FALLBACK', settings.DEBUG)

    if not raw:
        if not allow_fallback:
            return [Error(
                'MFA_ENCRYPTION_KEY is not set.',
                hint=(
                    'Set MFA_ENCRYPTION_KEY when DEBUG=False so authenticator '
                    'secrets are encrypted with a dedicated key. ' + KEY_HINT
                ),
                id='accounts.E001',
            )]
        if not settings.DEBUG:
            return [Warning(
                'MFA_ENCRYPTION_KEY is not set; authenticator secrets use a '
                'key derived from SECRET_KEY because '
                'MFA_ALLOW_SECRET_KEY_FALLBACK is enabled.',
                hint=(
                    'Set MFA_ENCRYPTION_KEY for production deployments. The '
                    'SECRET_KEY-derived fallback exists for development and '
                    'tests only. ' + KEY_HINT
                ),
                id='accounts.W001',
            )]
        return []

    try:
        Fernet(raw.encode('ascii'))
    except (ValueError, TypeError, UnicodeEncodeError):
        return [Error(
            'MFA_ENCRYPTION_KEY is not a valid Fernet key.',
            hint='It must be a 44-character urlsafe base64 value. ' + KEY_HINT,
            id='accounts.E002',
        )]
    return []
