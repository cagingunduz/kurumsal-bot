"""
scripts/download_data_vision.sh ile diske indirilmiş Binance Data Vision CSV
dosyalarını okuyan yerel (AĞ KULLANMAYAN) yükleyici.

Neden bu modül var: Python'un kendi HTTPS/SSL yığını bazı makinelerde (özellikle
antivirüs/VPN'in HTTPS trafiğine araya girdiği macOS kurulumlarında) certifi
sertifika listesiyle uyumsuz kalabiliyor ve `aiohttp` istekleri
'self-signed certificate in certificate chain' hatasıyla başarısız olabiliyor
-- `curl` ise (macOS'un sistem anahtarlığını kullandığı için) aynı makinede
sorunsuz çalışabiliyor. Bu yüzden indirme işini `curl`'e (scripts/download_data_vision.sh)
bırakıp, botun SADECE diskteki dosyaları okumasını sağlıyoruz -- ağ/SSL hiç
devreye girmiyor.

Binance Data Vision CSV formatı zaman içinde başlık satırı ekleyip
kaldırabiliyor; bu yüzden her iki durumu da (başlıklı / başlıksız) otomatik
tespit ediyoruz.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("exitpump_bot.data.data_vision")

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "n_trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
AGG_TRADE_COLS = [
    "agg_id", "price", "qty", "first_id", "last_id", "time", "buyer_is_maker",
]


def _has_header(csv_path: Path, expected_first_col_numeric: bool = True) -> bool:
    with open(csv_path, "r") as f:
        first_line = f.readline().strip()
    if not first_line:
        return False
    first_token = first_line.split(",")[0]
    if expected_first_col_numeric:
        try:
            float(first_token)
            return False  # sayısal -> başlık yok, doğrudan veri
        except ValueError:
            return True   # sayısal değil ("open_time" gibi) -> başlık var
    return False


def load_klines_dir(dir_path: str | Path, symbol: str) -> pd.DataFrame:
    """scripts/download_data_vision.sh'in indirdiği {SYMBOL}-{interval}-{date}.csv
    dosyalarının hepsini okuyup tek bir DataFrame'de birleştirir (BinanceRestClient.get_klines
    ile AYNI şema: index=open_time (UTC), open/high/low/close/volume/taker_buy_base vb.)."""
    dir_path = Path(dir_path)
    files = sorted(dir_path.glob(f"{symbol}-*.csv"))
    if not files:
        raise FileNotFoundError(
            f"{dir_path} içinde {symbol}-*.csv bulunamadı. Önce "
            f"scripts/download_data_vision.sh çalıştırdığından emin ol."
        )

    frames = []
    for fp in files:
        header = 0 if _has_header(fp) else None
        df = pd.read_csv(fp, header=header)
        if header is None:
            df.columns = KLINE_COLS[: len(df.columns)]
        else:
            # Karşılaştırma için alt çizgi/boşluk farklarını yok say (ör. "taker_buy_volume"
            # ve "takerBuyVolume" ve "taker buy volume" hepsi aynı normalize forma düşsün).
            normalized = {c: c.strip().lower().replace("_", "").replace(" ", "") for c in df.columns}
            rename_map = {
                "opentime": "open_time", "closetime": "close_time",
                "quotevolume": "quote_volume", "numberoftrades": "n_trades",
                "count": "n_trades",
                # Binance Data Vision'ın GERÇEK kline CSV başlığı "taker_buy_volume" --
                # "taker_buy_base_volume" değil. Her iki ihtimali de (ve olası base/asset
                # varyasyonlarını) eşleştir ki format değişirse de kırılmasın.
                "takerbuyvolume": "taker_buy_base",
                "takerbuybasevolume": "taker_buy_base",
                "takerbuybaseassetvolume": "taker_buy_base",
                "takerbuyquotevolume": "taker_buy_quote",
                "takerbuyquoteassetvolume": "taker_buy_quote",
                "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume",
            }
            df.columns = [rename_map.get(normalized[c], normalized[c]) for c in df.columns]
        frames.append(df)

    out = pd.concat(frames, ignore_index=True)
    for col in ["open", "high", "low", "close", "volume", "quote_volume",
                "taker_buy_base", "taker_buy_quote"]:
        if col in out.columns:
            out[col] = out[col].astype(float)
    if "n_trades" in out.columns:
        out["n_trades"] = out["n_trades"].astype(int)

    # open_time/close_time ms ya da us (mikrosaniye) olabilir -- Data Vision 2025 sonrası
    # bazı sembollerde mikrosaniye kullanmaya başladı; büyüklüğe göre otomatik tespit et.
    def _to_datetime_auto(series: pd.Series) -> pd.Series:
        sample = float(series.iloc[0])
        unit = "us" if sample > 1e14 else "ms"
        return pd.to_datetime(series.astype("int64"), unit=unit, utc=True)

    out["open_time"] = _to_datetime_auto(out["open_time"])
    out["close_time"] = _to_datetime_auto(out["close_time"])
    out = out.drop_duplicates(subset=["open_time"]).sort_values("open_time")
    out = out.set_index("open_time")
    keep_cols = [c for c in ["open", "high", "low", "close", "volume", "close_time",
                              "quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote"]
                 if c in out.columns]
    logger.info("Data Vision klines yüklendi: %d dosya, %d bar (%s -> %s)",
                len(files), len(out), out.index.min(), out.index.max())
    return out[keep_cols]


def load_agg_trades_dir(dir_path: str | Path, symbol: str) -> pd.DataFrame:
    """scripts/download_data_vision.sh'in indirdiği {SYMBOL}-aggTrades-{date}.csv
    dosyalarının hepsini okuyup BinanceRestClient.get_agg_trades ile AYNI şemada döner
    (index=time, kolonlar: price, qty, side, signed_qty, ...)."""
    dir_path = Path(dir_path)
    files = sorted(dir_path.glob(f"{symbol}-aggTrades-*.csv"))
    if not files:
        raise FileNotFoundError(
            f"{dir_path} içinde {symbol}-aggTrades-*.csv bulunamadı. "
            f"SKIP_AGGTRADES=1 vermeden scripts/download_data_vision.sh çalıştırdığından emin ol."
        )

    frames = []
    for fp in files:
        header = 0 if _has_header(fp) else None
        df = pd.read_csv(fp, header=header)
        if header is None:
            df.columns = AGG_TRADE_COLS[: len(df.columns)]
        else:
            normalized = {c: c.strip().lower().replace("_", "").replace(" ", "") for c in df.columns}
            rename_map = {
                "aggtradeid": "agg_id", "firsttradeid": "first_id", "lasttradeid": "last_id",
                "transacttime": "time", "isbuyermaker": "buyer_is_maker", "timestamp": "time",
                "price": "price", "quantity": "qty", "qty": "qty",
            }
            df.columns = [rename_map.get(normalized[c], normalized[c]) for c in df.columns]
        frames.append(df)

    out = pd.concat(frames, ignore_index=True)
    out["price"] = out["price"].astype(float)
    out["qty"] = out["qty"].astype(float)
    # buyer_is_maker CSV'de True/False string'i ya da 1/0 olabilir
    out["buyer_is_maker"] = out["buyer_is_maker"].astype(str).str.strip().str.lower().isin(["true", "1"])

    sample = float(out["time"].iloc[0])
    unit = "us" if sample > 1e14 else "ms"
    out["time"] = pd.to_datetime(out["time"].astype("int64"), unit=unit, utc=True)

    out["side"] = out["buyer_is_maker"].map({True: "sell", False: "buy"})
    out["signed_qty"] = out.apply(lambda r: r["qty"] if r["side"] == "buy" else -r["qty"], axis=1)
    out = out.drop_duplicates(subset=["agg_id"]).sort_values("time").set_index("time")
    logger.info("Data Vision aggTrades yüklendi: %d dosya, %d trade (%s -> %s)",
                len(files), len(out), out.index.min(), out.index.max())
    return out[["price", "qty", "side", "signed_qty"]]
