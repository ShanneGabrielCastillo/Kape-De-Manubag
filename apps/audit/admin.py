from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only review interface for the audit trail.

    Entries can be browsed, filtered and searched, but never created,
    edited or deleted from the admin site — the trail is append-only.
    """
    list_display = ['created_at', 'user', 'action', 'object_type', 'object_id', 'object_repr']
    list_filter = ['action', 'object_type']
    search_fields = ['action', 'object_type', 'object_id', 'object_repr', 'user__username']
    date_hierarchy = 'created_at'
    list_select_related = ['user']
    readonly_fields = ['created_at', 'user', 'action', 'object_type',
                       'object_id', 'object_repr', 'detail']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
