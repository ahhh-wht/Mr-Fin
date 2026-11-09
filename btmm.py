"""BTMM configurable backtester. Requires pandas, numpy, and yfinance."""
from __future__ import annotations

import argparse
import copy
import dataclasses
import itertools
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

from plan import TradePlan

import numpy as np
import pandas as pd

TIMEFRAME_PRESETS = {
    "1m": {"minutes": 1, "period": "7d", "yahoo_interval": "1m", "swing_window": 3},
    "5m": {"minutes": 5, "period": "60d", "yahoo_interval": "5m", "swing_window": 3},
    "15m": {"minutes": 15, "period": "60d", "yahoo_interval": "15m", "swing_window": 3},
    "30m": {"minutes": 30, "period": "60d", "yahoo_interval": "30m", "swing_window": 2},
    "60m": {"minutes": 60, "period": "730d", "yahoo_interval": "60m", "swing_window": 2},
    "4h": {"minutes": 240, "period": "730d", "yahoo_interval": "60m", "swing_window": 1, "resample": "4h"},
    "1d": {"minutes": 1440, "period": "5y", "yahoo_interval": "1d", "swing_window": 1},
}


@dataclass
class BTMMConfig:
    timeframe: str = "60m"
    sl_mode: str = "pattern"
    tp_mode: str = "fixed"
    fixed_sl_pips: float = 20.0
    fixed_tp_pips: float = 50.0
    sl_buffer_pips: float = 10.0
    rr_ratio: float = 2.5
    atr_mult_sl: float = 1.5
    max_sl_pips: float = 60.0
    min_sl_pips: float = 7.0
    max_tp_pips: float = 300.0
    breakeven_at_pips: Optional[float] = 25.0
    trailing_stop_pips: Optional[float] = None
    asian_range_max_pips: float = 50.0
    trading_zone_pips: Tuple[float, float] = (25.0, 50.0)
    require_best_time: bool = True
    min_confidence: float = 0.5
    scratch_hours: Optional[float] = None
    max_open_trades: int = 3
    initial_balance: float = 10_000.0
    lot_size: float = 0.10
    pip_value_per_lot: float = 10.0
    run_label: str = "run"
    tf_minutes: int = field(init=False)
    swing_window: int = field(init=False)
    effective_gap_min: float = field(init=False)
    effective_gap_max: float = field(init=False)
    effective_scratch_hours: float = field(init=False)

    def __post_init__(self):
        if self.timeframe not in TIMEFRAME_PRESETS:
            raise ValueError(f"timeframe must be one of {list(TIMEFRAME_PRESETS)}")
        self.tf_minutes = TIMEFRAME_PRESETS[self.timeframe]["minutes"]
        self.swing_window = TIMEFRAME_PRESETS[self.timeframe]["swing_window"]
        self.effective_gap_min = max(30.0, self.tf_minutes)
        self.effective_gap_max = max(90.0, self.tf_minutes * 2)
        self.effective_scratch_hours = self.scratch_hours if self.scratch_hours is not None else max(2.0, self.tf_minutes / 60 * 3)
        if self.sl_mode not in ("fixed", "pattern", "atr"):
            raise ValueError("sl_mode must be fixed, pattern, or atr")
        if self.tp_mode not in ("fixed", "rr", "pattern"):
            raise ValueError("tp_mode must be fixed, rr, or pattern")
        if not (0 < self.min_sl_pips <= self.max_sl_pips and self.max_tp_pips > 0):
            raise ValueError("SL/TP limits must be positive and min_sl_pips <= max_sl_pips")
        if self.max_open_trades < 1 or self.lot_size <= 0 or self.initial_balance <= 0:
            raise ValueError("max_open_trades, lot_size, and initial_balance must be positive")
        if self.trading_zone_pips[0] < 0 or self.trading_zone_pips[1] < self.trading_zone_pips[0]:
            raise ValueError("trading_zone_pips must be an ascending non-negative pair")

    def to_json(self) -> str:
        data = asdict(self)
        data["trading_zone_pips"] = list(self.trading_zone_pips)
        return json.dumps(data)


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class Outcome(str, Enum):
    OPEN = "open"
    WIN = "win"
    LOSS = "loss"
    BE = "be"


@dataclass
class Signal:
    pair: str
    timeframe: str
    signal_time: pd.Timestamp
    signal_type: str
    direction: Direction
    price: float
    confidence: float
    pattern_data: dict


@dataclass
class Trade:
    trade_id: str
    run_id: str
    pair: str
    strategy: str
    direction: Direction
    entry_time: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    lot_size: float
    notes: str = ""
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    pips: float = 0.0
    pnl: float = 0.0
    outcome: Outcome = Outcome.OPEN
    timeframe: str = ""


