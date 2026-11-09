import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import btmm
import ui
from plan import TradePlan


def make(**kw):
    base = dict(symbols=["EURUSD"], timeframes=["15m", "60m"])
    base.update(kw)
    return TradePlan.from_dict(base)


class PlanTests(unittest.TestCase):
    def test_limits_of_three(self):
        with self.assertRaises(ValueError):
            make(symbols=["A", "B", "C", "D"])
        with self.assertRaises(ValueError):
            make(timeframes=["1m", "5m", "15m", "30m"])
        with self.assertRaises(ValueError):
            make(days=4)

    def test_ratio_constants_and_requirements(self):
        plan = make(tpsl_mode="ratio", ratio=3)
        self.assertEqual(plan.cfg_overrides(), {"tp_mode": "rr", "rr_ratio": 3.0})
        with self.assertRaises(ValueError):
            make(tpsl_mode="ratio", sl_enabled=False)

    def test_sl_tp_switch_individually(self):
        self.assertEqual(make(sl_enabled=False).apply_levels(1.0, 2.0), (0.0, 2.0))
        self.assertEqual(make(tp_enabled=False).apply_levels(1.0, 2.0), (1.0, 0.0))
        self.assertEqual(make(sl_enabled=False, tp_enabled=False).apply_levels(1.0, 2.0), (0.0, 0.0))

    def test_pips_mode(self):
        plan = make(tpsl_mode="pips", sl_pips=15, tp_pips=45)
        self.assertEqual(plan.cfg_overrides()["fixed_tp_pips"], 45.0)
        with self.assertRaises(ValueError):
            make(tpsl_mode="pips", sl_pips=15)
        make(tpsl_mode="pips", sl_pips=15, tp_enabled=False)

    def test_trading_hours_including_overnight(self):
        day = make(hours_enabled=True, hours_start="07:00", hours_end="16:00")
        self.assertTrue(day.in_trading_hours(8 * 3600))
        self.assertFalse(day.in_trading_hours(16 * 3600))
        night = make(hours_enabled=True, hours_start="22:00", hours_end="03:00")
        self.assertTrue(night.in_trading_hours(23 * 3600))
        self.assertTrue(night.in_trading_hours(1 * 3600))
        self.assertFalse(night.in_trading_hours(12 * 3600))
        shifted = make(hours_enabled=True, hours_start="07:00", hours_end="16:00", utc_offset=2)
        self.assertTrue(shifted.in_trading_hours(5 * 3600))
        self.assertTrue(make().in_trading_hours(0))

    def test_day_expiry(self):
        plan = make(days=2)
        self.assertFalse(plan.expired(0, 2 * 86400 - 1))
        self.assertTrue(plan.expired(0, 2 * 86400))

    def test_formula(self):
        plan = make(initial_amount=1000, att=500, atl=300, atp=800, formula_enabled=True)
        self.assertIsNone(plan.risk_stop_reason(0))
        self.assertIsNone(plan.risk_stop_reason(-199))
        self.assertIn("ATL", plan.risk_stop_reason(-200))
        self.assertIn("ATL", plan.risk_stop_reason(-350))
        self.assertIsNone(plan.risk_stop_reason(299))
        self.assertIn("ATP", plan.risk_stop_reason(300))
        off = make(initial_amount=1000, att=500, atl=300, atp=800, formula_enabled=False)
        self.assertIsNone(off.risk_stop_reason(-5000))
        only_atl = make(att=500, atl=300, formula_enabled=True)
        self.assertIsNone(only_atl.risk_stop_reason(10_000))

    def test_formula_validation(self):
        with self.assertRaises(ValueError):
            make(att=500, atl=600, formula_enabled=True)
        with self.assertRaises(ValueError):
            make(att=500, atp=400, formula_enabled=True)
        with self.assertRaises(ValueError):
            make(initial_amount=400, att=500, atl=100, formula_enabled=True)
        make(att=500, atl=600, formula_enabled=False)


class ExportTests(unittest.TestCase):
    def test_signal_rows_respect_plan(self):
        import pandas as pd
        cfg = btmm.BTMMConfig(timeframe="60m", sl_mode="fixed", tp_mode="rr", rr_ratio=2.0, fixed_sl_pips=20)
        now = pd.Timestamp("2026-01-01", tz="UTC")
        sig = btmm.Signal("EURUSD", "60m", now, "x", btmm.Direction.LONG, 1.1, 0.9, {"pattern_stop": 1.09, "atr": 0.001})
        rows = btmm.build_signal_rows([sig], cfg, "EURUSD", 0.0001, now, 2, make(tp_enabled=False))
        self.assertEqual(rows[0]["tp"], 0.0)
        self.assertAlmostEqual(rows[0]["sl"], 1.098)
        both = btmm.build_signal_rows([sig], cfg, "EURUSD", 0.0001, now, 2, make())[0]
        self.assertAlmostEqual(both["tp"], 1.104)


class PageTests(unittest.TestCase):
    def setUp(self):
        args = type("A", (), dict(source="yahoo", watch=None, export_signals=None, mt_map=None, mt5_path=None))()
        self.controller = ui.Controller(args)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = self.server.server_address[1]
        self.server.RequestHandlerClass = ui.make_handler(self.controller, port)
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, body, headers=None):
        request = urllib.request.Request(self.base + path, json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_page_and_config(self):
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertIn(b"Mr Fin", response.read())
        with urllib.request.urlopen(self.base + "/api/config") as response:
            self.assertEqual(json.load(response)["ratios"], {"1:1": 1.0, "1:2": 2.0, "1:3": 3.0})

    def test_start_rejects_bad_plan_and_foreign_host(self):
        code, body = self.post("/api/start", {"symbols": ["A", "B", "C", "D"]})
        self.assertEqual(code, 400)
        self.assertFalse(self.controller.running())
        code, _ = self.post("/api/start", {}, {"Host": "evil.example"})
        self.assertEqual(code, 403)
        request = urllib.request.Request(self.base + "/api/stop", b"{}", {"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 415)

    def test_start_and_stop_pass_plan_to_exporter(self):
        seen = {}
        started = threading.Event()

        def watch(args, stop):
            seen.update(symbols=args.symbols, timeframe=args.timeframe, plan=args.plan)
            started.set()
            stop.wait(5)

        with patch.object(ui, "save_plan"), patch.object(btmm, "run_watch", side_effect=watch):
            code, _ = self.post("/api/start", {"symbols": ["EURUSD", "XYZ.m"], "timeframes": ["15m", "4h"], "tpsl_mode": "ratio", "ratio": 2})
            self.assertEqual(code, 200)
            self.assertTrue(started.wait(5))
            self.assertEqual(self.post("/api/start", {"symbols": ["EURUSD"]})[0], 400)
            self.assertEqual(self.post("/api/stop", {})[1]["running"], False)
        self.assertEqual((seen["symbols"], seen["timeframe"]), ("EURUSD,XYZ.m", "15m,4h"))
        self.assertEqual(seen["plan"].ratio, 2.0)


if __name__ == "__main__":
    unittest.main()
