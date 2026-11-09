"""Trade plan: what to trade, how to set TP/SL, when to trade and when to stop."""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

MAX_SELECTION = 3
MAX_DAYS = 3
RATIO_PRESETS = {"1:1": 1.0, "1:2": 2.0, "1:3": 3.0}
TPSL_MODES = ("auto", "pips", "ratio")


def _num(value, name, allow_none=True, minimum=None):
    if value in (None, ""):
        if allow_none:
            return None
        raise ValueError(f"{name} is required")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(number) or (minimum is not None and number <= minimum):
        raise ValueError(f"{name} must be greater than {minimum}" if minimum is not None else f"{name} must be finite")
    return number


def _minutes(value, name) -> int:
    try:
        hours, minutes = str(value).split(":")
        total = int(hours) * 60 + int(minutes)
    except ValueError:
        raise ValueError(f"{name} must be HH:MM") from None
    if not 0 <= total < 1440:
        raise ValueError(f"{name} must be HH:MM")
    return total


@dataclass
class TradePlan:
    symbols: list = field(default_factory=list)
    timeframes: list = field(default_factory=lambda: ["60m"])
    pip_size: Optional[float] = None
    sl_enabled: bool = True
    tp_enabled: bool = True
    tpsl_mode: str = "auto"
    sl_pips: Optional[float] = None
    tp_pips: Optional[float] = None
    ratio: float = 2.0
    days: int = 1
    hours_enabled: bool = False
    hours_start: str = "07:00"
    hours_end: str = "16:00"
    utc_offset: float = 0.0
    initial_amount: Optional[float] = None
    att: Optional[float] = None
    atl: Optional[float] = None
    atp: Optional[float] = None
    formula_enabled: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "TradePlan":
        known = {f.name for f in fields(cls)}
        plan = cls(**{k: v for k, v in data.items() if k in known})
        plan.validate()
        return plan

    def validate(self, known_timeframes=None) -> None:
        self.symbols = list(dict.fromkeys(str(s).strip() for s in self.symbols if str(s).strip()))
        self.timeframes = list(dict.fromkeys(str(t).strip() for t in self.timeframes if str(t).strip()))
        if not 1 <= len(self.symbols) <= MAX_SELECTION:
            raise ValueError(f"pick 1 to {MAX_SELECTION} instruments")
        if not 1 <= len(self.timeframes) <= MAX_SELECTION:
            raise ValueError(f"pick 1 to {MAX_SELECTION} timeframes")
        if known_timeframes and any(t not in known_timeframes for t in self.timeframes):
            raise ValueError("unknown timeframe selected")
        self.pip_size = _num(self.pip_size, "pip size", minimum=0)
        if self.tpsl_mode not in TPSL_MODES:
            raise ValueError(f"TP/SL mode must be one of {TPSL_MODES}")
        self.sl_enabled, self.tp_enabled = bool(self.sl_enabled), bool(self.tp_enabled)
        self.sl_pips = _num(self.sl_pips, "SL pips", minimum=0)
        self.tp_pips = _num(self.tp_pips, "TP pips", minimum=0)
        self.ratio = _num(self.ratio, "ratio", allow_none=False, minimum=0)
        if self.tpsl_mode == "pips":
            if self.sl_enabled and self.sl_pips is None:
                raise ValueError("enter SL pips or turn SL off")
            if self.tp_enabled and self.tp_pips is None:
                raise ValueError("enter TP pips or turn TP off")
        if self.tpsl_mode == "ratio" and not (self.sl_enabled and self.tp_enabled):
            raise ValueError("a TP/SL ratio needs both SL and TP turned on")
        try:
            self.days = int(self.days)
        except (TypeError, ValueError):
            raise ValueError("days must be 1 to 3") from None
        if not 1 <= self.days <= MAX_DAYS:
            raise ValueError(f"days must be 1 to {MAX_DAYS}")
        self.hours_enabled = bool(self.hours_enabled)
        if self.hours_enabled:
            if _minutes(self.hours_start, "start time") == _minutes(self.hours_end, "end time"):
                raise ValueError("trading hours start and end must differ")
        self.utc_offset = _num(self.utc_offset, "UTC offset", allow_none=False)
        if abs(self.utc_offset) > 14:
            raise ValueError("UTC offset must be between -14 and 14")
        for name in ("initial_amount", "att", "atl", "atp"):
            setattr(self, name, _num(getattr(self, name), name.upper(), minimum=0 if name != "atl" else -1))
        self.formula_enabled = bool(self.formula_enabled)
        if self.formula_enabled:
            if self.att is None:
                raise ValueError("the formula needs an amount to trade (ATT)")
            if self.atl is None and self.atp is None:
                raise ValueError("the formula needs ATL and/or ATP")
            if self.initial_amount is not None and self.att > self.initial_amount:
                raise ValueError("ATT cannot exceed the initial amount")
            if self.atl is not None and self.atl >= self.att:
                raise ValueError("ATL must be below ATT")
            if self.atp is not None and self.atp <= self.att:
                raise ValueError("ATP must be above ATT")

    def cfg_overrides(self) -> dict:
        """BTMMConfig overrides implementing the TP/SL mode."""
        if self.tpsl_mode == "pips":
            return {"sl_mode": "fixed", "tp_mode": "fixed", "fixed_sl_pips": self.sl_pips or 20.0, "fixed_tp_pips": self.tp_pips or 50.0}
        if self.tpsl_mode == "ratio":
            out = {"tp_mode": "rr", "rr_ratio": self.ratio}
            if self.sl_pips:
                out.update(sl_mode="fixed", fixed_sl_pips=self.sl_pips)
            return out
        return {}

    def apply_levels(self, sl: float, tp: float) -> tuple:
        """SL/TP written to the signal file; 0 means the level is switched off."""
        return (sl if self.sl_enabled else 0.0), (tp if self.tp_enabled else 0.0)

    def in_trading_hours(self, now: Optional[float] = None) -> bool:
        if not self.hours_enabled:
            return True
        now = time.time() if now is None else now
        local = time.gmtime(now + self.utc_offset * 3600)
        current = local.tm_hour * 60 + local.tm_min
        start, end = _minutes(self.hours_start, "start"), _minutes(self.hours_end, "end")
        return start <= current < end if start < end else current >= start or current < end

    def expired(self, started_at: float, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) >= started_at + self.days * 86400

    def running_amount(self, pnl: float) -> Optional[float]:
        return None if self.att is None else self.att + pnl

    def risk_stop_reason(self, pnl: float) -> Optional[str]:
        """The formula: stop when ATT (adjusted by P&L) is at/below ATL or at/above ATP."""
        if not self.formula_enabled:
            return None
        amount = self.running_amount(pnl)
        if self.atl is not None and amount <= self.atl:
            return f"amount {amount:.2f} reached ATL {self.atl:g}"
        if self.atp is not None and amount >= self.atp:
            return f"amount {amount:.2f} reached ATP {self.atp:g}"
        return None


def load_plan(path: Path) -> TradePlan:
    try:
        return TradePlan.from_dict(json.loads(Path(path).read_text()))
    except (OSError, ValueError, TypeError):
        return TradePlan()


def save_plan(plan: TradePlan, path: Path) -> None:
    Path(path).write_text(json.dumps(asdict(plan), indent=2))
