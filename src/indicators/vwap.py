"""
VWAP göstergeleri -- exitpump'ın "The Ultimate VWAP Guide" makalesindeki 5 türü:
  1. Session/Standard VWAP (günlük, her gün sıfırlanır)
  2. Anchored VWAP (AVWAP) - belirli bir mumdan (swing high/low, event) başlar
  3. Rolling VWAP (rVWAP) - sıfırlanmaz, N günlük hareketli pencere (30D/90D/365D)
  4. Multi-Period VWAP - haftalık/aylık/çeyreklik/yıllık, periyot başında sıfırlanır;
     bir önceki periyodun kapanışı statik seviye (pmVWAP) olarak kalır
  5. VWAP + Standart Sapma Bantları (VAH/VAL benzeri, ±1 veya ±2 std)

Girdi: OHLCV DataFrame'i (index=UTC datetime), 'high','low','close','volume' kolonları.
Tipik fiyat (typical price = (H+L+C)/3) hacim ağırlıklandırması için kullanılır.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd


def _typical_price(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"] + df["close"]) / 3.0


def session_vwap(df: pd.DataFrame, reset_hour_utc: int = 0) -> pd.Series:
    """Her gün reset_hour_utc'de sıfırlanan intraday VWAP."""
    tp = _typical_price(df)
    pv = tp * df["volume"]
    # Gün gruplarını reset saatine göre kaydır
    shifted_index = df.index - pd.Timedelta(hours=reset_hour_utc)
    session_key = shifted_index.floor("D")
    cum_pv = pv.groupby(session_key).cumsum()
    cum_vol = df["volume"].groupby(session_key).cumsum()
    vwap = cum_pv / cum_vol.replace(0, np.nan)
    vwap.name = "session_vwap"
    return vwap


def anchored_vwap(df: pd.DataFrame, anchor_time: pd.Timestamp) -> pd.Series:
    """anchor_time'dan itibaren (dahil) kümülatif VWAP. Öncesi NaN döner."""
    tp = _typical_price(df)
    pv = tp * df["volume"]
    mask = df.index >= anchor_time
    out = pd.Series(index=df.index, dtype=float)
    out[mask] = (pv[mask].cumsum() / df.loc[mask, "volume"].cumsum().replace(0, np.nan))
    out.name = f"avwap_{anchor_time.isoformat()}"
    return out


def rolling_vwap(df: pd.DataFrame, days: int) -> pd.Series:
    """Sıfırlanmayan, N günlük hareketli pencere VWAP'i (30D/90D/365D rVWAP).

    Not: Bar aralığından bağımsız çalışması için zaman-tabanlı rolling window
    kullanılır (ör. 1m mumlarda 30 gün = 43200 bar otomatik hesaplanır).
    """
    tp = _typical_price(df)
    pv = (tp * df["volume"]).rolling(f"{days}D", min_periods=1).sum()
    vol = df["volume"].rolling(f"{days}D", min_periods=1).sum()
    vwap = pv / vol.replace(0, np.nan)
    vwap.name = f"rvwap_{days}d"
    return vwap


_PERIOD_FREQ = {"W": "W-MON", "M": "MS", "Q": "QS", "Y": "YS"}


def multi_period_vwap(df: pd.DataFrame, period: str) -> tuple[pd.Series, pd.Series]:
    """period: 'W' | 'M' | 'Q' | 'Y'.

    Döndürür: (developing_vwap, previous_period_static_vwap)
      - developing_vwap: içinde bulunulan periyodun canlı VWAP'i (dynamic S/R)
      - previous_period_static_vwap: bir önceki periyodun kapanış VWAP değeri,
        periyot boyunca sabit kalır (pmVWAP -- statik S/R seviyesi)
    """
    if period not in _PERIOD_FREQ:
        raise ValueError(f"period must be one of {list(_PERIOD_FREQ)}")
    tp = _typical_price(df)
    pv = tp * df["volume"]
    tz = df.index.tz
    with warnings.catch_warnings():
        # tz-aware index -> Period dönüşümü tz bilgisini yapısal olarak taşıyamaz (pandas
        # bunu uyarır); hemen altta tz'yi geri ekliyoruz, bilgi kaybı yok.
        warnings.filterwarnings("ignore", message="Converting to PeriodArray/Index")
        period_key = df.index.to_period(period[0]).to_timestamp()
    if tz is not None:
        period_key = period_key.tz_localize(tz)

    cum_pv = pv.groupby(period_key).cumsum()
    cum_vol = df["volume"].groupby(period_key).cumsum()
    developing = cum_pv / cum_vol.replace(0, np.nan)
    developing.name = f"developing_{period.lower()}vwap"

    # Her periyodun SON (final) VWAP değeri -> bir sonraki periyoda "previous" olarak taşınır
    final_per_period = developing.groupby(period_key).last()
    prev_static = pd.Series(index=df.index, dtype=float)
    period_keys_arr = period_key
    # Her satır için bir önceki periyodun final değerini eşle
    shifted = final_per_period.shift(1)
    map_series = pd.Series(period_keys_arr, index=df.index).map(shifted)
    prev_static[:] = map_series.values
    prev_static.name = f"prev_{period.lower()}vwap"
    return developing, prev_static


def vwap_with_bands(
    df: pd.DataFrame, vwap: pd.Series, window: str | int, std_devs: tuple[float, ...] = (1.0,)
) -> pd.DataFrame:
    """Hacim-ağırlıklı standart sapma bantları (VAH/VAL benzeri) verilen bir VWAP serisi etrafında.

    window: rolling_vwap ile aynı pencere olmalı, ör. "30D" ya da bar sayısı (int).
    """
    tp = _typical_price(df)
    sq_diff = (tp - vwap) ** 2
    weighted_sq = sq_diff * df["volume"]
    if isinstance(window, str):
        var = weighted_sq.rolling(window, min_periods=1).sum() / df["volume"].rolling(
            window, min_periods=1
        ).sum().replace(0, np.nan)
    else:
        var = weighted_sq.rolling(window, min_periods=1).sum() / df["volume"].rolling(
            window, min_periods=1
        ).sum().replace(0, np.nan)
    std = np.sqrt(var.clip(lower=0))

    out = pd.DataFrame(index=df.index)
    out["vwap"] = vwap
    for k in std_devs:
        out[f"upper_{k}std"] = vwap + k * std
        out[f"lower_{k}std"] = vwap - k * std
    return out
