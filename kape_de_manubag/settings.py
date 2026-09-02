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
load_dotenv(BASE_DIR / '.env')


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
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

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
STATIC_ROOT = BASE_DIR / 'staticfiles'

# WhiteNoise compressed static files for production
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
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

# ── Production security headers (ignored when DEBUG=True) ────────────────────
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'


# ── Logging ───────────────────────────────────────────────────────────────────
# Development: console only (runserver output looks the same as Django's
# defaults). Production: rotating file handlers, one file per level
# (django_info.log / django_warning.log / django_error.log), plus a quiet
# console (WARNING and above) so nothing is duplicated on stdout.
LOG_DIR = BASE_DIR / 'logs'


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

if DEBUG:
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
