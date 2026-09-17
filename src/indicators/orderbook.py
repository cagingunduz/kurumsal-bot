"""
Order Book / Heatmap / Depth göstergeleri -- exitpump'ın "Order Book Guide" makalesi.

- depth_delta: belirli bir % aralığındaki bid/ask hacim dengesizliği
- large_orders: ortalamanın çok üstündeki "duvar" emirleri filtreler
- classify_liquidity_bias: depth_delta işaretine göre basit yorum

Not: Bu modül tek bir anlık order book snapshot'ı (REST /fapi/v1/depth ya da
WS depth20 mesajı) üzerinde çalışır. Zamana yayılmış bir "heatmap" görseli
için snapshot'ları periyodik olarak kaydedip (ör. her 1s) bir zaman serisi
haline getirmek gerekir -- bkz. execution/paper_broker.py'deki snapshot log.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class DepthDeltaResult:
    pct_range: float
    bid_volume: float
    ask_volume: float
    delta: float  # bid - ask (pozitif = daha fazla alım likiditesi)

    @property
    def bias(self) -> str:
        if self.delta > 0:
            return "bid_heavy"
        if self.delta < 0:
            return "ask_heavy"
        return "neutral"


def depth_delta(bids: pd.DataFrame, asks: pd.DataFrame, mid_price: float, pct_range: float) -> DepthDeltaResult:
    """bids/asks: 'price','qty' kolonlu DataFrame. pct_range: ör. 10 -> mid'in %10 altı/üstü."""
    lower = mid_price * (1 - pct_range / 100.0)
    upper = mid_price * (1 + pct_range / 100.0)
    bid_vol = bids.loc[bids["price"] >= lower, "qty"].sum()
    ask_vol = asks.loc[asks["price"] <= upper, "qty"].sum()
    return DepthDeltaResult(pct_range=pct_range, bid_volume=bid_vol, ask_volume=ask_vol, delta=bid_vol - ask_vol)


def multi_range_depth_delta(
    bids: pd.DataFrame, asks: pd.DataFrame, mid_price: float, pct_ranges: list[float]
) -> dict[float, DepthDeltaResult]:
    return {r: depth_delta(bids, asks, mid_price, r) for r in pct_ranges}


def large_orders(side_df: pd.DataFrame, multiplier: float = 5.0, min_levels: int = 5) -> pd.DataFrame:
    """Medyan emir boyutunun 'multiplier' katından büyük emirleri (duvarları) döndürür.

    exitpump'ın makalede bahsettiği "büyük limit emirleri filtrele" mantığı.
    """
    if len(side_df) < min_levels:
        return side_df.iloc[0:0]
    median_qty = side_df["qty"].median()
    threshold = median_qty * multiplier
    return side_df.loc[side_df["qty"] >= threshold].sort_values("qty", ascending=False)


def depth_curve(side_df: pd.DataFrame, mid_price: float, side: str, max_pct: float = 10.0, n_points: int = 20) -> pd.DataFrame:
    """Kümülatif derinlik eğrisi (Depth = birikimli likidite) -- 'thick vs thin book' görselleştirmesi için."""
    pct_grid = np.linspace(0, max_pct, n_points)
    rows = []
    for pct in pct_grid:
        if side == "bid":
            lower = mid_price * (1 - pct / 100.0)
            cum = side_df.loc[side_df["price"] >= lower, "qty"].sum()
        else:
            upper = mid_price * (1 + pct / 100.0)
            cum = side_df.loc[side_df["price"] <= upper, "qty"].sum()
        rows.append({"pct": pct, "cumulative_qty": cum})
    return pd.DataFrame(rows)


def classify_liquidity_bias(result: DepthDeltaResult, strong_threshold_pct_of_total: float = 0.15) -> str:
    """Basit yorum: delta, toplam hacmin belli bir yüzdesini aşıyorsa 'güçlü' etiketle."""
    total = result.bid_volume + result.ask_volume
    if total <= 0:
        return "unknown"
    ratio = result.delta / total
    if ratio > strong_threshold_pct_of_total:
        return "strong_bid_heavy"
    if ratio < -strong_threshold_pct_of_total:
        return "strong_ask_heavy"
    return result.bias
