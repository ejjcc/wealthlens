#!/usr/bin/env python3

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import _stock  # noqa: E402
import stock_order  # noqa: E402
import trade_quota  # noqa: E402


ET = ZoneInfo("America/New_York")


def at(year, month, day, hour, minute):
    return dt.datetime(year, month, day, hour, minute, tzinfo=ET)


class SessionRoutingTests(unittest.TestCase):
    def assert_session(self, value, name, route, active=True):
        result = _stock.classify_us_stock_session(value)
        self.assertEqual(name, result.name)
        self.assertEqual(route, result.route)
        self.assertEqual(active, result.active)

    def test_weekday_boundaries(self):
        # Monday, 2026-07-20 ET.
        self.assert_session(at(2026, 7, 20, 3, 49), "overnight", "OVERNIGHT")
        self.assert_session(at(2026, 7, 20, 3, 50), "session_gap", None, False)
        self.assert_session(at(2026, 7, 20, 4, 0), "premarket", "SMART")
        self.assert_session(at(2026, 7, 20, 9, 30), "regular", "SMART")
        self.assert_session(at(2026, 7, 20, 16, 0), "afterhours", "SMART")
        self.assert_session(at(2026, 7, 20, 20, 0), "overnight", "OVERNIGHT")

    def test_weekend_boundaries(self):
        self.assert_session(at(2026, 7, 24, 20, 0), "closed", None, False)  # Fri
        self.assert_session(at(2026, 7, 25, 12, 0), "closed", None, False)  # Sat
        self.assert_session(at(2026, 7, 26, 19, 59), "closed", None, False)
        self.assert_session(at(2026, 7, 26, 20, 0), "overnight", "OVERNIGHT")

    def test_route_and_marketable_guards(self):
        session = _stock.classify_us_stock_session(at(2026, 7, 20, 2, 0))
        self.assertTrue(_stock.route_is_live("OVERNIGHT", session))
        self.assertFalse(_stock.route_is_live("SMART", session))
        self.assertTrue(_stock.is_marketable("BUY", 10.1, 10.0, 10.05))
        self.assertFalse(_stock.is_marketable("BUY", 10.0, 10.0, 10.05))
        self.assertTrue(_stock.is_marketable("SELL", 9.99, 10.0, 10.05))
        self.assertIsNone(_stock.is_marketable("BUY", 10.0, 10.0, None))

    def test_target_position_uses_whole_share_delta(self):
        self.assertEqual(7, stock_order.quantity_for_target("BUY", 3, 10))
        self.assertEqual(7, stock_order.quantity_for_target("SELL", 10, 3))
        self.assertEqual(0, stock_order.quantity_for_target("BUY", 10, 10))
        with self.assertRaises(stock_order.OrderAbort):
            stock_order.quantity_for_target("BUY", 3.5, 10)

    def test_shared_ticker_list_is_filtered_by_tick_shape(self):
        class BidAsk:
            bidPrice = 10.0

        class Last:
            price = 10.1

        ticks = [Last(), BidAsk()]
        self.assertIsInstance(_stock.latest_tick_with(ticks, "bidPrice"), BidAsk)
        self.assertIsInstance(_stock.latest_tick_with(ticks, "price"), Last)

    def test_fresh_live_quote_source_guard(self):
        now = dt.datetime.now(dt.timezone.utc)
        base = {
            "source": "tick_by_tick", "time": now.isoformat(),
            "bid": 10.0, "ask": 10.1,
        }
        self.assertTrue(_stock.is_fresh_live_quote(base))
        self.assertTrue(_stock.is_fresh_live_quote({
            **base, "source": "streaming_mkt_data"
        }))
        self.assertFalse(_stock.is_fresh_live_quote({
            **base, "source": "historical_trade"
        }))
        self.assertFalse(_stock.is_fresh_live_quote({
            **base, "source": "streaming_mkt_data", "ask": 9.9
        }))
        self.assertFalse(_stock.is_fresh_live_quote({
            **base, "source": "streaming_mkt_data",
            "time": (now - dt.timedelta(seconds=11)).isoformat(),
        }))

    def test_stock_qualification_rejects_position_aligned_none(self):
        class FakeIB:
            async def qualifyContractsAsync(self, *_contracts):
                return [None]

        with self.assertRaisesRegex(RuntimeError, "cannot qualify TEST"):
            import asyncio
            asyncio.run(_stock.qualify_stock(FakeIB(), "TEST", "SMART"))

    def test_stock_position_ignores_options_with_same_symbol(self):
        class Contract:
            def __init__(self, symbol, sec_type):
                self.symbol = symbol
                self.secType = sec_type

        class Position:
            def __init__(self, symbol, sec_type, qty, avg_cost):
                self.contract = Contract(symbol, sec_type)
                self.position = qty
                self.avgCost = avg_cost

        class FakeIB:
            def positions(self, _account=None):
                return [
                    Position("XYZ", "STK", 500, 12.5),
                    Position("XYZ", "OPT", 2, 150.0),
                    Position("ABC", "STK", 5, 100.0),
                ]

        result = _stock.snapshot_position(FakeIB(), "XYZ")

        self.assertEqual(500, result["quantity"])
        self.assertAlmostEqual(12.5, result["avgCost"])


