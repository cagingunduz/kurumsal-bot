"""
Risk yönetimi: pozisyon boyutlandırma, stop/target hesaplama ve günlük zarar limiti.

exitpump'ın makalelerinde anlattığı iki stop yöntemi implemente edildi:
  - "avwap": AVWAP'ın hemen üstüne/altına stop koy (makalede geçen yöntem)
  - "atr"  : klasik ATR-tabanlı volatilite stopu (AVWAP mevcut değilse fallback)

ÖNEMLİ: Bu modül sadece HESAPLAMA yapar, gerçek para hareket ettirmez.
Gerçek emir gönderme execution/ klasöründeki broker sınıflarında olur ve
varsayılan olarak sadece paper (simüle) mod aktiftir.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    atr.name = f"atr_{period}"
    return atr


@dataclass
class StopTarget:
    entry: float
    stop: float
    target: float
    risk_per_unit: float  # |entry - stop|
    reward_per_unit: float  # |target - entry|


def calc_stop_target(
    direction: str, entry_price: float, reward_risk_ratio: float,
    method: str = "avwap", avwap_price: Optional[float] = None,
    atr_value: Optional[float] = None, atr_multiplier: float = 1.5,
    buffer_pct: float = 0.05,
) -> StopTarget:
    if direction not in ("long", "short"):
        raise ValueError("direction must be 'long' or 'short'")

    if method == "avwap":
        if avwap_price is None:
            raise ValueError("method='avwap' için avwap_price gerekli")
        buffer = entry_price * buffer_pct / 100.0
        stop = (avwap_price - buffer) if direction == "long" else (avwap_price + buffer)
    elif method == "atr":
        if atr_value is None:
            raise ValueError("method='atr' için atr_value gerekli")
        stop = entry_price - atr_value * atr_multiplier if direction == "long" else entry_price + atr_value * atr_multiplier
    else:
        raise ValueError("method 'avwap' ya da 'atr' olmalı")

    risk_per_unit = abs(entry_price - stop)
    if risk_per_unit <= 0:
        raise ValueError("Stop mesafesi sıfır/negatif olamaz -- avwap/atr girdilerini kontrol et")

    reward_per_unit = risk_per_unit * reward_risk_ratio
    target = entry_price + reward_per_unit if direction == "long" else entry_price - reward_per_unit

    return StopTarget(entry=entry_price, stop=stop, target=target,
                       risk_per_unit=risk_per_unit, reward_per_unit=reward_per_unit)


def position_size(equity_usdt: float, risk_per_trade_pct: float, stop_target: StopTarget) -> dict:
    """Sermayenin risk_per_trade_pct kadarını, stop mesafesine göre pozisyon büyüklüğüne çevirir."""
    risk_amount_usdt = equity_usdt * (risk_per_trade_pct / 100.0)
    qty = risk_amount_usdt / stop_target.risk_per_unit
    notional_usdt = qty * stop_target.entry
    return {
        "risk_amount_usdt": risk_amount_usdt,
        "qty": qty,
        "notional_usdt": notional_usdt,
        "leverage_implied": notional_usdt / equity_usdt if equity_usdt > 0 else float("nan"),
    }


class DailyLossTracker:
    """Günlük (UTC) gerçekleşmiş PnL'i izler; max_daily_loss_pct aşılırsa o gün yeni işlem engellenir."""

    def __init__(self, starting_equity: float, max_daily_loss_pct: float):
        self.starting_equity = starting_equity
        self.max_daily_loss_pct = max_daily_loss_pct
        self._day: Optional[str] = None
        self._day_start_equity = starting_equity
        self._realized_pnl_today = 0.0

    def _roll_day_if_needed(self, now: datetime, current_equity: float) -> None:
        day_key = now.strftime("%Y-%m-%d")
        if self._day != day_key:
            self._day = day_key
            self._day_start_equity = current_equity
            self._realized_pnl_today = 0.0

    def register_closed_trade(self, now: datetime, current_equity: float, realized_pnl: float) -> None:
        self._roll_day_if_needed(now, current_equity)
        self._realized_pnl_today += realized_pnl

    def can_trade(self, now: datetime, current_equity: float) -> tuple[bool, Optional[str]]:
        self._roll_day_if_needed(now, current_equity)
        if self._day_start_equity <= 0:
            return True, None
        loss_pct = -self._realized_pnl_today / self._day_start_equity * 100.0
        if loss_pct >= self.max_daily_loss_pct:
            return False, (
                f"Günlük zarar limiti aşıldı: {loss_pct:.2f}% >= {self.max_daily_loss_pct}% "
                f"(gün başı equity {self._day_start_equity:.2f})"
            )
        return True, None
