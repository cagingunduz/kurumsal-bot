"""
Backtest için tarihsel veri indirme ve yerel diske (parquet) önbelleğe alma.

Veri kısıtları (önemli):
  - Klines (OHLCV): Binance'ten istediğin kadar geriye gidilebilir. VWAP/AMT
    (Volume Profile) backtest'i için sorun yok.
  - aggTrades (CVD için): Binance API'den sayfalama ile çekilebilir ama çok
    uzun aralıklarda (aylar) milyonlarca satır olabileceğinden yavaş olur.
    Kısa/orta vadeli backtest pencereleri (birkaç hafta) için pratik.
  - Open Interest geçmişi: Binance'in openInterestHist endpoint'i sadece
    yakın geçmişi tutar (~30 gün). Daha eski dönemler için OI tabanlı
    sinyaller backtest'te devre dışı bırakılır (bkz. backtest/engine.py).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pandas as pd

from src.data.binance_client import BinanceRestClient
from src.data.okx_client import OKXRestClient

logger = logging.getLogger("exitpump_bot.data.history")

CACHE_DIR = Path(__file__).resolve().parents[2] / "data_cache"
CACHE_DIR.mkdir(exist_ok=True)

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}


async def fetch_klines_range(
    symbol: str, interval: str, start_time_ms: int, end_time_ms: int,
    use_cache: bool = True, provider: str = "binance",
) -> pd.DataFrame:
    cache_file = CACHE_DIR / f"klines_{provider}_{symbol}_{interval}_{start_time_ms}_{end_time_ms}.parquet"
    if use_cache and cache_file.exists():
        return pd.read_parquet(cache_file)

    if provider == "okx":
        async with OKXRestClient() as client:
            out = await client.get_klines_range(symbol, interval, start_time_ms, end_time_ms)
        if not out.empty and use_cache:
            out.to_parquet(cache_file)
        return out

    step_ms = INTERVAL_MS[interval] * 1000  # 1000 mum / istek
    frames = []
    cursor = start_time_ms
    async with BinanceRestClient() as client:
        while cursor < end_time_ms:
            chunk_end = min(cursor + step_ms, end_time_ms)
            df = await client.get_klines(
                symbol, interval, limit=1000,
                start_time_ms=cursor, end_time_ms=chunk_end,
            )
            if df.empty:
                break
            frames.append(df)
            last_close = int(df["close_time"].iloc[-1].timestamp() * 1000)
            if last_close <= cursor:
                break
            cursor = last_close + 1
            await asyncio.sleep(0.15)  # rate-limit dostu

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="first")]
    if use_cache:
        out.to_parquet(cache_file)
    return out


async def fetch_agg_trades_range(
    symbol: str, start_time_ms: int, end_time_ms: int, use_cache: bool = True,
    max_requests: int = 200, provider: str = "binance",
) -> pd.DataFrame:
    """Not: max_requests ile toplam istek sayısı sınırlanır (varsayılan ~200k trade).
    Daha uzun aralıklar için pencereyi küçük parçalara bölüp ayrı ayrı çağır."""
    cache_file = CACHE_DIR / f"aggtrades_{provider}_{symbol}_{start_time_ms}_{end_time_ms}.parquet"
    if use_cache and cache_file.exists():
        return pd.read_parquet(cache_file)

    if provider == "okx":
        async with OKXRestClient() as client:
            out = await client.get_trades_range(symbol, start_time_ms, end_time_ms, max_requests=max_requests)
        if not out.empty and use_cache:
            out.to_parquet(cache_file)
        return out

    frames = []
    cursor = start_time_ms
    async with BinanceRestClient() as client:
        for _ in range(max_requests):
            df = await client.get_agg_trades(
                symbol, start_time_ms=cursor, end_time_ms=end_time_ms, limit=1000,
            )
            if df.empty:
                break
            frames.append(df)
            last_ts = int(df.index[-1].timestamp() * 1000)
            if last_ts <= cursor or last_ts >= end_time_ms:
                break
            cursor = last_ts + 1
            await asyncio.sleep(0.15)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="first")]
    if use_cache:
        out.to_parquet(cache_file)
    return out


async def fetch_open_interest_hist(
    symbol: str, period: str = "1h", start_time_ms: int | None = None,
    end_time_ms: int | None = None, provider: str = "binance", okx_ccy: str = "BTC",
) -> pd.DataFrame:
    """Sadece exchange'in tuttuğu yakın geçmiş kadar veri döner (bkz. modül docstring
    ve okx_client.py'deki OI-geçmişi kısıtı notu)."""
    if provider == "okx":
        async with OKXRestClient() as client:
            return await client.get_open_interest_hist(
                symbol, ccy=okx_ccy, period="5m", limit=500,
                start_time_ms=start_time_ms, end_time_ms=end_time_ms,
            )
    async with BinanceRestClient() as client:
        return await client.get_open_interest_hist(
            symbol, period=period, limit=500,
            start_time_ms=start_time_ms, end_time_ms=end_time_ms,
        )
