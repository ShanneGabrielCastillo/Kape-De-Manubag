"""
Django settings for Kape De Manubag Food Ordering & Management System
"""

import logging
import os
import warnings
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from the project's .env file (if present).
# An explicit path is used so behaviour is identical no matter the current
# working directory (runserver, gunicorn, WSGI, cron, etc.).
load_dotenv(BASE_DIR / '.env', override=True)


# ── Environment helpers ──────────────────────────────────────────────────────
def _env_bool(name, default=False):
    """Parse an environment variable as a boolean.

    Accepts 1, true, yes, on, y (case-insensitive) as True. Everything else
    -- including a missing variable -- evaluates to ``default``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on', 'y'}


def _env_list(name, default=None):
    """Parse a comma-separated environment variable into a cleaned list."""
    raw = os.environ.get(name)
    if raw is None:
        return default if default is not None else []
    return [item.strip() for item in raw.split(',') if item.strip()]


def _require_env(name, hint=''):
    """Return a non-empty env variable or abort startup with a clear error."""
    value = os.environ.get(name)
    if not value or not value.strip():
        raise ImproperlyConfigured(
            f'The required environment variable {name} is not set. {hint}'.strip()
        )
    return value.strip()


# ── Security ──────────────────────────────────────────────────────────────────
# DEBUG decides whether the project runs in development or production mode.
# It defaults to False so that a deployment without configuration can never
# silently run with insecure development defaults.
DEBUG = _env_bool('DEBUG')

if DEBUG:
    # ── Development defaults (used ONLY when DEBUG=True) ────────────────────
    SECRET_KEY = os.environ.get(
        'SECRET_KEY',
        'django-insecure-dev-only-key-do-not-use-in-production',
    )
    if not os.environ.get('SECRET_KEY'):
        warnings.warn(
            'SECRET_KEY is not set - using an insecure development-only key. '
            'Set SECRET_KEY in your .env file (see .env.example).',
            stacklevel=1,
        )
    ALLOWED_HOSTS = _env_list('ALLOWED_HOSTS', ['localhost', '127.0.0.1', '[::1]'])
else:
    # ── Production configuration (fail-fast on missing variables) ───────────
    SECRET_KEY = _require_env(
        'SECRET_KEY',
        'This is required when DEBUG=False (production). Generate one with '
        '"python -c \'import secrets; print(secrets.token_urlsafe(50))\'" '
        'and add it to your .env file.',
    )
    ALLOWED_HOSTS = _env_list('ALLOWED_HOSTS')
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            'The required environment variable ALLOWED_HOSTS is not set. '
            'This is required when DEBUG=False (production). Add the '
            'comma-separated hostnames of your deployment '
            '(e.g. yourdomain.com, www.yourdomain.com) to your .env file.'
        )
    if '*' in ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            'ALLOWED_HOSTS contains "*", which is insecure in production. '
            'List your actual hostnames (e.g. yourdomain.com) in your .env file.'
        )

# Required for CSRF on HTTPS deployments (e.g. PythonAnywhere, which always
# uses HTTPS). Optional: an empty list simply means cross-origin POST
# requests are rejected.
CSRF_TRUSTED_ORIGINS = _env_list('CSRF_TRUSTED_ORIGINS')

# ── Application definition ────────────────────────────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'cloudinary_storage',          # must be before django.contrib.staticfiles
    'cloudinary',
    # Custom apps
    'apps.accounts',
    'apps.audit',
    'apps.menu',
    'apps.orders',
    'apps.inventory',
    'apps.reports',
    'apps.dashboard',
    'apps.realtime',
    'apps.finance',
    'apps.chatbot',
    'apps.kitchen',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',   # must be second
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'apps.accounts.middleware.ActiveUserMiddleware',
    'apps.accounts.middleware.SessionIdleTimeoutMiddleware',
    'apps.accounts.middleware.MFAEnforcementMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'kape_de_manubag.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.orders.context_processors.cart_count',
            ],
        },
    },
]

WSGI_APPLICATION = 'kape_de_manubag.wsgi.application'

# ── Database ──────────────────────────────────────────────────────────────────
# Selection rules (Phase 7B-1):
#   DATABASE_URL set                                -> PostgreSQL
#   DEBUG=True, no DATABASE_URL                     -> SQLite (development)
#   DEBUG=False, no DATABASE_URL, explicit opt-in   -> SQLite (deliberate)
#   DEBUG=False, no DATABASE_URL, no opt-in         -> refuse to start
#   DATABASE_URL AND ALLOW_SQLITE_IN_PRODUCTION     -> refuse (ambiguous)
#
# SQLITE_PATH only chooses where the SQLite file lives. It never authorizes
# SQLite in production; ALLOW_SQLITE_IN_PRODUCTION does that explicitly.
_database_url = (os.environ.get('DATABASE_URL') or '').strip()
_allow_sqlite_in_production = _env_bool('ALLOW_SQLITE_IN_PRODUCTION')
_sqlite_path = (os.environ.get('SQLITE_PATH') or '').strip() or (BASE_DIR / 'db.sqlite3')


def _build_databases(debug, database_url, sqlite_path, allow_sqlite_in_production):
    """Return the DATABASES setting for the given deployment inputs.

    Kept as a pure function so the selection rules can be tested without
    touching a real database or re-reading the environment.
    """
    if database_url:
        if allow_sqlite_in_production:
            raise ImproperlyConfigured(
                'DATABASE_URL and ALLOW_SQLITE_IN_PRODUCTION=1 are both set. '
                'That is ambiguous: choose PostgreSQL (unset '
                'ALLOW_SQLITE_IN_PRODUCTION) or SQLite (unset DATABASE_URL). '
            )
        # PostgreSQL via dj-database-url (default persistent connections).
        import dj_database_url
        return {
            'default': dj_database_url.config(
                default=database_url,
                conn_max_age=600,       # persistent connections — reuse instead of reconnect per request
                conn_health_checks=True,
            )
        }

    if not debug and not allow_sqlite_in_production:
        raise ImproperlyConfigured(
            'DATABASE_URL is not set and ALLOW_SQLITE_IN_PRODUCTION is not '
            'enabled. Production (DEBUG=False) refuses to fall back to SQLite '
            'silently. Set DATABASE_URL for PostgreSQL, or set '
            'ALLOW_SQLITE_IN_PRODUCTION=1 to use SQLite deliberately '
            '(SQLITE_PATH chooses the file location).'
        )

    # SQLite: WAL mode is enabled per connection in dashboard AppConfig.ready().
    return {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': sqlite_path,
            'OPTIONS': {
                'timeout': 20,
            },
        }
    }


DATABASES = _build_databases(
    DEBUG, _database_url, _sqlite_path, _allow_sqlite_in_production
)

# ── Password validation ───────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── Internationalisation ──────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True

# ── Static files (CSS, JavaScript, Images) ───────────────────────────────────
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
# STATIC_ROOT is overridable so a container can collect into a mounted
# volume (e.g. /app/runtime/staticfiles) instead of the image filesystem.
STATIC_ROOT = Path(os.environ.get('STATIC_ROOT', str(BASE_DIR / 'staticfiles')))

# ── Cloudinary — persistent media file storage ───────────────────────────────
# On Render the local filesystem is ephemeral: uploaded files are lost on
# every redeploy or dyno restart. Cloudinary provides permanent cloud storage.
# Configure the three env vars below on Render (never commit credentials).
#
# Sign up free at cloudinary.com → Dashboard → copy Cloud Name, API Key,
# API Secret → add them as Render environment variables.
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    'API_KEY':    os.environ.get('CLOUDINARY_API_KEY', ''),
    'API_SECRET': os.environ.get('CLOUDINARY_API_SECRET', ''),
}

# WhiteNoise compressed static files for production.
# Media files use Cloudinary in production (env vars set) or local filesystem
# in development (env vars absent).
_cloudinary_configured = bool(
    os.environ.get('CLOUDINARY_CLOUD_NAME')
    and os.environ.get('CLOUDINARY_API_KEY')
    and os.environ.get('CLOUDINARY_API_SECRET')
)

STORAGES = {
    'default': {
        'BACKEND': (
            'cloudinary_storage.storage.MediaCloudinaryStorage'
            if _cloudinary_configured
            else 'django.core.files.storage.FileSystemStorage'
        ),
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

# ── Media files (uploaded images) ────────────────────────────────────────────
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# ── Auth ──────────────────────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'accounts.CustomUser'
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# ── Brute-force login protection ─────────────────────────────────────────────
# Max failed login attempts (per username / per IP) before a temporary
# lockout, and how long the lockout lasts. Usernames that don't exist are
# counted too, so lockouts never reveal whether an account exists.
# See apps/accounts/bruteforce.py for details.
LOGIN_MAX_ATTEMPTS_PER_USERNAME = 5
LOGIN_MAX_ATTEMPTS_PER_IP = 10
LOGIN_LOCKOUT_MINUTES = 15

# Password-reset request throttling (Phase 5C). Limits are per keyed-email
# hash and per client IP with a shared sliding window; reset emails are
# low-volume operational mail, so the per-address quota is small while the
# per-IP quota tolerates several staff sharing one office address.
# See apps/accounts/reset_throttle.py for details.
RESET_MAX_PER_EMAIL = 3
RESET_MAX_PER_IP = 10
RESET_THROTTLE_MINUTES = 15

# ── Multi-factor authentication (Phase 5D) ────────────────────────────────────
# TOTP for staff accounts. Secrets are Fernet-encrypted at rest; set
# MFA_ENCRYPTION_KEY in production (generate it with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# When DEBUG=False the key is REQUIRED: startup fails fast (system check
# accounts.E001) instead of silently deriving one. Local development and the
# test suite may derive a dedicated key from SECRET_KEY instead; that fallback
# is allowed by MFA_ALLOW_SECRET_KEY_FALLBACK (defaults to DEBUG, i.e. on for
# development and off for production) and is surfaced by system check
# accounts.W001 when it is used outside DEBUG. A SECRET_KEY rotation makes
# fallback-encrypted authenticators unreadable -- recovery codes still work,
# and `manage.py mfa_reset <username>` restores access.
MFA_ENCRYPTION_KEY = os.environ.get('MFA_ENCRYPTION_KEY', '')
MFA_ALLOW_SECRET_KEY_FALLBACK = _env_bool('MFA_ALLOW_SECRET_KEY_FALLBACK', DEBUG)
# How long a successful password confirmation permits enrollment (seconds).
MFA_ENROLL_REAUTH_SECONDS = int(os.environ.get('MFA_ENROLL_REAUTH_SECONDS', '600'))
MFA_MAX_ATTEMPTS_PER_USER = int(os.environ.get('MFA_MAX_ATTEMPTS_PER_USER', '5'))
MFA_MAX_ATTEMPTS_PER_IP = int(os.environ.get('MFA_MAX_ATTEMPTS_PER_IP', '10'))
MFA_LOCKOUT_MINUTES = int(os.environ.get('MFA_LOCKOUT_MINUTES', '15'))
MFA_CHALLENGE_TIMEOUT_SECONDS = int(os.environ.get('MFA_CHALLENGE_TIMEOUT_SECONDS', '300'))
MFA_SETUP_SECRET_TIMEOUT_SECONDS = int(os.environ.get('MFA_SETUP_SECRET_TIMEOUT_SECONDS', '900'))
MFA_SESSION_MAX_AGE_SECONDS = int(os.environ.get('MFA_SESSION_MAX_AGE_SECONDS', str(12 * 60 * 60)))
MFA_RECOVERY_CODE_COUNT = int(os.environ.get('MFA_RECOVERY_CODE_COUNT', '10'))
# Hard-require enrollment before /admin/ access. Off during rollout so the
# existing administrator is not locked out; enable once every admin has enrolled.
MFA_REQUIRE_ADMIN_ENROLLMENT = _env_bool('MFA_REQUIRE_ADMIN_ENROLLMENT', False)

# ── Anonymous customer order rate limiting ────────────────────────────────────
# Maximum number of successful orders an anonymous customer session may place
# within the sliding time window (seconds).  Only successfully created orders
# count toward the limit — failed validation attempts are not penalised.
# Override via environment variables on Render (or any host).
ORDER_RATE_LIMIT = int(os.environ.get('ORDER_RATE_LIMIT', '3'))
ORDER_RATE_WINDOW = int(os.environ.get('ORDER_RATE_WINDOW', '600'))  # 10 minutes

# ── Pagination ────────────────────────────────────────────────────────────────
# Standard page size for all user-facing paginated lists in the system.
# Views should use:  Paginator(queryset, settings.PAGE_SIZE)
PAGE_SIZE = 10

# ── Session ───────────────────────────────────────────────────────────────────
# Absolute maximum session lifetime (hard ceiling). The session also expires
# after SESSION_IDLE_TIMEOUT_MINUTES of inactivity (see the
# SessionIdleTimeoutMiddleware), so a forgotten terminal never stays open
# indefinitely. Both are configurable through the environment for deployment.
SESSION_COOKIE_AGE = int(os.environ.get('SESSION_COOKIE_AGE', str(60 * 60 * 24)))  # 24h
SESSION_IDLE_TIMEOUT_MINUTES = int(
    os.environ.get('SESSION_IDLE_TIMEOUT_MINUTES', '30')
)

# ── Session / CSRF cookie security ────────────────────────────────────────────
# HttpOnly keeps the session cookie out of JavaScript (XSS cannot read it).
# SameSite=Lax blocks cross-site cookie sending on subrequests while keeping
# normal same-site navigation working (e.g. top-level redirects from
# external payment/GCash links still carry the cookie).
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG   # HTTPS-only outside development
# CSRF cookie must stay readable by JavaScript: main.js reads it to attach
# the X-CSRFToken header to fetch() calls, so it is intentionally NOT HttpOnly.
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = not DEBUG

# Sessions survive a browser restart (keeps the staff experience convenient),
# but the idle timeout above still bounds how long an unattended session lives.
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# ── AI / Chatbot ──────────────────────────────────────────────────────────────
# Gemini API key — set as environment variable, NEVER commit to source control.
# On Render: add GEMINI_API_KEY in the environment variables panel.
# For local dev: add GEMINI_API_KEY=<your-key> in your .env file.
# If not set, the chatbot uses deterministic fallback responses only.
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
# Never hard-code credentials here. Configure via environment variables on
# Render (or any host). For local development, console backend prints emails
# to stdout so no SMTP server is required.
#
# Required env vars for production SMTP (e.g. Gmail, SendGrid, Mailgun):
#   EMAIL_HOST          — e.g. smtp.gmail.com
#   EMAIL_PORT          — e.g. 587
#   EMAIL_HOST_USER     — your sending address / API key username
#   EMAIL_HOST_PASSWORD — your SMTP password or API key (keep out of git!)
#   DEFAULT_FROM_EMAIL  — e.g. "Kape De Manubag <noreply@yourdomain.com>"
#   EMAIL_USE_TLS       — "True" for port 587; set "False" for port 465 + SSL

if DEBUG:
    # Development: print emails to the console instead of sending them.
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
else:
    EMAIL_BACKEND = os.environ.get(
        'EMAIL_BACKEND',
        'django.core.mail.backends.smtp.EmailBackend',
    )

EMAIL_HOST          = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT          = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER     = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS       = os.environ.get('EMAIL_USE_TLS', 'True').strip().lower() in {'1', 'true', 'yes'}
EMAIL_USE_SSL       = os.environ.get('EMAIL_USE_SSL', 'False').strip().lower() in {'1', 'true', 'yes'}
EMAIL_TIMEOUT       = int(os.environ.get('EMAIL_TIMEOUT', '10'))  # fail fast instead of hanging
DEFAULT_FROM_EMAIL  = os.environ.get('DEFAULT_FROM_EMAIL', 'Kape De Manubag <noreply@example.com>')

# ── Resend — transactional email via HTTP API ─────────────────────────────────
# Render's free tier blocks all outbound SMTP ports (25, 465, 587), so Django's
# SMTP backend cannot send email there. The Resend SDK uses HTTPS (port 443)
# which is never blocked. Set RESEND_API_KEY in the Render environment variables
# panel. Get your key from https://resend.com → Dashboard → API Keys.
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')

# Django's password-reset token expires after this many seconds (default 3 days).
# Override via env if you want shorter-lived links.
PASSWORD_RESET_TIMEOUT = int(os.environ.get('PASSWORD_RESET_TIMEOUT', str(60 * 60 * 24 * 3)))

# ── Production security headers (ignored when DEBUG=True) ────────────────────
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    # Behind a TLS-terminating reverse proxy the request Django sees is plain
    # HTTP; trust the proxy's X-Forwarded-Proto header so request.is_secure()
    # (and CSRF origin checks) reflect the client's real scheme.
    # SECURE_SSL_REDIRECT is intentionally left off: redirects are the proxy's
    # job, and forcing them here would break health checks over plain HTTP.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# ── Logging ───────────────────────────────────────────────────────────────────
# Development: console only (runserver output looks the same as Django's
# defaults). Production: rotating file handlers, one file per level
# (django_info.log / django_warning.log / django_error.log), plus a quiet
# console (WARNING and above) so nothing is duplicated on stdout.
LOG_DIR = BASE_DIR / 'logs'

# When LOG_TO_STDOUT is enabled, logs go to stdout/stderr and no log directory
# is created and no rotating file handlers are opened. This is the supported
# mode for read-only container filesystems (Docker production).
LOG_TO_STDOUT = _env_bool('LOG_TO_STDOUT')


class _LevelFilter(logging.Filter):
    """Keep only records whose level falls within ``[min_level, max_level]``.

    Used to split INFO and WARNING into their own files so messages are
    never duplicated across log files.
    """

    def __init__(self, min_level=logging.NOTSET, max_level=logging.CRITICAL):
        super().__init__()
        self.min_level = min_level
        self.max_level = max_level

    def filter(self, record):
        return self.min_level <= record.levelno <= self.max_level


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name}: {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        'server': {
            '()': 'django.utils.log.ServerFormatter',
            'format': '[{server_time}] {message}',
            'style': '{',
        },
    },
    'filters': {
        'info_only': {'()': _LevelFilter, 'min_level': logging.INFO, 'max_level': logging.INFO},
        'warning_only': {'()': _LevelFilter, 'min_level': logging.WARNING, 'max_level': logging.WARNING},
    },
    'handlers': {},
    'loggers': {},
}

if LOG_TO_STDOUT:
    # ── Stdout mode (containers, read-only filesystems) ────────────────────
    # One handler, no files, no duplicate handlers. Levels match the
    # production file setup: framework INFO, request/security WARNING,
    # root WARNING.
    LOGGING['handlers'] = {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    }
    LOGGING['loggers'] = {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.server': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    }
    LOGGING['root'] = {
        'handlers': ['console'],
        'level': 'WARNING',
    }
elif DEBUG:
    # ── Development ────────────────────────────────────────────────────────
    # Everything goes to the console. django.server keeps the familiar
    # per-request lines; django.request surfaces 5xx + unhandled exceptions
    # and django.security surfaces suspicious requests.
    LOGGING['handlers'] = {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
        'server_console': {'class': 'logging.StreamHandler', 'formatter': 'server'},
    }
    LOGGING['loggers'] = {
        'django': {'handlers': ['console'], 'level': 'INFO'},
        'django.server': {'handlers': ['server_console'], 'level': 'INFO', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        'django.security': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    }
else:
    # ── Production ─────────────────────────────────────────────────────────
    # Rotating files (5 MB each, 3 backups) keep disk usage bounded. Levels
    # are split exactly: django_info.log holds INFO only, django_warning.log
    # WARNING only, django_error.log ERROR+CRITICAL (with tracebacks). The
    # console stays at WARNING (PythonAnywhere already captures stdout in
    # its server log). DEBUG-level SQL and per-request access lines are
    # intentionally not logged anywhere to avoid excessive logging.
    # Note: file rotation assumes a single WSGI process (PythonAnywhere);
    # multi-worker setups (e.g. gunicorn) would need a shared-safe handler.
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    _rotating_file = {
        'class': 'logging.handlers.RotatingFileHandler',
        'maxBytes': 5 * 1024 * 1024,
        'backupCount': 3,
        'formatter': 'verbose',
        'encoding': 'utf-8',
    }

    LOGGING['handlers'] = {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose', 'level': 'WARNING'},
        'info_file': {
            'filename': str(LOG_DIR / 'django_info.log'),
            'level': 'INFO',
            'filters': ['info_only'],
            **_rotating_file,
        },
        'warning_file': {
            'filename': str(LOG_DIR / 'django_warning.log'),
            'level': 'WARNING',
            'filters': ['warning_only'],
            **_rotating_file,
        },
        'error_file': {
            'filename': str(LOG_DIR / 'django_error.log'),
            'level': 'ERROR',
            **_rotating_file,
        },
    }
    LOGGING['loggers'] = {
        # Framework + app INFO -> django_info.log; WARNING/ERROR also land in
        # their own files via the same handlers.
        'django': {
            'handlers': ['console', 'info_file', 'warning_file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        # Failed requests: 4xx -> django_warning.log, 5xx (with traceback)
        # -> django_error.log.
        'django.request': {
            'handlers': ['console', 'warning_file', 'error_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['console', 'warning_file', 'error_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        # runserver-only logger (never used under WSGI). Its INFO request
        # lines are dropped so info.log stays clean; only failures are kept.
        'django.server': {
            'handlers': ['console', 'error_file'],
            'level': 'ERROR',
            'propagate': False,
        },
    }
    # Catch-all for third-party / non-Django loggers (WARNING and above).
    LOGGING['root'] = {
        'handlers': ['console', 'warning_file', 'error_file'],
        'level': 'WARNING',
    }
