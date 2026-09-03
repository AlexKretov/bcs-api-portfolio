"""CLI: ``python -m bcs_api`` — портфель и история сделок по BCS Trade API.

Примеры::

    export BCS_REFRESH_TOKEN='eyJhbGciOi…'
    python -m bcs_api token                      # проверка доступности и TTL токена
    python -m bcs_api portfolio                  # позиции + деньги + P/L
    python -m bcs_api trades --days 30           # сделки за 30 дней
    python -m bcs_api trades --since 2026-01-26 --ticker SBER --ticker GKGN -f csv
    python -m bcs_api export --days 90 --out ./reports   # JSON + CSV + Markdown
    python -m bcs_api demo                       # прогон на фикстурах, без сети и токена
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from typing import Any, Optional

from .client import (
    TRADE_HISTORY_FROM,
    TRADE_SORTS,
    BcsClient,
    Order,
    Portfolio,
    load_config,
    mask_secret,
    to_iso_z,
)

from .pnl import calculate_pnl, signed_operation_sum

from .errors import ApiError, AuthError, BcsError, RateLimitError, UnauthorizedError
from .export import portfolio_to_rows, save_report, trades_to_rows
from .formatting import build_table, format_limits, format_portfolio, format_trades, money, qty, short_datetime

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2

log = logging.getLogger("bcs.cli")


# --------------------------------------------------------------------- args


class _Formatter(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    """Сохраняем докстринг-примеры и показываем значения по умолчанию."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bcs_api",
        description="Чтение портфеля и истории сделок через БКС Торговое API (https://trade-api.bcs.ru).",
        epilog=(
            "Refresh-токен берётся из (в приоритете сверху вниз): ключей CLI, переменной окружения\n"
            "BCS_REFRESH_TOKEN, файла bcs-config.json. Получить токен: веб-версия «БКС Мир инвестиций»\n"
            "→ Профиль → «Счета и тарифы» → счёт → «Токены API» → «Выпустить токен».\n"
            f"Важно: сделки и заявки API отдаёт только с {TRADE_HISTORY_FROM:%d.%m.%Y}."
        ),
        formatter_class=_Formatter,
    )
    parser.add_argument("--refresh-token", help="refresh-токен из личного кабинета БКС (лучше через BCS_REFRESH_TOKEN)")
    parser.add_argument(
        "--client-id",
        choices=("trade-api-read", "trade-api-write"),
        help="тип токена: read — только чтение, write — торговля и чтение (по умолчанию read)",
    )
    parser.add_argument("--config", help="путь к JSON-конфигу (по умолчанию bcs-config.json рядом с запуском)")
    parser.add_argument("--cache", help="файл кэша access/refresh-токенов (по умолчанию .bcs-tokens.json)")
    parser.add_argument(
        "--base-url", help="база API; менять нужно только для тестов за прокси (по умолчанию https://be.broker.ru)"
    )
    parser.add_argument("--timeout", type=float, help="таймаут HTTP-запроса, с")
    parser.add_argument("--max-retries", type=int, help="число повторов на 429/5xx")
    parser.add_argument("--rps", type=float, help="самоограничение запросов в секунду (у БКС 10)")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="-v: INFO, -vv: DEBUG (показывает фактические запросы)"
    )
    parser.add_argument("--no-color", action="store_true", help="отключить цвет в таблице")
    parser.add_argument("--no-ask", action="store_true", help="не запрашивать refresh-токен в интерактивном режиме")

    sub = parser.add_subparsers(dest="command", required=True, metavar="КОМАНДА")

    p_token = sub.add_parser("token", help="проверить/получить access-токен, показать конфигурацию")
    p_token.add_argument("--force", action="store_true", help="принудительно перевыпустить access-токен")
    p_token.add_argument("--reset", action="store_true", help="удалить кэш токенов и выйти")
    p_token.add_argument(
        "--check",
        action="store_true",
        help="диагностика: откуда взялся токен, что с ним не так, какой конфиг реально читается",
    )
    p_token.set_defaults(func=cmd_token)

    p_port = sub.add_parser("portfolio", help="текущий портфель: позиции, деньги, P/L")
    _add_format_args(p_port)
    p_port.add_argument("--term", default="T0", help="режим расчётов (T0/T1/T2); 'all' — не фильтровать")
    p_port.add_argument("--top", type=int, default=0, help="показать только N крупнейших позиций (0 — все)")
    p_port.add_argument("--no-names", action="store_true", help="не обогащать названия через справочник инструментов")
    p_port.set_defaults(func=cmd_portfolio)

    p_lim = sub.add_parser("limits", help="лимиты/гарантийное обеспечение (второй источник данных по счёту)")
    _add_format_args(p_lim)
    p_lim.set_defaults(func=cmd_limits)

    p_tr = sub.add_parser("trades", help="история биржевых сделок")
    _add_format_args(p_tr)
    _add_range_args(p_tr)
    p_tr.add_argument("--page", type=int, default=0, help="номер страницы (0-based), если не задан --all-pages")
    p_tr.add_argument("--size", type=int, default=100, help="записей на странице (1..100)")
    p_tr.add_argument("--all-pages", action="store_true", help="собрать все страницы выборки")
    p_tr.add_argument("--max-pages", type=int, default=200, help="потолок страниц при --all-pages")
    p_tr.add_argument("--limit", type=int, help="остановиться после N сделок (полезно без фильтров)")
    p_tr.add_argument("--sort", action="append", choices=TRADE_SORTS, help="сортировка, можно несколько раз")
    p_tr.set_defaults(func=cmd_trades)

    p_or = sub.add_parser("orders", help="история заявок (с 26.01.2026)")
    _add_format_args(p_or)
    _add_range_args(p_or)
    p_or.add_argument("--status", type=int, action="append", choices=(1, 2, 3), help="1 снята, 2 исполнена, 3 активна")
    p_or.add_argument("--page", type=int, default=0)
    p_or.add_argument("--size", type=int, default=100)
    p_or.add_argument("--all-pages", action="store_true")
    p_or.add_argument("--limit", type=int)
    p_or.set_defaults(func=cmd_orders)

    p_op = sub.add_parser("operations", help="неторговые операции: купоны, дивиденды, комиссии, пополнения")
    _add_format_args(p_op)
    _add_range_args(p_op)
    p_op.add_argument("--type", dest="types", action="append", help="например Dividend, BondPayingOff, Commission")
    p_op.add_argument("--status", dest="statuses", action="append", choices=("Approved", "InProgress", "Rejected"))
    p_op.add_argument("--page", type=int, default=0)
    p_op.add_argument("--size", type=int, default=100)
    p_op.set_defaults(func=cmd_operations)

    p_pnl = sub.add_parser("pnl", help="отчёт о прибылях и убытках (P&L) за выбранный период")
    _add_format_args(p_pnl)
    _add_range_args(p_pnl)
    p_pnl.add_argument("--asset-type", dest="asset_types", action="append", help="STOCK, BONDS, FUTURES, FUNDS, MONEY")
    p_pnl.add_argument("--term", default="T0")
    p_pnl.set_defaults(func=cmd_pnl)

    p_exp = sub.add_parser("export", help="выгрузить портфель и сделки в файлы (JSON/CSV/Markdown)")
    p_exp.add_argument("--out", default="reports", help="папка для отчётов")
    p_exp.add_argument("--prefix", help="префикс имён файлов (по умолчанию bcs-report)")
    p_exp.add_argument("--formats", default="json,csv,md", help="список форматов через запятую")
    p_exp.add_argument("--include-orders", action="store_true", help="выгрузить и заявки")
    p_exp.add_argument("--include-operations", action="store_true", help="выгрузить неторговые операции")
    _add_range_args(p_exp)
    p_exp.add_argument("--term", default="T0")
    p_exp.add_argument("--size", type=int, default=100)
    p_exp.add_argument("--max-pages", type=int, default=200)
    p_exp.set_defaults(func=cmd_export)

    p_rep = sub.add_parser("report", help="всё сразу: портфель + лимиты + сделки за период")
    _add_range_args(p_rep)
    p_rep.add_argument("--term", default="T0", help="режим расчётов для портфеля; 'all' — все")
    p_rep.add_argument("--size", type=int, default=100, help="размер страницы при чтении сделок")
    p_rep.add_argument("--max-pages", type=int, default=200)
    p_rep.add_argument("--limit", type=int, help="не больше N сделок")
    p_rep.add_argument("--top", type=int, default=0, help="в таблице сделок показать только первые N строк")
    p_rep.set_defaults(func=cmd_report)

    p_watch = sub.add_parser("watch", help="обновлять портфель каждые N секунд (соблюдая лимит 10 RPS)")
    p_watch.add_argument("--interval", type=float, default=30.0, help="пауза между опросами, с")
    p_watch.add_argument("--count", type=int, default=0, help="сколько раз обновить (0 — бесконечно)")
    p_watch.add_argument("--term", default="T0")
    p_watch.set_defaults(func=cmd_watch)

    p_web = sub.add_parser(
        "web", help="веб-интерфейс: портфель/лимиты/сделки/заявки/операции кнопками в браузере"
    )
    p_web.add_argument("--host", default="127.0.0.1", help="адрес прослушивания")
    p_web.add_argument("--port", type=int, default=8080, help="порт")
    p_web.add_argument(
        "--mode",
        choices=("auto", "demo", "live"),
        default="auto",
        help="demo — синтетические данные; auto — по наличию токена",
    )
    p_web.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    p_web.set_defaults(func=cmd_web)

    p_demo = sub.add_parser("demo", help="показать вывод программы на фикстурах, без сети и токена")
    p_demo.add_argument("--days", type=int, default=45, help="за какой период сгенерировать фейковые сделки")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def _add_format_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-f", "--format", choices=("table", "json", "csv", "md"), default="table", help="формат вывода")
    parser.add_argument("--raw", action="store_true", help="напечатать сырой JSON ответа API")


