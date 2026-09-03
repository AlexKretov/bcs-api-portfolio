"""Тесты клиента: юниты на разбор/форматирование и интеграция против мока BCS API.

Запуск:  python -m pytest tests -q     (или  python tests/test_client.py)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bcs_api import BcsClient, Portfolio, TokenSet, TokenStore, to_iso_z
from bcs_api.client import Trade, parse_datetime
from bcs_api.errors import AuthError, ValidationError
from bcs_api.export import portfolio_to_rows, trades_to_rows
from bcs_api.formatting import build_table, display_width, money, percent, qty
from bcs_api.http_client import TokenBucket

sys.path.insert(0, str(ROOT / "tests"))
import mock_server

# ---------------------------------------------------------------- unit tests


class TokenSetTests(unittest.TestCase):
    def test_from_response_keeps_rotated_refresh(self) -> None:
        tokens = TokenSet.from_response(
            {"access_token": "a.b.c", "expires_in": 86400, "refresh_token": "new-refresh", "refresh_expires_in": 100},
            fallback_refresh="old",
        )
        self.assertEqual(tokens.refresh_token, "new-refresh")
        self.assertTrue(tokens.is_access_valid(min_ttl=300))
        self.assertGreater(tokens.access_ttl, 23 * 3600)

    def test_missing_refresh_falls_back_to_previous(self) -> None:
        tokens = TokenSet.from_response({"access_token": "x", "expires_in": 10}, fallback_refresh="old")
        self.assertEqual(tokens.refresh_token, "old")

    def test_expired_token_is_not_valid(self) -> None:
        tokens = TokenSet.from_response({"access_token": "x", "expires_in": -5})
        self.assertFalse(tokens.is_access_valid())

    def test_requires_access_token(self) -> None:
        with self.assertRaises(ValueError):
            TokenSet.from_response({"expires_in": 10})


class TokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "tokens.json"

    def test_roundtrip_and_permissions(self) -> None:
        store = TokenStore(self.path, refresh_token_provider=lambda: "refresh-from-env")
        store.save(TokenSet(access_token="acc", refresh_token="ref", expires_at=time.time() + 1000))
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, "файл с токенами должен быть доступен только владельцу")
        reloaded = TokenStore(self.path)
        tokens = reloaded.get()
        self.assertIsNotNone(tokens)
        self.assertEqual(tokens.access_token, "acc")
        self.assertEqual(tokens.refresh_token, "ref")

    def test_corrupted_file_recovers_from_provider(self) -> None:
        self.path.write_text("{broken json", encoding="utf-8")
        store = TokenStore(self.path, refresh_token_provider=lambda: "env-refresh")
        tokens = store.get()
        self.assertIsNotNone(tokens)
        self.assertEqual(tokens.refresh_token, "env-refresh")
        self.assertFalse(tokens.is_access_valid())

    def test_refresh_token_survives_after_rotation_saved(self) -> None:
        store = TokenStore(self.path, refresh_token_provider=lambda: "env-refresh")
        store.save(TokenSet(access_token="a", refresh_token="rotated-1", expires_at=time.time() + 1000))
        self.assertEqual(TokenStore(self.path).refresh_token(), "rotated-1")


class DateTests(unittest.TestCase):
    def test_iso_output_is_utc_with_millis(self) -> None:
        self.assertEqual(to_iso_z("2026-01-26"), "2026-01-26T00:00:00.000Z")
        self.assertEqual(to_iso_z(dt.date(2026, 1, 26)), "2026-01-26T00:00:00.000Z")

    def test_relative_days(self) -> None:
        before = to_iso_z("7d")
        after = to_iso_z("7d")
        self.assertTrue(before.endswith("Z"))
        delta = abs(dt.datetime.fromisoformat(after) - dt.datetime.fromisoformat(before))
        self.assertLessEqual(delta.total_seconds(), 5)

    def test_dot_format_and_naive_assumes_local(self) -> None:
        parsed = to_iso_z("26.01.2026 10:30")
        self.assertTrue(parsed.startswith("2026-01-26T"))

    def test_bad_date_raises(self) -> None:
        with self.assertRaises(ValueError):
            to_iso_z("на прошлой неделе")

    def test_parse_datetime_from_z_suffix(self) -> None:
        value = parse_datetime("2026-02-03T10:15:30.123Z")
        self.assertIsNotNone(value)
        self.assertEqual(value.astimezone(dt.timezone.utc).hour, 10)
        self.assertIsNone(parse_datetime(None))
        self.assertIsNone(parse_datetime("совсем не дата"))


class PortfolioParsingTests(unittest.TestCase):
    payload = [
        {
            "type": "depoLimit",
            "ticker": "SBER",
            "displayName": "Сбербанк",
            "instrumentType": "STOCK",
            "term": "T0",
            "quantity": 10,
            "locked": 1,
            "balancePrice": 250.0,
            "currentPrice": 280.0,
            "currentValueRub": 2800.0,
            "balanceValueRub": 2500.0,
            "unrealizedPL": 300.0,
            "unrealizedPercentPL": 12.0,
            "dailyPL": 10.0,
            "dailyPercentPL": 0.36,
            "portfolioShare": 70.0,
            "currency": "RUB",
            "board": "TQBR",
            "accruedIncome": 0,
            "ratioQuantity": 1,
            "loadDate": "2026-02-03T10:00:00.000Z",
        },
        {
            "type": "depoLimit",
            "ticker": "LKOH",
            "displayName": "Лукойл",
            "instrumentType": "STOCK",
            "term": "T1",
            "quantity": 1,
            "locked": 0,
            "balancePrice": 7000.0,
            "currentPrice": 7000.0,
            "currentValueRub": 7000.0,
            "unrealizedPL": 0,
            "portfolioShare": 30.0,
            "currency": "RUB",
            "board": "TQBR",
            "loadDate": "2026-02-03T10:00:00.000Z",
        },
        {
            "type": "moneyLimit",
            "ticker": "RUB",
            "currency": "RUB",
            "term": "T0",
            "quantity": 500.0,
            "locked": 100.0,
            "currentValueRub": 500.0,
            "loadDate": "2026-02-03T10:00:00.000Z",
        },
        {
            "type": "depoLimit",
            "ticker": "ZERO",
            "instrumentType": "STOCK",
            "term": "T0",
            "quantity": 0,
            "currentValueRub": 0,
            "loadDate": "2026-02-03T10:00:00.000Z",
        },
    ]

    def test_term_filter_drops_other_terms(self) -> None:
        portfolio = Portfolio.from_api(self.payload, term="T0")
        self.assertEqual([p.ticker for p in portfolio.positions], ["SBER"])
        self.assertEqual(len(portfolio.cash), 1)
        self.assertEqual(portfolio.cash[0].available, 400.0)

    def test_term_none_keeps_everything_but_zero_positions(self) -> None:
        portfolio = Portfolio.from_api(self.payload, term=None)
        self.assertEqual({p.ticker for p in portfolio.positions}, {"SBER", "LKOH"})

    def test_totals(self) -> None:
        portfolio = Portfolio.from_api(self.payload, term=None)
        self.assertEqual(portfolio.securities_value_rub, 9800.0)
        self.assertEqual(portfolio.cash_rub, 500.0)
        self.assertEqual(portfolio.total_value_rub, 10300.0)
        self.assertEqual(portfolio.total_unrealized_pl, 300.0)
        self.assertEqual(portfolio.by_type()["Акция РФ"], 9800.0)

    def test_accepts_wrapped_array(self) -> None:
        portfolio = Portfolio.from_api({"positions": self.payload}, term="T0")
        self.assertEqual(len(portfolio.positions), 1)

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            Portfolio.from_api({"unexpected": True}, term=None)

    def test_lots_from_ratio_quantity(self) -> None:
        payload = [dict(self.payload[0], quantity=50, ratioQuantity=10)]
        position = Portfolio.from_api(payload, term="T0").positions[0]
        self.assertEqual(position.lots, 5.0)

    def test_rows_export(self) -> None:
        rows = portfolio_to_rows(Portfolio.from_api(self.payload, term=None))
        self.assertTrue(any(r["ticker"] == "RUB" and r["instrument_type"] == "MONEY" for r in rows))
        self.assertTrue(all("current_value_rub" in r for r in rows))


class FormattingTests(unittest.TestCase):
    def test_money_grouping_and_negative(self) -> None:
        self.assertEqual(money(1234567.891), "1 234 567.89 ₽")
        self.assertEqual(money(-1000.0), "-1 000 ₽")
        self.assertEqual(money(None), "—")
        self.assertEqual(money(12.0, sign=True), "+12 ₽")

    def test_percent_and_qty(self) -> None:
        self.assertEqual(percent(3.5), "+3.50%")
        self.assertEqual(percent(-3.5, sign=False), "-3.50%")
        self.assertEqual(qty(10.0), "10")
        self.assertEqual(qty(0.5), "0.5")
        self.assertEqual(qty(1234567.0), "1 234 567")

    def test_wide_char_alignment(self) -> None:
        self.assertEqual(display_width("АБВ"), 3)
        self.assertEqual(display_width("日"), 2)  # полноширинные символы занимают 2 колонки
        table = build_table(["Тикер", "Кол-во"], [["SBER", "10"], ["ЛУКОЙЛ", "1"]], aligns=["left", "right"])
        lines = table.splitlines()
        self.assertEqual(len({display_width(line) for line in lines[:2]}), 1, lines)

    def test_trades_rows(self) -> None:
        trade = Trade.from_api(
            {
                "tradeNum": 1,
                "ticker": "SBER",
                "side": "1",
                "price": 10.0,
                "volume": 100.0,
                "tradeQuantity": 10,
                "tradeDateTime": "2026-02-03T10:00:00.000Z",
                "settlementCurrency": "RUB",
            }
        )
        self.assertEqual(trade.side_label, "покупка")
        row = trades_to_rows([trade])[0]
        self.assertEqual(row["ticker"], "SBER")
        self.assertEqual(row["volume"], 100.0)


class TokenBucketTests(unittest.TestCase):
    def test_limits_rate(self) -> None:
        bucket = TokenBucket(100.0, capacity=1.0)
        bucket.next_delay()  # забираем единственный токен
        start = time.monotonic()
        bucket.acquire()
        self.assertGreaterEqual(time.monotonic() - start, 0.005)

    def test_zero_delay_when_full(self) -> None:
        self.assertEqual(TokenBucket(10.0).next_delay(), 0.0)


class ClientValidationTests(unittest.TestCase):
    def test_side_aliases(self) -> None:
        from bcs_api.client import parse_side

        for alias, expected in (("buy", 1), ("1", 1), ("покупка", 1), ("sell", 2), ("2", 2), ("продажа", 2)):
            self.assertEqual(parse_side(alias), expected, alias)
        with self.assertRaises(ValueError):
            parse_side("maybe")

    def test_unknown_client_id(self) -> None:
        from bcs_api.http_client import BcsHttp

        with self.assertRaises(ValueError):
            BcsHttp(store=TokenStore(None), client_id="trade-api-superuser")

    def test_no_refresh_token_raises_actionable_auth_error(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "BCS_REFRESH_TOKEN"}
        old = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        try:
            client = BcsClient(config_path="/nonexistent/bcs.json", cache_path=str(Path("/tmp/nonexistent") / "c.json"))
            with self.assertRaises(AuthError) as ctx:
                client.http.authenticate()
        finally:
            os.environ.clear()
            os.environ.update(old)
        message = str(ctx.exception)
        self.assertIn("Токены API", message)
        self.assertIn("BCS_REFRESH_TOKEN", message)


def run_cli(*argv: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Запуск CLI отдельным процессом: проверяются и аргументы, и вывод, и коды возврата."""
    proc_env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("BCS_REFRESH_TOKEN", "BCS_CONFIG", "BCS_API_BASE_URL", "BCS_AUTH_URL", "BCS_TOKEN_CACHE"):
        proc_env.pop(key, None)
    proc_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "bcs_api", *argv],
        cwd=str(cwd or ROOT),
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# ------------------------------------------------------ диагностика токена


