"""
Audit trail views — Admin-only Activity Log page.

The Activity Log provides administrators with a chronological, read-only
view of significant system actions: WHO did WHAT to WHICH record and WHEN.

Access is enforced server-side by @admin_required. Cashiers who navigate
directly to /audit/ are redirected to the dashboard — hiding the sidebar
link is not sufficient on its own.
"""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from apps.accounts.decorators import admin_required
from apps.accounts.models import CustomUser

from .models import AuditLog

# ── Category → action string mapping ─────────────────────────────────────────
# Used to translate the "category" filter param into a set of action strings
# for the queryset filter. Keep in sync with the action strings used in
# log_action() calls across the project.

ACTION_CATEGORIES = {
    'authentication': [
        'account.password_change',
        'account.password_reset',
    ],
    'orders': [
        'order.payment',
        'order.cancel',
        'order.status_changed',
    ],
    'products': [
        'product.create',
        'product.update',
        'product.deactivate',
        'product.reactivate',
        'product.availability',
    ],
    'categories': [
        'category.create',
        'category.update',
        'category.deactivate',
        'category.reactivate',
        'category.delete',
    ],
    'staff': [
        'staff.create',
        'staff.deactivate',
        'staff.activate',
    ],
    'finance': [
        'finance.create',
        'finance.update',
    ],
    'inventory': [
        'inventory.restock',
    ],
    'system': [
        'settings.update',
    ],
}

# Reverse map: action string → category label.
# Used per-row so the template renders the right badge colour without per-row if/else logic.
ACTION_TO_CATEGORY = {
    action: cat
    for cat, actions in ACTION_CATEGORIES.items()
    for action in actions
}


@login_required
@admin_required
def activity_log(request):
    """Admin-only Activity Log page.

    Read-only view of the AuditLog table. Supports:
    - Full-text search across user, action, object_repr, and detail
    - Category filter (maps to sets of action strings)
    - User filter (by staff user pk)
    - Date range filter (date_from / date_to)
    - Pagination: 50 entries per page, newest first

    Access is enforced server-side by @admin_required. A cashier who
    navigates directly to /audit/ is redirected to the dashboard with
    an "Access denied" message — the sidebar link is hidden too, but
    the decorator is the real gate.
    """
    qs = AuditLog.objects.select_related('user').order_by('-created_at')

    q = request.GET.get('q', '').strip()
    category = request.GET.get('category', '').strip()
    user_id = request.GET.get('user', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    if q:
        qs = qs.filter(
            Q(user__username__icontains=q)
            | Q(action__icontains=q)
            | Q(object_repr__icontains=q)
            | Q(detail__icontains=q)
        )
    if category and category in ACTION_CATEGORIES:
        qs = qs.filter(action__in=ACTION_CATEGORIES[category])
    if user_id:
        try:
            qs = qs.filter(user_id=int(user_id))
        except (ValueError, TypeError):
            pass
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page', 1)
    logs_page = paginator.get_page(page_number)

    # Attach a human-readable category label to each log entry so the
    # template renders the right badge colour without per-row if/else logic.
    for entry in logs_page:
        entry.category_label = ACTION_TO_CATEGORY.get(entry.action, 'system')

    # Staff user list for the "Filter by user" dropdown.
    # Includes deactivated staff so historical entries remain filterable.
    staff_users = (
        CustomUser.objects
        .filter(role__in=['admin', 'cashier'])
        .order_by('username')
    )

    return render(request, 'audit/activity_log.html', {
        'logs': logs_page,
        'q': q,
        'category': category,
        'user_id': user_id,
        'date_from': date_from,
        'date_to': date_to,
        'categories': list(ACTION_CATEGORIES.keys()),
        'staff_users': staff_users,
        'total_count': paginator.count,
    })
