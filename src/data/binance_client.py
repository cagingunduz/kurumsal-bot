"""
Binance USD-M Futures veri istemcisi.

Sadece PUBLIC (kimlik doğrulama gerektirmeyen) endpoint'leri kullanır:
  - /fapi/v1/klines            -> OHLCV mumları (VWAP, Volume Profile için)
  - /fapi/v1/depth             -> anlık order book snapshot (Order Book/Heatmap için)
  - /fapi/v1/openInterest      -> anlık open interest
  - /futures/data/openInterestHist -> geçmiş open interest (limitli, ~30 gün)
  - /fapi/v1/aggTrades         -> geçmiş agregatlı trade'ler (CVD backtest için)
  - WS: aggTrade, depth, markPrice akışları (canlı CVD / order book / OI proxy için)

Not: Binance'in openInterestHist endpoint'i sadece yakın geçmişi (genelde
son ~30 gün, 5m/15m/30m/1h/2h/4h/6h/12h/1d periyotlarında) döndürür. Daha
uzun tarihli OI backtest'i için ayrıca arşivlenmiş veri gerekir -- bkz.
README "Veri kısıtları" bölümü.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import aiohttp
import pandas as pd

logger = logging.getLogger("exitpump_bot.data")

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "n_trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


class BinanceRestClient:
    def __init__(self, base_url: str = "https://fapi.binance.com", timeout_sec: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "BinanceRestClient":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        assert self._session is not None, "use 'async with BinanceRestClient() as client'"
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 429 or resp.status == 418:
                        wait = 2 ** attempt * 2
                        logger.warning("Rate limited (status=%s), retrying in %ss", resp.status, wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as e:
                if attempt == 2:
                    raise
                logger.warning("Request failed (%s), retrying (%s/3)", e, attempt + 1)
                await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Failed to GET {path} after retries")

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 500,
        start_time_ms: int | None = None, end_time_ms: int | None = None,
    ) -> pd.DataFrame:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        raw = await self._get("/fapi/v1/klines", params)
        df = pd.DataFrame(raw, columns=KLINE_COLUMNS)
        for col in ["open", "high", "low", "close", "volume", "quote_volume",
                    "taker_buy_base", "taker_buy_quote"]:
            df[col] = df[col].astype(float)
        df["n_trades"] = df["n_trades"].astype(int)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df = df.drop(columns=["ignore"]).set_index("open_time")
        return df

    async def get_order_book(self, symbol: str, limit: int = 500) -> dict:
        """limit: 5,10,20,50,100,500,1000"""
        raw = await self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        bids = pd.DataFrame(raw["bids"], columns=["price", "qty"]).astype(float)
        asks = pd.DataFrame(raw["asks"], columns=["price", "qty"]).astype(float)
        return {"bids": bids, "asks": asks, "lastUpdateId": raw["lastUpdateId"]}

    async def get_open_interest(self, symbol: str) -> dict:
        raw = await self._get("/fapi/v1/openInterest", {"symbol": symbol})
        return {
            "symbol": raw["symbol"],
            "open_interest": float(raw["openInterest"]),
            "time": pd.to_datetime(int(raw["time"]), unit="ms", utc=True),
        }

    async def get_open_interest_hist(
        self, symbol: str, period: str = "1h", limit: int = 500,
        start_time_ms: int | None = None, end_time_ms: int | None = None,
    ) -> pd.DataFrame:
        """period: 5m,15m,30m,1h,2h,4h,6h,12h,1d. Binance genelde ~30 gün geriye gider."""
        params: dict[str, Any] = {"symbol": symbol, "period": period, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        raw = await self._get("/futures/data/openInterestHist", params)
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        df["sumOpenInterest"] = df["sumOpenInterest"].astype(float)
        df["sumOpenInterestValue"] = df["sumOpenInterestValue"].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
        return df.set_index("timestamp")

    async def get_agg_trades(
        self, symbol: str, start_time_ms: int | None = None,
        end_time_ms: int | None = None, from_id: int | None = None, limit: int = 1000,
    ) -> pd.DataFrame:
        """CVD hesaplamak için: m=True ise alıcı taraf maker'dır (yani agresif SATIŞ)."""
        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if from_id is not None:
            params["fromId"] = from_id
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        raw = await self._get("/fapi/v1/aggTrades", params)
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        df = df.rename(columns={
            "a": "agg_id", "p": "price", "q": "qty", "f": "first_id",
            "l": "last_id", "T": "time", "m": "buyer_is_maker",
        })
        df["price"] = df["price"].astype(float)
        df["qty"] = df["qty"].astype(float)
        df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
        # buyer_is_maker True  -> alici pasif (limit buy hit edildi) -> agresif taraf SATICI -> sell volume
        # buyer_is_maker False -> alici agresif (market buy)         -> agresif taraf ALICI  -> buy volume
        df["side"] = df["buyer_is_maker"].map({True: "sell", False: "buy"})
        df["signed_qty"] = df.apply(lambda r: r["qty"] if r["side"] == "buy" else -r["qty"], axis=1)
        return df.set_index("time")


@dataclass
class WSMessage:
    stream: str
    channel: str  # "aggTrade" | "depth" | "markPrice"
    data: dict


class BinanceWSClient:
    """Reconnect eden, birden fazla stream'i tek bir combined WS bağlantısında dinleyen istemci."""

    def __init__(self, symbol: str, primary_interval: str = "1m", base_url: str = "wss://fstream.binance.com/stream"):
        self.symbol = symbol.lower()
        self.base_url = base_url
        self.streams = [
            f"{self.symbol}@aggTrade",
            f"{self.symbol}@depth20@100ms",
            f"{self.symbol}@markPrice@1s",
            f"{self.symbol}@kline_{primary_interval}",
        ]

    async def listen(self) -> AsyncIterator[WSMessage]:
        import websockets

        url = f"{self.base_url}?streams={'/'.join(self.streams)}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("WS connected: %s", url)
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        stream = msg.get("stream", "")
                        data = msg.get("data", {})
                        channel = data.get("e", stream.split("@")[-1] if "@" in stream else "unknown")
                        yield WSMessage(stream=stream, channel=channel, data=data)
            except Exception as e:  # noqa: BLE001 - resilient reconnect loop
                logger.warning("WS disconnected (%s). Reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


def now_ms() -> int:
    return int(time.time() * 1000)
