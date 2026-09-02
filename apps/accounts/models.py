"""
Custom User Model for Kape De Manubag System
Supports Admin, Cashier, and Customer roles
"""
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone


LAST_ADMIN_ERROR = (
    'Cannot remove the last administrator account. '
    'Promote another user to administrator first.'
)


class CustomUser(AbstractUser):
    """Extended user model with role-based access"""

    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('cashier', 'Cashier'),
        ('customer', 'Customer'),
    ]

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='customer')
    phone = models.CharField(max_length=15, blank=True, null=True)
    profile_image = models.ImageField(upload_to='profiles/', blank=True, null=True)
    deactivated_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When this account was soft-deactivated (is_active=False). '
                  'Null while the account is active.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def is_admin_user(self):
        return self.role == 'admin' or self.is_superuser

    @property
    def is_cashier(self):
        return self.role == 'cashier'

    @property
    def is_customer(self):
        return self.role == 'customer'

    # ── Soft-deactivation workflow ──────────────────────────────────────────
    # Accounts are deactivated, never deleted, so every historical record
    # (orders, finance records, reports) keeps its reference to this user.

    def deactivate(self):
        """Soft-deactivate this account (blocked for the last administrator)."""
        self.is_active = False
        self.deactivated_at = timezone.now()
        self.save(update_fields=['is_active', 'deactivated_at', 'updated_at'])

    def activate(self):
        """Re-activate a deactivated account."""
        self.is_active = True
        self.deactivated_at = None
        self.save(update_fields=['is_active', 'deactivated_at', 'updated_at'])

    # ── Last-administrator protection ────────────────────────────────────────
    # An "administrator" is any active user with role='admin' or is_superuser
    # (the same definition as is_admin_user). These guards are enforced at the
    # model layer so every code path -- forms, views, the admin site, and
    # direct ORM calls -- validates role changes identically.

    @classmethod
    def other_active_admins_exist(cls, exclude_pk):
        """True if an active administrator other than ``exclude_pk`` exists."""
        return cls.objects.filter(
            is_active=True,
        ).exclude(pk=exclude_pk).filter(
            Q(role='admin') | Q(is_superuser=True),
        ).exists()

    def _removes_last_admin(self):
        """True if saving/deleting this persisted user would leave the system
        with zero active administrators.

        Note: ``QuerySet.update()`` (e.g. a bulk ``role`` change) bypasses
        save/delete/signals entirely, so it is not covered here; the app has
        no such code path, and ``is_staff`` alone is intentionally not part
        of the definition (it affects only Django-admin access).
        """
        if self.pk is None:
            return False                      # creating a user never removes one
        if self.is_admin_user and self.is_active:
            return False                      # still an active administrator
        # Administrator capability is being removed (demotion or
        # deactivation) -- only allowed while another one remains.
        old = type(self).objects.filter(pk=self.pk).first()
        if old is None or not old.is_admin_user:
            return False                      # not an administrator (or deleted)
        return not type(self).other_active_admins_exist(self.pk)

    def clean(self):
        """Full validation; run by every ModelForm (including the admin site)."""
        super().clean()
        if self._removes_last_admin():
            raise ValidationError(LAST_ADMIN_ERROR)

    def save(self, *args, **kwargs):
        # Defense in depth: .save() bypasses clean(), so enforce the rule here
        # too (covers direct ORM writes such as staff_toggle deactivating a
        # user). Callers surface the friendly message where appropriate.
        # CAUTION: raising inside a transaction.atomic() block marks the
        # connection for rollback -- callers that catch and continue querying
        # must do so inside a savepoint (see the tests).
        if self._removes_last_admin():
            raise ValidationError(LAST_ADMIN_ERROR)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Permanent deletion is disabled: user accounts are soft-deactivated
        # instead (is_active=False, deactivated_at set), so historical records
        # (orders, finance records, reports) keep their references intact.
        # Bulk ``QuerySet.delete()`` is blocked by the ``pre_delete`` signal.
        if self._removes_last_admin():
            raise ValidationError(LAST_ADMIN_ERROR)
        self.deactivate()
        return 0, {}  # nothing was removed from the database

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'


SOFT_DELETE_ERROR = (
    'User accounts cannot be deleted from the database. '
    'Deactivate them instead.'
)


@receiver(pre_delete, sender=CustomUser)
def _block_hard_delete(sender, instance, **kwargs):
    """Block *any* hard delete of a user, including bulk ``QuerySet.delete()``
    (which bypasses ``CustomUser.delete()``). Soft-deactivation -- via
    ``deactivate()`` or ``CustomUser.delete()`` -- is the only way to remove
    access, preserving every historical reference to the account.

    CAUTION: raising inside ``transaction.atomic()`` marks the connection for
    rollback; callers that catch the exception and keep querying must do so
    inside a savepoint (see the tests).
    """
    raise ValidationError(SOFT_DELETE_ERROR)


class FailedLoginAttempt(models.Model):
    """Tracks failed login attempts to protect against brute-force attacks.

    One row exists per tracked entity: the submitted ``username`` and/or the
    client ``ip`` address. Both are counted independently so that both
    "many passwords for one account" and "many accounts from one machine"
    attacks hit a lockout. Rows are written for usernames that do not exist
    too, so the lockout behaviour never reveals whether an account exists.
    """

    SCOPE_USERNAME = 'username'
    SCOPE_IP = 'ip'
    SCOPE_CHOICES = [
        (SCOPE_USERNAME, 'Username'),
        (SCOPE_IP, 'IP address'),
    ]

    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES)
    # Normalized username (lowercased, stripped) or IP string. Plaintext on
    # purpose: this is an internal audit table, and keeping it debuggable
    # outweighs the negligible privacy cost of a failed-attempt log.
    value = models.CharField(max_length=255)
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Failed login attempt'
        verbose_name_plural = 'Failed login attempts'
        constraints = [
            models.UniqueConstraint(
                fields=['scope', 'value'],
                name='unique_failed_login_scope_value',
            ),
        ]

    def __str__(self):
        return f'{self.get_scope_display()}: {self.value} ({self.attempts})'
