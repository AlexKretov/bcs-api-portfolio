"""Отчёт о прибылях и убытках (P&L) по портфелю БКС.

Расчёт доходов, расходов, потенциальной прибыли от прироста курсовой стоимости
и итогового финансового результата за выбранный период с фильтрацией по типам активов.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .client import INSTRUMENT_LABELS, Portfolio, Position, Trade

#: Категории активов для удобной группировки в UI и фильтрах
ASSET_CATEGORIES = {
    "STOCK": ["STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPTS"],
    "BONDS": ["BONDS", "EURO_BONDS", "NOTES"],
    "FUTURES": ["FUTURES", "OPTIONS", "GOODS"],
    "FUNDS": ["MUTUAL_FUNDS", "ETF"],
    "MONEY": ["CURRENCY", "MONEY"],
}

CATEGORY_LABELS = {
    "STOCK": "Акции",
    "BONDS": "Облигации",
    "FUTURES": "Фьючерсы и опционы",
    "FUNDS": "ПИФ и ETF",
    "MONEY": "Валюта и денежные средства",
    "OTHER": "Прочее",
}

#: Обратный маппинг: тип инструмента -> категория
INSTRUMENT_TO_CATEGORY = {}
for _cat, _types in ASSET_CATEGORIES.items():
    for _t in _types:
        INSTRUMENT_TO_CATEGORY[_t] = _cat


def resolve_allowed_types(asset_types: Optional[Sequence[str]]) -> Optional[set[str]]:
    """Преобразовать список категорий / типов активов в множество разрешённых типов.

    Если asset_types содержит имена категорий ('STOCK', 'BONDS'...), раскладывает их
    в соответствующий набор типов инструментов. Если None/пусто — возвращает None (все типы).
    """
    if not asset_types:
        return None
    out = set()
    for item in asset_types:
        item_str = str(item).strip().upper()
        if not item_str or item_str == "ALL":
            return None
        if item_str in ASSET_CATEGORIES:
            out.update(ASSET_CATEGORIES[item_str])
        else:
            out.add(item_str)
    return out


def get_position_category(instrument_type: str) -> str:
    return INSTRUMENT_TO_CATEGORY.get(instrument_type.upper(), "OTHER")


def calculate_pnl(
    portfolio: Portfolio,
    trades: Sequence[Trade | dict[str, Any]],
    operations: Sequence[dict[str, Any]],
    *,
    asset_types: Optional[Sequence[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict[str, Any]:
    """Сформировать сводный отчёт о прибылях и убытках за период.

    Параметры:
      portfolio: текущий снимок портфеля (для нереализованной переоценки и НКД).
      trades: список сделок за период.
      operations: список неторговых операций за период.
      asset_types: фильтр типов активов (например, ['STOCK', 'BONDS'] или ['STOCK']).
      since, until: строковые метки периода для заголовка отчёта.
    """
    allowed_types = resolve_allowed_types(asset_types)

    # ---------------- 1. Потенциальная прибыль от прироста стоимости (Unrealized Capital Gain)
    unrealized_pl_total = 0.0
    accrued_income_total = 0.0
    securities_value_total = 0.0

    category_stats: dict[str, dict[str, float]] = {}

    for cat_key in CATEGORY_LABELS:
        category_stats[cat_key] = {
            "securities_value": 0.0,
            "unrealized_pl": 0.0,
            "accrued_income": 0.0,
            "trade_volume": 0.0,
            "realized_pnl": 0.0,
            "dividends": 0.0,
            "coupons": 0.0,
            "other_income": 0.0,
            "commissions": 0.0,
            "taxes": 0.0,
            "other_expenses": 0.0,
        }

    ticker_to_cat: dict[str, str] = {}

    for pos in portfolio.positions:
        cat = get_position_category(pos.instrument_type)
        if pos.ticker:
            ticker_to_cat[pos.ticker.upper()] = cat

        if allowed_types is not None and pos.instrument_type.upper() not in allowed_types:
            continue

        unrealized_pl_total += pos.unrealized_pl
        accrued_income_total += pos.accrued_income
        securities_value_total += pos.current_value_rub

        category_stats[cat]["securities_value"] += pos.current_value_rub
        category_stats[cat]["unrealized_pl"] += pos.unrealized_pl
        category_stats[cat]["accrued_income"] += pos.accrued_income

    # ---------------- 2. Реализованный результат по сделкам за период
    trade_objs: list[Trade] = []
    for item in trades:
        if isinstance(item, Trade):
            trade_objs.append(item)
        elif isinstance(item, dict):
            trade_objs.append(Trade.from_api(item))

    pos_by_ticker = {p.ticker.upper(): p for p in portfolio.positions if p.ticker}

    # Группировка сделок по тикеру
    ticker_trades: dict[str, list[Trade]] = {}
    for t in trade_objs:
        if not t.ticker:
            continue
        ticker_upper = t.ticker.upper()
        itype = t.instrument_type or (pos_by_ticker[ticker_upper].instrument_type if ticker_upper in pos_by_ticker else "STOCK")
        cat = get_position_category(itype)
        ticker_to_cat[ticker_upper] = cat

        if allowed_types is not None and itype.upper() not in allowed_types:
            continue

        ticker_trades.setdefault(ticker_upper, []).append(t)

    realized_trade_pnl = 0.0
    total_buy_volume = 0.0
    total_sell_volume = 0.0

    for ticker, t_list in ticker_trades.items():
        cat = ticker_to_cat.get(ticker, "OTHER")
        buys = [t for t in t_list if str(t.side) == "1"]
        sells = [t for t in t_list if str(t.side) == "2"]

        buy_vol = sum(t.volume or 0.0 for t in buys)
        sell_vol = sum(t.volume or 0.0 for t in sells)
        buy_qty = sum(t.trade_quantity or 0.0 for t in buys)
        sell_qty = sum(t.trade_quantity or 0.0 for t in sells)

        total_buy_volume += buy_vol
        total_sell_volume += sell_vol
        category_stats[cat]["trade_volume"] += buy_vol + sell_vol

        # Расчёт финансового результата по зафиксированным продажам
        if sell_qty > 0:
            if buy_qty > 0:
                avg_buy_price = buy_vol / buy_qty
                ticker_realized = sell_vol - (sell_qty * avg_buy_price)
            elif ticker in pos_by_ticker and pos_by_ticker[ticker].balance_price > 0:
                cost_basis = sell_qty * pos_by_ticker[ticker].balance_price
                ticker_realized = sell_vol - cost_basis
            else:
                ticker_realized = 0.0
            realized_trade_pnl += ticker_realized
            category_stats[cat]["realized_pnl"] += ticker_realized

    # ---------------- 3. Неторговые операции (дивиденды, купоны, комиссии, налоги)
    dividends_total = 0.0
    coupons_total = 0.0
    other_income_total = 0.0
    commissions_total = 0.0
    taxes_total = 0.0
    other_expenses_total = 0.0
    pay_in_total = 0.0
    pay_out_total = 0.0

    for op in operations:
        if not isinstance(op, dict):
            continue
        op_type = str(op.get("type") or "").strip()
        ticker = str(op.get("ticker") or "").strip().upper()
        amount = op.get("sum")
        try:
            amount = float(amount) if amount is not None else 0.0
        except (TypeError, ValueError):
            amount = 0.0

        if not amount and op_type not in ("PayIn", "PayOut"):
            continue

        cat = ticker_to_cat.get(ticker, "OTHER") if ticker else "MONEY"

        # Если операция привязана к тикеру с фильтруемым типом активов:
        if ticker and ticker in pos_by_ticker:
            pos_itype = pos_by_ticker[ticker].instrument_type.upper()
            if allowed_types is not None and pos_itype not in allowed_types:
                continue

        if op_type in ("Dividend", "Dividends"):
            dividends_total += abs(amount)
            category_stats[cat]["dividends"] += abs(amount)
        elif op_type in ("BondPayingOff", "Coupon", "Coupons", "BondYield"):
            coupons_total += abs(amount)
            category_stats[cat]["coupons"] += abs(amount)
        elif op_type in ("Interest", "Overnight"):
            other_income_total += abs(amount)
            category_stats[cat]["other_income"] += abs(amount)
        elif op_type in ("Commission", "Brokerage commission", "BrokerageCommission"):
            commissions_total += abs(amount)
            category_stats[cat]["commissions"] += abs(amount)
        elif op_type in ("Tax", "Taxes"):
            taxes_total += abs(amount)
            category_stats[cat]["taxes"] += abs(amount)
        elif op_type in ("PayIn", "Deposit", "TopUp", "Refill", "PaymentIn"):
            pay_in_total += abs(amount)
        elif op_type in ("PayOut", "Withdrawal", "PaymentOut"):
            pay_out_total += abs(amount)
        else:
            balance_change = str(op.get("balanceChange") or "").strip().lower()
            if amount > 0 or balance_change == "positive":
                other_income_total += abs(amount)
                category_stats[cat]["other_income"] += abs(amount)
            elif amount < 0 or balance_change == "negative":
                other_expenses_total += abs(amount)
                category_stats[cat]["other_expenses"] += abs(amount)

    # ---------------- 4. Итоговые статьи доходов и расходов
    trade_realized_income = max(0.0, realized_trade_pnl)
    trade_realized_loss = abs(min(0.0, realized_trade_pnl))

    total_realized_income = trade_realized_income + dividends_total + coupons_total + other_income_total
    total_expenses = trade_realized_loss + commissions_total + taxes_total + other_expenses_total

    net_realized_pnl = total_realized_income - total_expenses
    potential_capital_gain = unrealized_pl_total + accrued_income_total
    total_net_pnl = net_realized_pnl + potential_capital_gain

    # Формирование категориального отчёта
    by_category = []
    for cat_key, cat_name in CATEGORY_LABELS.items():
        st = category_stats[cat_key]
        cat_pnl = (
            st["realized_pnl"]
            + st["dividends"]
            + st["coupons"]
            + st["other_income"]
            - st["commissions"]
            - st["taxes"]
            - st["other_expenses"]
            + st["unrealized_pl"]
            + st["accrued_income"]
        )
        if (
            st["securities_value"] != 0
            or st["trade_volume"] != 0
            or st["dividends"] != 0
            or st["coupons"] != 0
            or st["unrealized_pl"] != 0
            or cat_pnl != 0
        ):
            by_category.append(
                {
                    "category": cat_key,
                    "name": cat_name,
                    "securities_value": round(st["securities_value"], 2),
                    "trade_volume": round(st["trade_volume"], 2),
                    "realized_pnl": round(st["realized_pnl"], 2),
                    "dividends": round(st["dividends"], 2),
                    "coupons": round(st["coupons"], 2),
                    "unrealized_pl": round(st["unrealized_pl"], 2),
                    "accrued_income": round(st["accrued_income"], 2),
                    "commissions": round(st["commissions"], 2),
                    "taxes": round(st["taxes"], 2),
                    "total_pnl": round(cat_pnl, 2),
                }
            )

    filter_labels = []
    if allowed_types:
        for cat_k, cat_lbl in CATEGORY_LABELS.items():
            if cat_k in ASSET_CATEGORIES and any(t in allowed_types for t in ASSET_CATEGORIES[cat_k]):
                filter_labels.append(cat_lbl)
    filter_description = ", ".join(filter_labels) if filter_labels else "Все типы активов"

    return {
        "ok": True,
        "period": {
            "since": since,
            "until": until,
        },
        "filter": {
            "asset_types": list(asset_types) if asset_types else ["ALL"],
            "description": filter_description,
        },
        "summary": {
            "net_pnl": round(total_net_pnl, 2),
            "net_realized_pnl": round(net_realized_pnl, 2),
            "potential_capital_gain": round(potential_capital_gain, 2),
            "total_income": round(total_realized_income, 2),
            "total_expenses": round(total_expenses, 2),
            "securities_value_rub": round(securities_value_total, 2),
        },
        "income_items": [
            {
                "key": "trade_realized_income",
                "label": "Прибыль от реализации ценных бумаг (сделки)",
                "value": round(trade_realized_income, 2),
            },
            {"key": "dividends", "label": "Дивиденды", "value": round(dividends_total, 2)},
            {"key": "coupons", "label": "Купоны и выплаты по облигациям", "value": round(coupons_total, 2)},
            {"key": "other_income", "label": "Прочие неторговые доходы (проценты, овернайт)", "value": round(other_income_total, 2)},
        ],
        "potential_items": [
            {
                "key": "unrealized_pl",
                "label": "Нереализованный прирост/падение курсовой стоимости",
                "value": round(unrealized_pl_total, 2),
            },
            {
                "key": "accrued_income",
                "label": "Накопленный купонный доход (НКД) по облигациям",
                "value": round(accrued_income_total, 2),
            },
        ],
        "expense_items": [
            {
                "key": "trade_realized_loss",
                "label": "Убыток от реализации ценных бумаг (сделки)",
                "value": round(trade_realized_loss, 2),
            },
            {"key": "commissions", "label": "Комиссии брокера и биржи", "value": round(commissions_total, 2)},
            {"key": "taxes", "label": "Удержанные налоги", "value": round(taxes_total, 2)},
            {"key": "other_expenses", "label": "Прочие расходы", "value": round(other_expenses_total, 2)},
        ],
        "cash_flow": {
            "pay_in": round(pay_in_total, 2),
            "pay_out": round(pay_out_total, 2),
            "net_flow": round(pay_in_total - pay_out_total, 2),
        },
        "by_category": by_category,
    }
