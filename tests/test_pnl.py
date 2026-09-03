"""Тесты для расчёта P&L, дедупликации денег, группировки позиций и P&L веб-эндпоинта."""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bcs_api.client import CashBalance, Portfolio, Position, Trade, deduplicate_cash
from bcs_api.demo import fake_operations, fake_portfolio, fake_trades
from bcs_api.pnl import calculate_pnl, get_position_category, resolve_allowed_types
from bcs_api.web import portfolio_payload


class CashDeduplicationTests(unittest.TestCase):
    def test_deduplicate_cash_merges_duplicates(self) -> None:
        cash_list = [
            CashBalance.from_api({"ticker": "RUB", "currency": "RUB", "quantity": 1000.0, "locked": 100.0, "currentValueRub": 1000.0, "term": "T0"}),
            CashBalance.from_api({"ticker": "RUB", "currency": "RUB", "quantity": 1000.0, "locked": 100.0, "currentValueRub": 1000.0, "term": "T1"}),
            CashBalance.from_api({"ticker": "USD", "currency": "USD", "quantity": 50.0, "locked": 0.0, "currentValueRub": 4500.0, "term": "T0"}),
        ]
        deduped = deduplicate_cash(cash_list)
        self.assertEqual(len(deduped), 2)
        rub = next(c for c in deduped if c.currency == "RUB")
        self.assertEqual(rub.quantity, 1000.0)
        self.assertEqual(rub.locked, 100.0)
        self.assertEqual(rub.available, 900.0)

    def test_deduplicate_cash_sums_different_accounts(self) -> None:
        cash_list = [
            CashBalance.from_api({"ticker": "RUB", "currency": "RUB", "account": "ACC1", "quantity": 1000.0, "locked": 0.0, "currentValueRub": 1000.0}),
            CashBalance.from_api({"ticker": "RUB", "currency": "RUB", "account": "ACC2", "quantity": 2000.0, "locked": 100.0, "currentValueRub": 2000.0}),
        ]
        deduped = deduplicate_cash(cash_list)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].quantity, 3000.0)
        self.assertEqual(deduped[0].locked, 100.0)
        self.assertEqual(deduped[0].available, 2900.0)


class PositionGroupingTests(unittest.TestCase):
    def test_positions_by_type_groups_correctly(self) -> None:
        positions = [
            Position.from_api({"ticker": "SBER", "displayName": "Сбербанк", "instrumentType": "STOCK", "quantity": 10, "currentValueRub": 2800.0, "unrealizedPL": 300.0}),
            Position.from_api({"ticker": "LKOH", "displayName": "Лукойл", "instrumentType": "STOCK", "quantity": 1, "currentValueRub": 7000.0, "unrealizedPL": 500.0}),
            Position.from_api({"ticker": "SU26238", "displayName": "ОФЗ 26238", "instrumentType": "BONDS", "quantity": 10, "currentValueRub": 6000.0, "unrealizedPL": -200.0}),
        ]
        portfolio = Portfolio(positions=positions)
        grouped = portfolio.positions_by_type()
        self.assertEqual(len(grouped), 2)
        stocks = next(g for g in grouped if g["class"] == "Акция РФ")
        self.assertEqual(stocks["count"], 2)
        self.assertEqual(stocks["value_rub"], 9800.0)
        self.assertEqual(stocks["unrealized_pl"], 800.0)

        bonds = next(g for g in grouped if g["class"] == "Облигация")
        self.assertEqual(bonds["count"], 1)
        self.assertEqual(bonds["value_rub"], 6000.0)
        self.assertEqual(bonds["unrealized_pl"], -200.0)


class PnlCalculationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trades = [
            Trade.from_api({
                "tradeNum": 1,
                "ticker": "SBER",
                "side": "1",
                "price": 250.0,
                "volume": 2500.0,
                "tradeQuantity": 10,
                "tradeDateTime": "2026-02-01T10:00:00.000Z",
                "settlementCurrency": "RUB",
            }),
            Trade.from_api({
                "tradeNum": 2,
                "ticker": "SBER",
                "side": "2",
                "price": 280.0,
                "volume": 2800.0,
                "tradeQuantity": 10,
                "tradeDateTime": "2026-02-05T12:00:00.000Z",
                "settlementCurrency": "RUB",
            }),
            Trade.from_api({
                "tradeNum": 3,
                "ticker": "SU26238",
                "side": "1",
                "price": 600.0,
                "volume": 6000.0,
                "tradeQuantity": 10,
                "tradeDateTime": "2026-02-02T11:00:00.000Z",
                "settlementCurrency": "RUB",
            }),
        ]
        self.operations = [
            {
                "date": "2026-02-03T10:00:00Z",
                "type": "Dividends",
                "ticker": "LKOH",
                "isin": "RU0009024277",
                "issuer": "ПАО ЛУКОЙЛ",
                "sum": 1500.0,
                "currency": "RUB",
                "status": "Approved",
                "balance_change": "Positive",
            },
            {
                "date": "2026-02-04T10:00:00Z",
                "type": "Coupons",
                "ticker": "SU26238",
                "isin": "RU000A101NJ1",
                "issuer": "Минфин РФ",
                "sum": 350.0,
                "currency": "RUB",
                "status": "Approved",
                "balance_change": "Positive",
            },
            {
                "date": "2026-02-05T10:00:00Z",
                "type": "Brokerage commission",
                "ticker": None,
                "sum": 50.0,
                "currency": "RUB",
                "status": "Approved",
                "balance_change": "Negative",
            },
            {
                "date": "2026-02-06T10:00:00Z",
                "type": "Tax",
                "ticker": None,
                "sum": 195.0,
                "currency": "RUB",
                "status": "Approved",
                "balance_change": "Negative",
            },
            {
                "date": "2026-02-01T08:00:00Z",
                "type": "Deposit",
                "ticker": None,
                "sum": 50000.0,
                "currency": "RUB",
                "status": "Approved",
                "balance_change": "Positive",
            },
        ]
        self.portfolio_positions = [
            Position.from_api({"ticker": "SBER", "instrumentType": "STOCK", "quantity": 20, "currentValueRub": 5600.0, "unrealizedPL": 600.0}),
            Position.from_api({"ticker": "SU26238", "instrumentType": "BONDS", "quantity": 10, "currentValueRub": 6000.0, "unrealizedPL": -200.0}),
        ]

    def test_calculate_pnl_all_assets(self) -> None:
        report = calculate_pnl(
            portfolio=Portfolio(positions=self.portfolio_positions),
            trades=self.trades,
            operations=self.operations,
            asset_types=None,
        )
        s = report["summary"]
        self.assertEqual(s["total_income"], 2150.0)  # 300 trade + 1500 div + 350 coupons
        self.assertEqual(s["total_expenses"], 245.0)  # 50 commission + 195 tax
        self.assertEqual(s["net_realized_pnl"], 1905.0)  # 2150 - 245
        self.assertEqual(s["potential_capital_gain"], 400.0)  # 600 - 200
        self.assertEqual(s["net_pnl"], 2305.0)  # 1905 + 400

        inc = {item["key"]: item["value"] for item in report["income_items"]}
        self.assertEqual(inc["trade_realized_income"], 300.0)
        self.assertEqual(inc["dividends"], 1500.0)
        self.assertEqual(inc["coupons"], 350.0)

        exp = {item["key"]: item["value"] for item in report["expense_items"]}
        self.assertEqual(exp["commissions"], 50.0)
        self.assertEqual(exp["taxes"], 195.0)

        self.assertEqual(report["cash_flow"]["pay_in"], 50000.0)
        self.assertEqual(report["cash_flow"]["pay_out"], 0.0)

    def test_calculate_pnl_filtered_stocks_only(self) -> None:
        report = calculate_pnl(
            portfolio=Portfolio(positions=self.portfolio_positions),
            trades=self.trades,
            operations=self.operations,
            asset_types=["STOCK"],
        )
        inc = {item["key"]: item["value"] for item in report["income_items"]}
        self.assertEqual(inc["trade_realized_income"], 300.0)  # SBER
        self.assertEqual(inc["dividends"], 1500.0)  # LKOH
        self.assertEqual(inc["coupons"], 0.0)  # SU26238 bond coupon excluded
        self.assertEqual(report["summary"]["potential_capital_gain"], 600.0)  # SBER unrealized pl (SU26238 excluded)

    def test_parse_asset_types(self) -> None:
        self.assertEqual(
            resolve_allowed_types(["STOCK", "BONDS"]),
            {"STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPTS", "BONDS", "EURO_BONDS", "NOTES"},
        )
        self.assertEqual(
            resolve_allowed_types(["FUTURES", "FUNDS"]),
            {"FUTURES", "OPTIONS", "GOODS", "MUTUAL_FUNDS", "ETF"},
        )
        self.assertIsNone(resolve_allowed_types(["ALL"]))