def fetch_data(pair: str, cfg: BTMMConfig) -> pd.DataFrame:
    import yfinance as yf

    preset = TIMEFRAME_PRESETS[cfg.timeframe]
    raw = yf.download(pair, interval=preset["yahoo_interval"], period=preset["period"], progress=False, auto_adjust=False)
    if raw.empty:
        raise ValueError(f"No data for {pair} @ {cfg.timeframe}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.reset_index().rename(columns={"Datetime": "datetime", "Date": "datetime", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    if "datetime" not in df:
        raise ValueError("Yahoo Finance response did not contain a datetime column")
    if "volume" not in df:
        df["volume"] = 0
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.sort_values("datetime").reset_index(drop=True)
    df = df[["datetime", "open", "high", "low", "close", "volume"]].dropna()
    if "resample" in preset:
        df = df.set_index("datetime").resample(preset["resample"], origin="start_day").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
    return df


BASE_DIR = Path(__file__).resolve().parent
MT5_TIMEFRAMES = {"1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30", "60m": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1"}
SIGNAL_COLUMNS = ["id", "symbol", "direction", "entry", "sl", "tp", "lots", "expires_utc", "comment"]


def clean_symbol(pair: str) -> str:
    return pair.replace("=X", "")


def default_pip(symbol: str) -> float:
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def to_utc(times, offset_hours: float):
    return pd.to_datetime(times, utc=True) - pd.Timedelta(offset_hours, unit="h")


def fetch_mt5(symbol: str, cfg: BTMMConfig, bars: int, offset_hours: float, terminal_path: Optional[str] = None):
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise SystemExit("MetaTrader5 package missing: pip install MetaTrader5 (Windows + MT5 terminal required)")
    if not (mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()):
        raise RuntimeError(f"MT5 initialize failed (is the terminal installed and logged in?): {mt5.last_error()}")
    try:
        if not mt5.symbol_select(symbol, True):
            raise ValueError(f"Symbol {symbol!r} not found in MT5 Market Watch (check broker suffix, use --mt-symbol)")
        info = mt5.symbol_info(symbol)
        digits = info.digits if info else 5
        pip = 10 ** -digits * (10 if digits in (3, 5) else 1)
        # start at 1 so the still-forming bar is excluded
        rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, MT5_TIMEFRAMES[cfg.timeframe]), 1, bars)
    finally:
        mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise ValueError(f"MT5 returned no {cfg.timeframe} bars for {symbol}")
    df = pd.DataFrame(rates)
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True) - pd.Timedelta(hours=offset_hours)
    df["volume"] = df["tick_volume"]
    return df[["datetime", "open", "high", "low", "close", "volume"]].dropna().sort_values("datetime").reset_index(drop=True), pip


EA_MAGIC = 26110601  # must match InpMagic in the MT4/MT5 bridge EA


def mt5_session_pnl(started_at: float, offset_hours: float, magic: int = EA_MAGIC, terminal_path: Optional[str] = None) -> float:
    """Realised + floating P&L of the EA's trades since started_at (epoch seconds)."""
    import MetaTrader5 as mt5
    if not (mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()):
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        since = pd.Timestamp(started_at - 86400, unit="s").to_pydatetime()
        until = pd.Timestamp(time.time() + 2 * 86400, unit="s").to_pydatetime()
        total = 0.0
        for deal in mt5.history_deals_get(since, until) or []:
            if deal.magic == magic and deal.time - offset_hours * 3600 >= started_at:
                total += deal.profit + deal.commission + deal.swap + getattr(deal, "fee", 0.0)
        total += sum(p.profit + p.swap for p in mt5.positions_get() or [] if p.magic == magic)
        return total
    finally:
        mt5.shutdown()


def mt5_close_positions(magic: int = EA_MAGIC, terminal_path: Optional[str] = None) -> int:
    """Closes every open position opened by the EA; returns how many were closed."""
    import MetaTrader5 as mt5
    if not (mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()):
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    closed = 0
    try:
        for pos in mt5.positions_get() or []:
            if pos.magic != magic:
                continue
            tick = mt5.symbol_info_tick(pos.symbol)
            is_buy = pos.type == mt5.POSITION_TYPE_BUY
            result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
                                     "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY, "position": pos.ticket,
                                     "price": tick.bid if is_buy else tick.ask, "deviation": 20, "magic": magic,
                                     "comment": "BTMM limit reached"})
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                closed += 1
            else:
                print(f"  ! could not close {pos.symbol} #{pos.ticket}: {getattr(result, 'retcode', mt5.last_error())}", flush=True)
        return closed
    finally:
        mt5.shutdown()


def fetch_csv(path: str, offset_hours: float) -> pd.DataFrame:
    """Reads MT4 History Center exports (no header) and MT5 exports (<DATE> <TIME> ... header)."""
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
        first = handle.readline().strip()
    if first[:1].isdigit():
        raw = pd.read_csv(path, header=None, sep=None, engine="python", encoding="utf-8-sig")
        names = ["date", "time", "open", "high", "low", "close", "volume"] if raw.shape[1] >= 7 else ["datetime", "open", "high", "low", "close", "volume"]
        raw = raw.iloc[:, :len(names)]
        raw.columns = names
    else:
        raw = pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")
        raw.columns = [str(c).strip("<>").strip().lower() for c in raw.columns]
        raw = raw.rename(columns={"tickvol": "volume", "tick_volume": "volume"})
    if "datetime" not in raw:
        if "date" not in raw:
            raise ValueError("CSV needs date/time columns (MT4 History Center or MT5 export format)")
        raw["datetime"] = raw["date"].astype(str) + (" " + raw["time"].astype(str) if "time" in raw else "")
    if "volume" not in raw:
        raw["volume"] = 0
    raw["datetime"] = to_utc(pd.to_datetime(raw["datetime"].astype(str).str.replace(".", "-", n=2, regex=False)), offset_hours)
    df = raw[["datetime", "open", "high", "low", "close", "volume"]].dropna().sort_values("datetime")
    return df.drop_duplicates("datetime").reset_index(drop=True)


# name -> (group, Yahoo ticker, pip size). Broker names may differ: use --mt-suffix / --mt-map.
INSTRUMENTS = {
    "EURUSD": ("forex", "EURUSD=X", 0.0001), "GBPUSD": ("forex", "GBPUSD=X", 0.0001),
    "USDJPY": ("forex", "USDJPY=X", 0.01), "USDCHF": ("forex", "USDCHF=X", 0.0001),
    "AUDUSD": ("forex", "AUDUSD=X", 0.0001), "NZDUSD": ("forex", "NZDUSD=X", 0.0001),
    "USDCAD": ("forex", "USDCAD=X", 0.0001), "EURJPY": ("forex", "EURJPY=X", 0.01),
    "GBPJPY": ("forex", "GBPJPY=X", 0.01), "EURGBP": ("forex", "EURGBP=X", 0.0001),
    "XAUUSD": ("commodities", "GC=F", 0.1), "XAGUSD": ("commodities", "SI=F", 0.01),
    "USOIL": ("commodities", "CL=F", 0.01), "UKOIL": ("commodities", "BZ=F", 0.01),
    "NATGAS": ("commodities", "NG=F", 0.001), "COPPER": ("commodities", "HG=F", 0.0005),
    "US30": ("indices", "^DJI", 1.0), "US500": ("indices", "^GSPC", 0.1),
    "NAS100": ("indices", "^NDX", 1.0), "GER40": ("indices", "^GDAXI", 1.0),
    "UK100": ("indices", "^FTSE", 1.0), "JP225": ("indices", "^N225", 1.0),
}
GROUPS = ("forex", "commodities", "indices")


