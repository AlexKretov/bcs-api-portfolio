"""Выгрузка отчётов в файлы: JSON, CSV, Markdown."""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Optional

from .client import Portfolio, Trade


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: str | Path) -> Path:
    out = Path(path).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_json(path: str | Path, payload: Any) -> Path:
    file = Path(path).parent / Path(path).name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_fallback), encoding="utf-8")
    return file


def write_csv(
    path: str | Path, rows: Sequence[dict[str, Any]], *, fields: Optional[Sequence[str]] = None
) -> Optional[Path]:
    """CSV с запятой как разделителем и UTF-8 BOM — чтобы корректно открывался в Excel."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return None
    keys: list[str] = list(fields or list(rows[0].keys()))
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with file.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_cell(row.get(k)) for k in keys})
    return file


def portfolio_to_rows(portfolio: Portfolio) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in portfolio.positions:
        rows.append(
            {
                "ticker": p.ticker,
                "name": p.display_name,
                "instrument_type": p.instrument_type,
                "type_label": p.type_label,
                "currency": p.currency,
                "board": p.board,
                "exchange": p.exchange,
                "account": p.account,
                "term": p.term,
                "quantity": p.quantity,
                "lots": p.lots,
                "locked": p.locked,
                "balance_price": p.balance_price,
                "current_price": p.current_price,
                "balance_value_rub": p.balance_value_rub,
                "current_value_rub": p.current_value_rub,
                "unrealized_pl": p.unrealized_pl,
                "unrealized_percent_pl": p.unrealized_percent_pl,
                "daily_pl": p.daily_pl,
                "daily_percent_pl": p.daily_percent_pl,
                "portfolio_share": p.portfolio_share,
                "accrued_income": p.accrued_income,
                "is_blocked": p.is_blocked,
            }
        )
    for c in portfolio.cash:
        rows.append(
            {
                "ticker": c.currency,
                "name": "Денежный остаток",
                "instrument_type": "MONEY",
                "type_label": "Деньги",
                "currency": c.currency,
                "exchange": c.exchange,
                "account": c.account,
                "term": c.term,
                "quantity": c.quantity,
                "locked": c.locked,
                "current_value_rub": c.current_value_rub,
            }
        )
    return rows


def trades_to_rows(trades: Iterable[Trade]) -> list[dict[str, Any]]:
    return [trade.summary() for trade in trades]


def portfolio_summary_markdown(portfolio: Portfolio, *, trades: Optional[Sequence[Trade]] = None) -> str:
    from .formatting import money, percent, qty, short_datetime

    lines = [
        "# Отчёт по счёту БКС",
        "",
        f"Сформировано: {dt.datetime.now():%Y-%m-%d %H:%M:%S} (данные API на {short_datetime(portfolio.as_of)})",
        "",
        "## Сводка",
        "",
        "| Показатель | Значение |",
        "| --- | ---: |",
        f"| Позиций | {len(portfolio.positions)} |",
        f"| Стоимость бумаг | {money(portfolio.securities_value_rub)} |",
        f"| Денежный остаток | {money(portfolio.cash_rub)} |",
        f"| Портфель целиком | {money(portfolio.total_value_rub)} |",
        f"| Нереализованный P/L | {money(portfolio.total_unrealized_pl, sign=True)} |",
        f"| Результат за день | {money(portfolio.total_daily_pl, sign=True)} |",
        "",
        "## Позиции",
        "",
        "| Тикер | Название | Кол-во | Цена позиции | Текущая цена | Стоимость ₽ | P/L | P/L % | Доля |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for p in portfolio.positions:
        lines.append(
            f"| {p.ticker} | {p.display_name} | {qty(p.quantity)} | {money(p.balance_price, currency='')} | "
            f"{money(p.current_price, currency='')} | {money(p.current_value_rub)} | "
            f"{money(p.unrealized_pl, sign=True)} | {percent(p.unrealized_percent_pl)} | "
            f"{percent(p.portfolio_share, sign=False)} |"
        )
    if not portfolio.positions:
        lines.append("| — | позиций нет | | | | | | | |")

    if portfolio.cash:
        lines += [
            "",
            "## Деньги",
            "",
            "| Валюта | Всего | Занято | Свободно | В ₽ |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for c in portfolio.cash:
            lines.append(
                f"| {c.currency} | {qty(c.quantity)} | {qty(c.locked)} | {qty(c.available)} | "
                f"{money(c.current_value_rub)} |"
            )

    if trades is not None:
        buys = [t for t in trades if str(t.side) == "1"]
        sells = [t for t in trades if str(t.side) == "2"]
        turnover = sum(t.volume or 0 for t in trades)
        lines += [
            "",
            "## Сделки",
            "",
            f"Всего: {len(trades)} · покупок {len(buys)} · продаж {len(sells)} · оборот {money(turnover)}",
            "",
            "| Дата | Тикер | Сторона | Кол-во | Цена | Объём | Валюта |",
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
        for t in trades[:50]:
            lines.append(
                f"| {short_datetime(t.date_time)} | {t.ticker} | {t.side_label} | {qty(t.trade_quantity)} | "
                f"{money(t.price, currency='')} | {money(t.volume)} | {t.settlement_currency} |"
            )
        if len(trades) > 50:
            lines.append(f"| … | ещё {len(trades) - 50} строк — в CSV | | | | | |")
    lines.append("")
    return "\n".join(lines)


def save_report(
    out_dir: str | Path,
    *,
    portfolio: Optional[Portfolio] = None,
    trades: Optional[Sequence[Trade]] = None,
    raw_portfolio: Any = None,
    raw_trades: Any = None,
    limits: Any = None,
    orders: Any = None,
    operations: Any = None,
    prefix: Optional[str] = None,
    formats: Sequence[str] = ("json", "csv", "md"),
) -> list[Path]:
    """Сохранить всё, что есть, в ``out_dir``; вернуть список созданных файлов."""
    out = ensure_dir(out_dir)
    name = f"{prefix or 'bcs-report'}-{_stamp()}"
    created: list[Path] = []

    if portfolio is not None:
        if "json" in formats:
            payload: dict[str, Any] = {
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "as_of": portfolio.as_of.isoformat() if portfolio.as_of else None,
                "summary": {
                    "positions": len(portfolio.positions),
                    "securities_value_rub": portfolio.securities_value_rub,
                    "cash_rub": portfolio.cash_rub,
                    "total_value_rub": portfolio.total_value_rub,
                    "unrealized_pl": portfolio.total_unrealized_pl,
                    "daily_pl": portfolio.total_daily_pl,
                    "by_type_rub": portfolio.by_type(),
                },
                "positions": [p.raw for p in portfolio.positions],
                "cash": [c.raw for c in portfolio.cash],
            }
            if raw_portfolio is not None:
                payload["raw_response"] = raw_portfolio
            created.append(write_json(out / f"{name}-portfolio.json", payload))
        if "csv" in formats:
            file = write_csv(out / f"{name}-portfolio.csv", portfolio_to_rows(portfolio))
            if file:
                created.append(file)
        if "md" in formats:
            md = out / f"{name}.md"
            md.write_text(portfolio_summary_markdown(portfolio, trades=trades), encoding="utf-8")
            created.append(md)

    if trades is not None and "csv" in formats:
        file = write_csv(out / f"{name}-trades.csv", trades_to_rows(trades))
        if file:
            created.append(file)

    extra: dict[str, Any] = {}
    if raw_trades is not None:
        extra["trades"] = raw_trades
    if limits is not None:
        extra["limits"] = limits
    if orders is not None:
        extra["orders"] = orders
    if operations is not None:
        extra["operations"] = operations
    if extra and "json" in formats:
        created.append(write_json(out / f"{name}-raw.json", extra))
    return created


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return f"{value:.10g}"
    return value


def _fallback(obj: Any) -> Any:
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if hasattr(obj, "raw"):
        return obj.raw
    return str(obj)
