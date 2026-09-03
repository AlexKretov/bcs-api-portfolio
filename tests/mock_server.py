"""Локальный мок BCS Trade API для тестов и проверки «без живого счёта».

Реализует ровно те контракты, что описаны в документации:

* ``POST /trade-api-keycloak/.../openid-connect/token`` — форма
  ``grant_type/refresh_token/client_id``, поворот refresh-токена в ответе;
* ``GET  /trade-api-bff-portfolio/api/v1/portfolio`` — массив позиций;
* ``GET  /trade-api-bff-limit/api/v1/limits`` — depoLimit/moneyLimits/…;
* ``POST /trade-api-bff-trade-details/api/v1/trades/search`` — {records,totalRecords,totalPages};
* ``POST /trade-api-bff-order-details/api/v1/orders/search`` — то же;
* ``POST /trade-api-bff-nontrade-operations/api/v1/operations/search``;
* ответы 401/429 в формате ошибок БКС.

Запуск вручную: ``python tests/mock_server.py --port 8765``.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # пакет виден при запуске скриптом

from bcs_api.demo import fake_portfolio_payload, fake_trades_payload

VALID_REFRESH = "valid-refresh-token"


def make_access_token(expires_in: int = 86400) -> str:
    """«JWT-подобная» строка: mock.<unixtime>.sig — чтобы можно было проверить срок жизни."""
    payload = json.dumps({"exp": int(time.time()) + expires_in}).encode()
    return "mock." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".sig"


class MockState:
    """Состояние мока: выданные токены, счётчики запросов, поведение."""

    def __init__(
        self,
        *,
        portfolio: Optional[list[dict[str, Any]]] = None,
        trades: Optional[dict[str, Any]] = None,
        total_rate_limit_hits: int = 1,
        refresh_token: str = VALID_REFRESH,
        rotate_refresh: bool = False,
    ) -> None:
        # Документация БКС не обещает «одноразовость» refresh-токена, поэтому по умолчанию
        # мок ведёт себя как обычный Bearer-сервер: токен можно использовать многократно.
        # rotate_refresh=True включает поведение Keycloak (rotation) — его тоже надо уметь пережить.
        self.rotate_refresh = rotate_refresh
        self.portfolio = portfolio if portfolio is not None else fake_portfolio_payload()
        self.trades = trades if trades is not None else fake_trades_payload(days=20)
        self.orders: dict[str, Any] = {
            "records": [
                {
                    "orderNum": 500001,
                    "orderId": "mock-order-1",
                    "ticker": "SBER",
                    "classCode": "TQBR",
                    "side": 1,
                    "price": 285.0,
                    "orderQuantity": 10,
                    "executedQuantity": 10,
                    "remainedQuantity": 0,
                    "averagePrice": 285.4,
                    "executedValue": 2854.0,
                    "orderStatus": 2,
                    "orderType": 2,
                    "settlementCurrency": "RUB",
                    "orderDateTime": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=45)).isoformat(),
                    "executionDateTime": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=44)).isoformat(),
                }
            ],
            "totalRecords": 1,
            "totalPages": 1,
        }
        self.operations: dict[str, Any] = {
            "records": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "date": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat(),
                    "ticker": "SBER",
                    "classCode": "TQBR",
                    "type": "Dividend",
                    "status": "Approved",
                    "sum": 1234.56,
                    "currency": "RUB",
                    "isin": "RU0009029557",
                    "issuerName": "ПАО Сбербанк",
                    "balanceChange": "Positive",
                }
            ],
            "pageSize": 1,
        }
        self.rate_limit_hits_left = total_rate_limit_hits
        self.expected_refresh = refresh_token
        self.tokens: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.auth_calls = 0
        self.refresh_rotations = 0
        self.returned_refresh: list[str] = []

    # ------------------------------------------------------------- behaviors

    def issue_token(self, refresh_token: str) -> Optional[dict[str, Any]]:
        self.auth_calls += 1
        if refresh_token != self.expected_refresh:
            return None
        self.refresh_rotations += 1
        new_refresh = f"{VALID_REFRESH}-r{self.refresh_rotations}" if self.rotate_refresh else refresh_token
        access = make_access_token()
        self.tokens.add(access)
        self.returned_refresh.append(new_refresh)
        if self.rotate_refresh:  # старый refresh становится невалидным, как у Keycloak
            self.expected_refresh = new_refresh
        return {
            "access_token": access,
            "expires_in": 86400,
            "refresh_expires_in": 90 * 24 * 3600,
            "refresh_token": new_refresh,
            "token_type": "bearer",
            "not-before-policy": "0",
            "session_state": "mock-session",
            "scope": "profile",
        }

    def authorized(self, header: Optional[str]) -> bool:
        return bool(header) and header.startswith("Bearer ") and header[7:] in self.tokens

    def record(self, method: str, path: str, *, body: Any = None, query: Any = None) -> None:
        self.requests.append({"method": method, "path": path, "body": body, "query": query, "at": time.time()})


class _Handler(BaseHTTPRequestHandler):
    state: MockState  # подменяется в MockServer.serve_forever

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:  # тихий сервер
        return

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if not raw:
            return {}
        ctype = self.headers.get("Content-Type", "")
        if "x-www-form-urlencoded" in ctype:
            return {k: v[0] for k, v in parse_qs(raw).items()}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def _send(self, code: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, type_name: str, text: str, *, retry_after: Optional[int] = None) -> None:
        payload: dict[str, Any] = {
            "timestamp": int(time.time() * 1000),
            "traceId": "mock-trace",
            "type": type_name,
            "errors": [{"type": type_name}],
            "displayOptions": {"text": text},
        }
        if retry_after is not None:
            self.send_response(code)
            self.send_header("Retry-After", str(retry_after))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(code, payload)

    def _paged(self, source: dict[str, Any], query: dict[str, list[str]], *, size_default: int = 50) -> None:
        records = list(source.get("records") or [])
        try:
            page = int((query.get("page") or ["0"])[0])
            size = int((query.get("size") or [str(size_default)])[0])
        except ValueError:
            return self._error(400, "VALIDATION_ERROR", "некорректная пагинация")
        if size < 1 or size > 100:
            return self._error(400, "VALIDATION_ERROR", "size должен быть 1..100")
        chunk = records[page * size : (page + 1) * size]
        self._send(
            200,
            {"records": chunk, "totalRecords": len(records), "totalPages": max(1, -(-len(records) // size))},
        )

    # --------------------------------------------------------------- routes

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        self.state.record("GET", path, query={k: v for k, v in parse_qs(parsed.query).items()})
        if path.endswith("/portfolio"):
            if not self._check_auth():
                return
            return self._send(200, self.state.portfolio)
        if path.endswith("/limits"):
            if not self._check_auth():
                return
            return self._send(200, self._limits())
        if path == "/health":
            return self._send(200, {"ok": True})
        return self._error(404, "NOT_FOUND", f"нет такого пути: {path}")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        self.state.record("POST", path, body=body, query={k: v for k, v in parse_qs(parsed.query).items()})

        if path.endswith("/openid-connect/token"):
            token = self.state.issue_token(str(body.get("refresh_token") or ""))
            if token is None:
                return self._send(
                    400,
                    {"error": "invalid_grant", "error_description": "Invalid refresh token (mock)"},
                )
            if body.get("grant_type") != "refresh_token" or body.get("client_id") not in (
                "trade-api-read",
                "trade-api-write",
            ):
                return self._send(400, {"error": "invalid_request", "error_description": "bad grant/client"})
            return self._send(200, token)

        if not self._check_auth():
            return
        if path.endswith("/trades/search"):
            return self._paged(self.state.trades, parse_qs(parsed.query))
        if path.endswith("/orders/search"):
            return self._paged(self.state.orders, parse_qs(parsed.query))
        if path.endswith("/operations/search"):
            if self.state.rate_limit_hits_left > 0:
                self.state.rate_limit_hits_left -= 1
                return self._error(429, "RESOURCE_EXHAUSTED", "слишком часто", retry_after=1)
            return self._paged(self.state.operations, parse_qs(parsed.query), size_default=100)
        if path.endswith("/instruments/by-tickers"):
            names = {p.get("ticker"): p.get("displayName") for p in self.state.portfolio if isinstance(p, dict)}
            wanted = body.get("tickers") if isinstance(body, dict) else None
            records = [{"ticker": t, "name": names.get(t) or t, "type": "stock"} for t in (wanted or []) if t in names]
            return self._send(200, records)
        return self._error(404, "NOT_FOUND", f"нет такого пути: {path}")

    # --------------------------------------------------------------- helpers

    def _check_auth(self) -> bool:
        if self.state.authorized(self.headers.get("Authorization")):
            return True
        self._error(
            401,
            "UNAUTHORIZED",
            "Пользователь не авторизован. Проверьте access токен и выполните запрос снова.",
        )
        return False

    def _limits(self) -> dict[str, Any]:
        depo, money_limits = [], []
        for item in self.state.portfolio:
            if item.get("type") == "moneyLimit":
                money_limits.append(
                    {
                        "exchange": item.get("exchange"),
                        "currencyCode": item.get("currency"),
                        "locked": item.get("locked"),
                        "averagePrice": None,
                        "instrumentType": "money",
                        "quantity": {"type": item.get("term"), "value": item.get("quantity")},
                        "loadDate": item.get("loadDate"),
                    }
                )
            elif item.get("type") == "depoLimit":
                depo.append(
                    {
                        "ticker": item.get("ticker"),
                        "classCode": item.get("board"),
                        "exchange": item.get("exchange"),
                        "averagePrice": item.get("balancePrice"),
                        "quantity": {"type": item.get("term"), "value": item.get("quantity")},
                        "quantityBatch": {"type": item.get("term"), "value": item.get("quantity")},
                        "instrumentType": item.get("instrumentType"),
                        "loadDate": item.get("loadDate"),
                        "lockedBuyValue": 0,
                        "lockedSellValue": 0,
                        "lockedBuyQuantity": 0,
                        "lockedSellQuantity": 0,
                    }
                )
        return {"depoLimit": depo, "moneyLimits": money_limits, "futuresLimits": [], "futureHolding": []}


class MockServer:
    """Контекстный менеджер: поднять мок на свободном порту."""

    def __init__(self, state: Optional[MockState] = None, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = state or MockState()
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def auth_url(self) -> str:
        return f"{self.base_url}/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token"

    def __enter__(self) -> MockServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Мок BCS Trade API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-token", default=VALID_REFRESH, help="какой refresh-токен считать валидным")
    parser.add_argument(
        "--rotate-refresh", action="store_true", help="эмулировать rotation refresh-токена (поведение Keycloak)"
    )
    args = parser.parse_args()
    server = MockServer(
        MockState(refresh_token=args.refresh_token, rotate_refresh=args.rotate_refresh), host=args.host, port=args.port
    )
    print(f"mock BCS API: {server.base_url}")
    print(f"  auth:     {server.auth_url}")
    print(f"  refresh:  {args.refresh_token}")
    print("  example:  BCS_API_BASE_URL=" + server.base_url + " BCS_REFRESH_TOKEN=" + args.refresh_token)
    print("            python3 -m bcs_api portfolio")
    try:
        server.thread.start()
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
