from django.db import models
from django.conf import settings
from django.db.models import Sum
from decimal import Decimal


class DailyFinance(models.Model):
    """
    Daily cash reconciliation record.
    One record per calendar day.
    Stored fields: inputs that cannot be recomputed.
    Computed properties: values derived from stored fields + Order data.
    """

    date = models.DateField(
        unique=True,
        help_text="Date of this cash reconciliation."
    )
    previous_coh = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Opening cash on hand for this day."
    )
    previous_coh_is_manual = models.BooleanField(
        default=False,
        help_text=(
            "True if previous COH was manually entered rather than "
            "auto-populated from yesterday's record."
        )
    )

    # Deduction fields — all stored, all editable
    expenses = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    expenses_notes = models.TextField(
        blank=True,
        help_text="Description of expenses paid today."
    )
    gcash_payments = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Total GCash order amounts (received digitally, not in cash drawer)."
    )
    coins = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Coins set aside separately."
    )
    cash_advance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Cash advances given to staff today."
    )
    floating_cash = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Change float reserved for tomorrow."
    )

    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='finance_records'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def get_cash_sales(self):
        """
        Sum of all completed, cash-paid orders for this date.
        Queries Order table at call time. Never stored —
        always reflects real order data.
        """
        from apps.orders.models import Order
        result = Order.objects.filter(
            created_at__date=self.date,
            is_paid=True,
            payment_method='cash',
            status='completed',
        ).aggregate(total=Sum('total'))
        return result['total'] or Decimal('0.00')

    def get_cash_order_count(self):
        """Count of completed cash orders for this date."""
        from apps.orders.models import Order
        return Order.objects.filter(
            created_at__date=self.date,
            is_paid=True,
            payment_method='cash',
            status='completed',
        ).count()

    @property
    def cash_sales(self):
        return self.get_cash_sales()

    @property
    def running_total(self):
        return self.previous_coh + self.cash_sales

    @property
    def total_deductions(self):
        return (
            self.expenses +
            self.gcash_payments +
            self.coins +
            self.cash_advance +
            self.floating_cash
        )

    @property
    def ending_coh(self):
        return self.running_total - self.total_deductions

    def __str__(self):
        return f"Finance {self.date} — Ending COH: ₱{self.ending_coh:.2f}"

    class Meta:
        ordering = ['-date']
        verbose_name = 'Daily Finance Record'
        verbose_name_plural = 'Daily Finance Records'
