"""
Management command to seed demo menu data (no user accounts).
Usage: python manage.py seed_data
"""
from django.core.management.base import BaseCommand
import os


class Command(BaseCommand):
    help = 'Seeds demo menu data (categories, products, settings); creates no accounts'

    def handle(self, *args, **options):
        seed_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
            'sample_data', 'seed_data.py'
        )
        self.stdout.write(self.style.SUCCESS('Seeding database...'))
        # The script contains UTF-8 text (emoji/product names); read it
        # explicitly instead of relying on the platform locale (cp1252 on
        # Windows would fail to decode it).
        with open(seed_file, encoding='utf-8') as handle:
            exec(compile(handle.read(), seed_file, 'exec'))
        self.stdout.write(self.style.SUCCESS('Done!'))
