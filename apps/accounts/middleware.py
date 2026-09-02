"""Request middleware for the accounts app.

Two middlewares live here:

* ``ActiveUserMiddleware`` -- logs out sessions whose account has been
  deactivated (or deleted), so deactivation takes effect immediately.
* ``SessionIdleTimeoutMiddleware`` -- expires authenticated sessions after a
  configurable period of inactivity.
"""

import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import SESSION_KEY, logout
from django.shortcuts import redirect


class ActiveUserMiddleware:
    """Log out any session whose account is deactivated (or deleted).

    If the session stores a user id but ``request.user`` is anonymous, the
    account is either deactivated or no longer exists -- in both cases the
    session is invalid and is flushed. Anonymous users and the login page
    itself pass straight through (no redirect loop). No extra database query
    is performed: Django already resolved ``request.user`` for us.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.path != '/accounts/login/'
            and not request.user.is_authenticated
            and request.session.get(SESSION_KEY) is not None
        ):
            # logout() flushes the session (removing _auth_user_id); the
            # notice is queued into the fresh session so it is shown on the
            # login page after the redirect.
            logout(request)
            messages.error(
                request,
                'Your account has been deactivated. '
                'Please contact an administrator.',
            )
            return redirect('accounts:login')
        return self.get_response(request)


# Key under which the idle timeout middleware stores the last-activity
# timestamp (epoch seconds, so it round-trips safely through Django's JSON
# session serializer).
LAST_ACTIVITY_KEY = 'last_activity'


class SessionIdleTimeoutMiddleware:
    """Expire authenticated sessions after a period of inactivity.

    On every request from an authenticated user, the time since the last
    recorded activity is checked against ``SESSION_IDLE_TIMEOUT_MINUTES``. If
    the session has been idle longer than that, the user is logged out
    (session flushed) and sent to the login page with a friendly notice.
    Otherwise the activity timestamp is refreshed so the timer slides.

    The timestamp is stored as epoch seconds (a float) because Django's JSON
    session serializer converts ``datetime`` objects back into strings on
    read. To avoid a session-store write on *every* request, the timestamp is
    only refreshed when at least ``SESSION_IDLE_ACTIVITY_STEP_SECONDS`` have
    elapsed since the last recorded one.

    Settings are read at request time (not import time) so tests can use
    ``override_settings``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.path != '/accounts/login/':
            response = self._enforce_idle_timeout(request)
            if response is not None:
                return response
        return self.get_response(request)

    def _enforce_idle_timeout(self, request):
        timeout_minutes = getattr(settings, 'SESSION_IDLE_TIMEOUT_MINUTES', 30)
        if not timeout_minutes:
            return None  # disabled (0 / None)

        now = time.time()
        last = request.session.get(LAST_ACTIVITY_KEY)

        # Guard against non-numeric values (corrupt/tampered session data)
        # so a bad timestamp can never turn into a 500 on the whole request.
        if last is not None and isinstance(last, (int, float)) \
                and now - last > timeout_minutes * 60:
            # logout() flushes the session; the notice is queued afterwards so
            # it lands in the fresh session and is shown on the login page.
            logout(request)
            messages.error(
                request,
                'Your session has expired due to inactivity. '
                'Please log in again.',
            )
            request.session[LAST_ACTIVITY_KEY] = now  # fresh session timestamp
            return redirect('accounts:login')

        # Slide the timer, but only write to the session store at most once
        # per step (avoids a DB/session write on every single request).
        step = getattr(settings, 'SESSION_IDLE_ACTIVITY_STEP_SECONDS', 60)
        if last is None or not isinstance(last, (int, float)) \
                or now - last >= step:
            request.session[LAST_ACTIVITY_KEY] = now
        return None
