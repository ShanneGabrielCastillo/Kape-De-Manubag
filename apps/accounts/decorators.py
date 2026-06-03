from django.shortcuts import redirect
from django.contrib import messages
from functools import wraps


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated and request.user.is_admin_user:
            return view_func(request, *args, **kwargs)
        messages.error(request, 'Access denied. Admin privileges required.')
        return redirect('dashboard:index')
    return wrapper


def cashier_or_admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated and (request.user.is_admin_user or request.user.is_cashier):
            return view_func(request, *args, **kwargs)
        messages.error(request, 'Access denied. Staff privileges required.')
        return redirect('menu:index')
    return wrapper
