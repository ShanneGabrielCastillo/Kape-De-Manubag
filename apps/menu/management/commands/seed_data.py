"""
Management command to seed sample data
Usage: python manage.py seed_data
"""
from django.core.management.base import BaseCommand
import subprocess
import sys
import os


class Command(BaseCommand):
    help = 'Seeds the database with sample menu data and user accounts'

    def handle(self, *args, **options):
        seed_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
            'sample_data', 'seed_data.py'
        )
        self.stdout.write(self.style.SUCCESS('Seeding database...'))
        exec(open(seed_file).read())
        self.stdout.write(self.style.SUCCESS('Done!'))
