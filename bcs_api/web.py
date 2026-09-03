"""Веб-интерфейс для BCS Trade API: портфель, лимиты, сделки, заявки, операции.

Сервер на стандартной библиотеке (``http.server``) — новых зависимостей не нужно.
Всё управление — кнопками в браузере; данные отдаются JSON-эндпоинтами, а
одностраничный интерфейс лежит в ``bcs_api/webapp/index.html``.

Режимы работы:

* ``demo``  — синтетические данные из :mod:`bcs_api.demo`, без сети и токена
  (режим по умолчанию, если рядом нет ни ``bcs-config.json``, ни
  ``BCS_REFRESH_TOKEN``). Позволяет посмотреть весь интерфейс сразу;
* ``live`` — реальный БКС Торговый API: refresh-токен берётся из веб-формы,
  переменной окружения ``BCS_REFRESH_TOKEN`` или ``bcs-config.json``.

Запуск::

    python -m bcs_api web --port 8080
    # или
    BCS_REFRESH_TOKEN='...' python -m bcs_api web --mode live --port 8080

Секреты: refresh-токен из веб-формы хранится **только в памяти** сервера и
никогда не возвращается клиенту обратно и не пишется в журнал. Управление
кэшем токенов — как в CLI (файл ``.bcs-tokens.json``, права ``0600``).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import mimetypes
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, urlparse

from .client import (
    BcsClient,
    Order,
    Portfolio,
    load_config,
    mask_secret,
)
<<<<<<< ours
from .pnl import calculate_pnl
=======
from .pnl import calculate_pnl, signed_operation_sum
>>>>>>> theirs
from .demo import (
    fake_limits_payload,
    fake_operations,
    fake_operations_payload,
    fake_orders,
    fake_orders_payload,
    fake_portfolio,
    fake_portfolio_payload,
    fake_trades,
    fake_trades_payload,
)
from .diagnostics import inspect_token, scan_config_files
from .errors import ApiError, AuthError, BcsError, RateLimitError, UnauthorizedError, ValidationError
from .export import save_report
from .formatting import short_datetime

log = logging.getLogger("bcs.web")

CLIENT_IDS = ("trade-api-read", "trade-api-write")
FORMATS = ("json", "csv", "md")

MAX_LOG = 300


# --------------------------------------------------------------------- settings


@dataclass
class Settings:
    """Настройки сервера. Пустая строка/None — «взять значение по умолчанию»."""

    mode: str = "demo"  # demo | live
    refresh_token: str = ""
    token_from_form: bool = False
    client_id: str = ""
    base_url: str = ""
    cache_path: str = ""
    timeout: str = ""
    max_retries: str = ""
    rps: str = ""
    export_dir: str = "reports"
    lock: threading.Lock = field(default_factory=threading.Lock)

    def sanitized(self) -> dict[str, Any]:
        """Для клиента: без значения токена и с флагом «задан ли он»."""
        return {
            "mode": self.mode,
            "client_id": self.client_id,
            "base_url": self.base_url,
            "cache_path": self.cache_path,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "rps": self.rps,
            "export_dir": self.export_dir,
            "token_set": bool(self.refresh_token and self.token_from_form),
            "token_source": "веб-форма" if self.token_from_form else "окружение / bcs-config.json",
        }

    def update(self, values: dict[str, Any]) -> list[str]:
        """Применить значения из веб-формы; вернуть список замечаний."""
        notes: list[str] = []
        mode = str(values.get("mode", self.mode)).strip().lower()
        if mode not in ("demo", "live"):
            raise ValueError("режим должен быть demo или live")
        self.mode = mode

        client_id = str(values.get("client_id", "") or "").strip()
        if client_id and client_id not in CLIENT_IDS:
            raise ValueError(f"client_id должен быть одним из {CLIENT_IDS}")
        self.client_id = client_id

        for key in ("base_url", "cache_path", "timeout", "max_retries", "rps", "export_dir"):
            value = str(values.get(key, "") or "").strip()
            if key == "export_dir" and not value:
                continue  # не затирать значение по умолчанию «reports»
            if key in ("timeout", "max_retries", "rps") and value:
                try:
                    float(value) if key != "max_retries" else int(value)
                except ValueError as exc:
                    raise ValueError(f"поле «{key}» должно быть числом") from exc
            if key == "export_dir":
                self._check_export_dir(value)
            setattr(self, key, value)

        token = str(values.get("refresh_token", "") or "").strip()
        if token:
            self.refresh_token = token
            self.token_from_form = True
            notes.append("refresh-токен принят и хранится в памяти сервера (на диск не пишется)")
        elif "refresh_token" in values:
            self.refresh_token = ""
            self.token_from_form = False
        return notes

    @staticmethod
    def _check_export_dir(value: str) -> None:
        path = Path(value).expanduser()
        if not path.is_absolute() and ".." in path.parts:
            raise ValueError("папка экспорта не может выходить за пределы рабочей папки")

    def as_client_kwargs(self) -> dict[str, Any]:
        """Аргументы для :class:`BcsClient` — только заданные пользователем значения."""
        kwargs: dict[str, Any] = {}
        if self.token_from_form and self.refresh_token:
            kwargs["refresh_token"] = self.refresh_token
            kwargs["refresh_token_source"] = "веб-форма"
        if self.client_id:
            kwargs["client_id"] = self.client_id
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.cache_path:
            kwargs["cache_path"] = self.cache_path
        if self.timeout:
            kwargs["timeout"] = float(self.timeout)
        if self.max_retries:
            kwargs["max_retries"] = int(self.max_retries)
        if self.rps:
            kwargs["rps"] = float(self.rps)
        return kwargs


def default_mode() -> str:
    """``live``, если refresh-токен уже доступен из env/конфига, иначе ``demo``."""
    try:
        cfg = load_config()
    except Exception:  # битый конфиг не должен мешать старту веб-интерфейса
        return "demo"
    return "live" if cfg.get("refresh_token") else "demo"


# --------------------------------------------------------------------- helpers


def _now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso(value: Any) -> Optional[str]:
    """Принять ISO/дату из формы и вернуть ``YYYY-MM-DDTHH:MM:SS`` (локальная зона)."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(text, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"не понял дату {text!r}; ожидается ГГГГ-ММ-ДД или ГГГГ-ММ-ДД ЧЧ:ММ")


