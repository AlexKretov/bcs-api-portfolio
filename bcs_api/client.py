"""Клиент BCS Trade API (https://trade-api.bcs.ru) — портфель, лимиты, сделки, заявки.

Эндпоинты (проверены по документации, все относительно ``https://be.broker.ru``):

======================================  ====  ==================================================
Сервис                                  Метод Путь
======================================  ====  ==================================================
Авторизация                             POST  ``/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token``
Портфель                                GET   ``/trade-api-bff-portfolio/api/v1/portfolio``
Лимиты                                  GET   ``/trade-api-bff-limit/api/v1/limits``
Сделки                                  POST  ``/trade-api-bff-trade-details/api/v1/trades/search``
Заявки                                  POST  ``/trade-api-bff-order-details/api/v1/orders/search``
Неторговые операции (купоны/дивиденды)  POST  ``/trade-api-bff-nontrade-operations/api/v1/operations/search``
======================================  ====  ==================================================

⚠️ По данным документации в списках сделок и заявок отдаются только операции,
начиная с **26.01.2026** (``TRADE_HISTORY_FROM``).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from .http_client import AUTH_URL, BcsHttp, join_url
from .tokens import TokenStore

log = logging.getLogger("bcs.client")

#: Путь Keycloak-реalm'а авторизации (приложение к любому base_url).
KEYCLOAK_TOKEN_PATH = "/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token"

#: Первый день доступной истории сделок/заявок по документации API.
TRADE_HISTORY_FROM = dt.date(2026, 1, 26)

#: Разрешённые значения сортировки для /trades/search.
TRADE_SORTS = (
    "tradeDateTime,asc",
    "tradeDateTime,desc",
    "ticker,asc",
    "ticker,desc",
    "classCode,asc",
    "classCode,desc",
    "side,asc",
    "side,desc",
)

SIDE_LABELS = {"1": "покупка", "2": "продажа"}

INSTRUMENT_LABELS = {
    "CURRENCY": "Валюта",
    "STOCK": "Акция РФ",
    "FOREIGN_STOCK": "Иностранная акция",
    "BONDS": "Облигация",
    "NOTES": "Нота",
    "DEPOSITARY_RECEIPTS": "Деп. расписка",
    "EURO_BONDS": "Еврооблигация",
    "MUTUAL_FUNDS": "ПИФ",
    "ETF": "ETF",
    "FUTURES": "Фьючерс",
    "OPTIONS": "Опцион",
    "GOODS": "Товар",
    "INDICES": "Индекс",
}

ORDER_STATUS_LABELS = {1: "снята", 2: "исполнена", 3: "активна"}
ORDER_TYPE_LABELS = {
    1: "рыночная",
    2: "лимитная",
    3: "айсберг",
    4: "стоп-лимит",
    5: "тейк-профит",
    6: "стоп-лосс",
    7: "тейк-профит и стоп-лосс",
    10: "лимитная на 30 дней",
    11: "тейк-профит",
}


# --------------------------------------------------------------------- models


@dataclass
class Position:
    """Одна позиция портфеля из ``/portfolio`` (элементы массива ``type != moneyLimit``)."""

    ticker: str = ""
    display_name: str = ""
    instrument_type: str = ""
    upper_type: str = ""
    currency: str = ""
    board: str = ""
    exchange: str = ""
    account: str = ""
    term: str = ""
    quantity: float = 0.0
    locked: float = 0.0
    balance_price: float = 0.0
    current_price: float = 0.0
    balance_value_rub: float = 0.0
    current_value_rub: float = 0.0
    current_value: float = 0.0
    unrealized_pl: float = 0.0
    unrealized_percent_pl: float = 0.0
    daily_pl: float = 0.0
    daily_percent_pl: float = 0.0
    portfolio_share: float = 0.0
    accrued_income: float = 0.0
    face_value: Optional[float] = None
    is_blocked: bool = False
    ratio_quantity: Optional[float] = None
    expire_date: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def lots(self) -> Optional[float]:
        """Количество в лотах (если API вернул размер лота)."""
        if self.ratio_quantity:
            return self.quantity / self.ratio_quantity
        return None

    @property
    def label(self) -> str:
        return self.display_name or self.ticker

    @property
    def type_label(self) -> str:
        return INSTRUMENT_LABELS.get(self.instrument_type, self.instrument_type or "—")

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Position:
        return cls(
            ticker=_s(data.get("ticker")),
            display_name=_s(data.get("displayName")),
            instrument_type=_s(data.get("instrumentType")),
            upper_type=_s(data.get("upperType")),
            currency=_s(data.get("currency")),
            board=_s(data.get("board")),
            exchange=_s(data.get("exchange")),
            account=_s(data.get("account")),
            term=_s(data.get("term")),
            quantity=_f(data.get("quantity")),
            locked=_f(data.get("locked")),
            balance_price=_f(data.get("balancePrice")),
            current_price=_f(data.get("currentPrice")),
            balance_value_rub=_f(data.get("balanceValueRub")),
            current_value_rub=_f(data.get("currentValueRub")),
            current_value=_f(data.get("currentValue")),
            unrealized_pl=_f(data.get("unrealizedPL")),
            unrealized_percent_pl=_f(data.get("unrealizedPercentPL")),
            daily_pl=_f(data.get("dailyPL")),
            daily_percent_pl=_f(data.get("dailyPercentPL")),
            portfolio_share=_f(data.get("portfolioShare")),
            accrued_income=_f(data.get("accruedIncome")),
            face_value=_f(data.get("faceValue"), None),
            is_blocked=bool(data.get("isBlocked")),
            ratio_quantity=_f(data.get("ratioQuantity"), None),
            expire_date=data.get("expireDate"),
            raw=dict(data),
        )


@dataclass
class CashBalance:
    """Денежная позиция (элементы массива ``type == moneyLimit``) — остаток по валютам."""

    currency: str = ""
    ticker: str = ""
    exchange: str = ""
    account: str = ""
    term: str = ""
    quantity: float = 0.0
    locked: float = 0.0
    current_value_rub: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def available(self) -> float:
        """Свободный остаток (без того, что занято заявками/ГО)."""
        return self.quantity - self.locked

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CashBalance:
        return cls(
            currency=_s(data.get("currency") or data.get("ticker")),
            ticker=_s(data.get("ticker")),
            exchange=_s(data.get("exchange")),
            account=_s(data.get("account")),
            term=_s(data.get("term")),
            quantity=_f(data.get("quantity")),
            locked=_f(data.get("locked")),
            current_value_rub=_f(data.get("currentValueRub")),
            raw=dict(data),
        )


@dataclass
class Portfolio:
    """Снимок портфеля: деньги + позиции + сводные итоги."""

    positions: list[Position] = field(default_factory=list)
    cash: list[CashBalance] = field(default_factory=list)
    as_of: Optional[dt.datetime] = None
    raw: list[dict[str, Any]] = field(default_factory=list)

    @property
    def securities_value_rub(self) -> float:
        return sum(p.current_value_rub for p in self.positions)

    @property
    def cash_rub(self) -> float:
        """Денежный остаток в рублёвом эквиваленте (API уже пересчитывает всё в RUB)."""
        return sum(c.current_value_rub for c in self.cash)

    @property
    def total_value_rub(self) -> float:
        return self.securities_value_rub + sum(c.current_value_rub for c in self.cash)

    @property
    def total_unrealized_pl(self) -> float:
        return sum(p.unrealized_pl for p in self.positions)

    @property
    def total_daily_pl(self) -> float:
        return sum(p.daily_pl for p in self.positions)

    def by_type(self) -> dict[str, float]:
        """Стоимость в ₽, сгруппированная по типу инструмента."""
        out: dict[str, float] = {}
        for pos in self.positions:
            key = pos.type_label
            out[key] = out.get(key, 0.0) + pos.current_value_rub
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def top_positions(self, n: int = 10) -> list[Position]:
        return sorted(self.positions, key=lambda p: -p.current_value_rub)[:n]

    @classmethod
    def from_api(cls, data: Any, *, term: Optional[str] = None) -> Portfolio:
        """Разложить ответ ``/portfolio`` на деньги и позиции.

        ``term`` — фильтр по режиму расчётов (``T0``/``T1``/…). По умолчанию ``T0``:
        API отдаёт одну и ту же бумагу отдельными строками под каждый режим, и без
        фильтра итоги задваиваются. ``term=None`` — оставить все строки.
        """
        if isinstance(data, dict):  # на случай, если массив придёт в обёртке
            for key in ("positions", "data", "items", "content"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise ValueError(f"неожиданный формат ответа /portfolio: {type(data).__name__}")

        positions: list[Position] = []
        cash: list[CashBalance] = []
        as_of: Optional[dt.datetime] = None
        for item in data:
            if not isinstance(item, dict):
                continue
            stamp = parse_datetime(item.get("loadDate") or item.get("updateDateTime"))
            if stamp and (as_of is None or stamp > as_of):
                as_of = stamp
            if item.get("type") == "moneyLimit":
                cash.append(CashBalance.from_api(item))
                continue
            if term and item.get("term") and item.get("term") != term:
                continue
            position = Position.from_api(item)
            if position.quantity == 0 and position.current_value_rub == 0:
                continue  # нулевые «хвосты» не интересны
            positions.append(position)
        return cls(
            positions=positions,
            cash=cash,
            as_of=as_of,
            raw=[x for x in data if isinstance(x, dict)],
        )


@dataclass
class Trade:
    """Биржевая сделка из ``/trades/search``."""

    trade_num: Optional[int] = None
    order_num: Optional[int] = None
    date_time: Optional[dt.datetime] = None
    ticker: str = ""
    class_code: str = ""
    side: str = ""
    price: float = 0.0
    volume: float = 0.0
    trade_quantity: float = 0.0
    trade_quantity_lots: Optional[float] = None
    settlement_currency: str = ""
    price_currency: str = ""
    base_currency: str = ""
    instrument_type: str = ""
    go: Optional[float] = None
    contract_amount: Optional[float] = None
    settle_date: Optional[str] = None
    deal_type: Optional[int] = None
    client_code: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def side_label(self) -> str:
        return SIDE_LABELS.get(str(self.side), str(self.side) or "—")

    @property
    def type_label(self) -> str:
        return INSTRUMENT_LABELS.get(self.instrument_type, self.instrument_type or "—")

    @property
    def datetime_utc(self) -> Optional[dt.datetime]:
        return self.date_time.astimezone(dt.timezone.utc) if self.date_time else None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Trade:
        return cls(
            trade_num=_i(data.get("tradeNum")),
            order_num=_i(data.get("orderNum")),
            date_time=parse_datetime(data.get("tradeDateTime")),
            ticker=_s(data.get("ticker")),
            class_code=_s(data.get("classCode")),
            side=_s(data.get("side")),
            price=_f(data.get("price")),
            volume=_f(data.get("volume")),
            trade_quantity=_f(data.get("tradeQuantity")),
            trade_quantity_lots=_f(data.get("tradeQuantityLots"), None),
            settlement_currency=_s(data.get("settlementCurrency")),
            price_currency=_s(data.get("priceCurrency")),
            base_currency=_s(data.get("baseCurrency")),
            instrument_type=_s(data.get("instrumentType")),
            go=_f(data.get("go"), None),
            contract_amount=_f(data.get("contractAmount"), None),
            settle_date=data.get("settleDate"),
            deal_type=_i(data.get("dealType")),
            client_code=_s(data.get("clientCode")),
            raw=dict(data),
        )

    def summary(self) -> dict[str, Any]:
        """Плоский dict для CSV/JSON-отчёта."""
        return {
            "trade_date": self.date_time.astimezone().strftime("%Y-%m-%d %H:%M:%S") if self.date_time else "",
            "ticker": self.ticker,
            "class_code": self.class_code,
            "side": self.side,
            "side_label": self.side_label,
            "quantity": self.trade_quantity,
            "quantity_lots": self.trade_quantity_lots,
            "price": self.price,
            "volume": self.volume,
            "currency": self.settlement_currency,
            "instrument_type": self.instrument_type,
            "order_num": self.order_num,
            "trade_num": self.trade_num,
            "settle_date": self.settle_date,
        }


@dataclass
class TradePage:
    """Страница ответа /trades/search + метаданные пагинации."""

    records: list[Trade]
    page: int
    size: int
    total_records: int
    total_pages: int


@dataclass
class Order:
    """Заявка из ``/orders/search``."""

    order_num: Optional[int] = None
    order_id: str = ""
    ticker: str = ""
    class_code: str = ""
    side: str = ""
    price: float = 0.0
    order_quantity: float = 0.0
    executed_quantity: float = 0.0
    remained_quantity: float = 0.0
    average_price: Optional[float] = None
    executed_value: Optional[float] = None
    order_status: Optional[int] = None
    order_type: Optional[int] = None
    order_date_time: Optional[dt.datetime] = None
    execution_date_time: Optional[dt.datetime] = None
    settlement_currency: str = ""
    reject_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def status_label(self) -> str:
        return ORDER_STATUS_LABELS.get(self.order_status or 0, str(self.order_status))

    @property
    def type_label(self) -> str:
        return ORDER_TYPE_LABELS.get(self.order_type or 0, str(self.order_type))

    @property
    def side_label(self) -> str:
        return SIDE_LABELS.get(str(self.side), str(self.side) or "—")

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Order:
        return cls(
            order_num=_i(data.get("orderNum")),
            order_id=_s(data.get("orderId")),
            ticker=_s(data.get("ticker")),
            class_code=_s(data.get("classCode")),
            side=_s(data.get("side")),
            price=_f(data.get("price")),
            order_quantity=_f(data.get("orderQuantity")),
            executed_quantity=_f(data.get("executedQuantity")),
            remained_quantity=_f(data.get("remainedQuantity")),
            average_price=_f(data.get("averagePrice"), None),
            executed_value=_f(data.get("executedValue"), None),
            order_status=_i(data.get("orderStatus")),
            order_type=_i(data.get("orderType")),
            order_date_time=parse_datetime(data.get("orderDateTime")),
            execution_date_time=parse_datetime(data.get("executionDateTime")),
            settlement_currency=_s(data.get("settlementCurrency")),
            reject_reason=_s(data.get("rejectReason")),
            raw=dict(data),
        )


# --------------------------------------------------------------------- client


class BcsClient:
    """Точка входа: авторизация + чтение портфеля, лимитов, сделок, заявок."""

    def __init__(
        self,
        *,
        refresh_token: Optional[str] = None,
        config_path: Optional[str] = None,
        cache_path: Optional[str] = None,
        client_id: str = "trade-api-read",
        base_url: str = "https://be.broker.ru",
        timeout: float = 30.0,
        max_retries: int = 4,
        rps: float = 10.0,
        session: Any = None,
        refresh_token_source: str = "аргумент BcsClient(refresh_token=…)",
    ) -> None:
        cfg = load_config(config_path, refresh_token=refresh_token, refresh_token_source=refresh_token_source)
        self.base_url = cfg.get("base_url") or base_url
        self._paths = dict(SERVICE_PATHS)
        if cfg.get("auth_url"):
            self._auth_url = str(cfg["auth_url"])
        elif str(self.base_url).rstrip("/") != "https://be.broker.ru":
            # Свой хост (тесты, корпоративный прокси) — путь авторизации берём оттуда же.
            self._auth_url = join_url(self.base_url, KEYCLOAK_TOKEN_PATH)
        else:
            self._auth_url = AUTH_URL
        if cfg.get("portfolio_path"):  # служебное, для интеграционных тестов
            self._paths["portfolio"] = str(cfg["portfolio_path"])

        self.config_sources: dict[str, str] = dict(cfg.get("_sources") or {})
        self.token_notes: list[str] = list(cfg.get("_token_notes") or [])
        self.config_path: Optional[str] = cfg.get("_config_path")
        self.configured_refresh_token: Optional[str] = cfg.get("refresh_token")
        self.store = TokenStore(
            cache_path or cfg.get("cache_path") or ".bcs-tokens.json",
            refresh_token_provider=lambda: cfg.get("refresh_token"),
            refresh_source_label=self.config_sources.get("refresh_token", "конфиг/окружение"),
        )
        self.http = BcsHttp(
            store=self.store,
            configured_refresh=lambda: self.configured_refresh_token,
            token_source=self.config_sources.get("refresh_token", "—"),
            session=session,
            auth_url=self._auth_url,
            client_id=cfg.get("client_id") or client_id,
            timeout=float(cfg.get("timeout") or timeout),
            max_retries=int(cfg.get("max_retries") or max_retries),
            rps=float(cfg.get("rps") or rps),
        )

    # ------------------------------------------------------------------ urls

    def url(self, service: str) -> str:
        return join_url(self.base_url, self._paths[service])

    # -------------------------------------------------------------- portfolio

    def get_portfolio(self, *, term: Optional[str] = "T0") -> Portfolio:
        """``GET /trade-api-bff-portfolio/api/v1/portfolio`` → разобранный портфель."""
        return Portfolio.from_api(self.http.request("GET", self.url("portfolio")), term=term)

    def get_portfolio_raw(self) -> Any:
        """Сырой JSON портфеля (все поля, включая те, что не маппятся)."""
        return self.http.request("GET", self.url("portfolio"))

    def get_limits(self) -> dict[str, Any]:
        """``GET /trade-api-bff-limit/api/v1/limits`` — позиции и деньги «как видит клиринг»."""
        data = self.http.request("GET", self.url("limits"))
        return data if isinstance(data, dict) else {}

    # ----------------------------------------------------------------- trades

    def search_trades(
        self,
        *,
        since: Any = None,
        until: Any = None,
        tickers: Optional[Sequence[str]] = None,
        class_codes: Optional[Sequence[str]] = None,
        side: Optional[str] = None,
        trade_nums: Optional[Sequence[int]] = None,
        page: int = 0,
        size: int = 50,
        sort: Optional[Sequence[str]] = None,
    ) -> TradePage:
        """Одна страница ``POST /trades/search``."""
        body = _trades_body(
            since=since,
            until=until,
            tickers=tickers,
            class_codes=class_codes,
            side=side,
            trade_nums=trade_nums,
        )
        params = {"page": int(page), "size": int(size)}
        if sort:
            params["sort"] = list(sort)
        data = self.http.request("POST", self.url("trades"), params=params, json_body=body)
        return _page_of(data, Trade, page=page, size=size)

    def iter_trades(self, **kwargs: Any) -> Iterator[Trade]:
        """Все сделки постранично: сам идёт по ``totalPages``; фильтры — как в :meth:`search_trades`.

        Ключевые слова ``max_pages`` и ``limit`` ограничивают выборку.
        """
        max_pages = int(kwargs.pop("max_pages", 200))
        limit = kwargs.pop("limit", None)
        size = int(kwargs.pop("size", 50))
        page_no = 0
        emitted = 0
        while page_no < max_pages:
            result = self.search_trades(page=page_no, size=size, **kwargs)
            if not result.records:
                break
            for trade in result.records:
                yield trade
                emitted += 1
                if limit is not None and emitted >= int(limit):
                    return
            if result.total_pages and page_no + 1 >= result.total_pages:
                break
            if not result.total_pages and len(result.records) < size:
                break
            page_no += 1
        if page_no >= max_pages:
            log.warning("остановлен обход сделок: достигнут лимит max_pages=%d", max_pages)

    # ----------------------------------------------------------------- orders

    def search_orders(
        self,
        *,
        since: Any = None,
        until: Any = None,
        tickers: Optional[Sequence[str]] = None,
        class_codes: Optional[Sequence[str]] = None,
        side: Optional[str] = None,
        order_status: Optional[Sequence[int]] = None,
        order_types: Optional[Sequence[int]] = None,
        page: int = 0,
        size: int = 50,
        sort: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """``POST /orders/search`` — список собственных заявок с теми же фильтрами."""
        body: dict[str, Any] = {}
        start, end = iso_or_none(since), iso_or_none(until)
        if start:
            body["startDateTime"] = start
        if end:
            body["endDateTime"] = end
        if tickers:
            body["tickers"] = list(tickers)
        if class_codes:
            body["classCodes"] = list(class_codes)
        if side is not None:
            body["side"] = parse_side(side)
        if order_status:
            body["orderStatus"] = [int(x) for x in order_status]
        if order_types:
            body["orderTypes"] = [int(x) for x in order_types]
        params: dict[str, Any] = {"page": int(page), "size": int(size)}
        if sort:
            params["sort"] = list(sort)
        return self.http.request("POST", self.url("orders"), params=params, json_body=body)

    def iter_orders(self, *, max_pages: int = 100, size: int = 50, **kwargs: Any) -> Iterator[Order]:
        """Собирает страницы подряд; ``kwargs`` — те же фильтры, что у :meth:`search_orders`."""
        page_no = 0
        while page_no < max_pages:
            data = self.search_orders(page=page_no, size=size, **kwargs)
            records = data.get("records") if isinstance(data, dict) else None
            if not records:
                break
            for item in records:
                yield Order.from_api(item)
            total_pages = int(data.get("totalPages") or 0) if isinstance(data, dict) else 0
            if total_pages and page_no + 1 >= total_pages:
                break
            if not total_pages and len(records) < size:
                break
            page_no += 1

    # -------------------------------------------------- non-trading operations

    def search_operations(
        self,
        *,
        since: Any = None,
        until: Any = None,
        operation_types: Optional[Sequence[str]] = None,
        statuses: Optional[Sequence[str]] = None,
        tickers: Optional[Sequence[str]] = None,
        currencies: Optional[Sequence[str]] = None,
        page: int = 0,
        size: int = 50,
    ) -> dict[str, Any]:
        """``POST /nontrade-operations`` — купоны, дивиденды, комиссии, пополнения/выводы.

        Сервис ограничени 3 RPS (см. /restrictions), он и идёт в отдельной «медленной» корзине.
        """
        body: dict[str, Any] = {}
        start, end = iso_or_none(since), iso_or_none(until)
        if start:
            body["startDateTime"] = start
        if end:
            body["endDateTime"] = end
        if operation_types:
            body["operationTypes"] = list(operation_types)
        if statuses:
            body["statuses"] = list(statuses)
        if tickers:
            body["tickers"] = list(tickers)
        if currencies:
            body["currencies"] = list(currencies)
        return self.http.request(
            "POST",
            self.url("operations"),
            params={"page": int(page), "size": int(size)},
            json_body=body,
            slow=True,
        )

    def iter_operations(self, *, max_pages: int = 100, size: int = 50, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Все неторговые операции постранично (сырые dict'ы — полей много, они почти текстовые)."""
        page_no = 0
        while page_no < max_pages:
            data = self.search_operations(page=page_no, size=size, **kwargs)
            records = data.get("records") if isinstance(data, dict) else None
            if not records:
                break
            yield from records
            page_size = int(data.get("pageSize") or 0) if isinstance(data, dict) else 0
            if len(records) < max(page_size, size):
                break
            page_no += 1

    # ------------------------------------------------------------ information

    def get_instruments_by_tickers(
        self,
        tickers: Sequence[str],
        *,
        class_codes: Optional[Sequence[str]] = None,
        page: int = 0,
        size: int = 50,
    ) -> list[dict[str, Any]]:
        """Справочник по тикерам: ``POST /api/v1/instruments/by-tickers``.

        Документация (https://trade-api.bcs.ru/http/information/get-instruments-by-tickers)
        не раскрывает схему тела — поэтому метод максимально терпим к формату ответа
        и никогда не роняет вызывающий код: ошибка справочника не должна мешать
        получить портфель.
        """
        body: dict[str, Any] = {"tickers": list(tickers), "page": int(page), "size": int(size)}
        if class_codes:
            body["classCodes"] = list(class_codes)
        try:
            data = self.http.request("POST", self.url("instruments"), json_body=body)
        except Exception as exc:
            log.debug("справочник инструментов недоступен: %s", exc)
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("records", "content", "items", "instruments", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        return []

    def instrument_names(self, tickers: Sequence[str]) -> dict[str, str]:
        """``тикер → краткое название`` для подписей в отчётах; пусто — если справочник недоступен."""
        unique = sorted({t for t in tickers if t})
        if not unique:
            return {}
        out: dict[str, str] = {}
        for item in self.get_instruments_by_tickers(unique):
            ticker = str(item.get("ticker") or item.get("shortName") or "").strip()
            name = str(
                item.get("name")
                or item.get("fullName")
                or item.get("title")
                or item.get("displayName")
                or item.get("description")
                or ""
            ).strip()
            if ticker and name:
                out[ticker] = name
        return out

    # -------------------------------------------------------------- diagnostics

    def whoami(self) -> dict[str, Any]:
        """Сводка о конфигурации и токене — удобно для проверки, что «всё подключилось»."""
        tokens = self.http.token_set
        return {
            "base_url": self.base_url,
            "client_id": self.http.client_id,
            "refresh_token": mask_secret(self.store.refresh_token()),
            "access_token": mask_secret(tokens.access_token if tokens else None),
            "access_expires_in_h": round(tokens.access_ttl / 3600, 1) if tokens and tokens.access_token else None,
            "endpoints": {name: self.url(name) for name in self._paths},
        }


SERVICE_PATHS = {
    "portfolio": "/trade-api-bff-portfolio/api/v1/portfolio",
    "instruments": "/trade-api-bff-information/api/v1/instruments/by-tickers",
    "limits": "/trade-api-bff-limit/api/v1/limits",
    "trades": "/trade-api-bff-trade-details/api/v1/trades/search",
    "orders": "/trade-api-bff-order-details/api/v1/orders/search",
    "operations": "/trade-api-bff-nontrade-operations/api/v1/operations/search",
}


# ------------------------------------------------------------- config & utils


#: Ключ конфига → имя переменной окружения. Порядок приоритета: CLI → env → файл.
ENV_KEYS = {
    "refresh_token": "BCS_REFRESH_TOKEN",
    "client_id": "BCS_CLIENT_ID",
    "base_url": "BCS_API_BASE_URL",
    "cache_path": "BCS_TOKEN_CACHE",
    "auth_url": "BCS_AUTH_URL",
    "portfolio_path": "BCS_PORTFOLIO_PATH",
    "timeout": "BCS_TIMEOUT",
    "max_retries": "BCS_MAX_RETRIES",
    "rps": "BCS_RPS",
}

#: Имена файлов конфига, которые программа реально читает (по порядку, относительно CWD).
CONFIG_FILENAMES = ("bcs-config.json", ".bcs-config.json")
#: Дополнительно заглядываем домой — иначе запуск «не из папки проекта» выглядит как «токена нет».
HOME_CONFIG_PATHS = (".config/bcs/config.json", "bcs-config.json")


def load_config(
    path: Optional[str] = None,
    *,
    refresh_token: Optional[str] = None,
    refresh_token_source: str = "аргумент load_config(refresh_token=…)",
) -> dict[str, Any]:
    """Слить конфигурацию: CLI-аргументы → переменные окружения → JSON-файл.

    Помимо значений возвращает служебные ключи: ``_config_path`` (какой файл прочитан),
    ``_sources`` (откуда приехал каждый параметр — без этого ``invalid_grant`` невозможно
    объяснить: ``export BCS_REFRESH_TOKEN`` молча перебивает файл) и ``_token_notes``
    (что пришлось вычистить из строки токена).
    """
    import os
    from pathlib import Path

    from .diagnostics import normalize_refresh_token

    cfg: dict[str, Any] = {}
    sources: dict[str, str] = {}
    if path:
        candidates: list[Optional[str]] = [path]
    else:
        candidates = [os.environ.get("BCS_CONFIG"), *CONFIG_FILENAMES]
        candidates += [str(Path.home() / name) for name in HOME_CONFIG_PATHS]
    for candidate in candidates:
        if not candidate:
            continue
        file = Path(candidate).expanduser()
        if not file.is_file():
            continue
        try:
            loaded = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"не могу прочитать конфиг {file}: {exc}. Проверьте, что это валидный JSON: "
                "значение обязано быть в двойных кавычках и без запятой в конце объекта"
            ) from exc
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                cfg[key] = value
                sources.setdefault(key, f"файл {file}")
            cfg["_config_path"] = str(file)
        break

    for key, env in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
            sources[key] = f"переменная окружения {env}"
    if refresh_token:
        cfg["refresh_token"] = refresh_token
        sources["refresh_token"] = refresh_token_source

    # Нормализация токена: пробелы/кавычки/`Bearer ` из буфера обмена — частая причина
    # «Invalid refresh token» при полностью живом токене.
    raw_token = cfg.get("refresh_token")
    clean_token, notes = normalize_refresh_token(raw_token)
    cfg["refresh_token"] = clean_token or None
    cfg["_token_notes"] = notes
    cfg["_sources"] = sources
    return cfg


