from django.contrib import admin
from django.core.exceptions import ValidationError
from django.contrib import messages
from .models import DailyFinance, FINANCE_DELETE_ERROR


@admin.register(DailyFinance)
class DailyFinanceAdmin(admin.ModelAdmin):
    list_display = [
        'date', 'previous_coh', 'expenses',
        'ending_coh_display', 'prepared_by', 'created_at',
    ]
    list_filter = ['date']
    search_fields = ['date']
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['-date']

    def ending_coh_display(self, obj):
        return f'₱{obj.ending_coh:.2f}'
    ending_coh_display.short_description = 'Ending COH'

    # ── Delete guards ─────────────────────────────────────────────────────────
    # The pre_delete signal already blocks ORM-level deletes (including the
    # queryset delete that backs the admin "delete selected" action).  These
    # two overrides provide a friendlier admin experience: instead of an
    # unhandled exception the admin shows a clear error message and stays on
    # the page.

    def delete_model(self, request, obj):
        """Block single-record delete from the admin change page."""
        self.message_user(
            request,
            FINANCE_DELETE_ERROR,
            level=messages.ERROR,
        )

    def delete_queryset(self, request, queryset):
        """Block bulk 'delete selected' action from the admin list page."""
        self.message_user(
            request,
            FINANCE_DELETE_ERROR,
            level=messages.ERROR,
        )
