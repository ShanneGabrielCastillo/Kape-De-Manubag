"""Kape De Manubag — Food Ordering & Management System."""

# Public application version, surfaced by the /health/ endpoint.
__version__ = "1.0.0"

# Register project-level Django system checks (see checks.py). Importing the
# module triggers @register() so the check runs on every `manage.py check`.
from . import checks  # noqa: F401

# Install the template-context copy workaround for Django 5.0 + Python 3.14
# (see compat.py). It is a no-op on compatible environments.
from .compat import install_context_copy_workaround

install_context_copy_workaround()
