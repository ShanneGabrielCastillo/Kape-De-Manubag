from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.db.models import Q
from .models import CustomUser, FailedLoginAttempt, LAST_ADMIN_ERROR


@admin.register(FailedLoginAttempt)
class FailedLoginAttemptAdmin(admin.ModelAdmin):
    list_display = ['scope', 'value', 'attempts', 'last_attempt_at']
    list_filter = ['scope']
    search_fields = ['value']
    readonly_fields = ['scope', 'value', 'attempts', 'last_attempt_at']
    date_hierarchy = 'last_attempt_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ['username', 'email', 'first_name', 'last_name', 'role',
                    'is_active', 'deactivated_at']
    list_filter = ['role', 'is_active', 'is_staff']
    actions = ['deactivate_users', 'activate_users']
    # deactivated_at is set programmatically by deactivate()/activate(); it is
    # shown read-only so admins cannot backdate or clear it by hand.
    readonly_fields = ['deactivated_at']

    # Permanent deletion is disabled for user accounts (see
    # CustomUser.delete and the pre_delete signal). Without this override the
    # admin's delete button / bulk delete would hard-delete users and orphan
    # their historical records, so it is removed from the interface entirely.
    def has_delete_permission(self, request, obj=None):
        return False

    def delete_queryset(self, request, queryset):
        # Never reached through the UI (has_delete_permission is False), but
        # kept as a guard so a programmatic bulk delete can never hard-delete
        # users either -- it soft-deactivates them instead.
        self.deactivate_users(request, queryset)

    @admin.action(description='Deactivate selected users')
    def deactivate_users(self, request, queryset):
        # Block when the selection includes the last remaining active
        # administrator (matching the model-level guard).
        admin_q = Q(role='admin') | Q(is_superuser=True)
        active_admins = queryset.filter(admin_q).filter(is_active=True)
        if active_admins.exists():
            remaining = CustomUser.objects.filter(admin_q).filter(
                is_active=True,
            ).exclude(pk__in=queryset.values_list('pk', flat=True))
            if not remaining.exists():
                self.message_user(request, LAST_ADMIN_ERROR, level=messages.ERROR)
                return
        for user in queryset.filter(is_active=True):
            user.deactivate()
        self.message_user(request, 'Selected users have been deactivated.')

    @admin.action(description='Activate selected users')
    def activate_users(self, request, queryset):
        for user in queryset.filter(is_active=False):
            user.activate()
        self.message_user(request, 'Selected users have been activated.')
    fieldsets = UserAdmin.fieldsets + (
        ('Additional Info', {'fields': ('role', 'phone', 'profile_image')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Additional Info', {'fields': ('role', 'phone', 'first_name', 'last_name', 'email')}),
    )
