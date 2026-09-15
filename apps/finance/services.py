"""
Finance service functions.

fill_missing_finance_records() is the canonical implementation of the
auto-fill logic. It is called:

  1. From finance_index (views.py) on every page load — so missed days are
     filled automatically the moment anyone opens the finance page.
  2. From the fill_missing_finance management command — for manual backfills
     or seeding historical data from the CLI.

Auto-filled records have:
  • previous_coh    — carried from the most recent prior record's ending_coh
  • gcash_payments  — auto-set to match GCash sales from the Order table so
                      digital revenue nets out and does not inflate ending COH
  • expenses, coins, cash_advance, floating_cash — all 0.00
  • is_auto_generated = True   — signals the cashier to review this record
  • prepared_by = None         — no human saved this record
"""

import datetime
import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


def fill_missing_finance_records(up_to_date=None):
    """
    Create DailyFinance records for any past dates (up to and including
    ``up_to_date``) that have no record yet.

    Parameters
    ----------
    up_to_date : datetime.date, optional
        The last date to fill (inclusive). Defaults to yesterday — today is
        never auto-filled because the cashier may still save it manually.

    Returns
    -------
    list[datetime.date]
        The dates for which new records were created (empty if nothing to fill).
    """
    # Import here to avoid circular imports (models → services → models)
    from apps.finance.models import DailyFinance
    from apps.orders.models import Order

    if up_to_date is None:
        up_to_date = timezone.localdate() - datetime.timedelta(days=1)

    # Find the earliest existing record — we only need to scan from there.
    earliest = (
        DailyFinance.objects
        .order_by('date')
        .values_list('date', flat=True)
        .first()
    )
    if earliest is None:
        # No records at all — nothing to anchor the chain against.
        return []

    scan_from = earliest

    if scan_from > up_to_date:
        return []

    # Collect dates in range that already have a record
    existing_dates = set(
        DailyFinance.objects
        .filter(date__gte=scan_from, date__lte=up_to_date)
        .values_list('date', flat=True)
    )

    # Build the list of missing dates in chronological order
    missing_dates = []
    current = scan_from
    while current <= up_to_date:
        if current not in existing_dates:
            missing_dates.append(current)
        current += datetime.timedelta(days=1)

    if not missing_dates:
        return []

    created = []

    for missing_date in missing_dates:
        # Carry forward ending_coh from the most recent prior record.
        # We re-query on each iteration so that a record created earlier in
        # this same loop is picked up as the predecessor for the next gap.
        prior = (
            DailyFinance.objects
            .filter(date__lt=missing_date)
            .order_by('-date')
            .first()
        )
        previous_coh = prior.ending_coh if prior else Decimal('0.00')

        # Auto-fill gcash_payments from the Order table so digital revenue
        # nets out and does not inflate ending COH.
        gcash_result = Order.objects.filter(
            created_at__date=missing_date,
            is_paid=True,
            payment_method='gcash',
            status='completed',
        ).aggregate(total=Sum('total'))
        gcash_payments = gcash_result['total'] or Decimal('0.00')

        try:
            with transaction.atomic():
                DailyFinance.objects.create(
                    date=missing_date,
                    previous_coh=previous_coh,
                    previous_coh_is_manual=False,
                    gcash_payments=gcash_payments,
                    expenses=Decimal('0.00'),
                    coins=Decimal('0.00'),
                    cash_advance=Decimal('0.00'),
                    floating_cash=Decimal('0.00'),
                    expenses_notes=(
                        "⚠️ Auto-generated record — cashier did not save this day. "
                        "Please review and fill in expenses, coins, cash advance, "
                        "and floating cash."
                    ),
                    is_auto_generated=True,
                    prepared_by=None,
                )
            created.append(missing_date)
            logger.info("Auto-generated finance record for %s", missing_date)
        except IntegrityError:
            # Another request created this record concurrently — not an error.
            logger.debug(
                "fill_missing_finance_records: concurrent create for %s, skipping",
                missing_date,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "fill_missing_finance_records: failed to create record for %s",
                missing_date,
            )

    return created
