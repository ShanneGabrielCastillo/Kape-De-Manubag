from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from functools import wraps


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            # Unauthenticated: send to login with a next= redirect so the
            # user lands back on the intended page after signing in.
            return redirect_to_login(request.get_full_path())
        if request.user.is_admin_user:
            return view_func(request, *args, **kwargs)
        messages.error(request, 'Access denied. Admin privileges required.')
        return redirect('dashboard:index')
    return wrapper


def cashier_or_admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            # Unauthenticated: send to login with a next= redirect.
            # Previously this fell through to the role check and redirected
            # to menu:index, which could be used to probe endpoint existence.
            return redirect_to_login(request.get_full_path())
        if request.user.is_admin_user or request.user.is_cashier:
            return view_func(request, *args, **kwargs)
        messages.error(request, 'Access denied. Staff privileges required.')
        return redirect('menu:index')
    return wrapper
