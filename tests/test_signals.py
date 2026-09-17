from datetime import datetime, timezone

import yaml

from src.strategy.signals import MarketState, evaluate


def load_cfg():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def test_confluence_short_setup():
    cfg = load_cfg()
    state = MarketState(
        timestamp=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        price=61000, vah=61010, val=59500, poc=60200,
        depth_bias="strong_ask_heavy", latest_cvd_divergence="exhaustion_top",
        price_oi_state="new_longs_trend_continuation",
        confirmed_short_squeeze=False, confirmed_long_unwind_risk=False,
        rvwap_30d=60000, rvwap_90d=60500,
    )
    sig = evaluate(state, cfg)
    assert sig.direction == "short"
    assert sig.score >= cfg["strategy"]["min_confluence_score"]


def test_confluence_long_setup_with_squeeze():
    cfg = load_cfg()
    state = MarketState(
        timestamp=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        price=59500, vah=61000, val=59490, poc=60200,
        depth_bias="strong_bid_heavy", latest_cvd_divergence="absorption_bottom",
        price_oi_state="short_squeeze",
        confirmed_short_squeeze=True, confirmed_long_unwind_risk=False,
        rvwap_30d=60000, rvwap_90d=60500,
    )
    sig = evaluate(state, cfg)
    assert sig.direction == "long"


def test_macro_blackout_blocks_signal():
    cfg = load_cfg()
    cfg["macro_blackout"]["events_utc"] = ["2026-09-17T12:30:00"]
    state = MarketState(
        timestamp=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        price=61000, vah=61010, val=59500, poc=60200,
        depth_bias="strong_ask_heavy", latest_cvd_divergence="exhaustion_top",
        price_oi_state="new_longs_trend_continuation",
        confirmed_short_squeeze=False, confirmed_long_unwind_risk=False,
        rvwap_30d=60000, rvwap_90d=60500,
    )
    sig = evaluate(state, cfg)
    assert sig.blocked_reason is not None
    assert not sig.is_actionable


def test_weak_confluence_yields_no_direction():
    cfg = load_cfg()
    state = MarketState(
        timestamp=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        price=60250, vah=61010, val=59500, poc=60200,  # değer alanının ortası -> value_area neutral
        depth_bias="neutral", latest_cvd_divergence=None,
        price_oi_state="flat", confirmed_short_squeeze=False, confirmed_long_unwind_risk=False,
        rvwap_30d=60000, rvwap_90d=60000,
    )
    sig = evaluate(state, cfg)
    assert sig.direction == "none"
