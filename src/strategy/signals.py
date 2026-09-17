"""
Sinyal orkestrasyonu: gösterge çıktılarını MarketState'e toplar, confluence
motorunu çalıştırır, makro blackout / cooldown kurallarını uygular ve
nihai TradeSignal'i üretir. Hem backtest hem canlı (paper) main loop bu
modülü kullanır -- bu sayede iki ortamda AYNI karar mantığı çalışır.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.strategy import confluence as conf


@dataclass
class MarketState:
    timestamp: datetime
    price: float
    vah: float
    val: float
    poc: float
    depth_bias: str                       # orderbook.classify_liquidity_bias çıktısı
    latest_cvd_divergence: Optional[str]   # cvd.Divergence.kind ya da None
    price_oi_state: str                    # open_interest.classify_price_oi çıktısı
    confirmed_short_squeeze: bool
    confirmed_long_unwind_risk: bool
    rvwap_30d: float
    rvwap_90d: float


@dataclass
class TradeSignal:
    timestamp: datetime
    direction: str          # "long" | "short" | "none"
    score: int
    max_score: int
    reasons: list[str]
    entry_price: float
    vah: float
    val: float
    blocked_reason: Optional[str] = None

    @property
    def is_actionable(self) -> bool:
        return self.direction in ("long", "short") and self.blocked_reason is None


def is_in_macro_blackout(now: datetime, events_utc: list[str], minutes_before: int, minutes_after: int) -> Optional[str]:
    for ev_str in events_utc:
        ev = datetime.fromisoformat(ev_str).replace(tzinfo=timezone.utc)
        window_start = ev - timedelta(minutes=minutes_before)
        window_end = ev + timedelta(minutes=minutes_after)
        if window_start <= now <= window_end:
            return f"Makro blackout: {ev.isoformat()} olayına {minutes_before}dk kala / {minutes_after}dk sonrasına kadar yeni işlem yok"
    return None


def evaluate(state: MarketState, cfg: dict, last_trade_close_time: Optional[datetime] = None) -> TradeSignal:
    votes = [
        conf.vote_value_area(state.price, state.vah, state.val),
        conf.vote_cvd_divergence(state.latest_cvd_divergence),
        conf.vote_order_book(state.depth_bias),
        conf.vote_oi_cvd(state.price_oi_state, state.confirmed_short_squeeze, state.confirmed_long_unwind_risk),
        conf.vote_htf_trend(state.price, state.rvwap_30d, state.rvwap_90d),
    ]
    result = conf.combine_votes(votes)

    min_score = cfg["strategy"]["min_confluence_score"]
    direction = result.direction if result.score >= min_score else "none"

    blocked_reason = None

    macro_cfg = cfg.get("macro_blackout", {})
    if macro_cfg.get("enabled") and direction != "none":
        blocked_reason = is_in_macro_blackout(
            state.timestamp, macro_cfg.get("events_utc", []),
            macro_cfg.get("minutes_before", 60), macro_cfg.get("minutes_after", 30),
        )

    if blocked_reason is None and direction != "none" and last_trade_close_time is not None:
        cooldown = timedelta(minutes=cfg["strategy"]["cooldown_minutes_after_trade"])
        if state.timestamp - last_trade_close_time < cooldown:
            blocked_reason = (
                f"Cooldown aktif: son işlem {last_trade_close_time.isoformat()} kapandı, "
                f"{cfg['strategy']['cooldown_minutes_after_trade']}dk beklenmeli"
            )

    return TradeSignal(
        timestamp=state.timestamp,
        direction=direction,
        score=result.score,
        max_score=len(votes),
        reasons=result.reasons,
        entry_price=state.price,
        vah=state.vah,
        val=state.val,
        blocked_reason=blocked_reason,
    )