def _add_range_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="начало периода: 2026-01-26, '2026-01-26 10:00' или ISO-8601")
    parser.add_argument("--until", help="конец периода (по умолчанию — сейчас)")
    parser.add_argument("--days", type=int, help="взять последние N дней (проще, чем --since)")
    parser.add_argument("--ticker", dest="tickers", action="append", help="фильтр по тикеру, можно несколько раз")
    parser.add_argument("--class-code", dest="class_codes", action="append", help="фильтр по классу (TQBR, ANCS, …)")
    parser.add_argument("--side", choices=("buy", "sell", "1", "2"), help="только покупки или только продажи")


# ------------------------------------------------------------------ helpers


def make_client(args: argparse.Namespace, *, force_refresh: bool = False) -> BcsClient:
    """Собрать клиент; при отсутствии токена — спросить в TTY."""
    refresh = args.refresh_token or os.environ.get("BCS_REFRESH_TOKEN")
    origin = (
        "ключ --refresh-token" if args.refresh_token else ("переменная окружения BCS_REFRESH_TOKEN" if refresh else "")
    )
    if not refresh and not args.no_ask:
        cfg = load_config(args.config)
        refresh = cfg.get("refresh_token")
        origin = cfg.get("_sources", {}).get("refresh_token", "")
    if not refresh and sys.stdin.isatty() and not args.no_ask:
        refresh = getpass.getpass("refresh-токен БКС (скрытый ввод): ").strip() or None
        if refresh:
            origin = "интерактивный ввод"
    kwargs: dict[str, Any] = {}
    # имена флагов (--config/--cache) отличаются от имён параметров BcsClient
    for attr, param in (
        ("client_id", "client_id"),
        ("config", "config_path"),
        ("cache", "cache_path"),
        ("base_url", "base_url"),
        ("timeout", "timeout"),
        ("max_retries", "max_retries"),
        ("rps", "rps"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            kwargs[param] = value
    kwargs["refresh_token"] = refresh
    kwargs["refresh_token_source"] = origin or "—"
    client = BcsClient(**kwargs)
    if force_refresh:
        client.http.invalidate_cache()
    return client


def resolve_range(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    """--days / --since / --until → ISO-8601 UTC для тела запроса."""
    since = getattr(args, "since", None)
    days = getattr(args, "days", None)
    if days:
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))
        since = since or start.isoformat()
    return (to_iso_z(since) if since else None, to_iso_z(getattr(args, "until", None)))


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def print_csv(rows: Sequence[dict[str, Any]]) -> None:
    import csv
    import io

    if not rows:
        print("# нет данных")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in keys})
    sys.stdout.write(buf.getvalue())


