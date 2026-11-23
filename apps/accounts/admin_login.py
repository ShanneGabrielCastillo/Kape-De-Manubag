"""Django Admin login wrapper with the shared brute-force lockout.

The custom ``/accounts/login/`` view calls the helpers in ``bruteforce``
directly.  Django's standard Admin login bypasses that view, so this wrapper
applies the same pre-authentication lockout check, failure recording, and
success clearing around ``admin.site.login``.

Authentication itself is still handled entirely by Django's ``AdminSite`` /
``LoginView``; this module only adds the account-security guard.
"""
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import NON_FIELD_ERRORS
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from . import bruteforce, mfa


def _locked_response(request, remaining, extra_context=None):
    """Render the standard Admin login page with a lockout error."""
    request.current_app = admin.site.name

    # The form is intentionally unbound: no credentials are submitted to the
    # authentication backend while the lockout is active. ``full_clean()`` on
    # an unbound form only initialises the error dict (it returns before
    # field cleaning or the authenticating ``clean()`` runs). The supported
    # ``add_error()`` cannot be used here — its tail guard reads
    # ``self.cleaned_data``, which an unbound form never creates — so the
    # non-field error is seeded directly, the standard workaround the admin
    # template renders via ``form.non_field_errors``.
    form = AdminAuthenticationForm(request)
    form.full_clean()
    form._errors[NON_FIELD_ERRORS] = form.error_class([
        bruteforce.lockout_message(remaining),
    ])

    next_url = (
        request.POST.get('next')
        or request.GET.get('next')
        or reverse('admin:index', current_app=admin.site.name)
    )
    context = {
        **admin.site.each_context(request),
        'title': 'Log in',
        'subtitle': None,
        'app_path': request.get_full_path(),
        'username': request.user.get_username(),
        'next': next_url,
        'form': form,
    }
    context.update(extra_context or {})
    return render(
        request,
        admin.site.login_template or 'admin/login.html',
        context,
        status=429,
    )


@never_cache
def admin_login(request, extra_context=None):
    """Drop-in replacement for ``admin.site.login`` with brute-force guarding."""
    if request.method == 'POST':
        remaining = bruteforce.get_lockout_remaining(request)
        if remaining is not None:
            return _locked_response(request, remaining, extra_context)

        # Phase 5D: if these credentials belong to an MFA-enrolled admin, stop
        # here -- no session is created until the TOTP/recovery step passes.
        # For anyone else this pre-validation is discarded and authentication
        # continues through ``admin.site.login`` below (unchanged Phase 5B
        # behaviour, including failure recording and 5B test expectations).
        form = AdminAuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if mfa.requires_admin_mfa(user) and mfa.has_confirmed_device(user):
                next_url = mfa.safe_next_url(
                    request,
                    request.POST.get('next') or request.GET.get('next'),
                    default=reverse('admin:index', current_app=admin.site.name),
                    prefix='/admin/',
                )
                mfa.start_pending(request, user, next_url=next_url, mode='admin')
                return redirect('accounts:mfa_challenge')

    # Delegate the actual authentication, form rendering, redirect
    # validation (including ``next``), and staff/superuser checks to Django.
    response = admin.site.login(request, extra_context)

    if request.method == 'POST':
        # Classify from the delegated view's outcome, not from
        # ``request.user``: ``LoginView`` answers a successful authentication
        # with a redirect and any failure (wrong credentials, non-staff
        # account) by re-rendering the form. The ``request.user`` heuristic
        # would misclassify an already-authenticated session posting bad
        # credentials for a different username as a success and wrongly clear
        # that account's counters.
        if response.status_code in (301, 302, 303, 307, 308):
            bruteforce.clear_failed_attempts(request)
        else:
            bruteforce.register_failed_attempt(request)

    return response
