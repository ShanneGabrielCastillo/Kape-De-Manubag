"""
PostgreSQL-only test settings for the Phase 1.1 cutover concurrency suite.

WHY THIS FILE EXISTS
--------------------
``kape_de_manubag/test_settings.py`` inherits production settings and leaves
the database configuration exactly as production builds it:

* ``DATABASE_URL`` set   -> PostgreSQL (parsed by dj-database-url);
* ``DATABASE_URL`` unset -> SQLite at ``BASE_DIR / 'db.sqlite3'``.

The Phase 1.1 concurrency tests are only meaningful against PostgreSQL, where
``SELECT ... FOR UPDATE`` actually takes row locks. On SQLite they skip. That
default is fine for local development and for the SQLite regression job, but
it is dangerous for CI: a misconfigured workflow could run the "PostgreSQL"
job against SQLite, collect five skips, and report success.

This module removes that possibility. It imports the existing test settings
(so every non-database setting — staticfiles storage, apps, middleware, auth,
templates, business logic — stays identical) and then REPLACES the ``default``
database with an explicit PostgreSQL configuration read from environment
variables.

GUARANTEES
----------
* ENGINE is always ``django.db.backends.postgresql``.
* Connection details come from the environment; no credentials are hardcoded.
* All five variables are required. If any is missing or empty, importing this
  module raises ``ImproperlyConfigured`` immediately — there is no silent
  fallback to SQLite.
* A database name that looks like a SQLite file is rejected.
* The development SQLite configuration in ``settings.py`` is untouched; this
  override exists only when this settings module is selected explicitly.

ENVIRONMENT VARIABLES
---------------------
``POSTGRES_DB``        database name (dedicated CI database)
``POSTGRES_USER``      role used by the test run
``POSTGRES_PASSWORD``  password for that role
``POSTGRES_HOST``      server host (the GitHub Actions service container)
``POSTGRES_PORT``      server port (5432 for the CI service container)
``POSTGRES_TEST_DB``   optional name for the test database Django creates;
                       defaults to ``test_<POSTGRES_DB>``

USAGE
-----
    python manage.py test apps.menu.tests_phase11_pg \
        --settings=kape_de_manubag.postgres_test_settings --verbosity=2
"""

import os

from django.core.exceptions import ImproperlyConfigured

from kape_de_manubag.test_settings import *  # noqa: F401, F403

# ── Fixed backend ─────────────────────────────────────────────────────────────
POSTGRES_BACKEND = 'django.db.backends.postgresql'

_REQUIRED_ENV_VARS = (
    'POSTGRES_DB',
    'POSTGRES_USER',
    'POSTGRES_PASSWORD',
    'POSTGRES_HOST',
    'POSTGRES_PORT',
)

# ── Fail fast when the environment is incomplete ──────────────────────────────
_missing = [
    name for name in _REQUIRED_ENV_VARS
    if not os.environ.get(name, '').strip()
]
if _missing:
    raise ImproperlyConfigured(
        'kape_de_manubag.postgres_test_settings requires a PostgreSQL '
        'environment; missing or empty variable(s): '
        + ', '.join(_missing)
        + '. This module never falls back to SQLite. Set all of '
        + ', '.join(_REQUIRED_ENV_VARS)
        + ' before importing it.'
    )

_POSTGRES_DB = os.environ['POSTGRES_DB'].strip()

# ── Refuse anything that looks like the development SQLite file ───────────────
if (
    _POSTGRES_DB.lower().endswith('.sqlite3')
    or 'sqlite' in _POSTGRES_DB.lower()
):
    raise ImproperlyConfigured(
        f'POSTGRES_DB={_POSTGRES_DB!r} looks like a SQLite database. '
        'kape_de_manubag.postgres_test_settings must point at a real '
        'PostgreSQL database, never db.sqlite3.'
    )

_TEST_DB_NAME = (
    os.environ.get('POSTGRES_TEST_DB', '').strip()
    or f'test_{_POSTGRES_DB}'
)

# ── Override ONLY the database configuration ──────────────────────────────────
DATABASES = {
    'default': {
        'ENGINE': POSTGRES_BACKEND,
        'NAME': _POSTGRES_DB,
        'USER': os.environ['POSTGRES_USER'].strip(),
        'PASSWORD': os.environ['POSTGRES_PASSWORD'],
        'HOST': os.environ['POSTGRES_HOST'].strip(),
        'PORT': os.environ['POSTGRES_PORT'].strip(),
        # No persistent connections: the concurrency tests open and close their
        # own connections from worker threads.
        'CONN_MAX_AGE': 0,
        'TEST': {
            'NAME': _TEST_DB_NAME,
        },
    },
}

# ── Belt-and-braces sanity checks (import-time, not runtime) ──────────────────
if DATABASES['default']['ENGINE'] != POSTGRES_BACKEND:
    raise ImproperlyConfigured(
        'kape_de_manubag.postgres_test_settings must use '
        f'{POSTGRES_BACKEND}; got {DATABASES["default"]["ENGINE"]!r}.'
    )

if str(DATABASES['default']['NAME']).lower().endswith('.sqlite3'):
    raise ImproperlyConfigured(
        'kape_de_manubag.postgres_test_settings resolved to a SQLite file '
        f'({DATABASES["default"]["NAME"]!r}). Refusing to continue.'
    )
