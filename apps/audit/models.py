"""
Audit trail — append-only log of significant administrative actions.

Each row records WHO performed WHAT action on WHICH object and WHEN:

* ``user``        — the authenticated user who performed the action.
* ``action``      — a dotted verb such as ``product.create`` or ``staff.deactivate``.
* ``object_type`` — the model name of the affected record (e.g. ``product``).
* ``object_id``   — the primary key of the affected record.
* ``object_repr`` — a short, safe textual snapshot of the affected record
                    (its ``str()`` representation).
* ``detail``      — optional human-readable context (e.g. stock before/after).

Rows are never updated or deleted through normal use, so the trail is
trustworthy for review (see admin.py for the read-only admin interface).
The table grows one row per admin action (low volume); old entries can be
pruned periodically if it ever becomes large.

Sensitive data (passwords, payment details, etc.) is intentionally never
stored — only the safe ``str()`` snapshot described above.
"""
from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='audit_logs',
    )
    action = models.CharField(max_length=50, db_index=True)
    object_type = models.CharField(max_length=50, blank=True, default='')
    object_id = models.CharField(max_length=50, blank=True, default='')
    object_repr = models.CharField(max_length=255, blank=True, default='')
    detail = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Audit Log Entry'
        verbose_name_plural = 'Audit Log Entries'

    def __str__(self):
        who = self.user.get_username() if self.user else 'system'
        target = self.object_repr or f'{self.object_type} {self.object_id}'.strip()
        return f'{self.created_at:%Y-%m-%d %H:%M} {who} {self.action} {target}'