def _resolve_range(body: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    days = body.get("days")
    since = _iso(body.get("since")) if body.get("since") else None
    if not since and days:
        start = dt.datetime.now() - dt.timedelta(days=int(days))
        since = start.strftime("%Y-%m-%dT%H:%M:%S")
    until = _iso(body.get("until")) if body.get("until") else None
    return since, until


def _split(value: Any) -> Optional[list[str]]:
    """'SBER, LKOH' или ['SBER', 'LKOH'] → список тикеров/кодов."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        out = [str(x).strip().upper() for x in value if str(x).strip()]
    else:
        out = [x.strip().upper() for x in str(value).replace(";", ",").split(",") if x.strip()]
    return out or None


def _size(body: dict[str, Any], default: int = 100) -> int:
    try:
        size = int(body.get("size") or default)
    except (TypeError, ValueError) as exc:
        raise ValueError("размер страницы должен быть целым числом") from exc
    return max(1, min(size, 100))


def _days(body: dict[str, Any], default: int = 30) -> int:
    try:
        return max(1, int(body.get("days") or default))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------- payloads


def portfolio_payload(portfolio: Portfolio, *, source: str) -> dict[str, Any]:
    positions = sorted(portfolio.positions, key=lambda p: -p.current_value_rub)
    cash = sorted(portfolio.cash, key=lambda c: -c.current_value_rub)
    by_type = portfolio.by_type()
    by_type_grouped = portfolio.positions_by_type()
    total = portfolio.securities_value_rub or 1.0
    return {
        "ok": True,
        "mode": source,
        "as_of": portfolio.as_of.isoformat() if portfolio.as_of else None,
        "summary": {
            "positions": len(positions),
            "securities_value_rub": round(portfolio.securities_value_rub, 2),
            "cash_rub": round(portfolio.cash_rub, 2),
            "total_value_rub": round(portfolio.total_value_rub, 2),
            "unrealized_pl": round(portfolio.total_unrealized_pl, 2),
            "daily_pl": round(portfolio.total_daily_pl, 2),
        },
        "cash": [
            {
                "currency": c.currency or "—",
                "quantity": c.quantity,
                "locked": c.locked,
                "available": round(c.available, 6),
                "value_rub": round(c.current_value_rub, 2),
            }
            for c in cash
        ],
        "positions": [
            {
                "ticker": p.ticker,
                "name": p.display_name or p.ticker,
                "type": p.type_label,
                "currency": p.currency,
                "board": p.board,
                "term": p.term,
                "quantity": p.quantity,
                "lots": p.lots,
                "locked": p.locked,
                "balance_price": p.balance_price,
                "current_price": p.current_price,
                "current_value_rub": round(p.current_value_rub, 2),
                "unrealized_pl": round(p.unrealized_pl, 2),
                "unrealized_percent_pl": round(p.unrealized_percent_pl, 2),
                "daily_pl": round(p.daily_pl, 2),
                "daily_percent_pl": round(p.daily_percent_pl, 2),
                "portfolio_share": round(p.portfolio_share, 4),
                "accrued_income": round(p.accrued_income, 2),
                "is_blocked": p.is_blocked,
            }
            for p in positions
        ],
        "positions_by_type": [
            {
                "class": group["class"],
                "value_rub": group["value_rub"],
                "unrealized_pl": group["unrealized_pl"],
                "daily_pl": group["daily_pl"],
                "share": group["share"],
                "count": group["count"],
                "positions": [
                    {
                        "ticker": p.ticker,
                        "name": p.display_name or p.ticker,
                        "type": p.type_label,
                        "currency": p.currency,
                        "board": p.board,
                        "term": p.term,
                        "quantity": p.quantity,
                        "lots": p.lots,
                        "locked": p.locked,
                        "balance_price": p.balance_price,
                        "current_price": p.current_price,
                        "current_value_rub": round(p.current_value_rub, 2),
                        "unrealized_pl": round(p.unrealized_pl, 2),
                        "unrealized_percent_pl": round(p.unrealized_percent_pl, 2),
                        "daily_pl": round(p.daily_pl, 2),
                        "daily_percent_pl": round(p.daily_percent_pl, 2),
                        "portfolio_share": round(p.portfolio_share, 4),
                        "accrued_income": round(p.accrued_income, 2),
                        "is_blocked": p.is_blocked,
                    }
                    for p in group["positions"]
                ],
            }
            for group in by_type_grouped
        ],
        "by_type": [
            {"name": name, "value": round(value, 2), "share": round(value / total * 100, 2)}
            for name, value in by_type.items()
        ],
        "note": (
            f"Режим расчётов term={positions[0].term or '—'}" if positions and positions[0].term else ""
        ),
    }


def deduplicate_money_limits(money_limits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for m in money_limits:
        if not isinstance(m, dict):
            continue
        curr = str(m.get("currencyCode") or "—").strip().upper()
        if curr not in seen:
            seen[curr] = dict(m)
        else:
            existing = seen[curr]
            ex_qty = _qty_num(existing.get("quantity")) or 0.0
            new_qty = _qty_num(m.get("quantity")) or 0.0
            if ex_qty == new_qty and _num_any(existing.get("locked")) == _num_any(m.get("locked")):
                pass
            else:
                existing_q = existing.get("quantity") if isinstance(existing.get("quantity"), dict) else {}
                existing["quantity"] = {"type": existing_q.get("type"), "value": ex_qty + new_qty}
                existing["locked"] = (_num_any(existing.get("locked")) or 0.0) + (_num_any(m.get("locked")) or 0.0)
    return list(seen.values())


def limits_payload(limits: dict[str, Any], *, source: str) -> dict[str, Any]:
    depo = limits.get("depoLimit") or []
    money_limits = deduplicate_money_limits(limits.get("moneyLimits") or [])
    futures = limits.get("futuresLimits") or []
    holdings = limits.get("futureHolding") or []

    stamp = _fmt_dt((money_limits[0] or {}).get("loadDate")) if money_limits else None
    return {
        "ok": True,
        "mode": source,
        "as_of": stamp,
        "money": [
            {
                "currency": (m.get("currencyCode") or "—"),
                "exchange": (m.get("exchange") or "—"),
                "total": _qty_num(m.get("quantity")),
                "locked": _num_any(m.get("locked")),
                "free": _free_num(m.get("quantity"), _num_any(m.get("locked"))),
                "updated": _fmt_dt(m.get("loadDate")),
            }
            for m in money_limits
        ],
        "securities": [
            {
                "ticker": (d.get("ticker") or "—"),
                "class_code": (d.get("classCode") or "—"),
                "quantity": _qty_num(d.get("quantity")),
                "lots": _qty_num(d.get("quantityBatch")),
                "average_price": _num_any(d.get("averagePrice")),
                "type": _qty_type(d.get("quantity")),
                "instrument_type": (d.get("instrumentType") or "—"),
            }
            for d in depo
        ],
        "futures": [
            {
                "currency": (f.get("currencyCode") or "—"),
                "cbp_limit": _num_any(f.get("cbpLimit")),
                "cbpl_used": _num_any(f.get("cbplUsed")),
                "cbpl_planned": _num_any(f.get("cbplPlanned")),
                "var_margin": _num_any(f.get("varMargin")),
                "real_var_margin": _num_any(f.get("realVarMargin")),
            }
            for f in futures
        ],
        "holdings": [
            {
                "ticker": (h.get("ticker") or "—"),
                "position": _num_any(h.get("totalNet")),
                "average_price": _num_any(h.get("averagePrice")),
                "value": _num_any(h.get("positionValue")),
                "var_margin": _num_any(h.get("varMargin")),
            }
            for h in holdings
        ],
    }


def trades_payload(trades: Sequence[Any], *, total: int, shown: int, source: str, note: str = "") -> dict[str, Any]:
    buys = sum(1 for t in trades if str(t.side) == "1")
    currencies = {t.settlement_currency for t in trades if t.settlement_currency}
    volume = sum(t.volume or 0.0 for t in trades) if len(currencies) <= 1 else None
    dates = [t.date_time for t in trades if t.date_time]
    return {
        "ok": True,
        "mode": source,
        "total": total,
        "returned": len(trades),
        "shown": shown,
        "stats": {
            "buys": buys,
            "sells": len(trades) - buys,
            "volume": round(volume, 2) if volume is not None else None,
            "currencies": sorted(currencies),
            "first": short_datetime(min(dates)) if dates else None,
            "last": short_datetime(max(dates)) if dates else None,
        },
        "trades": [
            {
                "date": short_datetime(t.date_time),
                "ticker": t.ticker,
                "class_code": t.class_code or "—",
                "side": t.side,
                "side_label": t.side_label,
                "quantity": t.trade_quantity,
                "lots": t.trade_quantity_lots,
                "price": t.price,
                "volume": t.volume,
                "currency": t.settlement_currency or "—",
                "instrument_type": t.type_label,
                "order_num": t.order_num,
                "trade_num": t.trade_num,
                "settle_date": t.settle_date,
            }
            for t in trades
        ],
        "note": note or "",
    }


def order_row(order: Order) -> dict[str, Any]:
    return {
        "date": short_datetime(order.order_date_time),
        "execution_date": short_datetime(order.execution_date_time),
        "ticker": order.ticker or "—",
        "class_code": order.class_code or "—",
        "side_label": order.side_label,
        "type_label": order.type_label,
        "price": order.price,
        "order_quantity": order.order_quantity,
        "executed_quantity": order.executed_quantity,
        "remained_quantity": order.remained_quantity,
        "average_price": order.average_price,
        "status_label": order.status_label,
        "currency": order.settlement_currency or "—",
        "order_num": order.order_num,
        "reject_reason": order.reject_reason,
    }


def _fmt_dt(value: Any) -> Optional[str]:
    from .client import parse_datetime

    parsed = parse_datetime(value)
    return short_datetime(parsed) if parsed else None


def _qty_num(quantity: Any) -> Optional[float]:
    """``value`` из вложенного объекта ``quantity`` сервиса «Лимиты»."""
    if not isinstance(quantity, dict):
        return None
    return _num_any(quantity.get("value"))


def _free_num(quantity: Any, locked: Optional[float]) -> Optional[float]:
    total = _qty_num(quantity)
    if total is None:
        return None
    return total - (locked or 0.0)


def _qty_type(quantity: Any) -> str:
    if not isinstance(quantity, dict):
        return "—"
    return str(quantity.get("type") or "—")


def _num_any(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def operations_rows(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for x in records:
        amount = x.get("sum")
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        op_type = str(x.get("type") or "—").strip()
        balance_change = str(x.get("balanceChange") or x.get("balance_change") or "—").strip()
        signed_sum = signed_operation_sum(amount, balance_change=balance_change, op_type=op_type)
        out.append(
            {
                "date": _fmt_dt(x.get("date")) or str(x.get("date") or "—"),
                "type": op_type,
                "status": x.get("status") or "—",
                "ticker": x.get("ticker") or "—",
                "isin": x.get("isin") or "—",
                "issuer": x.get("issuerName") or "—",
                "sum": signed_sum,
                "currency": x.get("currency") or "—",
                "balance_change": balance_change,
            }
        )
    return out


# ------------------------------------------------------------------ commands


def cmd_portfolio(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    term_value = str(body.get("term") or "T0")
    term = None if term_value.lower() in ("all", "none", "") else term_value
    top = int(body.get("top") or 0)
    if app.settings.mode == "demo":
        portfolio = fake_portfolio()
        source = "demo"
    else:
        client = app.make_client()
        raw = client.get_portfolio_raw()
        portfolio = Portfolio.from_api(raw, term=term)
        if body.get("include_names", True) and portfolio.positions:
            names = client.instrument_names([p.ticker for p in portfolio.positions])
            for pos in portfolio.positions:
                if not pos.display_name and names.get(pos.ticker):
                    pos.display_name = names[pos.ticker]
        source = "live"
    if top:
        portfolio.positions = portfolio.top_positions(top)
    return portfolio_payload(portfolio, source=source)


def cmd_limits(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    if app.settings.mode == "demo":
        return limits_payload(fake_limits_payload(), source="demo")
    client = app.make_client()
    return limits_payload(client.get_limits(), source="live")


def cmd_trades(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    since, until = _resolve_range(body)
    tickers = _split(body.get("tickers"))
    class_codes = _split(body.get("class_codes"))
    side = body.get("side") or None
    size = _size(body)
    all_pages = bool(body.get("all_pages"))
    max_pages = int(body.get("max_pages") or 200)
    limit = body.get("limit")
    sort = body.get("sort") or ["tradeDateTime,desc"]

    if app.settings.mode == "demo":
        trades = fake_trades(days=_days(body))
        total, shown = len(trades), len(trades)
        if limit:
            trades = trades[: int(limit)]
        note = "демо-данные: сделки сгенерированы без обращения к API"
        return trades_payload(trades, total=total, shown=shown, source="demo", note=note)

    client = app.make_client()
    common: dict[str, Any] = {
        "since": since,
        "until": until,
        "tickers": tickers,
        "class_codes": class_codes,
        "side": side,
        "sort": sort,
    }
    if all_pages:
        trades = list(client.iter_trades(**common, size=size, max_pages=max_pages, limit=limit))
        total, shown = len(trades), len(trades)
    else:
        page = client.search_trades(**common, page=0, size=size)
        trades, total, shown = page.records, page.total_records, len(page.records)
        if limit:
            trades = trades[: int(limit)]
    note = (
        f"показана только первая страница ({shown} из {total}); включите «Все страницы»"
        if not all_pages and total > shown
        else ""
    )
    return trades_payload(trades, total=total, shown=shown, source="live", note=note)


def cmd_orders(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    since, until = _resolve_range(body)
    size = _size(body)
    statuses = body.get("status")
    if statuses:
        statuses = [int(x) for x in statuses]

    if app.settings.mode == "demo":
        orders = fake_orders(days=_days(body))
        source = "demo"
    else:
        client = app.make_client()
        orders = list(
            client.iter_orders(
                since=since,
                until=until,
                tickers=_split(body.get("tickers")),
                class_codes=_split(body.get("class_codes")),
                side=body.get("side") or None,
                order_status=statuses,
                size=size,
            )
        )
        source = "live"
    rows = [order_row(o) for o in orders]
    return {"ok": True, "mode": source, "total": len(rows), "orders": rows}


def cmd_operations(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    since, until = _resolve_range(body)
    size = _size(body)
    if app.settings.mode == "demo":
        records = fake_operations(days=_days(body))
        source = "demo"
    else:
        client = app.make_client()
        if bool(body.get("all_pages")):
            records = list(
                client.iter_operations(
                    since=since,
                    until=until,
                    operation_types=_split(body.get("types")),
                    statuses=_split(body.get("statuses")),
                    tickers=_split(body.get("tickers")),
                    size=size,
                )
            )
        else:
            data = client.search_operations(
                since=since,
                until=until,
                operation_types=_split(body.get("types")),
                statuses=_split(body.get("statuses")),
                tickers=_split(body.get("tickers")),
                page=0,
                size=size,
            )
            records = [x for x in (data.get("records") or []) if isinstance(x, dict)]
        source = "live"
    rows = operations_rows(records)
    return {"ok": True, "mode": source, "total": len(rows), "operations": rows}


def cmd_pnl(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    since, until = _resolve_range(body)
    asset_types = _split(body.get("asset_types")) or _split(body.get("types"))
    term_value = str(body.get("term") or "T0")
    term = None if term_value.lower() in ("all", "none", "") else term_value

    if app.settings.mode == "demo":
        portfolio = fake_portfolio()
        trades = fake_trades(days=_days(body))
        operations = fake_operations(days=_days(body))
        source = "demo"
    else:
        client = app.make_client()
        portfolio = client.get_portfolio(term=term)
        trades = list(client.iter_trades(since=since, until=until))
        operations = list(client.iter_operations(since=since, until=until))
        source = "live"

    pnl_data = calculate_pnl(
        portfolio=portfolio,
        trades=trades,
        operations=operations,
        asset_types=asset_types,
        since=since,
        until=until,
    )
    pnl_data["mode"] = source
    return pnl_data


def cmd_status(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    check = bool(body.get("check"))
    if app.settings.mode == "demo":
        return {
            "ok": True,
            "mode": "demo",
            "demo": True,
            "note": (
                "Демо-режим: данные синтетические, сеть и токен не используются. "
                "Переключитесь на «Боевой API», чтобы читать реальный счёт."
            ),
            "settings": app.settings.sanitized(),
        }
    client = app.make_client()
    configured = client.configured_refresh_token
    cached = client.store.get()
    cached_refresh = cached.refresh_token if cached else None
    out: dict[str, Any] = {
        "ok": True,
        "mode": "live",
        "demo": False,
        "settings": app.settings.sanitized(),
        "client": {
            "base_url": client.base_url,
            "client_id": client.http.client_id,
            "config_path": client.config_path,
            "cache_path": str(client.store.path) if client.store.path else None,
            "cache_file_exists": bool(client.store.path and Path(client.store.path).is_file()),
            "request_count": client.http.request_count,
        },
        "token": {
            "present": bool(configured or cached_refresh),
            "source": app.settings.sanitized()["token_source"] if app.settings.token_from_form else (
                client.config_sources.get("refresh_token") or "—"
            ),
            "configured_masked": mask_secret(configured),
            "configured_length": len(configured) if configured else 0,
            "cache_masked": mask_secret(cached_refresh),
        },
        "access": {"obtained": False},
    }
    if check:
        try:
            tokens = client.http.authenticate(force=True)
            out["access"] = {
                "obtained": True,
                "masked": mask_secret(tokens.access_token),
                "ttl_h": round(tokens.access_ttl / 3600, 1),
            }
            out["ok"] = True
        except AuthError as exc:
            out["ok"] = False
            out["error"] = {"kind": "auth", "message": str(exc)}
    return out


def cmd_token_check(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    if app.settings.mode == "demo":
        return {
            "ok": True,
            "mode": "demo",
            "demo": True,
            "note": "В демо-режиме токен не требуется; переключитесь на «Боевой API» для диагностики.",
        }
    client = app.make_client()
    configured = client.configured_refresh_token
    cached = client.store.get()
    cached_refresh = cached.refresh_token if cached else None
    reports = []
    if configured:
        reports.append(
            asdict(
                inspect_token(
                    configured,
                    source=client.config_sources.get("refresh_token") or "веб-форма",
                    requested_client_id=client.http.client_id,
                )
            )
        )
    if cached_refresh and cached_refresh != configured:
        reports.append(
            asdict(
                inspect_token(
                    cached_refresh,
                    source=(cached.refresh_source if cached else None) or f"файл кэша {client.store.path}",
                    requested_client_id=client.http.client_id,
                )
            )
        )
    from pathlib import Path as _Path

    scan = scan_config_files(_Path.cwd())
    return {
        "ok": True,
        "mode": "live",
        "demo": False,
        "sources": client.config_sources,
        "overridden": bool(
            client.config_path
            and client.config_sources.get("refresh_token", "").startswith("переменная")
        ),
        "config_path": client.config_path,
        "cache_path": str(client.store.path) if client.store.path else None,
        "cache_file_exists": bool(client.store.path and Path(client.store.path).is_file()),
        "configured": {"masked": mask_secret(configured), "length": len(configured) if configured else 0},
        "token_notes": client.token_notes,
        "config_scan": scan,
        "reports": reports,
    }


def cmd_token_reset(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    client = app.make_client()
    client.store.clear()
    return {
        "ok": True,
        "mode": app.settings.mode,
        "message": "кэш токенов удалён",
        "cache_path": str(client.store.path) if client.store.path else None,
    }


def cmd_token_refresh(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    if app.settings.mode == "demo":
        return {"ok": True, "mode": "demo", "demo": True, "message": "В демо-режиме токен не используется."}
    client = app.make_client()
    client.http.invalidate_cache()
    tokens = client.http.authenticate(force=True)
    return {
        "ok": True,
        "mode": "live",
        "message": "пара токенов перевыпущена",
        "access_masked": mask_secret(tokens.access_token),
        "ttl_h": round(tokens.access_ttl / 3600, 1),
    }


def cmd_export(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    since, until = _resolve_range(body)
    size = _size(body)
    formats = tuple(x.strip().lower() for x in str(body.get("formats") or "json,csv,md").split(",") if x.strip())
    formats = tuple(x for x in formats if x in FORMATS) or ("json", "csv", "md")
    out_dir = str(body.get("out_dir") or app.settings.export_dir or "reports")
    app.settings._check_export_dir(out_dir)
    prefix = str(body.get("prefix") or "").strip() or None
    include_orders = bool(body.get("include_orders"))
    include_operations = bool(body.get("include_operations"))
    term_value = str(body.get("term") or "T0")
    term = None if term_value.lower() in ("all", "none", "") else term_value

    if app.settings.mode == "demo":
        raw_portfolio = fake_portfolio_payload()
        portfolio = Portfolio.from_api(raw_portfolio, term=term)
        trades = fake_trades(days=_days(body))
        limits = fake_limits_payload()
        orders = fake_orders_payload(days=_days(body)) if include_orders else None
        operations = fake_operations_payload(days=_days(body)) if include_operations else None
        source = "demo"
    else:
        client = app.make_client()
        raw_portfolio = client.get_portfolio_raw()
        portfolio = Portfolio.from_api(raw_portfolio, term=term)
        trades = list(
            client.iter_trades(
                since=since,
                until=until,
                tickers=_split(body.get("tickers")),
                class_codes=_split(body.get("class_codes")),
                side=body.get("side") or None,
                size=size,
            )
        )
        limits = client.get_limits()
        orders = client.search_orders(since=since, until=until, size=size) if include_orders else None
        operations = (
            client.search_operations(since=since, until=until, size=size) if include_operations else None
        )
        source = "live"

    created = save_report(
        out_dir,
        portfolio=portfolio,
        trades=trades,
        raw_portfolio=raw_portfolio,
        limits=limits,
        orders=orders,
        operations=operations,
        prefix=prefix,
        formats=formats,
    )
    base = Path(out_dir).resolve()
    files = []
    for file in created:
        rel = file.resolve().relative_to(base)
        rate = int(file.stat().st_size)
        files.append(
            {
                "name": file.name,
                "path": str(file.resolve()),
                "relative": str(rel),
                "size": rate,
                "format": file.suffix.lstrip("."),
                "url": "/api/download?file=" + quote(str(rel)),
            }
        )
    return {
        "ok": True,
        "mode": source,
        "summary": {"positions": len(portfolio.positions), "trades": len(trades), "files": len(files)},
        "files": files,
    }


def cmd_raw(app: WebApp, body: dict[str, Any]) -> dict[str, Any]:
    """Сырой ответ API: портфель/лимиты/сделки/заявки/операции."""
    section = str(body.get("section") or "")
    if app.settings.mode == "demo":
        demo_raw: dict[str, Any] = {
            "portfolio": fake_portfolio_payload(),
            "limits": fake_limits_payload(),
            "trades": fake_trades_payload(days=_days(body)),
            "orders": fake_orders_payload(days=_days(body)),
            "operations": fake_operations_payload(days=_days(body)),
        }
        if section not in demo_raw:
            raise ValueError(f"неизвестный раздел {section!r}")
        return {"ok": True, "mode": "demo", "raw": demo_raw[section]}
    client = app.make_client()
    if section == "portfolio":
        raw: Any = client.get_portfolio_raw()
    elif section == "limits":
        raw = client.get_limits()
    elif section == "trades":
        since, until = _resolve_range(body)
        page = client.search_trades(
            since=since,
            until=until,
            tickers=_split(body.get("tickers")),
            class_codes=_split(body.get("class_codes")),
            side=body.get("side") or None,
            page=0,
            size=_size(body),
        )
        raw = page.__dict__ | {"records": [t.raw for t in page.records]}
    elif section == "orders":
        since, until = _resolve_range(body)
        raw = client.search_orders(since=since, until=until, size=_size(body))
    elif section == "operations":
        since, until = _resolve_range(body)
        raw = client.search_operations(since=since, until=until, size=_size(body))
    else:
        raise ValueError(f"неизвестный раздел {section!r}")
    return {"ok": True, "mode": "live", "raw": raw}


# -------------------------------------------------------------------- web app


class WebApp:
    """Состояние сервера: настройки, журнал, фабрика клиентов."""

    def __init__(self, *, mode: Optional[str] = None, cwd: Optional[str | Path] = None) -> None:
        self.cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        self.settings = Settings(mode=(mode or default_mode()))
        self.start_time = time.time()
        self.log: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._index_cache: Optional[str] = None

    def make_client(self) -> BcsClient:
        return BcsClient(**self.settings.as_client_kwargs())

    def record(self, path: str, method: str, status: int, ms: int, error: Optional[str] = None) -> None:
        with self._lock:
            self.log.append(
                {
                    "time": _now_iso(),
                    "method": method,
                    "path": path,
                    "status": status,
                    "ms": ms,
                    "error": error,
                }
            )
            if len(self.log) > MAX_LOG:
                self.log = self.log[-MAX_LOG:]

    def read_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self.log))

    def index_html(self) -> str:
        if self._index_cache is None:
            file = Path(__file__).resolve().parent / "webapp" / "index.html"
            self._index_cache = file.read_text(encoding="utf-8")
        return self._index_cache


class _Handler(BaseHTTPRequestHandler):
    app: WebApp  # подменяется в WebServer.__init__

    server_version = "bcs-web/1.0"

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt: str, *args: Any) -> None:  # тихий HTTP-лог
        return

    def _send(
        self, code: int, payload: Any, *, content_type: str = "application/json; charset=utf-8"
    ) -> None:
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        )
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("тело запроса должно быть валидным JSON") from exc
        return data if isinstance(data, dict) else {}

    def _route(
        self,
        method: str,
        handler: Callable[[dict[str, Any]], Any],
        body: Optional[dict[str, Any]] = None,
    ) -> None:
        started = time.monotonic()
        error_kind = None
        try:
            payload = handler(body or {})
            self._send(200, payload)
        except AuthError as exc:
            error_kind = "auth"
            self._send(200, _error(exc, kind="auth"))
        except UnauthorizedError as exc:
            error_kind = "unauthorized"
            self._send(200, _error(exc, kind="unauthorized"))
        except RateLimitError as exc:
            error_kind = "rate"
            self._send(200, _error(exc, kind="rate"))
        except ValidationError as exc:
            error_kind = "validation"
            self._send(200, _error(exc, kind="validation"))
        except ApiError as exc:
            error_kind = "api"
            self._send(200, _error(exc, kind="api"))
        except BcsError as exc:
            error_kind = "bcs"
            self._send(200, _error(exc, kind="bcs"))
        except ValueError as exc:
            error_kind = "config"
            self._send(200, _error(exc, kind="config", message=str(exc)))
        except Exception as exc:  # ошибка не должна ронять сервер
            error_kind = "internal"
            log.exception("внутренняя ошибка %s %s", method, self.path)
            self._send(500, _error(exc, kind="internal"))
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            self.app.record(self.path, method, 200 if error_kind != "internal" else 500, elapsed, error_kind)

    # ----------------------------------------------------------------- GET

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        started = time.monotonic()
        if path in ("/", "/index.html"):
            self._send(200, self.app.index_html().encode("utf-8"), content_type="text/html; charset=utf-8")
            self.app.record(self.path, "GET", 200, int((time.monotonic() - started) * 1000))
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/health":
            self._send(200, {"ok": True, "uptime_s": round(time.time() - self.app.start_time, 1)})
            self.app.record(self.path, "GET", 200, int((time.monotonic() - started) * 1000))
            return
        if path == "/api/settings":
            self._send(200, {"ok": True, "settings": self.app.settings.sanitized()})
            self.app.record(self.path, "GET", 200, int((time.monotonic() - started) * 1000))
            return
        if path == "/api/log":
            self._send(200, {"ok": True, "entries": self.app.read_log()})
            self.app.record(self.path, "GET", 200, int((time.monotonic() - started) * 1000))
            return
        if path == "/api/download":
            self._download(parse_qs(parsed.query))
            return
        self._send(404, {"ok": False, "error": {"kind": "http", "message": f"нет такого пути: {path}"}})

    def _download(self, query: dict[str, list[str]]) -> None:
        started = time.monotonic()
        name = (query.get("file") or [""])[0]
        error = self._download_error(name)
        if not error:
            try:
                target = self._download_target(name)
                content = target.read_bytes()
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
            except OSError as exc:
                error = str(exc)
        if error:
            self._send(404, {"ok": False, "error": {"kind": "http", "message": error}})
        elapsed = int((time.monotonic() - started) * 1000)
        self.app.record(self.path, "GET", 200 if not error else 404, elapsed, None if not error else "http")

    def _download_target(self, name: str) -> Path:
        base = (Path(self.app.cwd) / self.app.settings.export_dir).resolve()
        target = (base / name).resolve()
        if not (str(target).startswith(str(base) + os.sep) or target == base):
            raise ValueError("файл вне папки экспорта")
        return target

    def _download_error(self, name: str) -> Optional[str]:
        if not name or Path(name).name != name:
            return "некорректное имя файла"
        if not self._download_target(name).is_file():
            return "файл не найден"
        return None

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json()
        except ValueError as exc:
            self.app.record(self.path, "POST", 400, 0, "config")
            self._send(400, {"ok": False, "error": {"kind": "config", "message": str(exc)}})
            return

        routes: dict[str, Callable[[dict[str, Any]], Any]] = {
            "/api/settings": self._api_settings,
            "/api/status": lambda b: cmd_status(self.app, b),
            "/api/token/check": lambda b: cmd_token_check(self.app, b),
            "/api/token/reset": lambda b: cmd_token_reset(self.app, b),
            "/api/token/refresh": lambda b: cmd_token_refresh(self.app, b),
            "/api/portfolio": lambda b: cmd_portfolio(self.app, b),
            "/api/limits": lambda b: cmd_limits(self.app, b),
            "/api/trades": lambda b: cmd_trades(self.app, b),
            "/api/orders": lambda b: cmd_orders(self.app, b),
            "/api/operations": lambda b: cmd_operations(self.app, b),
            "/api/pnl": lambda b: cmd_pnl(self.app, b),
            "/api/export": lambda b: cmd_export(self.app, b),
            "/api/raw": lambda b: cmd_raw(self.app, b),
        }
        handler = routes.get(path)
        if handler is None:
            self._send(404, {"ok": False, "error": {"kind": "http", "message": f"нет такого пути: {path}"}})
            return
        self._route("POST", handler, body)

    def _api_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "get")
        if action == "get":
            return {"ok": True, "settings": self.app.settings.sanitized()}
        if action == "set":
            values = body.get("values") if isinstance(body.get("values"), dict) else {}
            with self.app.settings.lock:
                notes = self.app.settings.update(values)
            return {
                "ok": True,
                "message": "настройки сохранены",
                "notes": notes,
                "settings": self.app.settings.sanitized(),
            }
        raise ValueError(f"неизвестное действие {action!r}")


def _error(exc: Exception, *, kind: str, message: Optional[str] = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": {
            "kind": kind,
            "message": message or str(exc),
            "trace_id": getattr(exc, "trace_id", None),
        },
    }
    if kind == "auth":
        out["error"]["hint"] = (
            "Проверьте: 1) refresh-токен выпущен для этого счёта; 2) он не отозван в ЛК и не старше 90 суток; "
            "3) client_id совпадает с правами токена (trade-api-read / trade-api-write). "
            "Подробности — кнопка «Проверить токен»."
        )
    elif kind == "rate":
        out["error"]["hint"] = "Снизьте частоту запросов (rps) или подождите — лимит БКС 10 RPS."
    elif kind == "unauthorized":
        out["error"]["hint"] = "Access-токен отклонён: программа уже попробовала перевыпустить его по refresh-токену."
    elif kind == "validation":
        out["error"]["hint"] = "Проверьте значения фильтров (даты, тикеры, размер страницы 1..100)."
    return out


# --------------------------------------------------------------------- server


class WebServer:
    """HTTP-сервер веб-интерфейса (``ThreadingHTTPServer`` + WebApp)."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        mode: Optional[str] = None,
        cwd: Optional[str | Path] = None,
    ) -> None:
        self.app = WebApp(mode=mode, cwd=cwd)
        handler = type("BoundHandler", (_Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[:2]
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="bcs-web")
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def serve_blocking(self) -> None:
        self.start()
        try:
            self._thread.join()  # type: ignore[union-attr]
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()


def serve_web(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    mode: Optional[str] = None,
    open_browser: bool = False,
) -> int:
    server = WebServer(host=host, port=port, mode=mode)
    print(f"Веб-интерфейс БКС Портфель: {server.url}")
    print(f"  режим: {server.app.settings.mode} · журнал настройки — кнопки в браузере")
    if server.app.settings.mode == "demo":
        print("  (демо-режим: без токена. Для реального API: BCS_REFRESH_TOKEN=... или настройки в браузере)")
    else:
        print("  (боевой API: токен найден в env/конфиге)")
    if open_browser:
        import webbrowser

        webbrowser.open(server.url)
    server.serve_blocking()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Веб-интерфейс BCS Trade API")
    parser.add_argument("--host", default="127.0.0.1", help="адрес прослушивания")
    parser.add_argument("--port", type=int, default=8080, help="порт")
    parser.add_argument("--mode", choices=("auto", "demo", "live"), default="auto", help="режим данных")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = parser.parse_args(argv)
    mode = None if args.mode == "auto" else args.mode
    return serve_web(host=args.host, port=args.port, mode=mode, open_browser=not args.no_browser)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