def make_test_jwt(claims: dict[str, object]) -> str:
    """Собрать «JWT» из claims — подпись для диагностики не проверяется."""
    import base64

    def enc(obj: dict[str, object]) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return enc({"alg": "RS256", "typ": "JWT"}) + "." + enc(claims) + "." + "sig"


class MaskSecretTests(unittest.TestCase):
    def test_whitespace_never_reaches_output(self) -> None:
        from bcs_api.client import mask_secret

        rendered = mask_secret("  abcd1234\nefgh \n ijkl ")
        self.assertNotIn("\n", rendered)
        self.assertNotIn(" ", rendered)
        self.assertEqual(rendered, "abcd…ijkl")

    def test_short_and_empty(self) -> None:
        from bcs_api.client import mask_secret

        self.assertEqual(mask_secret("abc"), "a…")
        self.assertEqual(mask_secret(None), "—")
        self.assertEqual(mask_secret("   "), "—")


class TokenDiagnosticsTests(unittest.TestCase):
    def test_strips_junk_and_reports_it(self) -> None:
        from bcs_api.diagnostics import normalize_refresh_token

        dirty = "   Bearer “abc\u200b.def.ghi\n”  ".replace("“", '"').replace("”", '"')
        clean, notes = normalize_refresh_token(dirty)
        self.assertEqual(clean, "abc.def.ghi")
        self.assertTrue(notes, "факт чистки обязан быть виден пользователю")

    def test_plain_token_untouched(self) -> None:
        from bcs_api.diagnostics import normalize_refresh_token

        clean, notes = normalize_refresh_token("629f704815abcd1234ef5678901234")
        self.assertEqual(clean, "629f704815abcd1234ef5678901234")
        self.assertEqual(notes, [])

    def test_truncated_token_from_docs_is_flagged(self) -> None:
        from bcs_api.diagnostics import inspect_token

        # «…» из примера в документации — ровно то, что получаешь при неаккуратном копировании
        report = inspect_token("629f704815…")
        self.assertFalse(report.ok)
        self.assertTrue(
            any("многоточие" in p or "длину" in p for p in report.problems + [x for x in report.notes]),
            report.problems + report.notes,
        )

    def test_placeholder_from_example_config_is_flagged(self) -> None:
        from bcs_api.diagnostics import inspect_token

        secret = "ПОДСТАВЬТЕ_СЮДА_ТОКЕН_ИЗ_ЛК_БКС"
        report = inspect_token(secret)
        self.assertTrue(any("заглушк" in p for p in report.problems), report.problems)
        joined = " ".join(report.problems + report.notes)
        self.assertNotIn("ПОДСТАВЬТЕ", joined, "в тексте диагностики не должно быть частей токена")

    def test_expired_jwt_expiry_is_reported(self) -> None:
        from bcs_api.diagnostics import inspect_token

        token = make_test_jwt({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200, "azp": "trade-api-read"})
        report = inspect_token(token, requested_client_id="trade-api-read")
        self.assertTrue(report.looks_like_jwt)
        self.assertIsNotNone(report.expired_at)
        self.assertTrue(any("истёк" in p for p in report.problems), report.problems)

    def test_client_id_mismatch_is_reported_with_fix(self) -> None:
        from bcs_api.diagnostics import inspect_token

        token = make_test_jwt({"exp": int(time.time()) + 86400, "azp": "trade-api-read"})
        report = inspect_token(token, requested_client_id="trade-api-write")
        problem = " ".join(report.problems)
        self.assertIn("trade-api-read", problem)
        self.assertIn("client_id", problem)

    def test_valid_token_has_no_false_positives(self) -> None:
        from bcs_api.diagnostics import inspect_token

        token = make_test_jwt({"exp": int(time.time()) + 86400, "azp": "trade-api-read"})
        report = inspect_token(token, requested_client_id="trade-api-read")
        self.assertTrue(report.ok, report.problems)

    def test_opaque_token_without_jwt_shape_is_not_called_broken(self) -> None:
        from bcs_api.diagnostics import inspect_token

        # У БКС access-токен в примерах — не JWT; не пугаем пользователя «плохой формой»
        report = inspect_token("0f1e2d3c-4b5a-6978-8a9b-c0d1e2f3a4b5c6d7")
        self.assertFalse(report.looks_like_jwt)
        self.assertTrue(report.ok, report.problems)

    def test_secret_is_never_rendered(self) -> None:
        from bcs_api.diagnostics import inspect_token

        secret = "sup3r-s3cr3t-refresh-token-value-abcdefgh"
        rendered = inspect_token(secret).render()
        self.assertNotIn(secret, rendered)
        self.assertIn("…", rendered)


class ConfigDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp())

    def test_typo_filename_is_detected_and_not_loaded(self) -> None:
        from bcs_api.diagnostics import format_config_scan, scan_config_files

        (self.dir / "bsc-config.json").write_text(  # опечатка: bsc вместо bcs
            json.dumps({"refresh_token": "tok" + "x" * 30}), encoding="utf-8"
        )
        records = scan_config_files(self.dir)
        self.assertEqual(len(records), 1, records)
        self.assertFalse(records[0]["is_expected_name"])
        self.assertTrue(records[0]["has_refresh_token"])
        self.assertNotIn("xxxxxxxxxx", format_config_scan(records, loaded_path=None))
        self.assertIn("НЕ читается", format_config_scan(records, loaded_path=None))

    def test_broken_json_is_reported(self) -> None:
        (self.dir / "bcs-config.json").write_text('{"refresh_token": ,}', encoding="utf-8")
        from bcs_api.diagnostics import scan_config_files

        records = scan_config_files(self.dir)
        self.assertTrue(records[0]["error"])

    def test_home_config_is_used_as_fallback(self) -> None:
        """Запуск из другой папки не должен выглядеть как «токена нет»: конфиг ищем и в home."""
        import tempfile

        home = Path(tempfile.mkdtemp())
        (home / "bcs-config.json").write_text(
            json.dumps({"refresh_token": "token-from-home-abcdefghijkl"}), encoding="utf-8"
        )
        from bcs_api.client import load_config

        old_home = Path.home
        Path.home = staticmethod(lambda: home)  # type: ignore[method-assign]
        try:
            cfg = load_config()
        finally:
            Path.home = old_home  # type: ignore[method-assign]
        self.assertEqual(cfg["refresh_token"], "token-from-home-abcdefghijkl")
        self.assertIn(str(home), cfg["_config_path"])

    def test_example_config_is_ignored(self) -> None:
        import shutil

        shutil.copy(ROOT / "bcs-config.example.json", self.dir / "bcs-config.example.json")
        from bcs_api.diagnostics import scan_config_files

        self.assertEqual(scan_config_files(self.dir), [])

    def test_env_overrides_file_and_source_is_recorded(self) -> None:
        from bcs_api.client import load_config

        cfg_file = self.dir / "bcs-config.json"
        cfg_file.write_text(
            json.dumps({"refresh_token": "token-from-file-abcdefghij", "client_id": "trade-api-read"}),
            encoding="utf-8",
        )
        old = os.environ.get("BCS_REFRESH_TOKEN")
        os.environ["BCS_REFRESH_TOKEN"] = "token-from-env-klmopqrstuvw"
        try:
            cfg = load_config(str(cfg_file))
            self.assertEqual(cfg["refresh_token"], "token-from-env-klmopqrstuvw")
            self.assertIn("BCS_REFRESH_TOKEN", cfg["_sources"]["refresh_token"])
        finally:
            if old is None:
                del os.environ["BCS_REFRESH_TOKEN"]
            else:
                os.environ["BCS_REFRESH_TOKEN"] = old
        # без env файл читается, и источник указан честно
        cfg = load_config(str(cfg_file))
        self.assertEqual(cfg["refresh_token"], "token-from-file-abcdefghij")
        self.assertIn("bcs-config.json", cfg["_sources"]["refresh_token"])

    def test_junk_around_token_in_file_is_cleaned(self) -> None:
        cfg_file = self.dir / "bcs-config.json"
        cfg_file.write_text(json.dumps({"refresh_token": "  tok\nwith-newline-and-spaces  "}), encoding="utf-8")
        from bcs_api.client import load_config

        cfg = load_config(str(cfg_file))
        self.assertEqual(cfg["refresh_token"], "tokwith-newline-and-spaces")
        self.assertTrue(cfg["_token_notes"], "факт очистки должен быть показан пользователю")


