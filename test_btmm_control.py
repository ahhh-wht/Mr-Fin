import argparse
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import btmm
import pandas as pd


class ExportControlTests(unittest.TestCase):
    def test_selected_symbols_are_resolved_together(self):
        args = argparse.Namespace(
            mt_map="GBPUSD=GBPUSD.m", mt_suffix=".x", symbols="EURUSD,GBPUSD,USDJPY"
        )
        instruments = btmm.resolve_instruments(args)
        self.assertEqual([item["label"] for item in instruments], ["EURUSD", "GBPUSD", "USDJPY"])
        self.assertEqual([item["mt"] for item in instruments], ["EURUSD.x", "GBPUSD.m", "USDJPY.x"])

    def test_export_lock_rejects_another_exporter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.csv"
            with btmm.signal_export_lock(path):
                with self.assertRaises(RuntimeError):
                    with btmm.signal_export_lock(path):
                        pass
            with btmm.signal_export_lock(path):
                pass

    def test_run_writes_all_three_instruments_to_one_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.csv"
            args = argparse.Namespace(
                db="unused", grid=False, timeframe="60m", sl=20, tp=50,
                mt_map=None, mt_suffix="", symbols="EURUSD,GBPUSD,USDJPY",
                sl_mode="fixed", tp_mode="fixed", sl_buffer=10, rr=2.5,
                atr_mult=1.5, no_be=False, be=25, trail=None, lot=0.1,
                balance=10000, export_signals=str(path), export_window=2,
            )
            frame = pd.DataFrame({"datetime": [pd.Timestamp("2026-01-01", tz="UTC")]})

            class Database:
                def __init__(self, _): pass
                def save_price(self, *args): pass
                def save_run(self, *args): pass
                def close(self): pass

            class Backtester:
                def __init__(self, *args): self.trades = []
                def run(self, *args): pass
                def metrics(self, _):
                    return dict.fromkeys(("total_trades", "wins", "losses", "win_rate", "total_pips",
                                          "final_balance", "profit_factor", "max_drawdown", "sharpe_ratio"), 0)

            class Strategy:
                def __init__(self, *args): pass
                def generate_signals(self, _): return []

            with patch.object(btmm, "BTMMDatabase", Database), \
                 patch.object(btmm, "Backtester", Backtester), \
                 patch.object(btmm, "BTMMStrategy", Strategy), \
                 patch.object(btmm, "load_data", side_effect=lambda inst, args, cfg: (frame, 0.0001, inst["mt"])), \
                 patch.object(btmm, "add_indicators", side_effect=lambda df: df), \
                 patch.object(btmm, "detect_swings", side_effect=lambda df, window: df), \
                 patch.object(btmm, "build_signal_rows", side_effect=lambda signals, cfg, symbol, *args: [{"symbol": symbol}]):
                btmm.run(args)
            self.assertEqual(pd.read_csv(path)["symbol"].tolist(), ["EURUSD", "GBPUSD", "USDJPY"])

    def test_stop_waits_for_refresh_then_clears_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.csv"
            args = argparse.Namespace(export_signals=str(path), watch=5)
            entered = threading.Event()
            release = threading.Event()
            stop = threading.Event()

            def refresh(_):
                entered.set()
                self.assertTrue(release.wait(5))
                btmm.write_signals([{"id": "test"}], path)

            with patch.object(btmm, "run", side_effect=refresh):
                worker = threading.Thread(target=btmm.run_watch, args=(args, stop))
                worker.start()
                self.assertTrue(entered.wait(5))
                stop.set()
                release.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_control_starts_three_pairs_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                export_signals=str(Path(directory) / "signals.csv"), watch=5
            )
            started = threading.Event()
            captured = []

            def watch(selected, stop):
                captured.append(selected.symbols)
                started.set()
                self.assertTrue(stop.wait(5))

            def command(_):
                if not captured:
                    return "start EURUSD,GBPUSD,USDJPY"
                self.assertTrue(started.wait(5))
                return "quit"

            with patch.object(btmm, "run_watch", side_effect=watch), patch("builtins.input", side_effect=command):
                btmm.control_console(args)
            self.assertEqual(captured, ["EURUSD,GBPUSD,USDJPY"])


if __name__ == "__main__":
    unittest.main()
