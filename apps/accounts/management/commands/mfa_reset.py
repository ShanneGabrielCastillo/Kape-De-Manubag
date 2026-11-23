"""Management command: reset a staff member's MFA (Phase 5D).

This is the controlled recovery path when someone loses both their
authenticator and every recovery code. It requires shell/server access (it is
not exposed over HTTP), asks for confirmation unless ``--yes`` is passed, and
writes an audit entry. It never prints or stores any secret or code.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.module_loading import import_string

from apps.accounts import mfa
from apps.audit.services import log_action

# Backends whose sessions can be enumerated and revoked per user. Cookie/file/
# cache-only backends cannot be revoked reliably from the server, so the
# command says so instead of claiming otherwise.
REVOCABLE_SESSION_ENGINES = (
    'django.contrib.sessions.backends.db',
    'django.contrib.sessions.backends.cached_db',
)


def revoke_user_sessions(user):
    """Delete every server-side session belonging to ``user``.

    Returns the number revoked, or ``None`` when the configured session
    backend cannot support reliable targeted revocation. Other users'
    sessions are never touched.
    """
    engine = getattr(
        settings, 'SESSION_ENGINE', 'django.contrib.sessions.backends.db')
    if engine not in REVOCABLE_SESSION_ENGINES:
        return None
    # SESSION_ENGINE names a module; the store class lives inside it.
    store_class = import_string(f'{engine}.SessionStore')
    revoked = 0
    active = Session.objects.filter(expire_date__gt=timezone.now())
    for row in active.iterator():
        try:
            data = row.get_decoded()
        except Exception:
            # Unreadable/corrupt row: leave it alone rather than guessing.
            continue
        if str(data.get('_auth_user_id')) != str(user.pk):
            continue
        # Deleting through the store also clears the cache entry for
        # ``cached_db``; a raw queryset delete would leave it behind.
        store_class(session_key=row.session_key).delete()
        revoked += 1
    return revoked


class Command(BaseCommand):
    help = "Remove a user's authenticator and recovery codes (audited reset)."

    def add_arguments(self, parser):
        parser.add_argument('username', help='Username of the account to reset')
        parser.add_argument(
            '--yes', action='store_true',
            help='Skip the interactive confirmation prompt',
        )

    def handle(self, *args, **options):
        username = options['username']
        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'No user with username {username!r}.')

        if not mfa.get_device(user) and not mfa.unused_recovery_code_count(user):
            self.stdout.write(self.style.WARNING(
                f'{user.username} has no two-factor authentication configured.'))
            return

        if not options['yes']:
            answer = input(
                f'Reset two-factor authentication for {user.username!r}? [y/N] ')
            if answer.strip().lower() not in ('y', 'yes'):
                self.stdout.write('Aborted; nothing was changed.')
                return

        mfa.delete_device(user)
        revoked = revoke_user_sessions(user)
        log_action(
            None, 'mfa.reset', user,
            detail=f'username={user.username} (management command)',
        )
        self.stdout.write(self.style.SUCCESS(
            f'Two-factor authentication reset for {user.username}. '
            'The account will be asked to enrol again.'))
        if revoked is None:
            engine = getattr(
                settings, 'SESSION_ENGINE',
                'django.contrib.sessions.backends.db')
            self.stdout.write(self.style.WARNING(
                'Existing sessions were NOT revoked: the configured session '
                f'backend ({engine}) cannot be revoked per user. '
                'Sign the account out manually if its session may be '
                'compromised.'))
        else:
            self.stdout.write(f'Revoked {revoked} existing session(s).')
