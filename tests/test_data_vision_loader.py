from pathlib import Path

import pandas as pd

from src.data.data_vision_loader import load_agg_trades_dir, load_klines_dir


def test_load_klines_headerless(tmp_path):
    d = tmp_path / "klines" / "BTCUSDT"
    d.mkdir(parents=True)
    open_time = 1725148800000  # ms
    rows = []
    for i in range(5):
        rows.append([
            open_time + i * 60000, 60000 + i, 60010 + i, 59990 + i, 60005 + i,
            1.5, open_time + i * 60000 + 59999, 90000.0, 100, 0.8, 48000.0, 0,
        ])
    df = pd.DataFrame(rows)
    df.to_csv(d / "BTCUSDT-1m-2026-09-01.csv", header=False, index=False)

    out = load_klines_dir(d, "BTCUSDT")
    assert len(out) == 5
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "close_time",
                                  "quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote"]
    assert out["open"].iloc[0] == 60000
    assert out.index.tz is not None


def test_load_klines_with_header(tmp_path):
    d = tmp_path / "klines" / "BTCUSDT"
    d.mkdir(parents=True)
    open_time = 1725148800000
    df = pd.DataFrame({
        "open_time": [open_time, open_time + 60000],
        "open": [60000.0, 60001.0], "high": [60010.0, 60011.0],
        "low": [59990.0, 59991.0], "close": [60005.0, 60006.0],
        "volume": [1.5, 1.6], "close_time": [open_time + 59999, open_time + 119999],
        "quote_volume": [90000.0, 96000.0], "count": [100, 110],
        "taker_buy_volume": [0.8, 0.9], "taker_buy_quote_volume": [48000.0, 54000.0],
        "ignore": [0, 0],
    })
    df.to_csv(d / "BTCUSDT-1m-2026-09-02.csv", header=True, index=False)

    out = load_klines_dir(d, "BTCUSDT")
    assert len(out) == 2
    assert out["taker_buy_base"].iloc[0] == 0.8


def test_load_agg_trades_headerless(tmp_path):
    d = tmp_path / "aggTrades" / "BTCUSDT"
    d.mkdir(parents=True)
    t0 = 1725148800000
    rows = [
        [1, 60000.0, 0.5, 10, 10, t0, False],   # buyer_is_maker=False -> agresif ALICI
        [2, 60001.0, 0.3, 11, 11, t0 + 1000, True],  # buyer_is_maker=True -> agresif SATICI
    ]
    pd.DataFrame(rows).to_csv(d / "BTCUSDT-aggTrades-2026-09-01.csv", header=False, index=False)

    out = load_agg_trades_dir(d, "BTCUSDT")
    assert len(out) == 2
    assert out["side"].iloc[0] == "buy"
    assert out["side"].iloc[1] == "sell"
    assert out["signed_qty"].iloc[0] == 0.5
    assert out["signed_qty"].iloc[1] == -0.3


def test_klines_and_aggtrades_feed_backtest(tmp_path):
    """Data Vision'dan okunan veri, run_backtest'e sorunsuz besleniyor mu -- uçtan uca kontrol."""
    import numpy as np
    from src.backtest.engine import run_backtest
    import yaml

    kdir = tmp_path / "klines" / "BTCUSDT"
    kdir.mkdir(parents=True)
    np.random.seed(5)
    n = 60 * 24 * 5  # 5 gün
    start_ms = 1725148800000
    price = 60000 + np.cumsum(np.random.randn(n) * 5)
    rows = []
    for i in range(n):
        ot = start_ms + i * 60000
        rows.append([ot, price[i], price[i] + 3, price[i] - 3, price[i] + 0.5,
                     2.0, ot + 59999, 2.0 * price[i], 50, 1.0, price[i], 0])
    pd.DataFrame(rows).to_csv(kdir / "BTCUSDT-1m-synthetic.csv", header=False, index=False)

    klines = load_klines_dir(kdir, "BTCUSDT")
    cfg = yaml.safe_load(open("config.yaml"))
    cfg["risk"]["stop_method"] = "atr"
    result = run_backtest(klines, cfg, agg_trades=None, oi_hist=None, verbose_every=0)
    assert result.metrics["total_return_pct"] is not None  # çökmeden bir sonuç üretti
