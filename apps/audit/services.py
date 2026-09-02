"""
Audit logging service.

``log_action()`` is the single entry point for recording significant
administrative actions. It is intentionally non-intrusive:

* It never raises on database failure — the failure is written to the app
  logger and swallowed, so the audit trail can never break the business
  action it records ("do not change existing business logic"). Only real
  ``DatabaseError`` failures are swallowed; bugs in the logging code itself
  still surface.
* It never stores sensitive values (passwords, payment data, etc.) — only
  the affected object's safe ``str()`` snapshot is kept.
* It is one INSERT per administrative action, so it has no measurable
  impact on normal system performance.
"""
import logging

from django.db import DatabaseError

from .models import AuditLog

logger = logging.getLogger(__name__)


def log_action(user, action, obj=None, *, object_type='', object_id='',
               object_repr='', detail=''):
    """Append one entry to the audit trail.

    Args:
        user: the authenticated user performing the action (may be ``None``
            for system/anonymous actions).
        action: a dotted verb, e.g. ``'product.create'``.
        obj: the affected model instance. Its model name, primary key and
            ``str()`` representation are captured automatically unless the
            ``object_type`` / ``object_id`` / ``object_repr`` overrides are
            supplied.
        detail: optional human-readable context (free-form, not shown in
            list views).
    """
    if obj is not None:
        if not object_type:
            object_type = obj._meta.model_name
        if not object_id:
            object_id = str(obj.pk)
        if not object_repr:
            object_repr = str(obj)

    # AnonymousUser (or plain ``None``) is stored as a NULL user.
    actor = user if (user is not None and user.is_authenticated) else None

    try:
        AuditLog.objects.create(
            user=actor,
            action=action[:50],
            object_type=str(object_type)[:50],
            object_id=str(object_id)[:50],
            object_repr=object_repr[:255],
            detail=detail,
        )
    except DatabaseError:
        # Only real database failures are swallowed (best-effort logging).
        # Programming errors in log_action itself still surface so bugs are
        # never silently hidden.
        logger.exception('Audit log entry could not be saved (action=%s)', action)
