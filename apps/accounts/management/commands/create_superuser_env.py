"""
Management command: create_superuser_env

Creates an administrator-role superuser from environment variables if one
doesn't already exist. Safe to run on every deploy — skips when the required
variables are missing or the username is already present, and never resets an
existing password.

Required env vars:
  DJANGO_SUPERUSER_USERNAME
  DJANGO_SUPERUSER_PASSWORD

Optional env var:
  DJANGO_SUPERUSER_EMAIL

The password is validated with Django's configured AUTH_PASSWORD_VALIDATORS
before the account is created. The password is never printed.
"""
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create an administrator-role superuser from environment variables (idempotent).'

    def handle(self, *args, **options):
        User = get_user_model()

        username = (os.environ.get('DJANGO_SUPERUSER_USERNAME') or '').strip()
        email = (os.environ.get('DJANGO_SUPERUSER_EMAIL') or '').strip()
        password = os.environ.get('DJANGO_SUPERUSER_PASSWORD') or ''

        if not username or not password:
            self.stdout.write(
                'DJANGO_SUPERUSER_USERNAME or DJANGO_SUPERUSER_PASSWORD '
                'not set — skipping superuser creation.'
            )
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(
                f'Superuser "{username}" already exists — skipping. '
                'The existing password was not changed.'
            )
            return

        # Validate against the project's configured policy (length, common
        # passwords, numeric-only, similarity to the username/email). The
        # candidate user is unsaved; it exists only to give the similarity
        # validator the attributes it needs.
        candidate = User(username=username, email=email)
        try:
            validate_password(password, user=candidate)
        except ValidationError as exc:
            raise CommandError(
                'Refusing to create the superuser: the supplied password does '
                'not meet the configured password policy. ' + ' '.join(exc.messages)
            )

        User.objects.create_superuser(
            username=username,
            email=email,
            password=password,
            role='admin',
        )
        self.stdout.write(self.style.SUCCESS(
            f'Superuser "{username}" created with the administrator role.'
        ))
