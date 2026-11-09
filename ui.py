"""Local browser page for choosing what Mr Fin trades. Served on 127.0.0.1 only."""
from __future__ import annotations

import collections
import copy
import json
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import btmm
from plan import MAX_DAYS, MAX_SELECTION, RATIO_PRESETS, TradePlan, load_plan, save_plan

PLAN_FILE = btmm.BASE_DIR / "btmm_plan.json"
PAGE = (Path(__file__).with_name("ui.html"))


class _Tee:
    """Mirrors console output into a buffer shown on the page."""

    def __init__(self, stream, lines):
        self.stream, self.lines, self.partial = stream, lines, ""

    def write(self, text):
        self.stream.write(text)
        self.partial += text
        *done, self.partial = self.partial.split("\n")
        self.lines.extend(line for line in done if line.strip())
        return len(text)

    def flush(self):
        self.stream.flush()


class Controller:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.worker = None
        self.stop_event = None
        self.plan = load_plan(PLAN_FILE)
        self.started_at = None
        self.log = collections.deque(maxlen=60)

    def running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def start(self, data: dict) -> None:
        plan = TradePlan.from_dict(data)
        plan.validate(btmm.TIMEFRAME_PRESETS)
        with self.lock:
            if self.running():
                raise ValueError("already running; stop first")
            save_plan(plan, PLAN_FILE)
            self.plan = plan
            selected = copy.copy(self.args)
            selected.symbols = ",".join(plan.symbols)
            selected.timeframe = ",".join(plan.timeframes)
            selected.export_signals = self.args.export_signals or "common"
            selected.watch = self.args.watch if self.args.watch is not None else 5
            selected.plan = plan
            custom = [n for n in plan.symbols if n.upper() not in btmm.INSTRUMENTS]
            selected.mt_map = ",".join(filter(None, [self.args.mt_map or ""] + [f"{n}={n}" for n in custom]))
            self.stop_event = threading.Event()
            self.started_at = time.time()

            def work(event=self.stop_event):
                try:
                    btmm.run_watch(selected, event)
                except Exception as exc:
                    print(f"Exporter stopped: {exc}", flush=True)

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

    def stop(self) -> None:
        with self.lock:
            if self.running():
                self.stop_event.set()
                self.worker.join()
            self.worker = None

    def status(self) -> dict:
        return {"running": self.running(), "started_at": self.started_at, "log": list(self.log)}


def broker_symbols(args) -> dict:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"symbols": [], "error": "MetaTrader5 package not installed (Windows + MT5 terminal required)"}
    if not (mt5.initialize(path=args.mt5_path) if args.mt5_path else mt5.initialize()):
        return {"symbols": [], "error": f"MT5 not available: {mt5.last_error()}"}
    try:
        return {"symbols": sorted(s.name for s in mt5.symbols_get() or [] if s.visible or s.select), "error": None}
    finally:
        mt5.shutdown()


def make_handler(controller: Controller, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, body, kind="application/json"):
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _host_ok(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._send(403, {"error": "forbidden host"})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            if self.path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/config":
                self._send(200, {
                    "groups": {g: [n for n, v in btmm.INSTRUMENTS.items() if v[0] == g] for g in btmm.GROUPS},
                    "pips": {n: v[2] for n, v in btmm.INSTRUMENTS.items()},
                    "timeframes": list(btmm.TIMEFRAME_PRESETS),
                    "ratios": RATIO_PRESETS, "max_selection": MAX_SELECTION, "max_days": MAX_DAYS,
                    "source": controller.args.source, "plan": asdict(controller.plan),
                })
            elif self.path == "/api/status":
                self._send(200, controller.status())
            elif self.path == "/api/broker-symbols":
                self._send(200, broker_symbols(controller.args))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._send(415, {"error": "JSON required"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(min(length, 100_000)) or b"{}")
                if self.path == "/api/start":
                    controller.start(data)
                elif self.path == "/api/stop":
                    controller.stop()
                else:
                    self._send(404, {"error": "not found"})
                    return
                self._send(200, controller.status())
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})

    return Handler


def serve(args) -> None:
    controller = Controller(args)
    sys.stdout = _Tee(sys.stdout, controller.log)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(controller, args.port))
    url = f"http://localhost:{args.port}/"
    print(f"Mr Fin page: {url}  (Ctrl+C to quit)")
    print("Stop clears pending signals but does not close positions or disable the EA.")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        controller.stop()
        server.server_close()
        sys.stdout = sys.stdout.stream
