from django.db import models


class SystemSetting(models.Model):
    key = models.CharField(max_length=100, unique=True)
    value = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key} = {self.value}"

    @classmethod
    def get(cls, key, default=None):
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default

    class Meta:
        verbose_name = "System Setting"
        verbose_name_plural = "System Settings"


class GCashSettings(models.Model):
    """
    Singleton model holding the business owner's GCash payment information.

    Only one row should ever exist (enforced via the singleton save pattern).
    All fields are optional so the admin can configure incrementally without
    breaking the customer-facing page.

    Accessible from templates via the `gcash_settings` context processor or
    a direct DB read in the view.  Customers may READ but never WRITE these.
    """
    account_name = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text="GCash account name (as it appears in GCash).",
    )
    account_number = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text="GCash mobile number (e.g. 09XXXXXXXXX).",
    )
    qr_image = models.ImageField(
        upload_to='gcash_qr/',
        blank=True,
        null=True,
        help_text="GCash QR code image displayed to customers.",
    )
    instructions = models.TextField(
        blank=True,
        default='',
        help_text="Optional extra instructions shown on the payment page.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "GCash Settings"
        verbose_name_plural = "GCash Settings"

    def __str__(self):
        return f"GCash Settings — {self.account_name or '(not configured)'}"

    def save(self, *args, **kwargs):
        # Singleton: always use pk=1 so there is only ever one row.
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Prevent accidental deletion of the singleton row.
        pass

    @classmethod
    def get_settings(cls):
        """Return the singleton instance, creating an empty one if needed."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_configured(self):
        """True if the minimum fields are set (account name + number)."""
        return bool(self.account_name and self.account_number)
