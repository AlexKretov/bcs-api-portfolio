"""Тесты веб-интерфейса: демо-режим + интеграция через мок BCS API.

Запуск:  python -m pytest tests/test_web.py -q
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import mock_server

from bcs_api.web import WebServer


class _WebClient:
    """Мини-клиент для тестов: POST/GET к веб-серверу."""

    def __init__(self, server: WebServer) -> None:
        self.server = server

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    def post(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=30) as resp:
            return resp.status, resp.read()


class _WebServerCtx:
    def __init__(self, *, mode: str | None = None) -> None:
        self._server: WebServer | None = None
        self.mode = mode

    def __enter__(self) -> _WebClient:
        self._server = WebServer(host="127.0.0.1", port=0, mode=self.mode)
        self._server.start()
        return _WebClient(self._server)

    def __exit__(self, *exc: object) -> None:
        if self._server:
            self._server.stop()


class WebDemoTests(unittest.TestCase):
    def test_index_page_is_served(self) -> None:
        with _WebServerCtx() as client:
            status, body = client.get("/")
            self.assertEqual(status, 200)
            html = body.decode("utf-8")
            self.assertIn("БКС Портфель", html)
            self.assertIn("btn-portfolio", html)
            self.assertIn("btn-export", html)

    def test_health_and_settings_roundtrip(self) -> None:
        with _WebServerCtx() as client:
            status, _ = client.get("/api/health")
            self.assertEqual(status, 200)
            data = client.post("/api/settings", {"action": "get"})
            self.assertTrue(data["ok"])
            self.assertEqual(data["settings"]["mode"], "demo")
            self.assertFalse(data["settings"]["token_set"])
            data = client.post(
                "/api/settings",
                {"action": "set", "values": {"mode": "live", "client_id": "trade-api-write"}},
            )
            self.assertTrue(data["ok"])
            self.assertEqual(data["settings"]["mode"], "live")
            self.assertEqual(data["settings"]["client_id"], "trade-api-write")

    def test_demo_status_and_token_check(self) -> None:
        with _WebServerCtx() as client:
            status = client.post("/api/status", {"check": True})
            self.assertTrue(status["ok"])
            self.assertTrue(status["demo"])
            check = client.post("/api/token/check", {})
            self.assertTrue(check["ok"])
            self.assertTrue(check["demo"])

    def test_demo_portfolio_renders_all_sections(self) -> None:
        with _WebServerCtx() as client:
            data = client.post("/api/portfolio", {"term": "T0"})
            self.assertTrue(data["ok"])
            self.assertEqual(data["mode"], "demo")
            self.assertGreater(data["summary"]["positions"], 0)
            self.assertGreater(data["summary"]["total_value_rub"], 0)
            self.assertGreater(len(data["positions"]), 0)
            self.assertGreater(len(data["cash"]), 0)
            self.assertTrue(all("unrealized_pl" in p for p in data["positions"]))

            limits = client.post("/api/limits", {})
            self.assertTrue(limits["ok"])
            self.assertGreater(len(limits["securities"]), 0)
            self.assertGreater(len(limits["money"]), 0)

            trades = client.post("/api/trades", {"days": 30})
            self.assertTrue(trades["ok"])
            self.assertGreater(trades["total"], 0)
            self.assertIn("buys", trades["stats"])

            orders = client.post("/api/orders", {"days": 30})
            self.assertTrue(orders["ok"])
            self.assertGreater(orders["total"], 0)

            operations = client.post("/api/operations", {"days": 90})
            self.assertTrue(operations["ok"])
            self.assertGreater(operations["total"], 0)

    def test_demo_raw_sections(self) -> None:
        with _WebServerCtx() as client:
            for section in ("portfolio", "limits", "trades", "orders", "operations"):
                data = client.post("/api/raw", {"section": section})
                self.assertTrue(data["ok"], section)
                self.assertIsNotNone(data["raw"])
            data = client.post("/api/raw", {"section": "nope"})
            self.assertFalse(data["ok"])

    def test_demo_export_and_download(self) -> None:
        with _WebServerCtx() as client:
            data = client.post("/api/export", {"formats": "json,csv,md"})
            self.assertTrue(data["ok"], data)
            self.assertGreaterEqual(data["summary"]["files"], 3)
            for file in data["files"]:
                status, body = client.get(file["url"])
                self.assertEqual(status, 200, file["url"])
                self.assertGreater(len(body), 0)

    def test_download_traversal_is_blocked(self) -> None:
        with _WebServerCtx() as client:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                client.get("/api/download?file=..%2FREADME.md")
            self.assertEqual(ctx.exception.code, 404)

    def test_bad_filter_is_a_clean_error(self) -> None:
        with _WebServerCtx() as client:
            data = client.post("/api/trades", {"since": "позавчера"})
            self.assertFalse(data["ok"])
            self.assertEqual(data["error"]["kind"], "config")
            self.assertIn("не понял дату", data["error"]["message"])


class WebLiveTests(unittest.TestCase):
    def test_live_flow_against_mock(self) -> None:
        state = mock_server.MockState(rotate_refresh=True)
        with mock_server.MockServer(state) as mock:
            with tempfile.TemporaryDirectory() as tmp:
                client_w = _WebServerCtx()
                web = client_w.__enter__()
                try:
                    data = web.post(
                        "/api/settings",
                        {
                            "action": "set",
                            "values": {
                                "mode": "live",
                                "base_url": mock.base_url,
                                "refresh_token": state.expected_refresh,
                                "cache_path": str(Path(tmp) / "tokens.json"),
                                "rps": "50",
                            },
                        },
                    )
                    self.assertTrue(data["ok"], data)
                    self.assertTrue(data["settings"]["token_set"])
                    self.assertNotIn(state.expected_refresh, json.dumps(data), "токен не должен возвращаться клиенту")

                    status = web.post("/api/status", {"check": True})
                    self.assertTrue(status["ok"], status)
                    self.assertTrue(status["access"]["obtained"])
                    self.assertGreater(status["access"]["ttl_h"], 0)
                    self.assertNotIn(state.expected_refresh, json.dumps(status))

                    portfolio = web.post("/api/portfolio", {})
                    self.assertTrue(portfolio["ok"])
                    self.assertEqual(portfolio["mode"], "live")
                    self.assertGreater(portfolio["summary"]["positions"], 0)

                    limits = web.post("/api/limits", {})
                    self.assertTrue(limits["ok"])
                    self.assertGreater(len(limits["securities"]), 0)

                    trades = web.post("/api/trades", {"days": 30})
                    self.assertTrue(trades["ok"])
                    self.assertGreaterEqual(trades["total"], 1)

                    orders = web.post("/api/orders", {"days": 30})
                    self.assertTrue(orders["ok"])
                    self.assertEqual(orders["total"], 1)

                    check = web.post("/api/token/check", {})
                    self.assertTrue(check["ok"])
                    self.assertFalse(check["demo"])
                    self.assertGreaterEqual(len(check["reports"]), 1)
                    self.assertNotIn(state.expected_refresh, json.dumps(check), "секрет не показывается")
                finally:
                    client_w.__exit__()

    def test_auth_error_is_typed_and_has_hint(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as mock:
            with _WebServerCtx() as web:
                web.post(
                    "/api/settings",
                    {
                        "action": "set",
                        "values": {
                            "mode": "live",
                            "base_url": mock.base_url,
                            "refresh_token": "bad-token",
                            "cache_path": str(Path(tempfile.mkdtemp()) / "tokens.json"),
                        },
                    },
                )
                data = web.post("/api/portfolio", {})
                self.assertFalse(data["ok"])
                self.assertEqual(data["error"]["kind"], "auth")
                self.assertIn("hint", data["error"])
                self.assertNotIn("bad-token", json.dumps(data))

    def test_size_is_clamped_and_bad_side_is_clean_error(self) -> None:
        """WEB-слой ограничивает размер страницы 1..100; ошибки фильтров — без traceback."""
        state = mock_server.MockState()
        with mock_server.MockServer(state) as mock:
            with _WebServerCtx() as web:
                web.post(
                    "/api/settings",
                    {
                        "action": "set",
                        "values": {
                            "mode": "live",
                            "base_url": mock.base_url,
                            "refresh_token": state.expected_refresh,
                            "cache_path": str(Path(tempfile.mkdtemp()) / "tokens.json"),
                            "rps": "50",
                        },
                    },
                )
                data = web.post("/api/trades", {"size": 500})
                self.assertTrue(data["ok"])  # 500 → 100, как в CLI
                self.assertLessEqual(len(data["trades"]), 100)
                data = web.post("/api/trades", {"side": "maybe"})
                self.assertFalse(data["ok"])
                self.assertEqual(data["error"]["kind"], "config")
                self.assertIn("не понял направление", data["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