def supports_color(args: argparse.Namespace) -> bool:
    return sys.stdout.isatty() and not getattr(args, "no_color", False) and os.environ.get("NO_COLOR") is None


def setup_logging(verbosity: int) -> None:
    level = logging.WARNING - min(verbosity, 2) * 10
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if verbosity >= 1:
        logging.getLogger("bcs").setLevel(level)
    if verbosity >= 2:
        logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)


# ------------------------------------------------------------------- commands


def cmd_token(args: argparse.Namespace) -> int:
    client = make_client(args, force_refresh=args.force)
    if getattr(args, "check", False):
        return cmd_token_check(args, client)
    if args.reset:
        client.store.clear()
        print("кэш токенов удалён:", client.store.path or "(память)")
        return EXIT_OK
    info = client.whoami()
    if args.force or not info["access_token"] or info["access_token"] == "—":
        tokens = client.http.authenticate(force=args.force)
        info = client.whoami()
        print(f"access-токен получен, хватит на {tokens.access_ttl / 3600:.1f} ч")
    endpoints = info.pop("endpoints", {})
    print(build_table(["Параметр", "Значение"], [[k, v] for k, v in info.items()], aligns=["left", "left"]))
    if endpoints:
        print("\nАдреса сервисов:")
        for name, url in endpoints.items():
            print(f"  {name:12s} {url}")
    return EXIT_OK


