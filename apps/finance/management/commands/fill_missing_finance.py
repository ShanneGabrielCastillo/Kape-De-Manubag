"""
Management command: fill_missing_finance

Delegates entirely to apps.finance.services.fill_missing_finance_records()
so the auto-fill logic lives in exactly one place.

The finance page now calls that service on every load, so this command is
only needed for:
  - One-off backfills of historical gaps (e.g. before the feature existed)
  - Seeding data in tests or staging environments
  - Verifying what would be created without touching the DB (--dry-run)

Usage:
    python manage.py fill_missing_finance
    python manage.py fill_missing_finance --dry-run
    python manage.py fill_missing_finance --start-date 2026-01-01
"""

import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from apps.finance.services import fill_missing_finance_records


class Command(BaseCommand):
    help = (
        "Auto-fill DailyFinance records for past dates that were never saved. "
        "gcash_payments is set to the day's GCash sales total so it nets out. "
        "All other deductions default to zero. "
        "Records are flagged is_auto_generated=True for cashier review. "
        "The finance page already runs this automatically on every load; "
        "this command is for manual / historical backfills."
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
                'Defaults to the earliest existing finance record date.'
            ),
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        yesterday = timezone.localdate() - datetime.timedelta(days=1)

        if options['start_date']:
            try:
                datetime.date.fromisoformat(options['start_date'])
            except ValueError:
                self.stderr.write(
                    self.style.ERROR(
                        f"Invalid --start-date: {options['start_date']}. "
                        "Expected format: YYYY-MM-DD"
                    )
                )
                return

        if dry_run:
            # For dry-run we replicate the logic manually so we never touch the DB.
            from apps.finance.models import DailyFinance
            from apps.orders.models import Order

            start_date = None
            if options['start_date']:
                start_date = datetime.date.fromisoformat(options['start_date'])
            else:
                start_date = (
                    DailyFinance.objects.order_by('date')
                    .values_list('date', flat=True)
                    .first()
                )

            if not start_date:
                self.stdout.write(self.style.WARNING(
                    "No existing finance records found. Nothing to fill."
                ))
                return

            existing_dates = set(
                DailyFinance.objects
                .filter(date__gte=start_date, date__lte=yesterday)
                .values_list('date', flat=True)
            )
            missing = []
            current = start_date
            while current <= yesterday:
                if current not in existing_dates:
                    missing.append(current)
                current += datetime.timedelta(days=1)

            if not missing:
                self.stdout.write(self.style.SUCCESS("No missing dates found."))
                return

            self.stdout.write(self.style.WARNING(
                f"DRY RUN — {len(missing)} date(s) would be created:\n"
            ))
            for d in missing:
                prior = (
                    DailyFinance.objects
                    .filter(date__lt=d)
                    .order_by('-date')
                    .first()
                )
                prev_coh = prior.ending_coh if prior else Decimal('0.00')
                gcash = Order.objects.filter(
                    created_at__date=d,
                    is_paid=True,
                    payment_method='gcash',
                    status='completed',
                ).aggregate(total=Sum('total'))['total'] or Decimal('0.00')
                self.stdout.write(
                    f"  {d}: prev_coh={prev_coh}, gcash_payments={gcash}"
                )
            self.stdout.write(
                self.style.WARNING(f"\nDry run complete. No records written.")
            )
            return

        # Live run — delegate to the service
        created = fill_missing_finance_records()

        if not created:
            self.stdout.write(self.style.SUCCESS(
                "No missing finance records found. Everything is up to date."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Created {len(created)} auto-generated record(s):"
            ))
            for d in created:
                self.stdout.write(self.style.SUCCESS(f"  ✓ {d}"))