class RedirectPrecautionTests(unittest.TestCase):
    @mock.patch.object(_stock.subprocess, "check_output")
    def test_gateway_start_time_uses_absolute_lsof_path(self, check_output):
        check_output.side_effect = ["123\n", "Mon Jul 20 09:30:00 2026\n"]

        result = _stock._gateway_start_time(4001)

        self.assertIsNotNone(result)
        self.assertEqual(_stock.LSOF_BIN, check_output.call_args_list[0].args[0][0])
        self.assertTrue(os.path.isabs(check_output.call_args_list[0].args[0][0]))

    def test_config_and_running_gateway_evidence_must_both_be_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = os.path.join(tmp, "config.ini")
            log = os.path.join(tmp, "gateway.out.log")
            with open(ini, "w") as f:
                f.write("BypassOrderPrecautions=\nBypassRedirectOrderWarning=yes\n")
            self.assertTrue(_stock.redirect_bypass_configured(ini))

            start = dt.datetime(2026, 7, 22, 14, 21, 55).astimezone()
            with open(log, "w") as f:
                f.write("2026-07-22 14:24:15:465 IBC: 'Bypass Redirect Order warning "
                        "for Stock API Orders' is now selected\n")
            ok, _ = _stock.redirect_bypass_applied(log, start, ini)
            self.assertTrue(ok)

            later_start = dt.datetime(2026, 7, 22, 15, 0, 0).astimezone()
            ok, detail = _stock.redirect_bypass_applied(log, later_start, ini)
            self.assertFalse(ok)
            self.assertIn("restart required", detail)

            with open(log, "a") as f:
                f.write("2026-07-22 15:00:10:074 IBC: 'Bypass Redirect Order warning "
                        "for Stock API Orders' is already selected\n")
            ok, _ = _stock.redirect_bypass_applied(log, later_start, ini)
            self.assertTrue(ok)

    def test_generic_bypass_is_not_required(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("BypassOrderPrecautions=\nBypassRedirectOrderWarning=yes\n")
            path = f.name
        try:
            self.assertTrue(_stock.redirect_bypass_configured(path))
        finally:
            os.unlink(path)


class QuotaLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "trade_quota.json")
        self.patch = mock.patch.object(trade_quota, "STATE", self.state)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def ops(self):
        if not os.path.exists(self.state):
            return []
        with open(self.state) as f:
            return json.load(f)["ops"]

    def write_ops(self, ops):
        with open(self.state, "w") as f:
            json.dump({"limit": 1, "ops": ops}, f)

    def test_sell_is_audited_but_does_not_open_buy_window(self):
        with mock.patch.object(trade_quota, "et_today",
                               return_value=dt.date(2026, 8, 17)):
            trade_quota.log_op("QQQM", "SELL", "raise cash")

        rows = self.ops()
        self.assertEqual(1, len(rows))
        self.assertFalse(rows[0]["consumesQuota"])

        with mock.patch.object(trade_quota, "et_today",
                               return_value=dt.date(2026, 8, 18)):
            self.assertEqual([], trade_quota.consuming_week_ops())
            buy_ok, buy_message = trade_quota.quota_available("BUY")
            sell_ok, sell_message = trade_quota.quota_available("SELL")
            self.assertTrue(buy_ok)
            self.assertIn("买入窗未开", buy_message)
            self.assertTrue(sell_ok)
            self.assertIn("不会打开或消耗", sell_message)
            self.assertIn("未开", trade_quota.status_text(short=True))

    def test_only_nonexempt_buy_sets_window_and_sell_stays_allowed(self):
        # Old SELL rows have no consumesQuota flag; explicit exempt BUY rows
        # are also ignored. Only the ordinary BUY establishes the buy day.
        self.write_ops([
            {"week": "2026-W34", "date": "2026-08-17",
             "symbol": "QQQM", "action": "SELL", "status": "Filled"},
            {"week": "2026-W34", "date": "2026-08-17",
             "symbol": "BAC", "action": "BUY", "quotaExempt": True,
             "status": "Filled"},
            {"week": "2026-W34", "date": "2026-08-17",
             "symbol": "700", "action": "BUY", "consumesQuota": True,
             "status": "Filled"},
        ])

        with mock.patch.object(trade_quota, "et_today",
                               return_value=dt.date(2026, 8, 18)):
            consuming = trade_quota.consuming_week_ops()
            self.assertEqual(["700"], [row["symbol"] for row in consuming])
            buy_ok, _ = trade_quota.quota_available("BUY")
            sell_ok, _ = trade_quota.quota_available("SELL")
            self.assertFalse(buy_ok)
            self.assertTrue(sell_ok)
            self.assertIn("已关", trade_quota.status_text(short=True))
            self.assertIn("1 笔 BUY", trade_quota.status_text(short=True))
            self.assertIn("人工显式 override", trade_quota.status_text(short=True))

            with self.assertRaisesRegex(stock_order.OrderAbort, "QUOTA BLOCKED"):
                stock_order.quota_preflight("BUY")
            _, override_applied = stock_order.quota_preflight(
                "BUY", "explicit buy exception"
            )
            self.assertTrue(override_applied)
            _, sell_override_applied = stock_order.quota_preflight(
                "SELL", "legacy unnecessary override"
            )
            self.assertFalse(sell_override_applied)

    def test_explicit_consumes_quota_flag_wins_for_exempt_buy(self):
        row = {"action": "BUY", "consumesQuota": False}
        self.assertFalse(trade_quota.operation_consumes_quota(row))
        self.assertTrue(trade_quota.operation_consumes_quota({"action": "BUY"}))
        self.assertFalse(trade_quota.operation_consumes_quota({"action": "SELL"}))
        self.assertFalse(trade_quota.operation_consumes_quota(
            {"action": "SELL", "consumesQuota": True}
        ))
        self.assertFalse(trade_quota.operation_consumes_quota(
            {"action": "BUY", "consumesQuota": True, "quotaExempt": True}
        ))

    def test_any_explicit_out_of_window_buy_override_is_allowed_and_logged(self):
        self.write_ops([
            {"week": "2026-W34", "date": "2026-08-17",
             "symbol": "700", "action": "BUY", "consumesQuota": True,
             "status": "Filled"},
        ])
        with mock.patch.object(trade_quota, "et_today",
                               return_value=dt.date(2026, 8, 18)):
            ok, blocked_message = trade_quota.quota_available("BUY")
            self.assertFalse(ok)
            self.assertIn('--quota-override "<具体理由>"', blocked_message)
            with self.assertRaisesRegex(stock_order.OrderAbort, "QUOTA BLOCKED"):
                stock_order.quota_preflight("BUY")

            message, override_applied = stock_order.quota_preflight(
                "BUY", "user chose a second discretionary buy day"
            )
            self.assertEqual(blocked_message, message)
            self.assertTrue(override_applied)
            self.assertTrue(trade_quota.upsert_order(
                "NBIS", "BUY", 789, status="Filled", qty=1, filled_qty=1,
                limit_price=250.0, route="SMART", override=override_applied,
            ))

        row = next(op for op in self.ops() if op.get("permId") == 789)
        self.assertTrue(row["override"])
        self.assertTrue(row["consumesQuota"])

    def test_sell_upsert_is_non_consuming_audit_row(self):
        self.assertTrue(trade_quota.upsert_order(
            "VOO", "SELL", 456, order_id=70, status="Filled",
            qty=4, filled_qty=4, limit_price=710.0, route="SMART",
            override=True,
        ))
        rows = self.ops()
        self.assertEqual(1, len(rows))
        self.assertFalse(rows[0]["consumesQuota"])
        self.assertFalse(rows[0]["override"])
        self.assertFalse(trade_quota.operation_consumes_quota(rows[0]))

    def test_rejected_new_order_is_not_logged(self):
        self.assertFalse(trade_quota.upsert_order(
            "XYZ", "BUY", 0, status="Cancelled", qty=100, filled_qty=0
        ))
        self.assertEqual([], self.ops())
        self.assertFalse(trade_quota.upsert_order(
            "XYZ", "BUY", 123, status="Inactive", qty=100, filled_qty=0
        ))
        self.assertEqual([], self.ops())

    def test_same_perm_id_is_updated_not_duplicated(self):
        self.assertTrue(trade_quota.upsert_order(
            "XYZ", "BUY", 123, order_id=69, status="PreSubmitted",
            qty=100, filled_qty=0, limit_price=48.25, route="SMART",
        ))
        self.assertTrue(trade_quota.upsert_order(
            "XYZ", "BUY", 123, order_id=69, status="Filled",
            qty=100, filled_qty=100, limit_price=48.25,
            avg_fill_price=48.25, route="SMART",
        ))
        rows = self.ops()
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["consumesQuota"])
        self.assertEqual("Filled", rows[0]["status"])
        self.assertEqual(100.0, rows[0]["filledQty"])
        self.assertEqual(48.25, rows[0]["avgFillPrice"])

    def test_read_only_snapshot_reconciles_existing_row_only(self):
        trade_quota.upsert_order(
            "XYZ", "BUY", 123, order_id=69, status="PreSubmitted",
            qty=100, filled_qty=0, limit_price=48.25, route="SMART",
        )
        updates = trade_quota.reconcile_existing([], [
            {"permId": 123, "shares": 40, "price": 48.25},
            {"permId": 123, "shares": 60, "price": 48.25},
            {"permId": 999, "shares": 10, "price": 1.0},
        ])
        self.assertEqual(1, updates)
        rows = self.ops()
        self.assertEqual(1, len(rows))
        self.assertEqual("Filled", rows[0]["status"])
        self.assertEqual(100.0, rows[0]["filledQty"])

    def test_order_lock_rejects_second_executor(self):
        lock = os.path.join(self.tmp.name, "stock_order.lock")
        with _stock.exclusive_order_lock(lock):
            with self.assertRaises(RuntimeError):
                with _stock.exclusive_order_lock(lock):
                    pass


