"""
Open Interest göstergeleri -- exitpump'ın "Open Interest Guide" makalesi.

Fiyat + OI + CVD "cheat sheet":
  Fiyat UP   + OI UP   -> yeni agresif long girişi (trend devamı)
  Fiyat DOWN + OI UP   -> yeni agresif short girişi (bearish momentum)
  Fiyat DOWN + OI DOWN -> long'lar kapanıyor / likidasyon
  Fiyat UP   + OI DOWN -> short squeeze (short'lar kapanıyor)

OI RSI/Z-Score: pozisyonlanmanın aşırı (overcrowded) ya da çok düşük (deleveraged)
olup olmadığını ölçer. 70+ overcrowded, 30- düşük pozisyon.
"""
from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

PriceOIState = Literal[
    "new_longs_trend_continuation",
    "new_shorts_bearish_momentum",
    "long_liquidation",
    "short_squeeze",
    "flat",
]


def oi_rsi(oi_series: pd.Series, period: int = 14) -> pd.Series:
    """Standart RSI formülünü OI serisine uygular (Wilder smoothing)."""
    delta = oi_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi.name = f"oi_rsi_{period}"
    return rsi


def oi_zscore(oi_series: pd.Series, window: int = 100) -> pd.Series:
    mean = oi_series.rolling(window).mean()
    std = oi_series.rolling(window).std()
    z = (oi_series - mean) / std.replace(0, np.nan)
    z.name = f"oi_zscore_{window}"
    return z


def classify_price_oi(
    price_change: float, oi_change: float, flat_threshold: float = 1e-9
) -> PriceOIState:
    if abs(price_change) < flat_threshold and abs(oi_change) < flat_threshold:
        return "flat"
    if price_change > 0 and oi_change > 0:
        return "new_longs_trend_continuation"
    if price_change < 0 and oi_change > 0:
        return "new_shorts_bearish_momentum"
    if price_change < 0 and oi_change < 0:
        return "long_liquidation"
    if price_change > 0 and oi_change < 0:
        return "short_squeeze"
    return "flat"


def price_oi_cvd_table(price: pd.Series, oi: pd.Series, cvd: pd.Series, window: int = 5) -> pd.DataFrame:
    """price/oi/cvd aynı index'e resample edilmiş olmalı. window bar'lık değişime bakar."""
    price_chg = price.diff(window)
    oi_chg = oi.diff(window)
    cvd_chg = cvd.diff(window)

    states = [
        classify_price_oi(p, o) for p, o in zip(price_chg.fillna(0), oi_chg.fillna(0))
    ]
    out = pd.DataFrame(
        {"price_chg": price_chg, "oi_chg": oi_chg, "cvd_chg": cvd_chg, "state": states},
        index=price.index,
    )
    # Squeeze teyidi: fiyat yukarı + OI aşağı + CVD yatay/aşağı -> güçlü short squeeze sinyali
    out["confirmed_short_squeeze"] = (
        (out["state"] == "short_squeeze") & (out["cvd_chg"] <= out["cvd_chg"].rolling(window).std().fillna(0))
    )
    # Long unwind teyidi: fiyat aşağı + OI yukarı + CVD aşağı -> long'lar sıkışmaya devam ediyor
    out["confirmed_long_unwind_risk"] = (
        (out["state"] == "new_shorts_bearish_momentum") & (out["cvd_chg"] < 0)
    )
    return out