def mask_secret(value: Optional[str], *, keep: int = 4) -> str:
    """``629f7048…d1a4`` — показывать токен в логах целиком нельзя.

    Пробелы и переносы вырезаются до маскирования: «грязные» значения из буфера обмена
    иначе ломают вёрстку отчёта тем, что внутри маски оказывается перевод строки.
    """
    if not value:
        return "—"
    squashed = "".join(str(value).split())
    if not squashed:
        return "—"
    if len(squashed) <= keep * 2:
        return squashed[0] + "…"
    return f"{squashed[:keep]}…{squashed[-keep:]}"


def parse_datetime(value: Any) -> Optional[dt.datetime]:
    """ISO-8601 → aware datetime в локальной зоне; None, если разобрать нельзя."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = dt.datetime.strptime(str(value).strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                log.debug("не распознана дата %r", value)
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone()


def to_iso_z(value: Any) -> Optional[str]:
    """Разнообразный ввод пользователя («2026-01-26», «7d», datetime) → ISO-8601 UTC для API."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    else:
        text = str(value).strip()
        rel = _relative_days(text)
        if rel is not None:
            moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=rel)
        else:
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
                "%d.%m.%Y",
                "%d.%m.%Y %H:%M",
            ):
                try:
                    moment = dt.datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                try:
                    moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(
                        f"не понял дату {text!r}; ожидается ГГГГ-ММ-ДД, «ГГГГ-ММ-ДД ЧЧ:ММ» или относительное «30d»"
                    ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def iso_or_none(value: Any) -> Optional[str]:
    return to_iso_z(value) if value not in (None, "") else None


def _relative_days(text: str) -> Optional[int]:
    if len(text) >= 2 and text[-1] in "dDд" and text[:-1].isdigit():
        return int(text[:-1])
    return None


def _trades_body(
    *,
    since: Any,
    until: Any,
    tickers: Optional[Sequence[str]],
    class_codes: Optional[Sequence[str]],
    side: Optional[str],
    trade_nums: Optional[Sequence[int]],
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    start, end = iso_or_none(since), iso_or_none(until)
    if start:
        body["startDateTime"] = start
    if end:
        body["endDateTime"] = end
    if tickers:
        body["tickers"] = list(tickers)
    if class_codes:
        body["classCodes"] = list(class_codes)
    if side is not None:
        # В схеме side описан как строка ("1"/"2") — держимся схемы строкой.
        body["side"] = str(parse_side(side))
    if trade_nums:
        body["tradeNums"] = [int(x) for x in trade_nums]
    return body


def parse_side(side: Any) -> int:
    """«buy»/«покупка»/«1» → 1, «sell»/«продажа»/«2» → 2."""
    text = str(side).strip().lower()
    mapping = {"1": 1, "buy": 1, "b": 1, "покупка": 1, "куп": 1, "2": 2, "sell": 2, "s": 2, "продажа": 2, "прод": 2}
    if text in mapping:
        return mapping[text]
    raise ValueError(f"не понял направление {side!r}: нужно buy/sell или 1/2")


def _page_of(data: Any, model: type, *, page: int, size: int) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"неожиданный ответ сервиса: {type(data).__name__}")
    records = [model.from_api(item) for item in (data.get("records") or []) if isinstance(item, dict)]
    return TradePage(
        records=records,
        page=int(data.get("page", page) or page),
        size=int(data.get("size", size) or size),
        total_records=int(data.get("totalRecords") or 0),
        total_pages=int(data.get("totalPages") or 0),
    )


def _f(value: Any, default: float = 0.0) -> float:  # float-поле
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _s(value: Any) -> str:
    return "" if value is None else str(value)


__all__ = [
    "INSTRUMENT_LABELS",
    "ORDER_STATUS_LABELS",
    "ORDER_TYPE_LABELS",
    "SERVICE_PATHS",
    "SIDE_LABELS",
    "TRADE_HISTORY_FROM",
    "TRADE_SORTS",
    "BcsClient",
    "CashBalance",
    "Order",
    "Portfolio",
    "Position",
    "Trade",
    "TradePage",
    "load_config",
    "mask_secret",
    "parse_datetime",
    "parse_side",
    "to_iso_z",
]
