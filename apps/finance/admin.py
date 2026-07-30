from django.contrib import admin
from .models import DailyFinance


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
