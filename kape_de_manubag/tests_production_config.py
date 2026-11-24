"""Focused tests for the Phase 7B-1 production configuration hardening.

These tests load ``kape_de_manubag/settings.py`` and ``gunicorn.conf.py``
directly from disk with a controlled environment. ``dotenv.load_dotenv`` is
stubbed during the load so the real ``.env`` file is never read.

No database is created, opened, or modified: every test is a
``SimpleTestCase`` and only inspects the settings dictionaries produced at
import time.
"""
import contextlib
import importlib.util
import os
import sys
import types
import uuid
from pathlib import Path
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / 'kape_de_manubag' / 'settings.py'
GUNICORN_PATH = PROJECT_ROOT / 'gunicorn.conf.py'

_MISSING = object()

# Environment names that influence the modules under test. They are removed
# (and restored) around every load so results never depend on the shell.
_ENV_KEYS = (
    'DEBUG',
    'SECRET_KEY',
    'ALLOWED_HOSTS',
    'CSRF_TRUSTED_ORIGINS',
    'DATABASE_URL',
    'SQLITE_PATH',
    'ALLOW_SQLITE_IN_PRODUCTION',
    'LOG_TO_STDOUT',
    'STATIC_ROOT',
    'GUNICORN_TIMEOUT',
    'WEB_CONCURRENCY',
    'PORT',
)

_PROD_ENV = {
    'DEBUG': 'False',
    'SECRET_KEY': 'settings-test-key-not-used-for-anything-real',
    'ALLOWED_HOSTS': 'example.com',
    # Avoid creating/opening log files while importing production settings;
    # stdout mode is also the correct container configuration.
    'LOG_TO_STDOUT': '1',
}

# Production environments that need a valid database choice before the module
# can finish importing (used by the logging tests, which are not about the
# database guard).
_PROD_SQLITE_ENV = {**_PROD_ENV, 'ALLOW_SQLITE_IN_PRODUCTION': '1'}


@contextlib.contextmanager
def _environment(values):
    """Temporarily replace the configuration-related environment variables."""
    previous = {}
    for key in set(values) | set(_ENV_KEYS):
        previous[key] = os.environ.pop(key, _MISSING)
    try:
        os.environ.update({key: str(value) for key, value in values.items()})
        yield
    finally:
        for key in set(values) | set(_ENV_KEYS):
            os.environ.pop(key, None)
        for key, value in previous.items():
            if value is not _MISSING:
                os.environ[key] = value


def _fake_dotenv():
    """A dotenv module whose ``load_dotenv`` never reads a real file."""
    module = types.ModuleType('dotenv')
    module.load_dotenv = lambda *args, **kwargs: None
    return module


def _load_settings(env, patch_log_dir=False):
    """Execute settings.py under ``env`` and return the module object."""
    module_name = f'kdm_settings_probe_{uuid.uuid4().hex}'
    spec = importlib.util.spec_from_file_location(module_name, SETTINGS_PATH)
    module = importlib.util.module_from_spec(spec)
    contexts = [
        _environment(env),
        mock.patch.dict(sys.modules, {'dotenv': _fake_dotenv()}),
    ]
    if patch_log_dir:
        # Production file logging calls LOG_DIR.mkdir() at import time; keep
        # the test filesystem untouched.
        contexts.append(mock.patch.object(Path, 'mkdir', autospec=True))
    with contextlib.ExitStack() as stack:
        for context in contexts:
            stack.enter_context(context)
        spec.loader.exec_module(module)
    return module


def _load_gunicorn(env):
    module_name = f'kdm_gunicorn_probe_{uuid.uuid4().hex}'
    spec = importlib.util.spec_from_file_location(module_name, GUNICORN_PATH)
    module = importlib.util.module_from_spec(spec)
    with _environment(env):
        spec.loader.exec_module(module)
    return module


