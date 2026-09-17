"""
Confluence (teyit) skorlama motoru.

exitpump'ın anlattığı metodolojiyi 5 bağımsız "oy"a indirger. Her bileşen
LONG, SHORT ya da NEUTRAL oy verir; min_confluence_score kadar aynı yönde
oy toplanırsa sinyal tetiklenir. Tek bir göstergeye güvenmemek (makalelerin
ortak teması) burada kod olarak zorunlu kılınıyor.

Bileşenler:
  1. value_area   -> AMT: fiyat VAH'a mı VAL'e mi yakın (mean-reversion ucu)
  2. cvd           -> CVD divergence (absorption/exhaustion)
  3. order_book     -> depth delta bias (25% aralık, reversal sinyali)
  4. oi_cvd         -> Fiyat+OI+CVD tablosu (squeeze / gerçek pozisyon girişi)
  5. htf_trend      -> Rolling VWAP'e göre üst zaman dilimi trend konteksti
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["long", "short", "neutral"]


@dataclass
class Vote:
    component: str
    direction: Direction
    reason: str


@dataclass
class ConfluenceResult:
    direction: Direction
    score: int
    votes: list[Vote] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [v.reason for v in self.votes if v.direction == self.direction]


def vote_value_area(price: float, vah: float, val: float, edge_tolerance_pct: float = 0.15) -> Vote:
    """Fiyat VAH'a yakınsa SHORT ('VAH'ta short ara'), VAL'e yakınsa LONG oy verir."""
    vah_dist = abs(price - vah) / price * 100
    val_dist = abs(price - val) / price * 100
    if price >= vah or vah_dist <= edge_tolerance_pct:
        return Vote("value_area", "short", f"Fiyat VAH'a çok yakın/üstünde ({price:.1f} vs VAH {vah:.1f})")
    if price <= val or val_dist <= edge_tolerance_pct:
        return Vote("value_area", "long", f"Fiyat VAL'e çok yakın/altında ({price:.1f} vs VAL {val:.1f})")
    return Vote("value_area", "neutral", "Fiyat değer alanının ortasında (Law 6: choppy, kenar yok)")


def vote_cvd_divergence(latest_divergence_kind: str | None) -> Vote:
    if latest_divergence_kind in ("absorption_top", "exhaustion_top"):
        return Vote("cvd", "short", f"CVD {latest_divergence_kind}: tepe agresyonu güvenilmiyor")
    if latest_divergence_kind in ("absorption_bottom", "exhaustion_bottom"):
        return Vote("cvd", "long", f"CVD {latest_divergence_kind}: dip agresyonu güvenilmiyor")
    return Vote("cvd", "neutral", "CVD divergence yok")


def vote_order_book(depth_bias: str) -> Vote:
    """depth_bias: classify_liquidity_bias çıktısı (ör. 'strong_ask_heavy')."""
    if "ask_heavy" in depth_bias:
        return Vote("order_book", "short", f"Order book {depth_bias}: üstte satış duvarı")
    if "bid_heavy" in depth_bias:
        return Vote("order_book", "long", f"Order book {depth_bias}: altta alış duvarı")
    return Vote("order_book", "neutral", "Order book dengeli")


def vote_oi_cvd(state: str, confirmed_short_squeeze: bool, confirmed_long_unwind_risk: bool) -> Vote:
    if confirmed_short_squeeze:
        return Vote("oi_cvd", "long", "Fiyat↑ OI↓ + CVD zayıf: short squeeze devam edebilir")
    if confirmed_long_unwind_risk:
        return Vote("oi_cvd", "short", "Fiyat↓ + yeni short girişi + CVD negatif: bearish momentum teyitli")
    if state == "new_longs_trend_continuation":
        return Vote("oi_cvd", "long", "Fiyat↑ OI↑: yeni agresif long girişi")
    if state == "long_liquidation":
        return Vote("oi_cvd", "short", "Fiyat↓ OI↓: long tasfiyesi sürüyor")
    return Vote("oi_cvd", "neutral", "OI+CVD net bir pozisyonlanma göstermiyor")


def vote_htf_trend(price: float, rvwap_30d: float, rvwap_90d: float) -> Vote:
    if price > rvwap_30d > rvwap_90d:
        return Vote("htf_trend", "long", "Fiyat 30G rVWAP üstünde, 30G>90G (bullish rejim)")
    if price < rvwap_30d < rvwap_90d:
        return Vote("htf_trend", "short", "Fiyat 30G rVWAP altında, 30G<90G (bearish rejim)")
    return Vote("htf_trend", "neutral", "rVWAP'lar net bir rejim göstermiyor (sıkışma)")


def combine_votes(votes: list[Vote]) -> ConfluenceResult:
    long_votes = [v for v in votes if v.direction == "long"]
    short_votes = [v for v in votes if v.direction == "short"]
    if len(long_votes) > len(short_votes):
        return ConfluenceResult("long", len(long_votes), votes)
    if len(short_votes) > len(long_votes):
        return ConfluenceResult("short", len(short_votes), votes)
    return ConfluenceResult("neutral", 0, votes)
