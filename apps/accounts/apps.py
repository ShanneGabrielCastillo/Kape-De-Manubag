from django.apps import AppConfig

class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'
    label = 'accounts'

    def ready(self):
        # Register the MFA configuration system checks (accounts.E001/E002).
        from . import checks  # noqa: F401