def parse_timeframes(value: str) -> list:
    frames = list(dict.fromkeys(t.strip() for t in value.split(",") if t.strip()))
    bad = [t for t in frames if t not in TIMEFRAME_PRESETS]
    if bad or not frames:
        raise SystemExit(f"invalid --timeframe {bad or value!r}; choose from {list(TIMEFRAME_PRESETS)}")
    return frames


def print_instruments():
    for group in GROUPS:
        print(f"{group}: " + ", ".join(n for n, v in INSTRUMENTS.items() if v[0] == group))
    print("\nUse: --symbols EURUSD,XAUUSD,US30 or --symbols forex,indices (any custom name is also accepted)")


def resolve_instruments(args) -> list:
    """Builds the list of instruments to process from --symbols (or the legacy --pair)."""
    mt_map = {}
    for item in filter(None, (args.mt_map or "").split(",")):
        name, _, broker = item.partition("=")
        mt_map[name.strip().upper()] = broker.strip()
    suffix = args.mt_suffix or ""
    if not args.symbols:
        name = args.label
        return [{"label": name, "yahoo": args.pair, "mt": args.mt_symbol or mt_map.get(name.upper()) or clean_symbol(args.pair) + suffix, "pip": None}]
    names = []
    for token in (t.strip() for t in args.symbols.split(",") if t.strip()):
        if token.lower() in GROUPS:
            names += [n for n, v in INSTRUMENTS.items() if v[0] == token.lower()]
        else:
            names.append(token.upper() if token.upper() in INSTRUMENTS else token)
    result = []
    for name in dict.fromkeys(names):
        if name in INSTRUMENTS:
            _, yahoo, pip = INSTRUMENTS[name]
        else:
            yahoo, pip = name, None
        result.append({"label": name, "yahoo": yahoo, "mt": mt_map.get(name.upper(), name + suffix), "pip": pip})
    return result


def load_data(inst: dict, args, cfg: BTMMConfig):
    """Returns (dataframe, pip_size, broker_symbol) for the selected data source."""
    symbol = inst["mt"]
    if args.source == "mt5":
        df, pip = fetch_mt5(symbol, cfg, args.bars, args.broker_utc_offset, args.mt5_path)
    elif args.source == "csv":
        if not args.csv:
            raise SystemExit("--source csv requires --csv FILE")
        df, pip = fetch_csv(args.csv, args.broker_utc_offset), default_pip(symbol)
    else:
        df, pip = fetch_data(inst["yahoo"], cfg), default_pip(inst["yahoo"])
    return df, inst["pip"] or pip, symbol


def signals_path(value: str) -> Path:
    if value == "common":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "MetaQuotes" / "Terminal" / "Common" / "Files" / "btmm_signals.csv"
    return Path(value)