class StopOrderGuardTests(unittest.TestCase):
    def parse(self, *extra):
        argv = ["stock_order.py", "--symbol", "XYZ", "--qty", "100"] + list(extra)
        with mock.patch.object(sys, "argv", argv):
            return stock_order.parse_args()

    def test_sell_stop_trigger_must_sit_below_bid(self):
        self.assertTrue(_stock.sell_stop_is_safe(2.16, 2.39))
        self.assertFalse(_stock.sell_stop_is_safe(2.40, 2.39))
        self.assertFalse(_stock.sell_stop_is_safe(2.39, 2.39))
        self.assertIsNone(_stock.sell_stop_is_safe(2.16, None))

    def test_stop_rejects_buy_and_marketable_and_inverted_limit(self):
        with self.assertRaises(SystemExit):
            self.parse("--action", "BUY", "--stop", "2.16", "--market")
        with self.assertRaises(SystemExit):
            self.parse("--action", "SELL", "--stop", "2.16", "--market",
                       "--require-marketable")
        with self.assertRaises(SystemExit):
            self.parse("--action", "SELL", "--stop", "2.16", "--limit", "2.30")

    def test_stop_defaults_to_rth_only_and_plain_orders_keep_outside_rth(self):
        stop_limit = self.parse("--action", "SELL", "--stop", "2.16", "--limit", "2.05")
        self.assertFalse(stop_limit.outside_rth)
        plain = self.parse("--action", "SELL", "--limit", "2.05")
        self.assertTrue(plain.outside_rth)
        explicit = self.parse("--action", "SELL", "--stop", "2.16", "--limit", "2.05",
                              "--outside-rth")
        self.assertTrue(explicit.outside_rth)

    def test_stop_limit_equal_trigger_is_allowed(self):
        args = self.parse("--action", "SELL", "--stop", "2.16", "--limit", "2.16")
        self.assertEqual(2.16, args.stop)
        self.assertEqual(2.16, args.limit)


if __name__ == "__main__":
    unittest.main()
