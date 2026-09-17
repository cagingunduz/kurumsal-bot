"""
OKX (V5) veri istemcisi -- Binance yerine kullanılabilecek alternatif kaynak.

Neden bu modül var: Kullanıcının makinesinde Binance'e (fapi.binance.com) giden
Python/aiohttp isteklerinde "self-signed certificate in certificate chain" hatası
vardı (bkz. README "Veri kısıtları"), curl ise aynı host'a sorunsuz bağlanıyordu.
OKX'in PUBLIC market data endpoint'leri kimlik doğrulama GEREKTİRMEZ (API key/
secret/passphrase sadece trading/account/private endpoint'ler için gerekli),
bu yüzden backtest ve paper-trading için OKX'e geçmek Binance'teki SSL sorununu
bypass edebilir (farklı bir host + farklı bir TLS zinciri).

Kullanılan public endpoint'ler (hiçbiri API key istemez):
  - GET /api/v5/market/candles          -> en son ~300 mum (limit<=300)
  - GET /api/v5/market/history-candles  -> daha eski mumlar (cursor tabanlı sayfalama, limit<=100)
  - GET /api/v5/market/books            -> anlık order book snapshot
  - GET /api/v5/market/trades           -> en son public trade'ler (limit<=500)
  - GET /api/v5/market/history-trades   -> geçmiş trade'ler (cursor tabanlı sayfalama, limit<=100)
  - GET /api/v5/public/open-interest    -> anlık open interest
  - GET /api/v5/rubik/stat/contracts/open-interest-volume -> geçmiş OI+hacim istatistiği

ÖNEMLİ DÜRÜSTLÜK NOTU: Bu sandbox'ın ağ erişimi de (Binance'e olduğu gibi)
okx.com'a kapalı -- yani bu istemci gerçek bir OKX isteğiyle test EDİLEMEDİ.
Alan adları/şema aşağıda OKX'in genel v5 dokümantasyon yapısına göre yazıldı
ama canlı yanıt şeklini birebir doğrulayamadım. İlk çalıştırmada
`pytest tests/test_okx_client.py -v` ve küçük bir `--days 2` backtest'i ile
sağlamasını yap; alan adı uyuşmazlığı çıkarsa (ör. "volCcy" yerine başka bir
key) bana hata mesajını yapıştır, hemen düzeltirim.

CVD notu: OKX'in kline (candle) verisi Binance'in aksine "taker_buy_base" alanı
VERMEZ. Bu yüzden gerçek trade'ler (get_trades_range) kullanılmadan hesaplanan
kline-proxy CVD'si burada nötr/düz bırakılır (taker_buy_base = volume/2).
OKX ile backtest alırken --with-agg-trades (gerçek trade tabanlı CVD) kullanmak
NEREDEYSE ZORUNLUDUR, aksi halde CVD sinyali anlamsızlaşır.
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

logger = logging.getLogger("exitpump_bot.data.okx")

# Binance tarzı "1m","1h" gibi interval string'lerini OKX'in "bar" parametresine çevir.
_BAR_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1Dutc", "1w": "1Wutc", "1M": "1Mutc",
}


def _to_bar(interval: str) -> str:
    return _BAR_MAP.get(interval, interval)


class OKXRestClient:
    def __init__(self, base_url: str = "https://www.okx.com", timeout_sec: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "OKXRestClient":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> list:
        assert self._session is not None, "use 'async with OKXRestClient() as client'"
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 429:
                        wait = 2 ** attempt * 2
                        logger.warning("Rate limited (status=429), retrying in %ss", wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    body = await resp.json()
                    if str(body.get("code", "0")) != "0":
                        raise RuntimeError(f"OKX API error: {body.get('code')} {body.get('msg')}")
                    return body.get("data", [])
            except aiohttp.ClientError as e:
                if attempt == 2:
                    raise
                logger.warning("Request failed (%s), retrying (%s/3)", e, attempt + 1)
                await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Failed to GET {path} after retries")

    @staticmethod
    def _candles_to_df(raw: list) -> pd.DataFrame:
        """raw: [[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm], ...] (en yeni ilk sırada)."""
        if not raw:
            return pd.DataFrame()
        cols = ["open_time", "open", "high", "low", "close", "volume", "vol_ccy", "vol_ccy_quote", "confirm"]
        n = len(raw[0])
        df = pd.DataFrame(raw, columns=cols[:n])
        for col in ["open", "high", "low", "close", "volume", "vol_ccy", "vol_ccy_quote"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
        df = df.sort_values("open_time").drop_duplicates(subset=["open_time"]).set_index("open_time")
        # Binance şemasıyla uyum için eksik alanları doldur.
        df["close_time"] = df.index
        df["quote_volume"] = df.get("vol_ccy_quote", df["volume"])
        df["n_trades"] = 0
        # OKX candle'da alıcı/satıcı ayrımı yok -> proxy CVD'yi nötr bırak (bkz. modül docstring).
        df["taker_buy_base"] = df["volume"] / 2.0
        df["taker_buy_quote"] = df["quote_volume"] / 2.0
        keep = ["open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote"]
        return df[keep]

    async def get_klines(self, symbol: str, interval: str, limit: int = 300,
                          start_time_ms: int | None = None, end_time_ms: int | None = None) -> pd.DataFrame:
        """Tek istek, en son ~300 mum (Binance ile arayüz uyumu için)."""
        params: dict[str, Any] = {"instId": symbol, "bar": _to_bar(interval), "limit": min(limit, 300)}
        if end_time_ms is not None:
            params["after"] = end_time_ms  # "after" = bu ts'ten ESKİ kayıtlar (OKX kuralı)
        if start_time_ms is not None:
            params["before"] = start_time_ms  # "before" = bu ts'ten YENİ kayıtlar
        raw = await self._get("/api/v5/market/candles", params)
        return self._candles_to_df(raw)

    async def get_klines_range(self, symbol: str, interval: str,
                                start_time_ms: int, end_time_ms: int) -> pd.DataFrame:
        """history-candles ile geriye doğru sayfalayarak [start, end] aralığını doldurur."""
        bar = _to_bar(interval)
        frames = []
        cursor = end_time_ms
        for _ in range(2000):  # güvenlik sınırı
            params = {"instId": symbol, "bar": bar, "limit": 100, "after": cursor}
            raw = await self._get("/api/v5/market/history-candles", params)
            if not raw:
                break
            df = self._candles_to_df(raw)
            if df.empty:
                break
            frames.append(df)
            oldest = int(df.index.min().timestamp() * 1000)
            if oldest <= start_time_ms or oldest >= cursor:
                break
            cursor = oldest
            await asyncio.sleep(0.12)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames).sort_index()
        out = out[~out.index.duplicated(keep="first")]
        return out.loc[(out.index >= pd.to_datetime(start_time_ms, unit="ms", utc=True)) &
                        (out.index <= pd.to_datetime(end_time_ms, unit="ms", utc=True))]

    async def get_order_book(self, symbol: str, limit: int = 400) -> dict:
        raw = await self._get("/api/v5/market/books", {"instId": symbol, "sz": min(limit, 400)})
        if not raw:
            return {"bids": pd.DataFrame(columns=["price", "qty"]),
                    "asks": pd.DataFrame(columns=["price", "qty"]), "lastUpdateId": None}
        book = raw[0]
        bids = pd.DataFrame(book["bids"], columns=["price", "qty", "liq_orders", "n_orders"])[["price", "qty"]].astype(float)
        asks = pd.DataFrame(book["asks"], columns=["price", "qty", "liq_orders", "n_orders"])[["price", "qty"]].astype(float)
        return {"bids": bids, "asks": asks, "lastUpdateId": book.get("ts")}

    async def get_open_interest(self, symbol: str) -> dict:
        raw = await self._get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": symbol})
        if not raw:
            raise RuntimeError(f"OKX open-interest boş döndü: {symbol}")
        row = raw[0]
        return {
            "symbol": row["instId"],
            "open_interest": float(row["oi"]),
            "time": pd.to_datetime(int(row["ts"]), unit="ms", utc=True),
        }

    async def get_open_interest_hist(self, symbol: str, ccy: str, period: str = "5m",
                                      limit: int = 500, start_time_ms: int | None = None,
                                      end_time_ms: int | None = None) -> pd.DataFrame:
        """OKX'in istatistik ucu (rubik) -- DİKKAT: instId değil, ccy (ör. "BTC") bazında,
        o para birimindeki TÜM sözleşmelerin toplam OI'sini döndürür (tek bir swap
        enstrümanına özel değil). Binance'in openInterestHist'i ile birebir eşdeğer değil;
        bkz. README "Veri kısıtları"."""
        params: dict[str, Any] = {"ccy": ccy, "period": period}
        if end_time_ms is not None:
            params["end"] = end_time_ms
        if start_time_ms is not None:
            params["begin"] = start_time_ms
        raw = await self._get("/api/v5/rubik/stat/contracts/open-interest-volume", params)
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw, columns=["timestamp", "sumOpenInterest", "sumOpenInterestValue"][:len(raw[0])])
        df["sumOpenInterest"] = df["sumOpenInterest"].astype(float)
        if "sumOpenInterestValue" in df.columns:
            df["sumOpenInterestValue"] = df["sumOpenInterestValue"].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
        return df.sort_values("timestamp").set_index("timestamp").iloc[-limit:]

    async def get_trades_range(self, symbol: str, start_time_ms: int, end_time_ms: int,
                                max_requests: int = 200) -> pd.DataFrame:
        """CVD için gerçek public trade'ler -- OKX 'side' alanını doğrudan verir
        (Binance'teki buyer_is_maker'dan çıkarım yapmaya gerek yok)."""
        frames = []
        cursor_ts = end_time_ms
        for _ in range(max_requests):
            params = {"instId": symbol, "type": "2", "after": str(cursor_ts), "limit": 100}
            raw = await self._get("/api/v5/market/history-trades", params)
            if not raw:
                break
            df = pd.DataFrame(raw)
            if df.empty:
                break
            df["price"] = df["px"].astype(float)
            df["qty"] = df["sz"].astype(float)
            df["time"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
            df["side"] = df["side"]  # zaten "buy"/"sell"
            df["signed_qty"] = df.apply(lambda r: r["qty"] if r["side"] == "buy" else -r["qty"], axis=1)
            df = df.set_index("time")[["price", "qty", "side", "signed_qty"]]
            frames.append(df)
            oldest_ts = int(df.index.min().timestamp() * 1000)
            if oldest_ts <= start_time_ms or oldest_ts >= cursor_ts:
                break
            cursor_ts = oldest_ts
            await asyncio.sleep(0.12)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames).sort_index()
        out = out[~out.index.duplicated(keep="first")]
        return out.loc[(out.index >= pd.to_datetime(start_time_ms, unit="ms", utc=True)) &
                        (out.index <= pd.to_datetime(end_time_ms, unit="ms", utc=True))]


@dataclass
class WSMessage:
    stream: str
    channel: str
    data: dict


class OKXWSClient:
    """OKX public WS (canlı mod için -- şu an main_live.py Binance'e bağlı,
    bu istemci ileride OKX'e geçiş için hazır bir iskelet olarak eklendi)."""

    def __init__(self, symbol: str, primary_interval: str = "1m",
                 base_url: str = "wss://ws.okx.com:8443/ws/v5/public"):
        self.symbol = symbol
        self.base_url = base_url
        self.args = [
            {"channel": "trades", "instId": symbol},
            {"channel": "books", "instId": symbol},
            {"channel": f"candle{_to_bar(primary_interval)}", "instId": symbol},
            {"channel": "mark-price", "instId": symbol},
        ]

    async def listen(self) -> AsyncIterator[WSMessage]:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.base_url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": self.args}))
                    logger.info("OKX WS connected: %s", self.base_url)
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        arg = msg.get("arg", {})
                        channel = arg.get("channel", "unknown")
                        data = msg.get("data", [{}])[0] if msg.get("data") else {}
                        yield WSMessage(stream=f"{channel}:{arg.get('instId', '')}", channel=channel, data=data)
            except Exception as e:  # noqa: BLE001 - resilient reconnect loop
                logger.warning("OKX WS disconnected (%s). Reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


def now_ms() -> int:
    return int(time.time() * 1000)