<<<<<<< ours
    def test_get_position_category(self) -> None:
        self.assertEqual(get_position_category("STOCK"), "STOCK")
        self.assertEqual(get_position_category("BONDS"), "BONDS")
        self.assertEqual(get_position_category("FUTURES"), "FUTURES")
=======
    def test_income_tax_handling_in_pnl_and_operations(self) -> None:
        from bcs_api.pnl import signed_operation_sum
        from bcs_api.web import operations_rows

        # 1. Проверяем правильное формирование знака для IncomeTax в отчёте об операциях
        income_tax_op = {
            "date": "2026-02-10T10:00:00Z",
            "type": "IncomeTax",
            "sum": 195.0,  # сырое значение из API положительное
            "balanceChange": "Negative",
            "currency": "RUB",
            "status": "Approved",
        }
        rows = operations_rows([income_tax_op])
        self.assertEqual(rows[0]["sum"], -195.0)  # Должно стать отрицательным
        self.assertEqual(signed_operation_sum(195.0, balance_change="Negative", op_type="IncomeTax"), -195.0)

        # 2. Проверяем, что в отчёте P&L IncomeTax попадает в расходы налоги, а не в прочие доходы
        report = calculate_pnl(
            portfolio=Portfolio(positions=[]),
            trades=[],
            operations=[income_tax_op],
        )
        exp = {item["key"]: item["value"] for item in report["expense_items"]}
        inc = {item["key"]: item["value"] for item in report["income_items"]}

        self.assertEqual(exp["taxes"], 195.0)  # Должно попасть в налоги
        self.assertEqual(inc["other_income"], 0.0)  # НЕ должно быть в прочих доходах!
        self.assertEqual(report["summary"]["total_expenses"], 195.0)
        self.assertEqual(report["summary"]["total_income"], 0.0)
>>>>>>> theirs


class DemoAndWebPnlTests(unittest.TestCase):
    def test_cli_pnl_command(self) -> None:
        import mock_server
        from tests.test_client import _tmp_cache, run_cli
        state = mock_server.MockState()
        with mock_server.MockServer(state) as server:
            with _tmp_cache() as cache:
                env = {
                    "BCS_REFRESH_TOKEN": state.expected_refresh,
                    "BCS_API_BASE_URL": server.base_url,
                    "BCS_TOKEN_CACHE": str(cache),
                }
                result = run_cli("pnl", "--days", "30", "-f", "json", env=env, cwd=Path(cache).parent)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertIn("summary", payload)
                self.assertIn("net_pnl", payload["summary"])

    def test_demo_data_pnl_calculation(self) -> None:
        portfolio = fake_portfolio()
        trades = fake_trades(30)
        operations = fake_operations(30)
        report = calculate_pnl(portfolio=portfolio, trades=trades, operations=operations)
        self.assertIn("summary", report)
        self.assertIn("net_pnl", report["summary"])
        self.assertIn("potential_capital_gain", report["summary"])
        self.assertIn("by_category", report)
        self.assertIn("income_items", report)
        self.assertIn("expense_items", report)

    def test_portfolio_payload_includes_positions_by_type(self) -> None:
        portfolio = fake_portfolio()
        payload = portfolio_payload(portfolio, source="demo")
        self.assertIn("positions_by_type", payload)
        self.assertTrue(len(payload["positions_by_type"]) > 0)
        for g in payload["positions_by_type"]:
            self.assertIn("class", g)
            self.assertIn("count", g)
            self.assertIn("value_rub", g)
            self.assertIn("positions", g)


if __name__ == "__main__":
    unittest.main(verbosity=2)
