"""
Custom User Model for Kape De Manubag System
Supports Admin, Cashier, and Customer roles
"""
from django.conf import settings
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
        ('kitchen_staff', 'Kitchen Staff'),
        ('customer', 'Customer'),
    ]

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='customer')
    phone = models.CharField(max_length=15, blank=True, null=True)
    profile_image = models.ImageField(upload_to='profiles/', blank=True, null=True)
    profile_image_filename = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Original filename of the uploaded profile image (user-facing display only).'
    )
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
    def is_kitchen_staff(self):
        return self.role == 'kitchen_staff'

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


class PasswordResetAttempt(models.Model):
    """Throttle counters for password-reset requests (Phase 5C).

    One row per tracked entity: a keyed one-way hash of the requested
    email address (HMAC-SHA256 over the casefolded address with the
    deployment ``SECRET_KEY`` — the raw address is never stored) and/or
    the client IP address. Both counters are independent so that repeated
    requests for one account and wide sweeps across many addresses from
    one machine are each throttled.

    Deliberately separate from ``FailedLoginAttempt``: the login limiter
    stores raw usernames/IPs and its success path *deletes* counters —
    reusing it would both expose reset-target identifiers and let any
    successful login erase reset throttling.
    """

    SCOPE_EMAIL = 'email'
    SCOPE_IP = 'ip'
    SCOPE_CHOICES = [
        (SCOPE_EMAIL, 'Email (keyed hash)'),
        (SCOPE_IP, 'IP address'),
    ]

    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES)
    # SCOPE_EMAIL: 64-char HMAC-SHA256 hex digest of the normalized email.
    # SCOPE_IP: the client IP string (as in FailedLoginAttempt — an IP is
    # not personal data in the same sense as an email address, and the
    # login limiter already stores it plaintext by documented decision).
    value = models.CharField(max_length=64)
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField()

    class Meta:
        verbose_name = 'Password reset attempt'
        verbose_name_plural = 'Password reset attempts'
        constraints = [
            models.UniqueConstraint(
                fields=['scope', 'value'],
                name='unique_password_reset_scope_value',
            ),
        ]

    def __str__(self):
        return f'{self.get_scope_display()}: {self.value[:12]}… ({self.attempts})'


class StaffMFADevice(models.Model):
    """A staff member's confirmed TOTP authenticator (Phase 5D).

    The shared TOTP secret is **never** stored in plaintext: it is a Fernet
    token produced with a key derived from ``MFA_ENCRYPTION_KEY`` (or, when
    that is unset, HKDF from ``SECRET_KEY``). A row only exists once the
    member has proved possession of the secret by entering a valid code, so
    there is no "half-enabled" state.

    ``last_used_timestep`` stores the most recently accepted TOTP time step
    so the same 6-digit code (or an earlier one) can never be replayed, even
    if it is presented inside the normal ±1 step clock-drift window.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='mfa_device',
    )
    secret_encrypted = models.TextField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_timestep = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Staff MFA device'
        verbose_name_plural = 'Staff MFA devices'

    def __str__(self):
        state = 'confirmed' if self.confirmed_at else 'pending'
        return f'{self.user_id} ({state})'


class MFARecoveryCode(models.Model):
    """One single-use MFA recovery code, stored only as a password hash.

    Plaintext codes are shown to the member exactly once, at generation
    time. Verification uses Django's password hasher, so a database leak
    does not expose usable recovery codes.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='mfa_recovery_codes',
    )
    code_hash = models.CharField(max_length=128)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'MFA recovery code'
        verbose_name_plural = 'MFA recovery codes'

    def __str__(self):
        return f'{self.user_id} ({"used" if self.used_at else "unused"})'


class MFAVerificationAttempt(models.Model):
    """Throttle counters for failed MFA code / recovery-code attempts.

    Deliberately separate from ``FailedLoginAttempt`` (Phase 5B) so a burst
    of bad TOTP codes can never lock the password-login counters, and a
    successful password login can never clear MFA brute-force protection.
    Mirrors the per-user / per-IP sliding-window pattern of the login and
    password-reset limiters.
    """

    SCOPE_USER = 'user'
    SCOPE_IP = 'ip'
    SCOPE_CHOICES = [
        (SCOPE_USER, 'User'),
        (SCOPE_IP, 'IP address'),
    ]

    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES)
    # SCOPE_USER: the numeric primary key as a string (never a username or
    # email). SCOPE_IP: the direct peer address, as in FailedLoginAttempt.
    value = models.CharField(max_length=64)
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField()

    class Meta:
        verbose_name = 'MFA verification attempt'
        verbose_name_plural = 'MFA verification attempts'
        constraints = [
            models.UniqueConstraint(
                fields=['scope', 'value'],
                name='unique_mfa_verification_scope_value',
            ),
        ]

    def __str__(self):
        return f'{self.get_scope_display()}: {self.value} ({self.attempts})'
