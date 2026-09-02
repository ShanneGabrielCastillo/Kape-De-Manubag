"""
Lightweight health-check endpoint.

Public (unauthenticated) JSON endpoint for uptime monitors, load balancers
and operators to confirm the app is alive, can reach the database and has
its static assets in place.

Deliberately minimal:
* database connectivity is one trivial ``SELECT 1`` that reuses Django's
  cached connection (no reconnect overhead on repeated checks),
* static availability is a cheap filesystem scan (no finders / no HTTP),
* no secrets, hostnames or error details are ever returned to the caller;
  failure details go to the server log only.
"""
import logging
import os
from datetime import datetime, timezone

from django.db import connection
from django.http import JsonResponse

from . import __version__

logger = logging.getLogger(__name__)


def _database_available():
    """Return True when a trivial query executes. Never raises."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        return True
    except Exception:
        # Health monitors poll frequently, so log a short message without a
        # traceback to avoid spamming the error logs during an outage.
        logger.warning('Health check: database connectivity check failed')
        return False


def _static_available():
    """Return True when static assets are present on disk. Never raises.

    Checks the collected ``STATIC_ROOT`` (production / WhiteNoise). The
    source ``STATICFILES_DIRS`` are consulted only in development
    (``DEBUG=True``), where ``collectstatic`` may not have been run yet — in
    production a missing ``STATIC_ROOT`` must NOT be masked by the
    always-present source directory.
    """
    from django.conf import settings

    candidates = []
    if getattr(settings, 'STATIC_ROOT', None):
        candidates.append(str(settings.STATIC_ROOT))
    if getattr(settings, 'DEBUG', False):
        candidates.extend(
            str(d) for d in getattr(settings, 'STATICFILES_DIRS', []) if d
        )

    for directory in candidates:
        try:
            with os.scandir(directory) as entries:
                if any(entries):
                    return True
        except OSError:
            continue
    return False


def health_check(request):
    """Return a JSON summary of application health. No authentication."""
    db_ok = _database_available()
    static_ok = _static_available()
    healthy = db_ok and static_ok

    payload = {
        'status': 'ok' if healthy else 'degraded',
        'version': __version__,
        'database': 'ok' if db_ok else 'unavailable',
        'static': 'ok' if static_ok else 'unavailable',
        'server_time': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    response = JsonResponse(payload, status=200 if healthy else 503)
    # Health checks must never be served from cache.
    response['Cache-Control'] = 'no-store'
    return response
