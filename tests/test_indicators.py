import numpy as np
import pandas as pd
import pytest

from src.indicators import cvd, open_interest, orderbook, volume_profile, vwap


@pytest.fixture
def synthetic_klines():
    np.random.seed(42)
    n = 60 * 24 * 40  # 40 gün, 1m
    idx = pd.date_range("2026-08-01", periods=n, freq="1min", tz="UTC")
    price = 60000 + np.cumsum(np.random.randn(n) * 5)
    high = price + np.abs(np.random.randn(n) * 3)
    low = price - np.abs(np.random.randn(n) * 3)
    close = price + np.random.randn(n)
    vol = np.abs(np.random.randn(n) * 2) + 0.5
    taker_buy = vol * np.random.uniform(0.3, 0.7, n)
    return pd.DataFrame(
        {"high": high, "low": low, "close": close, "volume": vol, "taker_buy_base": taker_buy},
        index=idx,
    )


def test_vwap_variants(synthetic_klines):
    df = synthetic_klines
    sv = vwap.session_vwap(df)
    rv30 = vwap.rolling_vwap(df, 30)
    dev_w, prev_w = vwap.multi_period_vwap(df, "W")
    bands = vwap.vwap_with_bands(df, rv30, "30D", (1.0, 2.0))

    assert sv.dropna().gt(0).all()
    assert rv30.dropna().gt(0).all()
    assert dev_w.dropna().gt(0).all()
    assert prev_w.dropna().gt(0).all()
    assert (bands["upper_1.0std"].dropna() >= bands["vwap"].dropna()).all()
    assert (bands["lower_1.0std"].dropna() <= bands["vwap"].dropna()).all()


def test_orderbook_depth_delta():
    mid = 60000.0
    bids = pd.DataFrame({"price": mid - np.arange(1, 50), "qty": [10.0] * 49})
    asks = pd.DataFrame({"price": mid + np.arange(1, 50), "qty": [1.0] * 49})
    dd = orderbook.depth_delta(bids, asks, mid, 10)
    assert dd.delta > 0
    assert orderbook.classify_liquidity_bias(dd) in ("strong_bid_heavy", "bid_heavy")


def test_cvd_divergence_detects_exhaustion_top():
    idx = pd.date_range("2026-01-01", periods=200, freq="1min", tz="UTC")
    price = np.concatenate([
        np.linspace(90, 100, 40), np.linspace(100, 95, 20),
        np.linspace(95, 110, 60), np.linspace(110, 100, 20),
        np.linspace(100, 105, 60),
    ])[:200]
    cvd_vals = np.concatenate([
        np.linspace(0, 50, 40), np.linspace(50, 45, 20),
        np.linspace(45, 40, 60), np.linspace(40, 35, 20),
        np.linspace(35, 38, 60),
    ])[:200]
    price_s = pd.Series(price, index=idx)
    cvd_s = pd.Series(cvd_vals, index=idx)
    divs = cvd.detect_divergences(price_s, cvd_s, lookback_bars=15)
    assert any(d.kind == "exhaustion_top" for d in divs)


def test_oi_rsi_bounds(synthetic_klines):
    idx = synthetic_klines.index
    np.random.seed(1)
    oi = pd.Series(1_000_000 + np.cumsum(np.random.randn(len(idx)) * 50), index=idx)
    rsi = open_interest.oi_rsi(oi, 14)
    valid = rsi.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_volume_profile_poc_within_range(synthetic_klines):
    vp = volume_profile.build_volume_profile(synthetic_klines, lookback_days=9, price_bins=100)
    assert vp.val <= vp.poc <= vp.vah
    lo, hi = synthetic_klines["low"].min(), synthetic_klines["high"].max()
    assert lo <= vp.val and vp.vah <= hi
