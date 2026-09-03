"""Форматирование для консоли: таблицы с учётом ширины кириллицы, «человеческие» числа."""

from __future__ import annotations

import datetime as dt
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, Optional

# ---------------------------------------------------------------- widths/text


def display_width(text: str) -> int:
    """Ширина строки в терминальных колонках (CJK и «полноширинные» символы = 2)."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int, *, align: str = "left") -> str:
    fill = width - display_width(text)
    if fill <= 0:
        return text
    if align == "right":
        return " " * fill + text
    if align == "center":
        left = fill // 2
        return " " * left + text + " " * (fill - left)
    return text + " " * fill


def truncate(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if display_width(out + ch) > width - 1:
            return out + "…"
        out += ch
    return out


def build_table(
    headers: Sequence[str], rows: Iterable[Sequence[Any]], *, aligns: Optional[Sequence[str]] = None
) -> str:
    """Собрать текстовую таблицу. ``rows`` — уже отформатированные строки/числа."""
    body = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    if not body:
        return ""
    aligns = list(aligns or ["left"] * len(headers))
    widths = [display_width(h) for h in headers]
    for row in body:
        for idx, cell in enumerate(row):
            if idx < len(widths):
                widths[idx] = max(widths[idx], display_width(cell))
    lines = ["  ".join(pad(truncate(h, widths[i]), widths[i], align=aligns[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in body:
        cells = [
            pad(truncate(row[i] if i < len(row) else "", widths[i]), widths[i], align=aligns[i])
            for i in range(len(headers))
        ]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


# ------------------------------------------------------------------- numbers


def money(value: Optional[float], *, currency: str = "₽", digits: int = 2, sign: bool = False) -> str:
    """``-12 345.60 ₽`` — разделители разрядов, необязательный знак."""
    if value is None:
        return "—"
    sign_char = ""
    if value < 0:
        sign_char = "-"
        value = -value
    elif sign:
        sign_char = "+"
    text = f"{value:,.{digits}f}"
    # «1 000.00» показывать нечего: дробная часть нулевая → оставляем целое
    if digits and value == int(value):
        text = f"{int(value):,}"
    return f"{sign_char}{text.replace(',', ' ')}{(' ' + currency) if currency else ''}"


def qty(value: Optional[float], *, digits: int = 6) -> str:
    """Количество бумажек: без хвостовых нулей, но с разделителями разрядов."""
    if value is None:
        return "—"
    if float(value) == int(float(value)):
        return f"{int(value):,}".replace(",", " ")
    text = f"{value:,.{digits}f}".replace(",", " ").rstrip("0").rstrip(".")
    return text


def percent(value: Optional[float], *, digits: int = 2, sign: bool = True) -> str:
    if value is None:
        return "—"
    sign_char = "+" if (sign and value > 0) else ""
    return f"{sign_char}{value:,.{digits}f}%".replace(",", " ")


def colorize(text: str, value: Optional[float], *, color: bool) -> str:
    """Зелёный/красный для P&L — только если вывод в TTY и не запрещён."""
    if not color or value is None or value == 0:
        return text
    return f"\033[32m{text}\033[0m" if value > 0 else f"\033[31m{text}\033[0m"


def short_datetime(value: Optional[dt.datetime], *, with_time: bool = True) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


# --------------------------------------------------------------- view builders


def format_portfolio(portfolio: Any, *, color: bool = True, title: str = "Портфель") -> str:
    """Картинка портфеля: итоги, деньги, таблица позиций."""
    out: list[str] = []
    stamp = short_datetime(getattr(portfolio, "as_of", None))
    out.append(f"{title}: {len(portfolio.positions)} поз. · данные на {stamp}")
    out.append("")

    summary_rows = [
        ["Стоимость бумаг", money(portfolio.securities_value_rub)],
        ["Денежный остаток", money(portfolio.cash_rub)],
        ["Итого портфель", money(portfolio.total_value_rub)],
        [
            "Нереализованный P/L",
            colorize(
                f"{money(portfolio.total_unrealized_pl, sign=True)}",
                portfolio.total_unrealized_pl,
                color=color,
            ),
        ],
        [
            "За день",
            colorize(f"{money(portfolio.total_daily_pl, sign=True)}", portfolio.total_daily_pl, color=color),
        ],
    ]
    out.append(build_table(["Показатель", "Значение"], summary_rows, aligns=["left", "right"]))

    if portfolio.cash:
        cash_rows = [
            [c.currency or "—", qty(c.quantity), qty(c.locked), qty(c.available), money(c.current_value_rub)]
            for c in portfolio.cash
        ]
        out.append("")
        out.append("Денежные остатки")
        out.append(
            build_table(
                ["Валюта", "Всего", "Занято", "Свободно", "В ₽"],
                cash_rows,
                aligns=["left", "right", "right", "right", "right"],
            )
        )

    if portfolio.positions:
        rows = []
        for p in portfolio.positions:
            rows.append(
                [
                    p.ticker,
                    truncate(p.display_name, 26),
                    qty(p.quantity),
                    money(p.balance_price, currency=""),
                    money(p.current_price, currency=""),
                    money(p.current_value_rub),
                    colorize(money(p.unrealized_pl, sign=True), p.unrealized_pl, color=color),
                    colorize(percent(p.unrealized_percent_pl), p.unrealized_percent_pl, color=color),
                    colorize(percent(p.daily_percent_pl), p.daily_percent_pl, color=color),
                    percent(p.portfolio_share, sign=False),
                    p.type_label,
                ]
            )
        out.append("")
        out.append("Позиции")
        out.append(
            build_table(
                [
                    "Тикер",
                    "Название",
                    "Кол-во",
                    "Цена поз.",
                    "Цена тек.",
                    "Стоимость ₽",
                    "P/L",
                    "P/L %",
                    "За день %",
                    "Доля",
                    "Тип",
                ],
                rows,
                aligns=["left", "left"] + ["right"] * 8 + ["left"],
            )
        )
        by_type = portfolio.by_type()
        if by_type:
            out.append("")
            out.append("Разбивка по классам активов")
            total = portfolio.securities_value_rub or 1.0
            out.append(
                build_table(
                    ["Класс", "Стоимость ₽", "Доля"],
                    [[k, money(v), percent(v / total * 100, sign=False)] for k, v in by_type.items()],
                    aligns=["left", "right", "right"],
                )
            )
    else:
        out.append("")
        out.append("Позиции: пусто (или все остатки нулевые).")
    return "\n".join(out)


def format_trades(trades: Sequence[Any], *, limit: Optional[int] = None, color: bool = False) -> str:
    shown = list(trades[:limit]) if limit else list(trades)
    rows = []
    for t in shown:
        side_text = t.side_label
        side_color = 32 if str(t.side) == "1" else 31
        rows.append(
            [
                short_datetime(t.date_time),
                t.ticker,
                t.class_code or "—",
                (f"\033[{side_color}m{side_text}\033[0m" if color else side_text),
                qty(t.trade_quantity),
                money(t.price, currency=""),
                money(t.volume),
                t.settlement_currency or "—",
                str(t.order_num or "—"),
                str(t.trade_num or "—"),
            ]
        )
    return build_table(
        ["Дата (локал.)", "Тикер", "Класс", "Сторона", "Кол-во", "Цена", "Объём", "Валюта", "№ заявки", "№ сделки"],
        rows,
        aligns=["left", "left", "left", "left"] + ["right"] * 4 + ["left", "left"],
    )


def format_limits(limits: dict[str, Any], *, color: bool = False) -> str:
    """Короткая сводка по «Лимитам» — второй, независимый источник данных о портфеле."""
    out: list[str] = []
    depo = limits.get("depoLimit") or []
    money_limits = limits.get("moneyLimits") or []
    futures = limits.get("futuresLimits") or []
    holdings = limits.get("futureHolding") or []

    if money_limits:
        rows = [
            [
                m.get("currencyCode") or "—",
                m.get("exchange") or "—",
                qty(_num((_dig(m.get("quantity")) or {}).get("value"))),
                qty(_num(m.get("locked"))),
                money(qty_free(_dig(m.get("quantity")), _num(m.get("locked"))), digits=2),
                short_datetime(parse_dt(m.get("loadDate"))),
            ]
            for m in money_limits
        ]
        stamp = short_datetime(parse_dt(money_limits[0].get("loadDate")))
        out.append(f"Лимиты: деньги по валютам (данные на {stamp})")
        out.append(
            build_table(
                ["Валюта", "Биржа", "Всего", "Занято", "Свободно", "Обновлено"],
                rows,
                aligns=["left", "left", "right", "right", "right", "left"],
            )
        )
        out.append("")

    if depo:
        rows = [
            [
                d.get("ticker") or "—",
                d.get("classCode") or "—",
                qty(_num((_dig(d.get("quantity")) or {}).get("value"))),
                qty(_num((_dig(d.get("quantityBatch")) or {}).get("value"))),
                money(_num(d.get("averagePrice")), currency=""),
                (_dig(d.get("quantity")) or {}).get("type") or "—",
                d.get("instrumentType") or "—",
            ]
            for d in depo
        ]
        out.append(f"Лимиты: ценные бумаги ({len(depo)})")
        out.append(
            build_table(
                ["Тикер", "Класс", "Шт.", "Лоты", "Средн. цена", "Т", "Тип"],
                rows,
                aligns=["left", "left", "right", "right", "right", "left", "left"],
            )
        )
        out.append("")

    if futures or holdings:
        rows = [
            [
                f.get("currencyCode") or "—",
                money(_num(f.get("cbpLimit"))),
                money(_num(f.get("cbplUsed"))),
                money(_num(f.get("cbplPlanned"))),
                money(_num(f.get("varMargin")), sign=True),
                money(_num(f.get("realVarMargin")), sign=True),
            ]
            for f in futures
        ]
        if rows:
            out.append("Лимиты: срочный рынок (ГО и вариационная маржа)")
            out.append(
                build_table(
                    ["Валюта", "ЛГО", "ГО после клиринга", "ГО план", "Вар. маржа (оценка)", "Вар. маржа (начисл.)"],
                    rows,
                    aligns=["left"] + ["right"] * 5,
                )
            )
            out.append("")
        hrows = [
            [
                h.get("ticker") or "—",
                qty(_num(h.get("totalNet"))),
                money(_num(h.get("averagePrice")), currency=""),
                money(_num(h.get("positionValue"))),
                money(_num(h.get("varMargin")), sign=True),
            ]
            for h in holdings
        ]
        if hrows:
            out.append("Лимиты: позиции по фьючерсам")
            out.append(
                build_table(
                    ["Тикер", "Позиция", "Средн. цена", "Стоимость", "Вар. маржа"],
                    hrows,
                    aligns=["left", "right", "right", "right", "right"],
                )
            )
    if not out:
        out.append("Лимиты: ответов нет (возможно, по счету еще нет данных).")
    return "\n".join(x for x in out if x is not None).rstrip()


def _dig(value: Any) -> Optional[dict[str, Any]]:
    return value if isinstance(value, dict) else None


def _num(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def qty_free(quantity: Optional[dict[str, Any]], locked: Optional[float]) -> Optional[float]:
    """Свободный остаток денег: всего минус занятое в заявках.

    «Лимиты» отдают сумму отдельно, а количество — вложенным объектом quantity.
    """
    total = _num((quantity or {}).get("value"))
    if total is None:
        return None
    return total - (locked or 0.0)


def parse_dt(value: Any) -> Optional[dt.datetime]:
    """Дата из ответов сервиса «Лимиты» (loadDate и т.п.)."""
    from .client import parse_datetime

    return parse_datetime(value)
