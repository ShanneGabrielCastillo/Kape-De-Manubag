from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.db.models.signals import pre_delete
from django.dispatch import receiver
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

    def _cash_orders_qs(self):
        """Return the base queryset for completed cash orders on this date.

        Centralises the filter so get_cash_sales() and get_cash_order_count()
        cannot silently diverge.  Both methods build on this queryset rather
        than each repeating the same four-field filter.

        Note: calling multiple properties that depend on cash_sales in
        sequence (e.g. running_total then ending_coh) will execute this query
        twice because each property evaluates independently.  In view code,
        prefer the _get_cash_sales_for_date() helper in views.py, which
        fetches both the sum and count in a single aggregate query.
        """
        from apps.orders.models import Order
        return Order.objects.filter(
            created_at__date=self.date,
            is_paid=True,
            payment_method='cash',
            status='completed',
        )

    def get_cash_sales(self):
        """
        Sum of all completed, cash-paid orders for this date.

        Queries the Order table on every call — never stored.
        Use the ``cash_sales`` property when you only need the total.
        For the total *and* count in one round-trip, call the view-level
        ``_get_cash_sales_for_date()`` helper instead.
        """
        result = self._cash_orders_qs().aggregate(total=Sum('total'))
        return result['total'] or Decimal('0.00')

    def get_cash_order_count(self):
        """Count of completed cash orders for this date.

        Convenience method used by the Django admin display and tests.
        Production views obtain the count as the second return value of
        ``_get_cash_sales_for_date()`` (a single aggregate that returns both
        sum and count without a second query).
        """
        return self._cash_orders_qs().count()

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

    def delete(self, *args, **kwargs):
        # Finance records are permanent financial audit documents.
        # Hard-deleting a record would corrupt the COH carry-forward chain
        # and permanently destroy stored values (expenses, previous_coh,
        # deductions) that cannot be recomputed.  Corrections must be made
        # by editing the record, not deleting it.
        raise ValidationError(FINANCE_DELETE_ERROR)

    class Meta:
        ordering = ['-date']
        verbose_name = 'Daily Finance Record'
        verbose_name_plural = 'Daily Finance Records'


FINANCE_DELETE_ERROR = (
    'Finance records cannot be deleted from the database. '
    'Edit the record to correct any mistakes — deleting would corrupt '
    'the Cash on Hand carry-forward chain and permanently destroy '
    'stored financial values that cannot be recomputed.'
)


@receiver(pre_delete, sender=DailyFinance)
def _block_finance_hard_delete(sender, instance, **kwargs):
    """Block *any* hard delete of a DailyFinance row.

    Covers:
    - ``record.delete()`` instance calls (also blocked by the method above)
    - ``DailyFinance.objects.filter(...).delete()`` bulk queryset deletes,
      which bypass the instance method entirely
    - Django admin "delete selected" list action
    - ``DailyFinanceAdmin.delete_model()`` (blocked separately in admin.py
      if an admin class exists)

    Modelled on the identical guards for Order and Product in this codebase.
    Raising ``ValidationError`` inside a ``transaction.atomic()`` block marks
    the connection for rollback; callers that catch and continue querying must
    use a savepoint.
    """
    raise ValidationError(FINANCE_DELETE_ERROR)
