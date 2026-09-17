"""
CVD (Cumulative Volume Delta) göstergeleri -- exitpump'ın "CVD Guide" makalesi.

Kural: hacim sadece market emri (agresif) limit emri (pasif) ile eşleştiğinde oluşur.
  - taker buy (ask'i vurdu)  -> +qty  (buy volume)
  - taker sell (bid'i vurdu) -> -qty  (sell volume)
  delta = buy_volume - sell_volume ; CVD = delta'nın kümülatif toplamı

Divergence sınıflandırması (makaledeki 4 senaryo):
  Absorption (uptrend):  price LOWER high, CVD HIGHER high  -> dağıtım / bearish
  Absorption (downtrend): price HIGHER low, CVD LOWER low   -> toplama / bullish
  Exhaustion (uptrend):  price HIGHER high, CVD LOWER high  -> alıcı yorgunluğu / bearish
  Exhaustion (downtrend): price LOWER low, CVD HIGHER low   -> satıcı yorgunluğu / bullish
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


def _to_pandas_freq(timeframe: str) -> str:
    """Borsa-stili interval string'ini ("1m", "5m", "1h", "1d") pandas resample
    frekans alias'ına çevirir. pandas >=2.2'de bare 'm' (dakika) alias'ı kaldırıldı,
    yerine 'min' kullanılmalı -- 'h'/'d' zaten geçerli."""
    match = re.fullmatch(r"(\d+)m", timeframe)
    if match:
        return f"{match.group(1)}min"
    return timeframe


def trades_to_delta_bars(agg_trades: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """aggTrades (index=time, kolonlar: signed_qty, qty) -> OHLC-benzeri delta barları."""
    freq = _to_pandas_freq(timeframe)
    delta = agg_trades["signed_qty"].resample(freq).sum().rename("delta")
    buy_vol = agg_trades.loc[agg_trades["side"] == "buy", "qty"].resample(freq).sum().rename("buy_volume")
    sell_vol = agg_trades.loc[agg_trades["side"] == "sell", "qty"].resample(freq).sum().rename("sell_volume")
    out = pd.concat([delta, buy_vol, sell_vol], axis=1).fillna(0.0)
    out["cvd"] = out["delta"].cumsum()
    return out


def cvd_from_klines_proxy(df: pd.DataFrame) -> pd.Series:
    """aggTrade verisi yoksa (ör. sadece kline geçmişi mevcutsa) taker_buy_base/volume
    kolonlarından YAKLAŞIK bir CVD proxy'si üretir. Binance klines "taker_buy_base"
    kolonunu döndürür; sell = volume - taker_buy_base.

    Bu gerçek trade-bazlı CVD kadar hassas değildir ama backtest'te aggTrade
    verisi bulunamayan uzun dönemler için kullanılabilir bir yaklaşımdır.
    """
    buy = df["taker_buy_base"]
    sell = df["volume"] - df["taker_buy_base"]
    delta = buy - sell
    cvd = delta.cumsum()
    cvd.name = "cvd_proxy"
    return cvd


@dataclass
class Divergence:
    kind: Literal["absorption_top", "absorption_bottom", "exhaustion_top", "exhaustion_bottom"]
    at_index: pd.Timestamp
    price_pivot: float
    cvd_pivot: float
    bias: Literal["bearish", "bullish"]


def find_pivots(series: pd.Series, lookback: int) -> pd.Series:
    """Public wrapper -- bkz. _find_pivots. backtest/engine.py gibi diğer modüllerden
    pivot tespitini lookahead-güvenli şekilde (delayed reveal) kullanmak için."""
    return _find_pivots(series, lookback)


def _find_pivots(series: pd.Series, lookback: int) -> pd.Series:
    """Basit rolling-window pivot tespiti: bir noktanın 'lookback' pencerede
    lokal maksimum/minimum olup olmadığını işaretler. +1 = pivot high, -1 = pivot low, 0 = yok."""
    roll_max = series.rolling(lookback * 2 + 1, center=True).max()
    roll_min = series.rolling(lookback * 2 + 1, center=True).min()
    pivots = pd.Series(0, index=series.index)
    pivots[series == roll_max] = 1
    pivots[series == roll_min] = -1
    return pivots


def _dedupe_consecutive(index: pd.DatetimeIndex, series_index: pd.Index) -> pd.DatetimeIndex:
    """Bir plato (aynı değere sahip ardışık barlar) tek bir pivot olarak sayılsın diye
    ardışık pivot bar'larını gruplayıp her grubun ORTA noktasını temsilci pivot yapar."""
    if len(index) == 0:
        return index
    positions = series_index.get_indexer(index)
    groups: list[list[int]] = [[positions[0]]]
    for pos in positions[1:]:
        if pos - groups[-1][-1] <= 1:
            groups[-1].append(pos)
        else:
            groups.append([pos])
    reps = [series_index[grp[len(grp) // 2]] for grp in groups]
    return pd.DatetimeIndex(reps)


def detect_divergences(price: pd.Series, cvd: pd.Series, lookback_bars: int = 20) -> list[Divergence]:
    """Fiyat ve CVD serilerindeki son pivotları karşılaştırarak absorption/exhaustion tespit eder.

    Not: Bu basitleştirilmiş, kural-tabanlı bir tespit yöntemidir (gerçek zamanlı
    trading için yeterli olsa da akademik bir "swing detection" algoritması değildir).
    """
    price_pivots = _find_pivots(price, lookback_bars)

    divergences: list[Divergence] = []
    price_highs = _dedupe_consecutive(price_pivots[price_pivots == 1].index, price.index)
    price_lows = _dedupe_consecutive(price_pivots[price_pivots == -1].index, price.index)

    for pivot_type, idx_list in (("high", price_highs), ("low", price_lows)):
        if len(idx_list) < 2:
            continue
        prev_idx, curr_idx = idx_list[-2], idx_list[-1]
        p_prev, p_curr = price.loc[prev_idx], price.loc[curr_idx]
        c_prev, c_curr = cvd.loc[prev_idx], cvd.loc[curr_idx]

        if pivot_type == "high":
            if p_curr < p_prev and c_curr > c_prev:
                divergences.append(Divergence("absorption_top", curr_idx, p_curr, c_curr, "bearish"))
            elif p_curr > p_prev and c_curr < c_prev:
                divergences.append(Divergence("exhaustion_top", curr_idx, p_curr, c_curr, "bearish"))
        else:  # low
            if p_curr > p_prev and c_curr < c_prev:
                divergences.append(Divergence("absorption_bottom", curr_idx, p_curr, c_curr, "bullish"))
            elif p_curr < p_prev and c_curr > c_prev:
                divergences.append(Divergence("exhaustion_bottom", curr_idx, p_curr, c_curr, "bullish"))

    return divergences


def spot_perp_divergence(spot_cvd: pd.Series, perp_cvd: pd.Series, window: int = 30) -> pd.Series:
    """Spot CVD düşerken perp CVD'nin yükseldiği (short-covering rallisi) durumları işaretler.

    exitpump: 'falling spot CVD during a bounce is usually bearish' -- gerçek
    talep spot'tan gelmeli, sadece short-covering ile sürdürülemez.
    """
    spot_chg = spot_cvd.diff(window)
    perp_chg = perp_cvd.diff(window)
    label = pd.Series("neutral", index=spot_cvd.index)
    label[(spot_chg < 0) & (perp_chg > 0)] = "bearish_short_covering_bounce"
    label[(spot_chg > 0) & (perp_chg < 0)] = "bullish_perp_short_absorption"
    return label
