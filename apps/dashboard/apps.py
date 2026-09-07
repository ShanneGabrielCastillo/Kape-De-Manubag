from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.dashboard'
    label = 'dashboard'

    def ready(self):
        # Enable SQLite WAL mode on every new database connection.
        # WAL (Write-Ahead Logging) significantly reduces expensive fsync()
        # calls on networked storage (e.g. PythonAnywhere NFS), making writes
        # faster without any risk to data integrity.
        # PRAGMA synchronous=NORMAL is safe with WAL — it still syncs on
        # checkpoints, just not on every individual write.
        from django.db.backends.signals import connection_created

        def _set_wal(sender, connection, **kwargs):
            if connection.vendor == 'sqlite':
                connection.cursor().execute('PRAGMA journal_mode=WAL;')
                connection.cursor().execute('PRAGMA synchronous=NORMAL;')

        connection_created.connect(_set_wal)
