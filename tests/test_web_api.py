from __future__ import annotations

import unittest
from pathlib import Path

from fastapi.middleware.gzip import GZipMiddleware

from app.control import create_app
from app.version import __version__


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.routes = {route.path: route for route in cls.app.routes}

    def test_static_frontend_and_admin_routes_exist(self) -> None:
        self.assertIn("/static", self.routes)
        self.assertIn("/chat", self.routes)
        self.assertIn("/admin", self.routes)

    def test_api_schema_uses_application_version(self) -> None:
        self.assertEqual(self.app.version, __version__)

    def test_operational_api_routes_exist(self) -> None:
        expected = {
            "/api/sources",
            "/api/users",
            "/api/backups",
            "/api/services",
            "/api/mcp",
            "/api/jobs",
            "/api/audit",
            "/api/chat/stream",
            "/api/chat/sessions",
            "/api/dashboard",
            "/api/connect-diagnostics/modules",
            "/api/connect-diagnostics/modules/{module_id}",
            "/api/integrations/netbox",
            "/api/integrations/openstack",
        }
        self.assertFalse(expected - self.routes.keys())

    def test_security_middleware_is_installed(self) -> None:
        self.assertGreaterEqual(len(self.app.user_middleware), 3)

    def test_gzip_and_csp_compatible_assets_are_configured(self) -> None:
        self.assertTrue(any(middleware.cls is GZipMiddleware for middleware in self.app.user_middleware))
        web_dir = Path(__file__).resolve().parents[1] / "app" / "web"
        self.assertNotIn("style=", (web_dir / "chat.html").read_text(encoding="utf-8"))
        self.assertNotIn("style=", (web_dir / "admin.html").read_text(encoding="utf-8"))