def write_signals(rows: list, path: Path) -> None:
    """Rewrites the EA signal file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pd.DataFrame(rows, columns=SIGNAL_COLUMNS).to_csv(tmp, index=False)
    os.replace(tmp, path)


@contextmanager
def signal_export_lock(path: Path):
    """Prevent multiple exporters from overwriting the same signal file."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"Signal file is already in use or cannot be locked: {path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"Signal file is already in use: {path}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def build_signal_rows(signals, cfg: BTMMConfig, symbol: str, pip: float, last_bar, window: int, plan: Optional[TradePlan] = None) -> list:
    """Fresh signals for the BTMM_Bridge EA (MT4/MT5)."""
    rows = []
    for sig in signals:
        if sig.signal_time < last_bar - pd.Timedelta(minutes=cfg.tf_minutes * (window - 1)):
            continue
        sl, tp, _ = compute_sl_tp(cfg, sig.direction, sig.price, sig.pattern_data["pattern_stop"], sig.pattern_data["atr"], pip)
        if plan:
            sl, tp = plan.apply_levels(sl, tp)
        expires = sig.signal_time + pd.Timedelta(minutes=cfg.tf_minutes * (window + 1))
        rows.append({"id": f"{symbol}-{cfg.timeframe}-{int(sig.signal_time.timestamp())}", "symbol": symbol, "direction": sig.direction.value, "entry": round(sig.price, 5), "sl": round(sl, 5), "tp": round(tp, 5), "lots": cfg.lot_size, "expires_utc": int(expires.timestamp()), "comment": sig.signal_type.replace(",", " ")})
    return rows


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for span in (5, 13, 50, 200):
        df[f"ema{span}"] = df["close"].ewm(span=span, adjust=False).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)
    df["tdi_signal"] = df["rsi"].ewm(span=13, adjust=False).mean()
    df["tdi_base"] = df["rsi"].rolling(34).mean()
    std = df["rsi"].rolling(20).std()
    df["tdi_upper"] = df["tdi_base"] + 2 * std
    df["tdi_lower"] = df["tdi_base"] - 2 * std
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(), (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["date"] = df["datetime"].dt.date
    prev = df.groupby("date").agg(ph=("high", "max"), pl=("low", "min"), pc=("close", "last")).shift(1)
    df = df.merge(prev, left_on="date", right_index=True, how="left")
    pivot = (df["ph"] + df["pl"] + df["pc"]) / 3
    df["pivot"] = pivot
    df["r1"], df["s1"] = 2 * pivot - df["pl"], 2 * pivot - df["ph"]
    df["r2"], df["s2"] = pivot + (df["ph"] - df["pl"]), pivot - (df["ph"] - df["pl"])
    df["m1"], df["m2"] = (df["s1"] + df["s2"]) / 2, (df["s1"] + pivot) / 2
    df["m3"], df["m4"] = (pivot + df["r1"]) / 2, (df["r1"] + df["r2"]) / 2
    daily_range = df.groupby("date").agg(h=("high", "max"), l=("low", "min"))
    daily_range["adr"] = (daily_range["h"] - daily_range["l"]).rolling(14).mean()
    df = df.merge(daily_range[["adr"]], left_on="date", right_index=True, how="left")
    hour = df["datetime"].dt.hour
    df["session"] = np.select([(hour < 7), (hour >= 7) & (hour < 13), (hour >= 13) & (hour < 21)], ["asian", "london", "us"], default="late_us")
    daily = df.groupby("date").agg(dh=("high", "max"), dl=("low", "min")).shift(1)
    df = df.merge(daily, left_on="date", right_index=True, how="left").rename(columns={"dh": "prev_hod", "dl": "prev_lod"})
    return df


def detect_swings(df: pd.DataFrame, window: int) -> pd.DataFrame:
    df = df.copy()
    width = 2 * window + 1
    df["swing_high"] = df["high"].eq(df["high"].rolling(width, center=True).max())
    df["swing_low"] = df["low"].eq(df["low"].rolling(width, center=True).min())
    return df


def compute_sl_tp(cfg: BTMMConfig, direction: Direction, entry: float, pattern_stop: float, atr: float, pip: float):
    if pip <= 0:
        raise ValueError("pip must be positive")
    if cfg.sl_mode == "fixed":
        sl_dist = cfg.fixed_sl_pips
    elif cfg.sl_mode == "atr":
        sl_dist = cfg.atr_mult_sl * atr / pip
    else:
        sl_dist = ((entry - pattern_stop) if direction == Direction.LONG else (pattern_stop - entry)) / pip + cfg.sl_buffer_pips
    sl_dist = max(cfg.min_sl_pips, min(cfg.max_sl_pips, sl_dist))
    tp_dist = cfg.fixed_tp_pips if cfg.tp_mode in ("fixed", "pattern") else sl_dist * cfg.rr_ratio
    tp_dist = min(cfg.max_tp_pips, max(sl_dist, tp_dist))
    if direction == Direction.LONG:
        return entry - sl_dist * pip, entry + tp_dist * pip, sl_dist
    return entry + sl_dist * pip, entry - tp_dist * pip, sl_dist


def find_mw_pattern(df: pd.DataFrame, cfg: BTMMConfig, pip: float) -> list:
    patterns = []
    highs, lows = df[df["swing_high"]].reset_index(), df[df["swing_low"]].reset_index()

    def gap_ok(first, second):
        gap = (second - first).total_seconds() / 60
        return cfg.effective_gap_min <= gap <= cfg.effective_gap_max, gap

    for i in range(len(lows) - 1):
        a, b = lows.iloc[i], lows.iloc[i + 1]
        ok, gap = gap_ok(a["datetime"], b["datetime"])
        if not ok or b["low"] <= a["low"]:
            continue
        middle = df[(df["datetime"] > a["datetime"]) & (df["datetime"] < b["datetime"])]
        if middle.empty or middle["high"].max() <= max(a["high"], b["high"]):
            continue
        patterns.append({"type": "W", "direction": Direction.LONG, "leg1_time": a["datetime"], "leg2_time": b["datetime"], "leg1_price": a["low"], "leg2_price": b["low"], "entry_time": b["datetime"], "entry_price": b["close"], "pattern_stop": b["low"], "gap_min": gap})
    for i in range(len(highs) - 1):
        a, b = highs.iloc[i], highs.iloc[i + 1]
        ok, gap = gap_ok(a["datetime"], b["datetime"])
        if not ok or b["high"] >= a["high"]:
            continue
        middle = df[(df["datetime"] > a["datetime"]) & (df["datetime"] < b["datetime"])]
        if middle.empty or middle["low"].min() >= min(a["low"], b["low"]):
            continue
        patterns.append({"type": "M", "direction": Direction.SHORT, "leg1_time": a["datetime"], "leg2_time": b["datetime"], "leg1_price": a["high"], "leg2_price": b["high"], "entry_time": b["datetime"], "entry_price": b["close"], "pattern_stop": b["high"], "gap_min": gap})
    return patterns


class BTMMStrategy:
    def __init__(self, pair: str, cfg: BTMMConfig, pip: float = 0.0001):
        self.pair, self.cfg, self.pip = pair, cfg, pip

    def _in_zone(self, price, asian, direction):
        low, high = self.cfg.trading_zone_pips
        if direction == Direction.SHORT:
            return asian["asian_high"] + low * self.pip <= price <= asian["asian_high"] + high * self.pip
        return asian["asian_low"] - high * self.pip <= price <= asian["asian_low"] - low * self.pip

    def _best_time(self, ts):
        if not self.cfg.require_best_time:
            return True
        et_hour = (ts.hour - 4) % 24
        return 1 <= et_hour <= 5 or 8 <= et_hour <= 11

    def generate_signals(self, df: pd.DataFrame) -> list:
        if len(df) < 220:
            return []
        if "swing_high" not in df or "swing_low" not in df:
            df = detect_swings(df, self.cfg.swing_window)
        asian = df[df["session"].eq("asian")].groupby("date").agg(asian_high=("high", "max"), asian_low=("low", "min"))
        asian["asian_range"] = asian["asian_high"] - asian["asian_low"]
        signals = []
        for pattern in find_mw_pattern(df, self.cfg, self.pip):
            rows = df[df["datetime"].eq(pattern["entry_time"])]
            if rows.empty:
                continue
            row = rows.iloc[0]
            day = pattern["entry_time"].date()
            if day not in asian.index:
                continue
            session = asian.loc[day]
            if session["asian_range"] > self.cfg.asian_range_max_pips * self.pip or not self._in_zone(pattern["entry_price"], session, pattern["direction"]):
                continue
            if not self._best_time(pattern["entry_time"]):
                continue
            ema_ok = (pattern["direction"] == Direction.LONG and row["ema5"] > row["ema13"] and row["close"] > row["ema50"]) or (pattern["direction"] == Direction.SHORT and row["ema5"] < row["ema13"] and row["close"] < row["ema50"])
            tdi_ok = (pattern["direction"] == Direction.LONG and row["rsi"] < 50) or (pattern["direction"] == Direction.SHORT and row["rsi"] > 50)
            confidence = 0.5 + (0.2 if ema_ok else 0) + (0.15 if tdi_ok else 0) + (0.15 if session["asian_range"] < 25 * self.pip else 0)
            if confidence < self.cfg.min_confidence:
                continue
            tags = [f"{pattern['type']}Reversal"]
            if abs(row["close"] - row["ema200"]) < 15 * self.pip:
                tags.append("200Bounce")
            if 8 <= (pattern["entry_time"].hour - 4) % 24 <= 11:
                tags.append("NYCReversal")
            _, _, sl_pips = compute_sl_tp(self.cfg, pattern["direction"], pattern["entry_price"], pattern["pattern_stop"], row["atr"], self.pip)
            signals.append(Signal(self.pair, self.cfg.timeframe, pattern["entry_time"], "|".join(tags), pattern["direction"], pattern["entry_price"], min(confidence, 1.0), {**{k: str(v) for k, v in pattern.items()}, "pattern_stop": float(pattern["pattern_stop"]), "atr": float(row["atr"]), "sl_dist_pips": float(sl_pips)}))
        return sorted(signals, key=lambda signal: signal.signal_time)


class BTMMDatabase:
    def __init__(self, path="btmm_trading.db"):
        self.conn = sqlite3.connect(path)
        self.cursor = self.conn.cursor()
        self._schema()

    def _schema(self):
        self.cursor.executescript("""
        CREATE TABLE IF NOT EXISTS price_data(id INTEGER PRIMARY KEY AUTOINCREMENT, pair TEXT, timeframe TEXT, datetime TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, UNIQUE(pair,timeframe,datetime));
        CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT UNIQUE, run_id TEXT, pair TEXT, strategy TEXT, timeframe TEXT, direction TEXT, entry_time TEXT, entry_price REAL, exit_time TEXT, exit_price REAL, stop_loss REAL, take_profit REAL, lot_size REAL, pips REAL, pnl REAL, outcome TEXT, notes TEXT);
        CREATE TABLE IF NOT EXISTS backtest_runs(run_id TEXT PRIMARY KEY, strategy TEXT, pair TEXT, timeframe TEXT, start_date TEXT, end_date TEXT, initial_balance REAL, final_balance REAL, total_trades INT, wins INT, losses INT, win_rate REAL, total_pips REAL, max_drawdown REAL, sharpe_ratio REAL, profit_factor REAL, avg_win REAL, avg_loss REAL, config_json TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT, pair TEXT, timeframe TEXT, signal_time TEXT, signal_type TEXT, direction TEXT, price REAL, confidence REAL, pattern_data TEXT);
        """)
        for table, column, kind in (("backtest_runs", "timeframe", "TEXT"), ("backtest_runs", "config_json", "TEXT"), ("trades", "timeframe", "TEXT")):
            columns = {row[1] for row in self.cursor.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.conn.commit()

    def save_price(self, pair, timeframe, df):
        rows = [(pair, timeframe, row.datetime.isoformat(), row.open, row.high, row.low, row.close, row.volume) for row in df.itertuples()]
        self.cursor.executemany("INSERT OR REPLACE INTO price_data(pair,timeframe,datetime,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def save_trade(self, trade):
        self.cursor.execute("INSERT OR REPLACE INTO trades(trade_id,run_id,pair,strategy,timeframe,direction,entry_time,entry_price,exit_time,exit_price,stop_loss,take_profit,lot_size,pips,pnl,outcome,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (trade.trade_id, trade.run_id, trade.pair, trade.strategy, trade.timeframe, trade.direction.value, trade.entry_time.isoformat(), trade.entry_price, trade.exit_time.isoformat() if trade.exit_time is not None else None, trade.exit_price, trade.stop_loss, trade.take_profit, trade.lot_size, trade.pips, trade.pnl, trade.outcome.value, trade.notes))
        self.conn.commit()

    def save_run(self, metrics):
        metrics = dict(metrics)
        metrics["created_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        keys = list(metrics)
        self.cursor.execute(f"INSERT OR REPLACE INTO backtest_runs({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", [metrics[key] for key in keys])
        self.conn.commit()

    def close(self):
        self.conn.close()


class Backtester:
    def __init__(self, pair: str, cfg: BTMMConfig, pip: float = 0.0001):
        self.pair, self.cfg, self.pip = pair, cfg, pip
        self.balance = cfg.initial_balance
        self.run_id = str(uuid.uuid4())[:8]
        self.trades = []
        self.equity_curve = [cfg.initial_balance]

    def _pip_val(self):
        return self.cfg.pip_value_per_lot * self.cfg.lot_size

    def _simulate(self, signal, df):
        sl, tp, sl_pips = compute_sl_tp(self.cfg, signal.direction, signal.price, signal.pattern_data["pattern_stop"], signal.pattern_data["atr"], self.pip)
        trade = Trade(str(uuid.uuid4())[:8], self.run_id, self.pair, signal.signal_type, signal.direction, signal.signal_time, signal.price, sl, tp, self.cfg.lot_size, f"sl={sl_pips:.1f}p tp={abs(tp-signal.price)/self.pip:.1f}p", timeframe=self.cfg.timeframe)
        be_triggered = False
        trail_high = trail_low = signal.price
        future = df[df["datetime"] > signal.signal_time]
        for bar in future.itertuples(index=False):
            held_hours = (bar.datetime - signal.signal_time).total_seconds() / 3600
            sign = 1 if signal.direction == Direction.LONG else -1
            if held_hours >= self.cfg.effective_scratch_hours and (bar.close-signal.price)/self.pip*sign < 5:
                self._exit(trade, bar.datetime, bar.close, Outcome.BE, "scratch")
                break
            if (not be_triggered and self.cfg.breakeven_at_pips is not None and (bar.close-signal.price)/self.pip*sign >= self.cfg.breakeven_at_pips):
                trade.stop_loss = signal.price
                be_triggered = True
            if self.cfg.trailing_stop_pips is not None:
                if signal.direction == Direction.LONG:
                    trail_high = max(trail_high, bar.high)
                    trade.stop_loss = max(trade.stop_loss, trail_high-self.cfg.trailing_stop_pips*self.pip)
                else:
                    trail_low = min(trail_low, bar.low)
                    trade.stop_loss = min(trade.stop_loss, trail_low+self.cfg.trailing_stop_pips*self.pip)
            if signal.direction == Direction.LONG:
                if bar.low <= trade.stop_loss:
                    outcome = Outcome.BE if trade.stop_loss == trade.entry_price else Outcome.LOSS
                    self._exit(trade, bar.datetime, trade.stop_loss, outcome, "SL")
                    break
                if bar.high >= trade.take_profit:
                    self._exit(trade, bar.datetime, trade.take_profit, Outcome.WIN, "TP")
                    break
            else:
                if bar.high >= trade.stop_loss:
                    outcome = Outcome.BE if trade.stop_loss == trade.entry_price else Outcome.LOSS
                    self._exit(trade, bar.datetime, trade.stop_loss, outcome, "SL")
                    break
                if bar.low <= trade.take_profit:
                    self._exit(trade, bar.datetime, trade.take_profit, Outcome.WIN, "TP")
                    break
        if trade.outcome == Outcome.OPEN and not future.empty:
            last = future.iloc[-1]
            self._exit(trade, last["datetime"], last["close"], Outcome.BE, "end")
        return trade

    def run(self, df, strategy):
        signals = strategy.generate_signals(df)
        print(f"  -> {len(signals)} raw signals")
        accepted = []
        for signal in signals:
            active = sum(t.entry_time <= signal.signal_time and t.exit_time > signal.signal_time for t in accepted)
            if active >= self.cfg.max_open_trades:
                continue
            trade = self._simulate(signal, df)
            if trade.outcome != Outcome.OPEN:
                accepted.append(trade)
                self.trades.append(trade)
                self.balance += trade.pnl
                self.equity_curve.append(self.balance)
        return self.trades

    def _exit(self, trade, ts, price, outcome, note):
        sign = 1 if trade.direction == Direction.LONG else -1
        trade.exit_time, trade.exit_price = ts, price
        trade.pips = (price-trade.entry_price)/self.pip*sign
        trade.pnl = trade.pips*self._pip_val()
        trade.outcome, trade.notes = outcome, trade.notes+f"|{note}"

    def metrics(self, df):
        closed = self.trades
        wins = [t for t in closed if t.outcome == Outcome.WIN]
        losses = [t for t in closed if t.outcome == Outcome.LOSS]
        equity = pd.Series(self.equity_curve)
        drawdown = (equity-equity.cummax())/equity.cummax()
        returns = equity.pct_change().dropna()
        std = returns.std()
        sharpe = returns.mean()/std*np.sqrt(252) if pd.notna(std) and std > 0 else 0
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {"run_id": self.run_id, "strategy": self.cfg.run_label, "pair": self.pair, "timeframe": self.cfg.timeframe, "config_json": self.cfg.to_json(), "start_date": str(df["datetime"].iloc[0].date()), "end_date": str(df["datetime"].iloc[-1].date()), "initial_balance": self.cfg.initial_balance, "final_balance": round(self.balance, 2), "total_trades": len(closed), "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins)/len(closed)*100, 2) if closed else 0, "total_pips": round(sum(t.pips for t in closed), 1), "max_drawdown": round(drawdown.min()*100, 2) if len(drawdown) else 0, "sharpe_ratio": round(sharpe, 2), "profit_factor": round(gross_win/gross_loss, 2) if gross_loss else (round(gross_win, 2) if gross_win else 0), "avg_win": round(np.mean([t.pnl for t in wins]), 2) if wins else 0, "avg_loss": round(np.mean([t.pnl for t in losses]), 2) if losses else 0}


def build_cfg(args):
    return BTMMConfig(timeframe=parse_timeframes(args.timeframe)[0], sl_mode=args.sl_mode, tp_mode=args.tp_mode, fixed_sl_pips=args.sl, fixed_tp_pips=args.tp, sl_buffer_pips=args.sl_buffer, rr_ratio=args.rr, atr_mult_sl=args.atr_mult, breakeven_at_pips=None if args.no_be else args.be, trailing_stop_pips=args.trail, lot_size=args.lot, initial_balance=args.balance, run_label=f"{args.label}-{args.timeframe}-{args.sl_mode}{args.sl}-{args.tp_mode}{args.tp}")


def plan_stop_reason(args, plan: TradePlan, started_at: float) -> Optional[str]:
    """Why the session must end now, or None. Limits are checked once per refresh."""
    if plan.expired(started_at):
        return f"the {plan.days}-day schedule has ended"
    if not plan.formula_enabled:
        return None
    if args.source != "mt5":
        print("  ! ATL/ATP formula needs --source mt5 to read profit/loss; it is not being enforced", flush=True)
        return None
    try:
        pnl = mt5_session_pnl(started_at, args.broker_utc_offset, terminal_path=args.mt5_path)
    except Exception as exc:
        print(f"  ! could not read P&L: {exc}", flush=True)
        return None
    print(f"  session P&L {pnl:+.2f}; amount to trade now {plan.running_amount(pnl):.2f}", flush=True)
    return plan.risk_stop_reason(pnl)


def run_watch(args, stop_event=None):
    plan = getattr(args, "plan", None)
    started_at = time.time()
    with signal_export_lock(signals_path(args.export_signals)) if args.export_signals else nullcontext():
        try:
            while True:
                try:
                    reason = plan_stop_reason(args, plan, started_at) if plan else None
                    if reason:
                        print(f"Stopping: {reason}.", flush=True)
                        if args.export_signals:
                            write_signals([], signals_path(args.export_signals))
                        if plan.formula_enabled and args.source == "mt5":
                            print(f"Closed {mt5_close_positions(terminal_path=args.mt5_path)} open position(s).", flush=True)
                        if stop_event:
                            stop_event.set()
                        break
                    if plan and not plan.in_trading_hours():
                        print("  outside trading hours; no signals exported", flush=True)
                        if args.export_signals:
                            write_signals([], signals_path(args.export_signals))
                    else:
                        run(args)
                except Exception as exc:
                    if not args.watch:
                        raise
                    print(f"  ! run failed: {exc}", flush=True)
                if not args.watch:
                    break
                if stop_event:
                    if stop_event.wait(args.watch * 60):
                        break
                else:
                    time.sleep(args.watch * 60)
        finally:
            if stop_event and args.export_signals:
                write_signals([], signals_path(args.export_signals))
                print("Signal file cleared. Existing positions remain open.", flush=True)


def control_console(args):
    worker = None
    stop_event = None
    print("Commands: start EURUSD,GBPUSD,USDJPY | stop | status | symbols | quit")
    print("Stop clears pending signals but does not close positions or disable the EA.")

    def stop():
        nonlocal worker, stop_event
        if worker and worker.is_alive():
            print("Stopping after the current data refresh finishes...")
            stop_event.set()
            worker.join()
        else:
            print("Exporter is not running.")
        worker = None
        stop_event = None

    def export(selected, event):
        try:
            run_watch(selected, event)
        except Exception as exc:
            print(f"Exporter stopped: {exc}", flush=True)

    try:
        while True:
            try:
                line = input("Mr Fin> ").strip()
            except EOFError:
                print()
                break
            command, _, value = line.partition(" ")
            command = command.lower()
            if command == "start":
                if worker and worker.is_alive():
                    print("Exporter is already running. Stop it before changing pairs.")
                    continue
                names = [name.strip() for name in value.replace(",", " ").split()]
                if not 1 <= len(names) <= 3 or len({name.upper() for name in names}) != len(names):
                    print("Enter one to three distinct symbols, e.g. start EURUSD,GBPUSD,USDJPY")
                    continue
                if any(name.lower() in GROUPS for name in names):
                    print("Select individual symbols, not instrument groups.")
                    continue
                selected = copy.copy(args)
                selected.symbols = ",".join(names)
                selected.export_signals = args.export_signals or "common"
                selected.watch = args.watch if args.watch is not None else 5
                stop_event = threading.Event()
                worker = threading.Thread(target=export, args=(selected, stop_event))
                print(f"Starting {selected.symbols}; refresh every {selected.watch:g} minutes.")
                worker.start()
            elif command == "stop" and not value.strip():
                stop()
            elif command == "status" and not value.strip():
                print("Exporter is running." if worker and worker.is_alive() else "Exporter is stopped.")
            elif command == "symbols" and not value.strip():
                print_instruments()
            elif command in ("quit", "exit") and not value.strip():
                break
            elif line:
                print("Unknown command. Use start, stop, status, symbols, or quit.")
    except KeyboardInterrupt:
        print()
    finally:
        if worker and worker.is_alive():
            stop()


def main():
    parser = argparse.ArgumentParser("BTMM backtester")
    parser.add_argument("--pair", default="EURUSD=X")
    parser.add_argument("--label", default="EURUSD")
    parser.add_argument("--timeframe", default="60m", help=f"one or more comma-separated timeframes: {','.join(TIMEFRAME_PRESETS)}")
    parser.add_argument("--sl-mode", default="pattern", choices=["fixed", "pattern", "atr"])
    parser.add_argument("--tp-mode", default="fixed", choices=["fixed", "rr", "pattern"])
    parser.add_argument("--sl", type=float, default=20.0)
    parser.add_argument("--tp", type=float, default=50.0)
    parser.add_argument("--sl-buffer", type=float, default=10.0)
    parser.add_argument("--rr", type=float, default=2.5)
    parser.add_argument("--atr-mult", type=float, default=1.5)
    parser.add_argument("--be", type=float, default=25.0)
    parser.add_argument("--no-be", action="store_true")
    parser.add_argument("--trail", type=float)
    parser.add_argument("--lot", type=float, default=0.1)
    parser.add_argument("--balance", type=float, default=10000)
    parser.add_argument("--grid", action="store_true")
    parser.add_argument("--source", default="yahoo", choices=["yahoo", "mt5", "csv"], help="yahoo (internet), mt5 (local MT5 terminal), csv (MT4/MT5 export)")
    parser.add_argument("--csv", help="MT4 History Center / MT5 exported CSV file")
    parser.add_argument("--mt-symbol", help="broker symbol name, e.g. EURUSD.m (default: --pair without =X)")
    parser.add_argument("--mt5-path", help="path to terminal64.exe if MT5 is not auto-detected")
    parser.add_argument("--bars", type=int, default=20000, help="bars to pull from MT5")
    parser.add_argument("--broker-utc-offset", type=float, default=0.0, help="broker server time minus UTC in hours (e.g. 2 or 3)")
    parser.add_argument("--export-signals", metavar="PATH", help="write fresh signals for the EA; use 'common' for the MT Common\\Files folder")
    parser.add_argument("--export-window", type=int, default=2, help="export signals from the last N bars")
    parser.add_argument("--watch", type=float, metavar="MIN", help="repeat every MIN minutes (live signal feed)")
    parser.add_argument("--db", default=str(BASE_DIR / "btmm_trading.db"))
    parser.add_argument("--symbols", help="comma list of instruments and/or groups (forex, commodities, indices), e.g. EURUSD,XAUUSD,US30")
    parser.add_argument("--list-symbols", action="store_true", help="show the built-in instrument list and exit")
    parser.add_argument("--mt-suffix", default="", help="broker symbol suffix applied to --symbols, e.g. .m")
    parser.add_argument("--mt-map", help="broker name overrides, e.g. US30=DJ30.cash,XAUUSD=GOLD")
    parser.add_argument("--control", action="store_true", help="interactive start/stop console for up to three symbols")
    parser.add_argument("--ui", action="store_true", help="open the browser page to choose pairs, timeframes, TP/SL, schedule and limits")
    parser.add_argument("--port", type=int, default=8765, help="port for --ui (served on 127.0.0.1 only)")
    args = parser.parse_args()
    if args.list_symbols:
        print_instruments()
        return
    if args.grid and (args.export_signals or args.watch):
        parser.error("--grid cannot be combined with --export-signals or --watch")
    if args.watch is not None and (not math.isfinite(args.watch) or args.watch <= 0):
        parser.error("--watch must be a positive number of minutes")
    if args.ui:
        if args.grid or args.symbols or args.csv or args.control or args.source == "csv":
            parser.error("--ui cannot be combined with --grid, --symbols, --csv or --control; choose them on the page")
        import ui
        ui.serve(args)
    elif args.control:
        if args.grid or args.symbols or args.csv:
            parser.error("--control does not support --grid, --symbols, or --csv; select symbols with start")
        if args.source == "csv":
            parser.error("--control requires yahoo or mt5 as its data source")
        parse_timeframes(args.timeframe)
        control_console(args)
    else:
        run_watch(args)


def run(args):
    database = BTMMDatabase(args.db)
    combos = [(tf, sl, tp) for tf in ("15m", "60m", "4h") for sl in (15, 20, 30) for tp in (30, 50, 80)] if args.grid else [(tf, args.sl, args.tp) for tf in parse_timeframes(args.timeframe)]
    results = []
    plan = getattr(args, "plan", None)
    instruments = resolve_instruments(args)
    signal_rows, loaded = [], False
    try:
        for inst, (timeframe, sl_value, tp_value) in itertools.product(instruments, combos):
            label = inst["label"]
            cfg = BTMMConfig(timeframe=timeframe, sl_mode="fixed" if args.grid else args.sl_mode, tp_mode="fixed" if args.grid else args.tp_mode, fixed_sl_pips=sl_value, fixed_tp_pips=tp_value, sl_buffer_pips=args.sl_buffer, rr_ratio=args.rr, atr_mult_sl=args.atr_mult, breakeven_at_pips=None if args.grid or args.no_be else args.be, trailing_stop_pips=None if args.grid else args.trail, lot_size=args.lot, initial_balance=args.balance, run_label=f"{label}-{timeframe}-SL{sl_value}-TP{tp_value}" if args.grid else f"{label}-{timeframe}-{args.sl_mode}{sl_value}-{args.tp_mode}{tp_value}")
            if plan:
                cfg = dataclasses.replace(cfg, **plan.cfg_overrides())
            print(f"\n=== {label} @ {timeframe} ===")
            print(f"  SL mode={cfg.sl_mode} TP mode={cfg.tp_mode} scratch={cfg.effective_scratch_hours:.1f}h gap window={cfg.effective_gap_min:.0f}-{cfg.effective_gap_max:.0f}min")
            try:
                df, pip, symbol = load_data(inst, args, cfg)
            except Exception as exc:
                if len(instruments) == 1:
                    raise
                print(f"  ! {label} skipped: {exc}")
                continue
            pip = plan.pip_size if plan and plan.pip_size else pip
            df = detect_swings(add_indicators(df), cfg.swing_window)
            database.save_price(label, timeframe, df)
            backtester = Backtester(label, cfg, pip)
            strategy = BTMMStrategy(label, cfg, pip)
            backtester.run(df, strategy)
            if args.export_signals:
                fresh = build_signal_rows(strategy.generate_signals(df), cfg, symbol, pip, df["datetime"].iloc[-1], args.export_window, plan)
                signal_rows += fresh
                loaded = True
                print(f"  -> {len(fresh)} fresh signal(s) for {symbol}")
            for trade in backtester.trades:
                database.save_trade(trade)
            metrics = backtester.metrics(df)
            database.save_run(metrics)
            results.append(metrics)
            if args.grid:
                print(f"  {metrics['strategy']:30s} trades={metrics['total_trades']:>3} win%={metrics['win_rate']:>5} pips={metrics['total_pips']:>7}")
            else:
                for key in ("total_trades", "wins", "losses", "win_rate", "total_pips", "final_balance", "profit_factor", "max_drawdown", "sharpe_ratio"):
                    print(f"  {key:15s}: {metrics[key]}")
        if args.export_signals and loaded:
            target = signals_path(args.export_signals)
            write_signals(signal_rows, target)
            print(f"  -> wrote {len(signal_rows)} signal(s) to {target}")
        if args.grid:
            ranked = pd.DataFrame(results)
            ranked.to_csv(BASE_DIR / "btmm_grid_results.csv", index=False)
            print("\nTop 10 by total pips:")
            print(ranked.sort_values("total_pips", ascending=False).head(10)[["strategy", "timeframe", "total_trades", "win_rate", "total_pips", "profit_factor", "max_drawdown"]].to_string(index=False))
    finally:
        database.close()


if __name__ == "__main__":
    main()