class DatabaseSelectionTests(SimpleTestCase):
    """Rules A-E from the Phase 7B-1 brief."""

    def test_development_defaults_to_existing_sqlite_path(self):
        settings = _load_settings({'DEBUG': 'True'})
        default = settings.DATABASES['default']
        self.assertEqual(default['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(Path(default['NAME']), PROJECT_ROOT / 'db.sqlite3')
        self.assertEqual(default['OPTIONS'], {'timeout': 20})

    def test_development_honors_sqlite_path(self):
        custom = str(PROJECT_ROOT / 'tmp' / 'custom-dev.sqlite3')
        settings = _load_settings({'DEBUG': 'True', 'SQLITE_PATH': custom})
        self.assertEqual(settings.DATABASES['default']['NAME'], custom)

    def test_production_with_database_url_selects_postgresql(self):
        settings = _load_settings({
            **_PROD_ENV,
            'DATABASE_URL': 'postgresql://user:password@localhost:5432/kdm',
        })
        default = settings.DATABASES['default']
        self.assertEqual(default['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(default['NAME'], 'kdm')
        self.assertEqual(default['CONN_MAX_AGE'], 600)

    def test_production_without_database_url_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            _load_settings(_PROD_ENV)
        self.assertIn('ALLOW_SQLITE_IN_PRODUCTION', str(caught.exception))

    def test_production_sqlite_opt_in_works_when_explicit(self):
        settings = _load_settings({
            **_PROD_ENV,
            'ALLOW_SQLITE_IN_PRODUCTION': '1',
        })
        default = settings.DATABASES['default']
        self.assertEqual(default['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(Path(default['NAME']), PROJECT_ROOT / 'db.sqlite3')

    def test_sqlite_path_does_not_authorize_production(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_settings({
                **_PROD_ENV,
                'SQLITE_PATH': str(PROJECT_ROOT / 'tmp' / 'prod.sqlite3'),
            })

    def test_ambiguous_database_url_and_sqlite_opt_in_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            _load_settings({
                **_PROD_ENV,
                'DATABASE_URL': 'postgresql://user:password@localhost:5432/kdm',
                'ALLOW_SQLITE_IN_PRODUCTION': 'true',
            })
        self.assertIn('ambiguous', str(caught.exception).lower())

    def test_falsey_opt_in_values_do_not_enable_sqlite_production(self):
        for value in ('0', 'false', 'False', 'no', 'off', '', '   '):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ImproperlyConfigured):
                    _load_settings({
                        **_PROD_ENV,
                        'ALLOW_SQLITE_IN_PRODUCTION': value,
                    })

    def test_opt_in_value_is_stripped_and_case_insensitive(self):
        for value in (' 1 ', 'TRUE', 'Yes', 'on', 'y'):
            with self.subTest(value=repr(value)):
                settings = _load_settings({
                    **_PROD_ENV,
                    'ALLOW_SQLITE_IN_PRODUCTION': value,
                })
                self.assertEqual(
                    settings.DATABASES['default']['ENGINE'],
                    'django.db.backends.sqlite3',
                )

    def test_existing_test_settings_remain_compatible(self):
        from django.conf import settings as django_settings

        self.assertIn('default', django_settings.DATABASES)
        self.assertEqual(
            django_settings.STORAGES['staticfiles']['BACKEND'],
            'whitenoise.storage.CompressedManifestStaticFilesStorage',
        )
        self.assertIsInstance(django_settings.DEBUG, bool)


class LoggingConfigurationTests(SimpleTestCase):
    def _assert_no_file_handlers(self, settings):
        handlers = settings.LOGGING['handlers']
        self.assertEqual(set(handlers), {'console'})
        for handler in handlers.values():
            self.assertEqual(handler['class'], 'logging.StreamHandler')
            self.assertNotIn('filename', handler)

    def _assert_unique_handler_references(self, settings):
        handlers = settings.LOGGING['handlers']
        for logger_name, logger in settings.LOGGING['loggers'].items():
            with self.subTest(logger=logger_name):
                self.assertEqual(
                    len(logger['handlers']), len(set(logger['handlers']))
                )
                self.assertTrue(set(logger['handlers']) <= set(handlers))
        root = settings.LOGGING.get('root')
        if root is not None:
            self.assertEqual(len(root['handlers']), len(set(root['handlers'])))
            self.assertTrue(set(root['handlers']) <= set(handlers))

    def test_stdout_mode_has_no_file_handlers(self):
        settings = _load_settings(_PROD_SQLITE_ENV)
        self.assertNotIn('info_file', settings.LOGGING['handlers'])
        self._assert_no_file_handlers(settings)
        self._assert_unique_handler_references(settings)
        self.assertEqual(
            settings.LOGGING['loggers']['django']['level'], 'INFO'
        )
        self.assertEqual(settings.LOGGING['root']['level'], 'WARNING')

    def test_stdout_mode_wins_in_development_without_duplicates(self):
        settings = _load_settings({'DEBUG': 'True', 'LOG_TO_STDOUT': '1'})
        self._assert_no_file_handlers(settings)
        self._assert_unique_handler_references(settings)

    def test_development_logging_is_unchanged_when_disabled(self):
        settings = _load_settings({'DEBUG': 'True'})
        handlers = settings.LOGGING['handlers']
        self.assertEqual(set(handlers), {'console', 'server_console'})
        for handler in handlers.values():
            self.assertNotIn('filename', handler)
        self._assert_unique_handler_references(settings)

    def test_production_file_logging_is_preserved_when_disabled(self):
        settings = _load_settings(
            {**_PROD_SQLITE_ENV, 'LOG_TO_STDOUT': '0'},
            patch_log_dir=True,
        )
        handlers = settings.LOGGING['handlers']
        self.assertTrue(
            {'info_file', 'warning_file', 'error_file'} <= set(handlers)
        )
        for name in ('info_file', 'warning_file', 'error_file'):
            self.assertIn('filename', handlers[name])
        self._assert_unique_handler_references(settings)


class StaticFilesAndProxyHeaderTests(SimpleTestCase):
    def test_default_static_root_is_project_staticfiles(self):
        settings = _load_settings({'DEBUG': 'True'})
        self.assertEqual(Path(settings.STATIC_ROOT), PROJECT_ROOT / 'staticfiles')

    def test_custom_static_root_is_respected(self):
        settings = _load_settings({
            'DEBUG': 'True',
            'STATIC_ROOT': '/app/runtime/staticfiles',
        })
        self.assertEqual(Path(settings.STATIC_ROOT), Path('/app/runtime/staticfiles'))

    def test_production_configures_proxy_ssl_header(self):
        settings = _load_settings(_PROD_SQLITE_ENV)
        self.assertEqual(
            settings.SECURE_PROXY_SSL_HEADER,
            ('HTTP_X_FORWARDED_PROTO', 'https'),
        )

    def test_development_does_not_configure_proxy_ssl_header(self):
        # The module itself never sets the setting outside production, so
        # Django's global default (None) applies.
        settings = _load_settings({'DEBUG': 'True'})
        self.assertFalse(hasattr(settings, 'SECURE_PROXY_SSL_HEADER'))


class GunicornConfigurationTests(SimpleTestCase):
    def test_defaults_are_preserved_except_timeout(self):
        config = _load_gunicorn({})
        self.assertEqual(config.worker_class, 'gthread')
        self.assertEqual(config.threads, 4)
        self.assertEqual(config.workers, 1)
        self.assertEqual(config.timeout, 120)
        self.assertEqual(config.keepalive, 5)
        self.assertEqual(config.bind, '0.0.0.0:10000')
        self.assertEqual(config.accesslog, '-')
        self.assertEqual(config.errorlog, '-')

    def test_environment_overrides_are_honored(self):
        config = _load_gunicorn({
            'GUNICORN_TIMEOUT': '45',
            'WEB_CONCURRENCY': '3',
            'PORT': '8000',
        })
        self.assertEqual(config.timeout, 45)
        self.assertEqual(config.workers, 3)
        self.assertEqual(config.bind, '0.0.0.0:8000')