class SourceAttributionTests(unittest.TestCase):
    """Источник значения должен называться честно: «кэш» только когда файл кэша реально есть."""

    def test_no_cache_file_means_source_is_config(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:  # файл намеренно не создаётся
                client = BcsClient(
                    refresh_token=state.expected_refresh, base_url=server.base_url, cache_path=str(cache), rps=50.0
                )
                tokens = client.store.get()
                self.assertIsNotNone(tokens)
                self.assertNotIn("кэш", (tokens.refresh_source or "").lower())
                self.assertIn("BcsClient(refresh_token", tokens.refresh_source or "")

    def test_cache_file_is_named_in_source(self) -> None:
        with _tmp_cache() as cache:
            cache.write_text(
                json.dumps({"access_token": "a", "refresh_token": "from-cache-token-abcdefghijkl", "expires_at": 0}),
                encoding="utf-8",
            )
            store = TokenStore(cache, refresh_token_provider=lambda: "from-config-abcdefghijklmn")
            tokens = store.get()
            self.assertIn(str(cache), tokens.refresh_source)

    def test_check_output_labels_env_value_not_cache(self) -> None:
        """Раньше значение из env подписывалось «кэш» — диагностика вела по ложному следу."""
        with _tmp_cache() as cache:
            env = {
                "BCS_REFRESH_TOKEN": "some-long-refresh-token-abcdefghijklmn",
                "BCS_TOKEN_CACHE": str(cache),
                "BCS_API_BASE_URL": "http://127.0.0.1:1",
            }
            result = run_cli("--no-ask", "token", "--check", env=env, cwd=Path(cache).parent)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = result.stdout.split("Осмотр значения", 1)[-1]
            self.assertIn("источник значения : переменная окружения BCS_REFRESH_TOKEN", block)
            self.assertNotIn("источник значения : кэш", block)
            self.assertIn("Значение, которое уйдёт в запрос: some…klmn", result.stdout)


class AuthCandidateTests(unittest.TestCase):
    """Кэш и конфиг могут спорить: программа обязана попробовать оба значения."""

    def test_stale_cache_falls_back_to_configured_token(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                # в кэше — чужой refresh (например, от старого токена), в конфиге — рабочий
                cache.write_text(
                    json.dumps({"access_token": "", "refresh_token": "stale-token-from-old-run", "expires_at": 0}),
                    encoding="utf-8",
                )
                client = BcsClient(
                    refresh_token=state.expected_refresh,
                    base_url=server.base_url,
                    cache_path=str(cache),
                    rps=50.0,
                )
                tokens = client.http.authenticate()
                self.assertTrue(tokens.access_token)
                self.assertGreaterEqual(server.state.auth_calls, 2, "первый кандидат должен был не сработать")
                # победившее значение сохраняется в кэш
                self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["refresh_token"], state.expected_refresh)

    def test_good_cache_preferred_over_stale_config(self) -> None:
        state = mock_server.MockState(rotate_refresh=True)
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token=mock_server.VALID_REFRESH, base_url=server.base_url, cache_path=str(cache), rps=50.0
                )
                client.http.authenticate()  # rotation: в кэше -r1, в конфиге остался исходный
                client.store.get().expires_at = 0
                server.state.auth_calls = 0
                client.http.authenticate(force=True)
                self.assertEqual(server.state.auth_calls, 1, "нужно обновиться по токену из кэша, без повторов")

    def test_auth_failure_message_contains_diagnostics(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token="ПОДСТАВЬТЕ_СЮДА_ТОКЕН_ИЗ_ЛК_БКС",
                    base_url=server.base_url,
                    cache_path=str(cache),
                    rps=50.0,
                )
                with self.assertRaises(AuthError) as ctx:
                    client.http.authenticate()
                message = str(ctx.exception)
                self.assertIn("Проверка значения токена", message)
                self.assertIn("заглушк", message)
                self.assertIn("invalid_grant", message)
                self.assertNotIn("ПОДСТАВЬТЕ_СЮДА_ТОКЕН_ИЗ_ЛК_БКС", message)  # секрет не печатается


