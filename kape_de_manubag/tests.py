"""
Tests for the public /health/ endpoint and route integrity.

Note: these tests deliberately avoid rendering HTML templates through the
test client. The project's Django 4.2.16 + Python 3.14 local environment has
a pre-existing incompatibility that crashes template rendering inside the
test client (``BaseContext.__copy__``), while the live server renders pages
fine. URL resolution/reversal and the health endpoint (pure JSON, no
template) are therefore used to prove route integrity.
"""
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse

from kape_de_manubag import health


class HealthCheckTests(TestCase):
    def test_health_endpoint_returns_ok(self):
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['database'], 'ok')
        self.assertEqual(payload['static'], 'ok')

    def test_health_payload_has_expected_fields(self):
        response = self.client.get(reverse('health'))
        payload = response.json()
        for key in ('status', 'version', 'database', 'static', 'server_time'):
            self.assertIn(key, payload)
        # Never leaks internals: only simple JSON-safe values.
        for value in payload.values():
            self.assertIsInstance(value, (str, int, float, bool, type(None)))

    def test_health_does_not_require_login(self):
        response = self.client.get('/health/')
        # Not redirected to the login page and not forbidden.
        self.assertNotEqual(response.status_code, 302)
        self.assertEqual(response.status_code, 200)

    def test_health_is_not_cached(self):
        response = self.client.get('/health/')
        self.assertEqual(response['Cache-Control'], 'no-store')

    @mock.patch('kape_de_manubag.health._database_available', return_value=False)
    @mock.patch('kape_de_manubag.health._static_available', return_value=True)
    def test_health_is_degraded_when_database_down(self, *_mocks):
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload['status'], 'degraded')
        self.assertEqual(payload['database'], 'unavailable')

    @mock.patch('kape_de_manubag.health._database_available', return_value=True)
    @mock.patch('kape_de_manubag.health._static_available', return_value=False)
    def test_health_is_degraded_when_static_unavailable(self, *_mocks):
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload['status'], 'degraded')
        self.assertEqual(payload['static'], 'unavailable')


class RouteIntegrityTests(SimpleTestCase):
    """Ensure the new /health/ route did not disturb existing routes."""

    def test_existing_route_names_still_reverse(self):
        for name in ('menu:index', 'accounts:login', 'orders:pos', 'admin:index'):
            self.assertTrue(reverse(name))

    def test_health_route_resolves_to_health_view(self):
        match = resolve('/health/')
        self.assertIs(match.func, health.health_check)
        self.assertEqual(match.url_name, 'health')

    def test_menu_index_route_is_unchanged(self):
        match = resolve('/')
        self.assertIsNot(match.func, health.health_check)
        self.assertNotEqual(match.url_name, 'health')

    def test_login_route_is_unchanged(self):
        match = resolve('/accounts/login/')
        self.assertIsNot(match.func, health.health_check)
        self.assertNotEqual(match.url_name, 'health')

    def test_admin_route_is_unchanged(self):
        match = resolve('/admin/login/')
        self.assertIsNot(match.func, health.health_check)
        self.assertNotEqual(match.url_name, 'health')

    def test_pos_route_is_unchanged(self):
        match = resolve('/orders/pos/')
        self.assertIsNot(match.func, health.health_check)
        self.assertNotEqual(match.url_name, 'health')
