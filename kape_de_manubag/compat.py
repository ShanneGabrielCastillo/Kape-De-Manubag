"""
Runtime compatibility workarounds for the local environment.

The virtual environment runs Django 5.0.14 on Python 3.14, where Django's
template ``BaseContext.__copy__`` implementation (``copy(super())``) crashes
with ``AttributeError: 'super' object has no attribute 'dicts'`` because
PEP 667 made ``super()`` proxies immutable. Any page whose templates clone
the template context -- most notably the Django admin change form, which is
the primary UI for editing user roles -- then returns HTTP 500.

The probe below confirms the incompatibility at startup and, only then,
replaces ``BaseContext.__copy__`` with the behaviour Django intends (a
shallow copy of the instance with a fresh ``dicts`` list). On a compatible
Python/Django combination the probe succeeds and nothing is patched, so this
module is inert once the environment is upgraded to a Django release that
supports Python 3.14 (e.g. Django 5.2+).
"""
import copy as _copy

from django.template.context import BaseContext


def _safe_base_context_copy(self):
    """Shallow-copy a template context without touching ``super()``."""
    duplicate = self.__class__.__new__(self.__class__)
    duplicate.__dict__.update(self.__dict__)
    duplicate.dicts = self.dicts[:]
    return duplicate


def install_context_copy_workaround():
    """Patch ``BaseContext.__copy__`` only if the environment is broken.

    Returns True when the patch was applied.
    """
    try:
        _copy.copy(BaseContext())
    except AttributeError:
        BaseContext.__copy__ = _safe_base_context_copy
        return True
    return False