class CliTokenCheckTests(unittest.TestCase):
    def test_token_check_reports_source_and_typo(self) -> None:
        with _tmp_cache() as cache:
            run_dir = Path(cache).parent
            (run_dir / "bsc-config.json").write_text(  # опечатка в имени — её видно в отчёте
                json.dumps({"refresh_token": "abc" + "d" * 40}), encoding="utf-8"
            )
            env = {
                "BCS_REFRESH_TOKEN": "   " + mock_server.VALID_REFRESH + " ",
                "BCS_API_BASE_URL": "http://127.0.0.1:1",
                "BCS_TOKEN_CACHE": str(cache),
            }
            result = run_cli("--no-ask", "token", "--check", env=env, cwd=run_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = result.stdout
            self.assertIn("BCS_REFRESH_TOKEN", out)
            self.assertIn("bsc-config.json", out)
            self.assertIn("НЕ читается", out)
            self.assertIn("пробельные символы", out)  # env-значение было с пробелами по краям
            self.assertNotIn(mock_server.VALID_REFRESH, out, "секрет не должен печататься целиком")


# ---------------------------------------------------------- integration (mock)


class MockIntegrationTests(unittest.TestCase):
    def test_full_flow_auth_portfolio_and_trades(self) -> None:
        state = mock_server.MockState(rotate_refresh=True)
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token=mock_server.VALID_REFRESH,
                    base_url=server.base_url,
                    cache_path=str(cache),
                    rps=50.0,
                )
                tokens = client.http.authenticate()
                self.assertTrue(tokens.access_token.startswith("mock."))
                self.assertEqual(server.state.auth_calls, 1)
                # rotation: в кэше должен лежать НОВЫЙ refresh, иначе следующий обмен упадёт
                saved = json.loads(cache.read_text(encoding="utf-8"))
                self.assertEqual(saved["refresh_token"], f"{mock_server.VALID_REFRESH}-r1")
                self.assertNotEqual(saved["refresh_token"], mock_server.VALID_REFRESH)
                self.assertEqual(server.state.expected_refresh, saved["refresh_token"])

                portfolio = client.get_portfolio()
                self.assertGreater(len(portfolio.positions), 0)
                self.assertGreater(portfolio.total_value_rub, 0)
                self.assertTrue(all(p.term == "T0" for p in portfolio.positions))

                limits = client.get_limits()
                self.assertIn("depoLimit", limits)

                page = client.search_trades(size=5, sort=["tradeDateTime,desc"])
                self.assertLessEqual(len(page.records), 5)
                self.assertGreaterEqual(page.total_records, 1)

                # access-токен кэшируется: второй вызов не идёт в авторизацию
                client.get_portfolio_raw()
                self.assertEqual(server.state.auth_calls, 1)

                names = client.instrument_names([portfolio.positions[0].ticker])
                self.assertIn(portfolio.positions[0].ticker, names)

    def test_pagination_collects_all_records(self) -> None:
        trades = mock_server.fake_trades_payload(days=10)
        total = len(trades["records"])
        self.assertGreater(total, 12, "фикстура должна быть достаточной для пагинации")
        state = mock_server.MockState(trades=trades)
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token=state.expected_refresh,
                    base_url=server.base_url,
                    cache_path=str(cache),
                    rps=50.0,
                )
                collected = list(client.iter_trades(size=5, max_pages=10, since="2026-01-26"))
                self.assertEqual(len(collected), total)
                self.assertEqual(len({t.raw["tradeNum"] for t in collected}), total)
                search_calls = [q for q in server.state.requests if q["path"].endswith("/trades/search")]
                self.assertEqual(len(search_calls), -(-total // 5))  # ровно по одной странице на 5 записей
                # фильтры доезжают до API в каждой странице
                for call in search_calls:
                    self.assertEqual(call["body"]["startDateTime"], "2026-01-26T00:00:00.000Z")
                    self.assertIn("page", call["query"])

    def test_filters_are_serialized_in_body(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token=state.expected_refresh, base_url=server.base_url, cache_path=str(cache), rps=50.0
                )
                client.search_trades(since="2026-01-26", tickers=["SBER", "LKOH"], side="buy", size=10)
                call = [q for q in server.state.requests if q["path"].endswith("/trades/search")][-1]
                self.assertEqual(call["body"]["tickers"], ["SBER", "LKOH"])
                self.assertEqual(call["body"]["side"], "1")
                self.assertTrue(call["body"]["startDateTime"].startswith("2026-01-26T00:00:00"))
                self.assertEqual(call["query"]["size"], ["10"])

    def test_rate_limit_is_retried(self) -> None:
        state = mock_server.MockState(total_rate_limit_hits=2)
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token=state.expected_refresh, base_url=server.base_url, cache_path=str(cache), rps=50.0
                )
                started = time.monotonic()
                data = client.search_operations(size=10)
                self.assertEqual(len(data["records"]), 1)
                self.assertGreaterEqual(time.monotonic() - started, 1.9)  # два Retry-After: 1
                rate_limited = [q for q in server.state.requests if q["path"].endswith("/operations/search")]
                self.assertEqual(len(rate_limited), 3)

    def test_401_triggers_single_reauth(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token=state.expected_refresh, base_url=server.base_url, cache_path=str(cache), rps=50.0
                )
                client.http.authenticate()
                auth_calls_before = server.state.auth_calls
                state.tokens.clear()  # «токен удалили в ЛК»
                portfolio = client.get_portfolio()  # 401 → перевыпуск → успех
                self.assertGreater(len(portfolio.positions), 0)
                self.assertEqual(server.state.auth_calls, auth_calls_before + 1)

    def test_validation_error_is_typed(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token=state.expected_refresh, base_url=server.base_url, cache_path=str(cache), rps=50.0
                )
                with self.assertRaises(ValidationError) as ctx:
                    client.search_trades(size=500)
                self.assertIn("size", str(ctx.exception))
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.trace_id, "mock-trace")

    def test_refresh_token_without_rotation_is_reusable(self) -> None:
        """Если БКС не ротирует refresh-токен — программа тоже должна работать (один и тот же токен)."""
        state = mock_server.MockState(rotate_refresh=False)
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token=state.expected_refresh, base_url=server.base_url, cache_path=str(cache), rps=50.0
                )
                client.http.authenticate()
                self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["refresh_token"], state.expected_refresh)
                # кэш access-токена протух — обмен по тому же refresh проходит успешно
                client.store.get().expires_at = 0
                self.assertTrue(client.get_portfolio().positions)

    def test_expired_refresh_token_raises_auth_error_with_hint(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                client = BcsClient(
                    refresh_token="stale-token", base_url=server.base_url, cache_path=str(cache), rps=50.0
                )
                with self.assertRaises(AuthError) as ctx:
                    client.http.authenticate()
                message = str(ctx.exception)
                self.assertIn("invalid_grant", message)
                self.assertIn("90 суток", message)  # человекочитаемая подсказка


class CliTests(unittest.TestCase):
    """CLI проверяем настоящим запуском — так покрывается и разбор аргументов, и вывод."""

    def run_cli(self, *argv: str, env: dict[str, str] | None = None, cwd: Path | None = None):
        return run_cli(*argv, env=env, cwd=cwd)

    def test_demo_command_renders_everything(self) -> None:
        result = self.run_cli("demo", "--days", "30")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        for needle in ("ПОРТФЕЛЬ (демо-данные)", "Стоимость бумаг", "Позиции", "СДЕЛКИ (демо-данные)", "Лимиты:"):
            self.assertIn(needle, out)
        self.assertIn("SBER", out)

    def test_portfolio_and_trades_json_against_mock(self) -> None:
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                env = {
                    "BCS_REFRESH_TOKEN": state.expected_refresh,
                    "BCS_API_BASE_URL": server.base_url,
                    "BCS_TOKEN_CACHE": str(cache),
                }
                result = self.run_cli("portfolio", "-f", "json", "--no-names", env=env, cwd=Path(cache).parent)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertIn("summary", payload)
                self.assertGreater(payload["summary"]["positions"], 0)

                result = self.run_cli("trades", "-f", "csv", "--days", "30", env=env, cwd=Path(cache).parent)
                self.assertEqual(result.returncode, 0, result.stderr)
                header = result.stdout.splitlines()[0]
                self.assertIn("trade_date", header)
                self.assertIn("side_label", header)

                result = self.run_cli("limits", env=env, cwd=Path(cache).parent)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Лимиты:", result.stdout)

                result = self.run_cli(
                    "export",
                    "--out",
                    str(Path(cache).parent / "reports"),
                    "--days",
                    "20",
                    env=env,
                    cwd=Path(cache).parent,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                files = sorted(Path(cache).parent.glob("reports/*"))
                self.assertEqual(len(files), 5, files)
                names = {f.name.split("-", 2)[-1] for f in files}
                self.assertTrue(any(n.endswith(".md") for n in names))
                self.assertTrue(any(n.endswith("-portfolio.json") for n in names))
                self.assertTrue(any(n.endswith("-portfolio.csv") for n in names))
                self.assertTrue(any(n.endswith("-trades.csv") for n in names))
                self.assertTrue(any(n.endswith("-raw.json") for n in names))
                markdown = next(f for f in files if f.suffix == ".md")
                text = markdown.read_text(encoding="utf-8")
                self.assertIn("## Сделки", text)
                self.assertIn("## Позиции", text)

    def test_bad_date_is_reported_without_traceback(self) -> None:
        result = self.run_cli("trades", "--since", "позавчера", env={"BCS_REFRESH_TOKEN": mock_server.VALID_REFRESH})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("не понял дату", result.stderr)

    def test_global_flags_reach_the_client(self) -> None:
        # --cache должен применяться: иначе запуск писал бы кэш токенов в текущую папку
        with _tmp_cache() as cache:
            result = self.run_cli("--cache", str(cache), "token", "--reset")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(cache), result.stdout)

    def test_global_option_after_subcommand_is_rejected(self) -> None:
        result = self.run_cli("portfolio", "--no-ask")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_missing_token_exits_with_auth_hint(self) -> None:
        # глобальные ключи — до подкоманды (так задумано в argparse), проверяем и это
        result = self.run_cli("--no-ask", "--cache", "/tmp/definitely-not-here/tokens.json", "portfolio")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("refresh-токен", result.stderr)


class _tmp_cache:
    """Контекст-менеджер временного файла кэша токенов."""

    def __init__(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp())

    def __enter__(self) -> Path:
        return self.dir / "tokens.json"

    def __exit__(self, *exc: object) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
