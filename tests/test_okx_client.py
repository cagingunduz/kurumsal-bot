"""OKX istemcisinin AĞ KULLANMAYAN parça(lar)ını test eder: ham OKX JSON şekli
verildiğinde doğru DataFrame'e dönüşüyor mu. Gerçek bir OKX isteği ATMAZ
(sandbox'ın ağ erişimi okx.com'a da kapalı) -- sadece response-şekli sabit
(fixture) veri ile parse mantığını doğrular."""
from src.data.okx_client import OKXRestClient, _to_bar


def test_to_bar_mapping():
    assert _to_bar("1m") == "1m"
    assert _to_bar("1h") == "1H"
    assert _to_bar("4h") == "4H"


def test_candles_to_df_shape():
    # OKX /market/candles örnek satırı: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    raw = [
        ["1725148920000", "60005", "60015", "59995", "60010", "12.5", "750000", "750000", "1"],
        ["1725148860000", "60000", "60010", "59990", "60005", "10.0", "600000", "600000", "1"],
    ]
    df = OKXRestClient._candles_to_df(raw)
    assert len(df) == 2
    # index artan sırada olmalı (en eski ilk)
    assert df.index[0] < df.index[1]
    assert list(df.columns) == [
        "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote",
    ]
    # taker_buy_base = volume/2 (nötr proxy, bkz. modül docstring)
    assert df["taker_buy_base"].iloc[0] == df["volume"].iloc[0] / 2.0
    assert df["open"].iloc[-1] == 60005.0


def test_candles_to_df_empty():
    df = OKXRestClient._candles_to_df([])
    assert df.empty
