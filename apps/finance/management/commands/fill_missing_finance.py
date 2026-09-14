"""
Management command: fill_missing_finance

Auto-generates DailyFinance records for any past calendar dates that have
no record, starting from the date of the first existing record up to
yesterday (never today — the cashier may still save today manually).

For each missing day the command:
  1. Carries previous_coh forward from the most recent prior record's ending_coh.
  2. Sets gcash_payments = GCash sales from the Order table for that date
     (mirrors the "Use this amount" hint on the finance page so GCash nets out).
  3. Leaves expenses, coins, cash_advance, floating_cash as 0.00.
  4. Sets is_auto_generated = True so the cashier knows to review it.
  5. Sets previous_coh_is_manual = False (value came from a prior record).

Usage:
    python manage.py fill_missing_finance
    python manage.py fill_missing_finance --dry-run
    python manage.py fill_missing_finance --start-date 2026-01-01
"""

import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.finance.models import DailyFinance
from apps.orders.models import Order


class Command(BaseCommand):
    help = (
        "Auto-fill DailyFinance records for past dates that were never saved. "
        "gcash_payments is set to the day's GCash sales total so it nets out "
        "correctly. All other deductions default to zero. "
        "Records are flagged is_auto_generated=True for cashier review."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be created without writing to the database.',
        )
        parser.add_argument(
            '--start-date',
            type=str,
            default=None,
            help=(
                'ISO date (YYYY-MM-DD) to start scanning from. '
                'Defaults to the date of the earliest existing finance record. '
                'If no records exist at all, defaults to today and exits early.'
            ),
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        yesterday = timezone.localdate() - datetime.timedelta(days=1)

        # ── Determine scan start date ─────────────────────────────────────────
        if options['start_date']:
            try:
                scan_from = datetime.date.fromisoformat(options['start_date'])
            except ValueError:
                self.stderr.write(
                    self.style.ERROR(
                        f"Invalid --start-date: {options['start_date']}. "
                        "Expected format: YYYY-MM-DD"
                    )
                )
                return
        else:
            earliest = (
                DailyFinance.objects.order_by('date').values_list('date', flat=True).first()
            )
            if earliest is None:
                self.stdout.write(
                    self.style.WARNING(
                        "No existing finance records found. "
                        "Nothing to fill — save the first record manually."
                    )
                )
                return
            scan_from = earliest

        if scan_from > yesterday:
            self.stdout.write(
                self.style.SUCCESS("No past dates to fill. Everything is up to date.")
            )
            return

        # ── Collect all dates in range that have no record ────────────────────
        existing_dates = set(
            DailyFinance.objects
            .filter(date__gte=scan_from, date__lte=yesterday)
            .values_list('date', flat=True)
        )

        missing_dates = []
        current = scan_from
        while current <= yesterday:
            if current not in existing_dates:
                missing_dates.append(current)
            current += datetime.timedelta(days=1)

        if not missing_dates:
            self.stdout.write(
                self.style.SUCCESS(
                    f"No missing finance records between {scan_from} and {yesterday}."
                )
            )
            return

        self.stdout.write(
            f"Found {len(missing_dates)} missing date(s) between "
            f"{scan_from} and {yesterday}."
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no records will be written.\n"))

        # ── Create records in chronological order ─────────────────────────────
        created_count = 0
        skipped_count = 0

        for missing_date in missing_dates:
            # Get previous COH from the most recent record before this date
            prior = (
                DailyFinance.objects
                .filter(date__lt=missing_date)
                .order_by('-date')
                .first()
            )
            if prior is None:
                previous_coh = Decimal('0.00')
            else:
                # ending_coh is a computed property — safe to call on a single record
                previous_coh = prior.ending_coh

            # Get GCash sales for this date from the Order table
            gcash_result = Order.objects.filter(
                created_at__date=missing_date,
                is_paid=True,
                payment_method='gcash',
                status='completed',
            ).aggregate(total=Sum('total'))
            gcash_payments = gcash_result['total'] or Decimal('0.00')

            if dry_run:
                self.stdout.write(
                    f"  Would create {missing_date}: "
                    f"prev_coh={previous_coh}, gcash_payments={gcash_payments}"
                )
                skipped_count += 1
                continue

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
                            "Please review and correct expenses, coins, cash advance, "
                            "and floating cash."
                        ),
                        is_auto_generated=True,
                        prepared_by=None,
                    )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ Created {missing_date}: "
                        f"prev_coh={previous_coh}, gcash_payments={gcash_payments}"
                    )
                )
                created_count += 1
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(
                    self.style.ERROR(f"  ✗ Failed to create {missing_date}: {exc}")
                )
                skipped_count += 1

        # ── Summary ───────────────────────────────────────────────────────────
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDry run complete. Would have created {skipped_count} record(s)."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nDone. Created {created_count} record(s)."
                    + (f" {skipped_count} failed." if skipped_count else "")
                )
            )
