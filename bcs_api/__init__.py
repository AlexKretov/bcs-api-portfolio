"""Клиент БКС Торгового API: портфель, лимиты, сделки, заявки, неторговые операции.

Быстрый старт::

    from bcs_api import BcsClient

    from datetime import datetime, timedelta, timezone

    client = BcsClient()          # refresh-токен из BCS_REFRESH_TOKEN / bcs-config.json
    portfolio = client.get_portfolio()
    print(portfolio.total_value_rub, len(portfolio.positions))

    since = datetime.now(timezone.utc) - timedelta(days=30)
    for trade in list(client.iter_trades(since=since, size=100))[:10]:
        print(trade.date_time, trade.ticker, trade.side_label, trade.trade_quantity, trade.price)

CLI: ``python -m bcs_api portfolio`` / ``trades --days 30`` / ``export --out reports``.
"""

from __future__ import annotations

from .client import (
    INSTRUMENT_LABELS,
    SIDE_LABELS,
    TRADE_HISTORY_FROM,
    TRADE_SORTS,
    BcsClient,
    CashBalance,
    Order,
    Portfolio,
    Position,
    Trade,
    TradePage,
    mask_secret,
    to_iso_z,
)
from .errors import ApiError, AuthError, BcsError, RateLimitError, UnauthorizedError, ValidationError
from .http_client import BcsHttp
from .tokens import TokenSet, TokenStore

__version__ = "1.0.0"

__all__ = [
    "INSTRUMENT_LABELS",
    "SIDE_LABELS",
    "TRADE_HISTORY_FROM",
    "TRADE_SORTS",
    "ApiError",
    "AuthError",
    "BcsClient",
    "BcsError",
    "BcsHttp",
    "CashBalance",
    "Order",
    "Portfolio",
    "Position",
    "RateLimitError",
    "TokenSet",
    "TokenStore",
    "Trade",
    "TradePage",
    "UnauthorizedError",
    "ValidationError",
    "__version__",
    "mask_secret",
    "to_iso_z",
]
