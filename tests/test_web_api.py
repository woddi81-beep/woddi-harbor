from __future__ import annotations

import unittest

from app.control import create_app


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.routes = {route.path: route for route in cls.app.routes}

    def test_static_frontend_and_admin_routes_exist(self) -> None:
        self.assertIn("/static", self.routes)
        self.assertIn("/chat", self.routes)
        self.assertIn("/admin", self.routes)

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
        }
        self.assertFalse(expected - self.routes.keys())

    def test_security_middleware_is_installed(self) -> None:
        self.assertGreaterEqual(len(self.app.user_middleware), 2)
