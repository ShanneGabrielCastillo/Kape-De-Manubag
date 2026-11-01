"""
Test-only Django settings for Kape De Manubag.

Inherits every production setting from kape_de_manubag.settings and
overrides only what automated tests require.

WHY THIS FILE EXISTS
--------------------
The production settings unconditionally use
whitenoise.storage.CompressedManifestStaticFilesStorage, which reads
staticfiles/staticfiles.json at startup and raises ValueError for any
{% static %} template tag whose file is not listed in that manifest.

staticfiles/staticfiles.json is gitignored and never committed, so it does
not exist in a fresh CI checkout. Every test that renders a template
referencing {% static 'images/favicon.svg' %} (or any other static asset)
therefore fails with:

    ValueError: Missing staticfiles manifest entry for 'images/favicon.svg'

This override replaces the manifest-backed storage with the standard
StaticFilesStorage, which resolves {% static %} tags to simple URL paths
without reading any manifest. No collectstatic step is needed in CI.

WHAT IS PRESERVED
-----------------
- All authentication, authorization, and permission logic.
- All database settings (SQLite in development, PostgreSQL via DATABASE_URL).
- All application business logic, models, views, and services.
- All INSTALLED_APPS, MIDDLEWARE, TEMPLATES, and URL routing.
- All brute-force protection, rate limiting, and session settings.
- All order, inventory, finance, and reporting behavior.
- Production WhiteNoise manifest storage is untouched.

WHAT IS CHANGED (test environment only)
----------------------------------------
- STORAGES['staticfiles']['BACKEND'] uses StaticFilesStorage instead of
  CompressedManifestStaticFilesStorage.  This is correct for test runs:
  tests verify application behavior, not static-file fingerprinting.
"""

from kape_de_manubag.settings import *  # noqa: F401, F403

# Override only the staticfiles backend.
# StaticFilesStorage resolves {% static %} to simple URL paths without
# requiring a pre-built manifest file.
STORAGES = {
    **STORAGES,  # noqa: F405 — imported via * above
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}