def cmd_portfolio(args: argparse.Namespace) -> int:
    client = make_client(args)
    term = None if str(args.term).lower() in ("all", "none", "") else args.term
    raw = client.get_portfolio_raw()
    if args.raw:
        print_json(raw)
        return EXIT_OK
    portfolio = Portfolio.from_api(raw, term=term)
    if not args.no_names and portfolio.positions:
        names = client.instrument_names([p.ticker for p in portfolio.positions])
        for pos in portfolio.positions:
            if not pos.display_name and names.get(pos.ticker):
                pos.display_name = names[pos.ticker]
    if args.top:
        portfolio.positions = portfolio.top_positions(args.top)

    if args.format == "json":
        print_json({"summary": _portfolio_summary(portfolio), "positions": portfolio_to_rows(portfolio)})
    elif args.format == "csv":
        print_csv(portfolio_to_rows(portfolio))
    elif args.format == "md":
        from .export import portfolio_summary_markdown

        print(portfolio_summary_markdown(portfolio))
    else:
        note = "" if term is None else f" (term={term})"
        print(format_portfolio(portfolio, color=supports_color(args), title=f"Портфель{note}"))
        if term is not None:
            skipped = sum(
                1 for x in portfolio.raw if x.get("type") != "moneyLimit" and x.get("term") not in (None, term)
            )
            if skipped:
                print(
                    f"\nСкрыто строк с другим режимом расчётов (term ≠ {term}): {skipped}. "
                    "Показать все строки — --term all."
                )
    return EXIT_OK


def cmd_token_check(args: argparse.Namespace, client: BcsClient) -> int:
    """Развёрнутая диагностика конфигурации токена.

    Печатает только маски значений: длина, первые/последние символы, claims из JWT.
    """
    from pathlib import Path as _Path

    from .diagnostics import format_config_scan, inspect_token, scan_config_files

    print("=" * 68)
    print("Откуда берётся refresh-токен (приоритет: CLI → env → файл → кэш)")
    print("=" * 68)
    sources = client.config_sources or {}
    for key in ("refresh_token", "client_id", "base_url", "cache_path"):
        print(f"  {key:14s}: {sources.get(key, 'значение по умолчанию в коде')}")
    overridden = [
        key for key in ("refresh_token",) if sources.get(key, "").startswith("переменная") and client.config_path
    ]
    if overridden:
        print(
            "  ⚠ значение из переменной окружения перебивает bcs-config.json — правка файла ничего не меняет."
            " Сброс: unset BCS_REFRESH_TOKEN"
        )

    print()
    print(f"Кэш токенов           : {client.store.path or '(только память)'}")
    if client.store.path and _Path(client.store.path).is_file():
        cached = client.store.get()
        print(f"  в кэше лежит refresh: {mask_secret(cached.refresh_token if cached else None)}")
    configured = client.configured_refresh_token
    print(
        f"Значение, которое уйдёт в запрос: {mask_secret(configured)} "
        f"(длина {len(configured) if configured else 0}, источник: "
        f"{client.config_sources.get('refresh_token') or '—'})"
    )
    if client.token_notes:
        print("Что вычищено из значения:")
        for note in client.token_notes:
            print(f"  · {note}")

    print()
    print(format_config_scan(scan_config_files(_Path.cwd()), loaded_path=client.config_path))

    print()
    print("=" * 68)
    print("Осмотр значения (секрет не печатается)")
    print("=" * 68)
    tokens = client.http.token_set
    configured_source = client.config_sources.get("refresh_token") or "конфиг/окружение"
    reports = []
    if configured:
        reports.append((configured_source, configured))
    cached_refresh = tokens.refresh_token if tokens else None
    if cached_refresh and cached_refresh != configured:
        reports.append(
            ((tokens.refresh_source if tokens else None) or f"файл кэша {client.store.path}", cached_refresh)
        )
    if not reports:
        print("значения нет ни в конфиге, ни в кэше — обменять токен невозможно")
    for origin, value in reports:
        print()
        print(inspect_token(value, source=origin, requested_client_id=client.http.client_id).render())

    print()
    print("Что делать дальше:")
    print("  python3 -m bcs_api token --force   # принудительно перевыпустить пару токенов")
    print("  python3 -m bcs_api token --reset   # забыть кэш (если в нём осел чужой refresh)")
    return 0 if reports else EXIT_ERROR


