"""Синтетические данные для ``--demo`` / ``python demo.py``.

Нужны, чтобы проверить внешний вид отчётов, логику агрегирования и форматирование
без реального токена и без нагрузки на API БКС. Структура полей повторяет ответы
``/portfolio`` и ``/trades/search`` из документации.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

from .client import Order, Portfolio, Trade

_INSTRUMENTS = [
    # (тикер, название, тип, валюта, класс, цена)
    ("SBER", "Сбербанк", "STOCK", "RUB", "TQBR", 287.4),
    ("LKOH", "Лукойл", "STOCK", "RUB", "TQBR", 6912.0),
    ("GAZP", "Газпром", "STOCK", "RUB", "TQBR", 131.8),
    ("GKGN", "Группа Гемм", "STOCK", "RUB", "TQBR", 312.0),
    ("XTTT", "Т-Технологии", "STOCK", "RUB", "TQBR", 98.5),
    ("SU26216", "ОФЗ 26216", "BONDS", "RUB", "TQOB", 89.34),
    ("SU26243", "ОФЗ 26243", "BONDS", "RUB", "TQOB", 99.12),
    ("SRV0", "Фьючерс RUB-USD", "FUTURES", "RUB", "FXTS", 92.35),
]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)


def fake_portfolio_payload(rng: random.Random | None = None) -> list[dict[str, Any]]:
    """Сырой ответ ``GET /portfolio``."""
    rng = rng or random.Random(7)
    now = _now()
    payload: list[dict[str, Any]] = []
    for ticker, name, itype, currency, board, price in _INSTRUMENTS:
        if itype == "FUTURES":
            quantity = rng.choice([-2, 3, 5])
        elif itype == "BONDS":
            quantity = rng.choice([10, 20, 40])
        else:
            quantity = rng.choice([10, 45, 120, 300])
        balance_price = round(price * rng.uniform(0.78, 1.14), 4)
        current_price = round(price * rng.uniform(0.985, 1.02), 4)
        payload.append(
            {
                "type": "depoLimit" if itype != "FUTURES" else "futuresHolding",
                "account": "1000123456",
                "exchange": "MOEX",
                "ticker": ticker,
                "displayName": name,
                "currency": currency,
                "upperType": "RUSSIA",
                "instrumentType": itype,
                "term": "T0",
                "quantity": quantity,
                "locked": rng.choice([0, 0, 1]),
                "balancePrice": balance_price,
                "currentPrice": current_price,
                "balanceValueRub": round(balance_price * quantity, 2),
                "currentValueRub": round(current_price * quantity, 2),
                "unrealizedPL": round((current_price - balance_price) * quantity, 2),
                "unrealizedPercentPL": round((current_price / balance_price - 1) * 100, 2),
                "dailyPL": round(current_price * quantity * rng.uniform(-0.012, 0.015), 2),
                "dailyPercentPL": round(rng.uniform(-1.2, 1.5), 2),
                "portfolioShare": 0,  # пересчитаем ниже
                "scale": 2,
                "minimumStep": 0.01,
                "board": board,
                "priceUnit": "RUB",
                "faceValue": 1000.0 if itype == "BONDS" else None,
                "accruedIncome": round(price * 0.006 * quantity, 2) if itype == "BONDS" else 0,
                "isBlocked": False,
                "ratioQuantity": 1,
                "expireDate": "2028-03-04" if itype == "BONDS" else None,
                "loadDate": now.isoformat(),
            }
        )
    for currency, amount in (("RUB", rng.uniform(18_000, 140_000)), ("CNY", rng.uniform(0, 4_000))):
        payload.append(
            {
                "type": "moneyLimit",
                "account": "1000123456",
                "exchange": "MOEX",
                "ticker": currency,
                "displayName": f"Денежные средства, {currency}",
                "currency": currency,
                "upperType": "CURRENCY",
                "instrumentType": "MONEY",
                "term": "T0",
                "quantity": round(amount, 2),
                "locked": round(amount * 0.03, 2),
                "currentValueRub": round(amount * (1.0 if currency == "RUB" else 12.4), 2),
                "loadDate": now.isoformat(),
            }
        )

    total = sum(item["currentValueRub"] for item in payload if item["type"] != "moneyLimit") or 1.0
    for item in payload:
        if item["type"] != "moneyLimit":
            item["portfolioShare"] = round(item["currentValueRub"] / total * 100, 2)
    # Дубликат строки с T1 — чтобы показать, что фильтр по term реально работает.
    clone = dict(payload[0])
    clone["term"] = "T1"
    clone["quantity"] = payload[0]["quantity"] + 0  # тот же остаток, другой режим расчётов
    payload.append(clone)
    return payload


def fake_trades_payload(days: int = 45, *, rng: random.Random | None = None) -> dict[str, Any]:
    """Сырой ответ ``POST /trades/search`` (одна страница, «сколько есть»)."""
    rng = rng or random.Random(11)
    now = _now()
    records: list[dict[str, Any]] = []
    trade_num = 10_000_000
    for _ in range(rng.randint(34, 60)):
        ticker, _name, itype, _currency, board, price = rng.choice(_INSTRUMENTS)
        side = rng.choice(["1", "2"])
        quantity = rng.choice([1, 2, 5, 10, 25, 50]) * (10 if itype == "BONDS" else 1)
        trade_price = round(price * rng.uniform(0.94, 1.06), 4)
        moment = now - dt.timedelta(
            days=rng.randint(0, max(1, days)), hours=rng.randint(0, 7), minutes=rng.randint(0, 59)
        )
        trade_num += rng.randint(1, 9)
        records.append(
            {
                "orderNum": 900_000_000 + trade_num,
                "ticker": ticker,
                "tradeNum": trade_num,
                "clientCode": "1000123456",
                "classCode": board,
                "settlementCurrency": "RUB",
                "baseCurrency": "RUB",
                "priceCurrency": "RUB",
                "side": side,
                "instrumentType": itype,
                "dealType": 0,
                "tradeDateTime": moment.isoformat().replace("+00:00", "Z"),
                "price": trade_price,
                "volume": round(trade_price * quantity, 2),
                "go": round(trade_price * quantity * 0.12, 2) if itype == "FUTURES" else None,
                "contractAmount": round(trade_price * quantity, 2) if itype == "FUTURES" else None,
                "settleDate": (moment + dt.timedelta(days=1)).date().isoformat(),
                "tradeQuantity": quantity,
                "tradeQuantityLots": quantity,
            }
        )
    records.sort(key=lambda x: x["tradeDateTime"], reverse=True)
    return {"records": records, "totalRecords": len(records), "totalPages": 1}


def fake_portfolio(rng: random.Random | None = None) -> Portfolio:
    return Portfolio.from_api(fake_portfolio_payload(rng), term="T0")


def fake_trades(days: int = 45, rng: random.Random | None = None) -> list[Trade]:
    payload = fake_trades_payload(days, rng=rng)
    return [Trade.from_api(item) for item in payload["records"]]


def fake_limits_payload(rng: random.Random | None = None) -> dict[str, Any]:
    """Сырой ответ ``GET /limits``, синтезированный из фикстуры портфеля."""
    rng = rng or random.Random(29)
    depo: list[dict[str, Any]] = []
    money: list[dict[str, Any]] = []
    for item in fake_portfolio_payload(rng):
        if item.get("type") == "moneyLimit":
            money.append(
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
    return {"depoLimit": depo, "moneyLimits": money, "futuresLimits": [], "futureHolding": []}


def fake_orders_payload(days: int = 45, *, rng: random.Random | None = None) -> dict[str, Any]:
    """Сырой ответ ``POST /orders/search`` (одна страница)."""
    rng = rng or random.Random(17)
    now = _now()
    records: list[dict[str, Any]] = []
    order_num = 899_000_000
    for _ in range(rng.randint(8, 18)):
        ticker, _name, _itype, _currency, board, base_price = rng.choice(_INSTRUMENTS)
        side = rng.choice(["1", "2"])
        quantity = rng.choice([1, 2, 5, 10, 25, 50])
        order_type = rng.choice([1, 2, 2, 4])
        status = rng.choice([1, 2, 2, 2, 3])
        if status == 2:
            executed = quantity
        elif status == 3:
            executed = rng.choice([0, quantity // 2, quantity])
        else:
            executed = 0
        price = round(base_price * rng.uniform(0.97, 1.04), 2)
        moment = now - dt.timedelta(
            days=rng.randint(0, max(1, days)), hours=rng.randint(0, 8), minutes=rng.randint(0, 59)
        )
        order_num += rng.randint(1, 7)
        records.append(
            {
                "orderNum": order_num,
                "orderId": f"mock-order-{order_num}",
                "ticker": ticker,
                "classCode": board,
                "side": int(side),
                "price": price,
                "orderQuantity": quantity,
                "executedQuantity": executed,
                "remainedQuantity": quantity - executed,
                "averagePrice": round(price * rng.uniform(0.99, 1.02), 2) if executed else None,
                "executedValue": round(price * executed, 2) if executed else None,
                "orderStatus": status,
                "orderType": order_type,
                "settlementCurrency": "RUB",
                "orderDateTime": moment.isoformat().replace("+00:00", "Z"),
                "executionDateTime": (moment + dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            }
        )
    records.sort(key=lambda x: x["orderDateTime"], reverse=True)
    return {"records": records, "totalRecords": len(records), "totalPages": 1}


def fake_operations_payload(days: int = 90, *, rng: random.Random | None = None) -> dict[str, Any]:
    """Сырой ответ ``POST /operations/search`` — купоны, дивиденды, комиссии и т.п."""
    rng = rng or random.Random(31)
    now = _now()
    defs = [
        # (тип, эмитент, тикер, ISIN, валюта, знак суммы, балансовое изменение)
        ("Dividend", "ПАО Сбербанк", "SBER", "RU0009029557", "RUB", 1, "Positive"),
        ("BondPayingOff", "ОФЗ 26216", "SU26216", "RU000A0JXG57", "RUB", 1, "Positive"),
        ("Commission", "Брокерская комиссия", "", "", "RUB", -1, "Negative"),
        ("Tax", "Налог на дивиденды", "SBER", "RU0009029557", "RUB", -1, "Negative"),
        ("PayIn", "Пополнение счёта", "", "", "RUB", 1, "Positive"),
        ("PayOut", "Вывод средств", "", "", "RUB", -1, "Negative"),
        ("BondPayingOff", "ОФЗ 26243", "SU26243", "RU000A0JXT11", "RUB", 1, "Positive"),
    ]
    records: list[dict[str, Any]] = []
    for i in range(rng.randint(6, 14)):
        kind, issuer, ticker, isin, currency, sign, change = rng.choice(defs)
        if kind in ("Dividend", "BondPayingOff"):
            amount = round(rng.uniform(80.0, 9_000.0), 2)
        elif kind == "Commission":
            amount = round(rng.uniform(15.0, 600.0), 2)
        elif kind == "Tax":
            amount = round(rng.uniform(30.0, 1_500.0), 2)
        elif kind == "PayIn":
            amount = round(rng.uniform(5_000.0, 250_000.0), 2)
        else:
            amount = round(rng.uniform(5_000.0, 120_000.0), 2)
        moment = now - dt.timedelta(
            days=rng.randint(0, max(1, days)), hours=rng.randint(0, 20), minutes=rng.randint(0, 59)
        )
        records.append(
            {
                "id": f"demo-op-{i + 1}",
                "date": moment.isoformat(),
                "ticker": ticker or None,
                "classCode": "TQBR" if ticker else None,
                "type": kind,
                "status": "Approved",
                "sum": round(amount * sign, 2),
                "currency": currency,
                "isin": isin or None,
                "issuerName": issuer,
                "balanceChange": change,
            }
        )
    records.sort(key=lambda x: x["date"], reverse=True)
    return {"records": records, "pageSize": len(records)}


def fake_orders(days: int = 45, rng: random.Random | None = None) -> list[Order]:
    payload = fake_orders_payload(days, rng=rng)
    return [Order.from_api(item) for item in payload["records"]]


def fake_operations(days: int = 90, rng: random.Random | None = None) -> list[dict[str, Any]]:
    payload = fake_operations_payload(days, rng=rng)
    return list(payload["records"])