def _portfolio_summary(portfolio: Portfolio) -> dict[str, Any]:
    return {
        "as_of": portfolio.as_of.isoformat() if portfolio.as_of else None,
        "positions": len(portfolio.positions),
        "securities_value_rub": round(portfolio.securities_value_rub, 2),
        "cash_rub": round(portfolio.cash_rub, 2),
        "total_value_rub": round(portfolio.total_value_rub, 2),
        "unrealized_pl": round(portfolio.total_unrealized_pl, 2),
        "daily_pl": round(portfolio.total_daily_pl, 2),
        "by_type_rub": {k: round(v, 2) for k, v in portfolio.by_type().items()},
    }


def cmd_limits(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get_limits()
    if args.raw or args.format == "json":
        print_json(data)
    elif args.format == "csv":
        rows: list[dict[str, Any]] = []
        for item in data.get("depoLimit") or []:
            rows.append(
                {
                    **{k: v for k, v in item.items() if not isinstance(v, dict)},
                    **{f"quantity_{k}": v for k, v in (item.get("quantity") or {}).items()},
                }
            )
        print_csv(rows)
    elif args.format == "md":
        print("```text")
        print(format_limits(data))
        print("```")
    else:
        print(format_limits(data))
    return EXIT_OK


def cmd_trades(args: argparse.Namespace) -> int:
    client = make_client(args)
    since, until = resolve_range(args)
    common: dict[str, Any] = {
        "since": since,
        "until": until,
        "tickers": args.tickers,
        "class_codes": args.class_codes,
        "side": args.side,
    }
    if args.all_pages:
        trades = list(
            client.iter_trades(
                **common,
                size=args.size,
                max_pages=args.max_pages,
                limit=args.limit,
                sort=args.sort,
            )
        )
        total = len(trades)
        shown = total
    else:
        page = client.search_trades(**common, page=args.page, size=args.size, sort=args.sort)
        trades = page.records
        total = page.total_records
        shown = len(trades)
        if args.limit:
            trades = trades[: args.limit]

    if args.raw:
        print_json(
            client.search_trades(**common, page=args.page, size=args.size, sort=args.sort).__dict__
            | {"records": [t.raw for t in trades]}
        )
        return EXIT_OK

    _print_trades(trades, args, total=total, shown=shown)
    return EXIT_OK


def _print_trades(trades: Sequence[Any], args: argparse.Namespace, *, total: int, shown: int) -> None:
    if args.format == "json":
        print_json({"total_records": total, "returned": len(trades), "trades": [t.summary() for t in trades]})
        return
    if args.format == "csv":
        print_csv(trades_to_rows(trades))
        return
    if args.format == "md":
        print("| Дата | Тикер | Сторона | Кол-во | Цена | Объём | Валюта |")
        print("|---|---|---|---|---|---|---|")
        for t in trades:
            print(
                f"| {short_datetime(t.date_time)} | {t.ticker} | {t.side_label} | {qty(t.trade_quantity)} | "
                f"{money(t.price, currency='')} | {money(t.volume)} | {t.settlement_currency} |"
            )
        return

    if not trades:
        since = args.since or (f"последние {args.days} дн." if args.days else "весь доступный период")
        print(f"Сделок нет в выбранном периоде: {since}.")
        print(f"Помните: API отдаёт сделки только с {TRADE_HISTORY_FROM:%d.%m.%Y}.")
        return
    print(format_trades(trades, color=supports_color(args)))
    print()
    stats = _trade_stats(trades)
    print(
        f"Показано {len(trades)} из {total} записей (в выборке {shown}) · "
        f"покупок {stats['buys']}, продаж {stats['sells']}, объём {money(stats['volume'])}"
        + (f" в {len(stats['currencies'])} валютах" if len(stats["currencies"]) > 1 else "")
    )
    if stats["first"] and stats["last"]:
        print(f"Период сделок: {short_datetime(stats['first'])} → {short_datetime(stats['last'])}")


def _trade_stats(trades: Sequence[Any]) -> dict[str, Any]:
    buys = sum(1 for t in trades if str(t.side) == "1")
    currencies = {t.settlement_currency for t in trades if t.settlement_currency}
    volume = 0.0
    if len(currencies) <= 1:  # складывать оборосмы в разных валютах нельзя
        volume = sum(t.volume or 0.0 for t in trades)
    dates = [t.date_time for t in trades if t.date_time]
    return {
        "buys": buys,
        "sells": len(trades) - buys,
        "volume": volume,
        "currencies": currencies,
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
    }


def cmd_orders(args: argparse.Namespace) -> int:
    client = make_client(args)
    since, until = resolve_range(args)
    common: dict[str, Any] = {
        "since": since,
        "until": until,
        "tickers": args.tickers,
        "class_codes": args.class_codes,
        "side": args.side,
        "order_status": args.status,
    }
    if args.raw:
        print_json(client.search_orders(**common, page=args.page, size=args.size))
        return EXIT_OK
    if args.all_pages:
        orders = []
        for idx, order in enumerate(client.iter_orders(size=args.size, **common)):
            orders.append(order)
            if args.limit and idx + 1 >= args.limit:
                break
    else:
        data = client.search_orders(**common, page=args.page, size=args.size)
        orders = [Order.from_api(item) for item in (data.get("records") or []) if isinstance(item, dict)]
        if args.limit:
            orders = orders[: args.limit]
    if args.format in ("json", "csv"):
        rows = [_order_row(o) for o in orders]
        print_json(rows) if args.format == "json" else print_csv(rows)
        return EXIT_OK
    if not orders:
        print("Заявок нет в выбранном периоде.")
        return EXIT_OK
    print(
        build_table(
            ["Подана", "Тикер", "Класс", "Сторона", "Тип", "Цена", "Кол-во", "Исполнено", "Средн. цена", "Статус"],
            [
                [
                    short_datetime(o.order_date_time),
                    o.ticker,
                    o.class_code,
                    o.side_label,
                    o.type_label,
                    money(o.price, currency=""),
                    qty(o.order_quantity),
                    qty(o.executed_quantity),
                    money(o.average_price, currency="") if o.average_price else "—",
                    o.status_label,
                ]
                for o in orders
            ],
            aligns=["left", "left", "left", "left", "left"] + ["right"] * 4 + ["left"],
        )
    )
    print(f"\nЗаявок показано: {len(orders)}")
    return EXIT_OK


def _order_row(o: Order) -> dict[str, Any]:
    return {
        "order_date": o.order_date_time.strftime("%Y-%m-%d %H:%M:%S") if o.order_date_time else "",
        "execution_date": o.execution_date_time.strftime("%Y-%m-%d %H:%M:%S") if o.execution_date_time else "",
        "order_num": o.order_num,
        "ticker": o.ticker,
        "class_code": o.class_code,
        "side": o.side,
        "order_type": o.type_label,
        "order_status": o.status_label,
        "price": o.price,
        "order_quantity": o.order_quantity,
        "executed_quantity": o.executed_quantity,
        "remained_quantity": o.remained_quantity,
        "average_price": o.average_price,
        "executed_value": o.executed_value,
        "currency": o.settlement_currency,
        "reject_reason": o.reject_reason,
    }


def cmd_operations(args: argparse.Namespace) -> int:
    client = make_client(args)
    since, until = resolve_range(args)
    data = client.search_operations(
        since=since,
        until=until,
        operation_types=args.types,
        statuses=args.statuses,
        tickers=args.tickers,
        page=args.page,
        size=args.size,
    )
    if args.raw or args.format == "json":
        print_json(data)
        return EXIT_OK
    records = [x for x in (data.get("records") or []) if isinstance(x, dict)]
    rows = []
    for x in records:
        raw_sum = x.get("sum")
        try:
            amt = float(raw_sum) if raw_sum not in (None, "") else None
        except (TypeError, ValueError):
            amt = None
        op_type = str(x.get("type") or "—").strip()
        balance_change = str(x.get("balanceChange") or x.get("balance_change") or "—").strip()
        s_amt = signed_operation_sum(amt, balance_change=balance_change, op_type=op_type)
        rows.append(
            {
                "date": _fmt_api_date(x.get("date")),
                "type": op_type,
                "status": x.get("status"),
                "ticker": x.get("ticker"),
                "isin": x.get("isin"),
                "issuer": x.get("issuerName"),
                "sum": s_amt,
                "currency": x.get("currency"),
                "balance_change": balance_change,
            }
        )
    if args.format == "csv":
        print_csv(rows)
        return EXIT_OK
    if not rows:
        print("Неторговых операций в выбранном периоде нет.")
        return EXIT_OK
    table_rows = []
    for r in rows:
        amount = r["sum"]
        table_rows.append(
            [
                r["date"],
                r["type"],
                r["status"],
                r["ticker"] or "—",
                r["issuer"] or "—",
                money(amount, currency=""),
                r["currency"],
            ]
        )
    print(
        build_table(
            ["Дата", "Тип", "Статус", "Тикер", "Инструмент", "Сумма", "Валюта"],
            table_rows,
            aligns=["left", "left", "left", "left", "left", "right", "left"],
        )
    )
    print(f"\nОпераций показано: {len(rows)}")
    return EXIT_OK


def cmd_pnl(args: argparse.Namespace) -> int:
    client = make_client(args)
    since, until = resolve_range(args)
    term = None if str(args.term).lower() in ("all", "none", "") else args.term
    asset_types = args.asset_types

    portfolio = client.get_portfolio(term=term)
    trades = list(client.iter_trades(since=since, until=until))
    operations = list(client.iter_operations(since=since, until=until))

    pnl_data = calculate_pnl(
        portfolio=portfolio,
        trades=trades,
        operations=operations,
        asset_types=asset_types,
        since=since,
        until=until,
    )

    if args.raw or args.format == "json":
        print_json(pnl_data)
        return EXIT_OK

    print("=" * 68)
    print(f"Отчёт о прибылях и убытках (P&L) [{pnl_data['filter']['description']}]")
    if since or until:
        print(f"Период: {since or '—'} → {until or '—'}")
    print("=" * 68)

    sum_data = pnl_data["summary"]
    print(f"Чистая прибыль (Итоговый P&L) : {money(sum_data['net_pnl'], sign=True)}")
    print(f"  Реализованный фин. результат: {money(sum_data['net_realized_pnl'], sign=True)}")
    print(f"  Потенциальная прибыль (курс): {money(sum_data['potential_capital_gain'], sign=True)}")
    print(f"  Всего доходов               : {money(sum_data['total_income'])}")
    print(f"  Всего расходов              : {money(sum_data['total_expenses'])}")

    print("\nДоходы:")
    for item in pnl_data["income_items"]:
        print(f"  {item['label']:50s}: {money(item['value'])}")

    print("\nПотенциальная прибыль от прироста стоимости:")
    for item in pnl_data["potential_items"]:
        print(f"  {item['label']:50s}: {money(item['value'], sign=True)}")

    print("\nРасходы:")
    for item in pnl_data["expense_items"]:
        print(f"  {item['label']:50s}: {money(item['value'])}")

    return EXIT_OK


def _fmt_api_date(value: Any) -> str:
    if not value:
        return "—"
    try:
        text = str(value).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def cmd_export(args: argparse.Namespace) -> int:
    client = make_client(args)
    since, until = resolve_range(args)
    print("Читаю портфель…", file=sys.stderr)
    raw_portfolio = client.get_portfolio_raw()
    portfolio = Portfolio.from_api(raw_portfolio, term=None if str(args.term).lower() == "all" else args.term)
    print("Читаю сделки…", file=sys.stderr)
    trades = list(
        client.iter_trades(
            since=since,
            until=until,
            tickers=args.tickers,
            class_codes=args.class_codes,
            side=args.side,
            size=args.size,
            max_pages=args.max_pages,
        )
    )
    print("Читаю лимиты…", file=sys.stderr)
    limits = client.get_limits()

    orders = client.search_orders(since=since, until=until, size=args.size) if args.include_orders else None
    operations = client.search_operations(since=since, until=until, size=args.size) if args.include_operations else None

    formats = tuple(x.strip() for x in str(args.formats).split(",") if x.strip())
    created = save_report(
        args.out,
        portfolio=portfolio,
        trades=trades,
        raw_portfolio=raw_portfolio,
        limits=limits,
        orders=orders,
        operations=operations,
        prefix=args.prefix,
        formats=formats,
    )
    print(f"Портфель: {len(portfolio.positions)} поз. на {money(portfolio.total_value_rub)} · сделок: {len(trades)}")
    if created:
        print("Сохранено:")
        for file in created:
            print(f"  {file}")
    else:
        print("Нечего сохранять — ответы API пустые.")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    """Всё сразу: портфель, лимиты и сделки за период — один запуск, один скролл."""
    client = make_client(args)
    since, until = resolve_range(args)
    color = supports_color(args)

    portfolio = client.get_portfolio(term=None if str(args.term).lower() in ("all", "none") else args.term)
    print(format_portfolio(portfolio, color=color, title="Портфель"))

    print("\n" + "=" * 72)
    print("Лимиты (данные сервиса «Лимиты» — независимый снимок того же счёта)")
    print("=" * 72 + "\n")
    try:
        print(format_limits(client.get_limits()))
    except BcsError as exc:
        print(f"лимиты недоступны: {exc}", file=sys.stderr)

    print("\n" + "=" * 72)
    if args.since:
        period = f"с {args.since}"
    elif args.days:
        period = f"за последние {args.days} дн."
    else:
        period = "за весь доступный период"
    print(f"Сделки {period}")
    print("=" * 72)
    trades = list(
        client.iter_trades(
            since=since,
            until=until,
            tickers=args.tickers,
            class_codes=args.class_codes,
            side=args.side,
            size=args.size,
            max_pages=args.max_pages,
            limit=args.limit,
        )
    )
    if not trades:
        print("Сделок нет.")
        print(f"Напоминание: API отдаёт сделки только с {TRADE_HISTORY_FROM:%d.%m.%Y}.")
    else:
        print(format_trades(trades, limit=args.top or None, color=color))
        stats = _trade_stats(trades)
        print(
            f"\nВсего сделок: {len(trades)} · покупок {stats['buys']} · продаж {stats['sells']}"
            + (f" · оборот {money(stats['volume'])}" if stats["volume"] or len(stats["currencies"]) <= 1 else "")
        )
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    client = make_client(args)
    iteration = 0
    try:
        while True:
            iteration += 1
            portfolio = client.get_portfolio(term=None if str(args.term).lower() == "all" else args.term)
            if sys.stdout.isatty():
                sys.stdout.write("\033[2J\033[H")
            print(f"=== обновление №{iteration} · {dt.datetime.now():%Y-%m-%d %H:%M:%S} ===")
            print(format_portfolio(portfolio, color=supports_color(args)))
            if args.count and iteration >= args.count:
                break
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print("\nостановлено пользователем")
    return EXIT_OK


def cmd_web(args: argparse.Namespace) -> int:
    """Веб-интерфейс: всё управление кнопками в браузере."""
    from .web import serve_web

    mode = None if args.mode == "auto" else args.mode
    return serve_web(host=args.host, port=args.port, mode=mode, open_browser=not args.no_browser)


def cmd_demo(args: argparse.Namespace) -> int:
    """Прогон тех же функций вывода, но на фикстурах вместо живого API."""
    from .demo import fake_portfolio, fake_trades

    portfolio = fake_portfolio()
    trades = fake_trades(days=args.days)
    print(format_portfolio(portfolio, color=supports_color(args), title="ПОРТФЕЛЬ (демо-данные)"))
    print()
    print("СДЕЛКИ (демо-данные)")
    print(format_trades(trades))
    print()
    print(format_limits(_fake_limits(portfolio)))
    print("\nЭто демонстрация вывода: данные синтетические, сеть не использовалась.")
    print("Чтобы работать с реальным счётом, задайте BCS_REFRESH_TOKEN и запустите 'python -m bcs_api portfolio'.")
    return EXIT_OK


def _fake_limits(portfolio: Portfolio) -> dict[str, Any]:
    return {
        "depoLimit": [
            {
                "ticker": p.ticker,
                "classCode": p.board or "TQBR",
                "exchange": "MOEX",
                "averagePrice": p.balance_price,
                "quantity": {"type": p.term or "T0", "value": p.quantity},
                "instrumentType": p.instrument_type,
            }
            for p in portfolio.positions
        ],
        "moneyLimits": [
            {
                "exchange": "MOEX",
                "currencyCode": c.currency,
                "locked": c.locked,
                "quantity": {"type": c.term or "T0", "value": c.quantity},
                "loadDate": (portfolio.as_of or dt.datetime.now(dt.timezone.utc)).isoformat(),
            }
            for c in portfolio.cash
        ],
        "futuresLimits": [],
        "futureHolding": [],
    }


# ----------------------------------------------------------------------- main


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except AuthError as exc:
        print(f"Ошибка авторизации: {exc}", file=sys.stderr)
        print("\nПолная диагностика: python3 -m bcs_api token --check", file=sys.stderr)
        print(
            "\nЧто проверить:\n"
            "  1. refresh-токен выпущен для того счёта, который вы смотрите;\n"
            "  2. он не удалён в ЛК и не старше 90 суток;\n"
            "  3. --client-id совпадает с правами токена: 'trade-api-read' для токена «только чтение»,\n"
            "     'trade-api-write' — для токена «для торговли и чтения»;\n"
            "  4. после удаления кэша (--cache) программа запросит токен заново.",
            file=sys.stderr,
        )
        return EXIT_AUTH
    except UnauthorizedError as exc:
        print(f"API отклонил access-токен: {exc}", file=sys.stderr)
        print(
            "Подсказка: программа уже попыталась перевыпустить токен. Скорее всего refresh-токен отозван\n"
            "в личном кабинете или выпущен с другими правами — сбросьте кэш (`token --reset`) и возьмите токен заново.",
            file=sys.stderr,
        )
        return EXIT_AUTH
    except RateLimitError as exc:
        print(f"Превышен лимит запросов БКС: {exc}", file=sys.stderr)
        print("Подсказка: уменьшите частоту опроса (--rps 2, для watch --interval 30).", file=sys.stderr)
        return EXIT_ERROR
    except ApiError as exc:
        print(f"Ошибка API: {exc}", file=sys.stderr)
        if exc.trace_id:
            print(f"traceId для обращения в поддержку: {exc.trace_id}", file=sys.stderr)
        return EXIT_ERROR
    except BcsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"Ошибка в аргументах: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nпрервано", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
