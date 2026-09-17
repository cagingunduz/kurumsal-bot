"""
Canlı PAPER-TRADING ana döngüsü.

ÖNEMLİ: Bu betik varsayılan olarak (execution.mode: "paper") GERÇEK PARA
KULLANMAZ, GERÇEK EMİR GÖNDERMEZ. Sadece public Binance verisiyle (kline,
aggTrade, depth, markPrice, openInterest) beslenip PaperBroker üzerinde
sanal pozisyon açıp kapatır. "Canlıya geçiş" (testnet/live) bu proje
kapsamında implemente edilmemiştir -- bkz. README.

Karar mantığı backtest/engine.py ile AYNI modülleri kullanır:
  indicators/* -> strategy/confluence.py + strategy/signals.py -> strategy/risk.py -> execution/paper_broker.py
Bu sayede backtest'te iyi/kötü çıkan bir davranış canlıda da aynı şekilde tekrar eder.

Çalıştırma:
    python -m src.main_live
"""
from __future__ import annotations

try:
    # bkz. main_backtest.py'deki aynı bloğun açıklaması -- macOS'ta bazı güvenlik/VPN
    # yazılımlarının HTTPS'e araya girmesi yüzünden Python'un kendi sertifika listesi
    # yetersiz kalabiliyor; truststore işletim sisteminin güven deposunu kullandırır.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from src.data.binance_client import BinanceRestClient, BinanceWSClient
from src.data.history_loader import fetch_klines_range, fetch_open_interest_hist
from src.execution.paper_broker import PaperBroker
from src.indicators.cvd import detect_divergences
from src.indicators.open_interest import oi_rsi, price_oi_cvd_table
from src.indicators.orderbook import classify_liquidity_bias, depth_delta
from src.indicators.vwap import anchored_vwap, rolling_vwap
from src.indicators.volume_profile import VolumeProfileResult, build_volume_profile
from src.notify.telegram_bot import TelegramNotifier
from src.strategy.risk import DailyLossTracker, calc_stop_target, compute_atr, position_size
from src.strategy.signals import MarketState, evaluate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("exitpump_bot.live")

KLINE_COLS_FROM_WS = ["open", "high", "low", "close", "volume", "taker_buy_base"]


class LiveTradingBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.symbol = cfg["exchange"]["symbol"]
        self.primary_tf = cfg["timeframes"]["primary"]
        self.warmup_days = max(cfg["vwap"]["rolling_periods_days"]) + 5

        self.klines_buf = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "taker_buy_base"])
        self.klines_buf.index.name = "open_time"

        self.cvd_running = 0.0
        self._bar_delta_accum = 0.0
        self.cvd_series = pd.Series(dtype=float)

        self.oi_series = pd.Series(dtype=float)

        self.latest_bids = pd.DataFrame(columns=["price", "qty"])
        self.latest_asks = pd.DataFrame(columns=["price", "qty"])
        self.last_price: float | None = None

        self.vp_cache: dict[pd.Timestamp, VolumeProfileResult] = {}

        self.broker = PaperBroker(
            starting_equity=cfg["risk"]["account_equity_usdt"],
            trade_log_csv=cfg["logging"]["trade_log_csv"],
            equity_log_csv=cfg["logging"]["equity_log_csv"],
            max_concurrent_positions=cfg["strategy"]["max_concurrent_positions"],
        )
        self.daily_tracker = DailyLossTracker(cfg["risk"]["account_equity_usdt"], cfg["risk"]["max_daily_loss_pct"])
        self.last_trade_close_time: datetime | None = None
        self.notifier = TelegramNotifier(cfg)

    # ---------------------------------------------------------------- warmup

    async def warmup(self) -> None:
        logger.info("Isınma: son %d günün %s mumları indiriliyor (bu biraz sürebilir)...", self.warmup_days, self.primary_tf)
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = end_ms - self.warmup_days * 86_400_000
        klines = await fetch_klines_range(self.symbol, self.primary_tf, start_ms, end_ms, use_cache=True)
        self.klines_buf = klines[["open", "high", "low", "close", "volume", "taker_buy_base"]].copy()
        logger.info("Isınma tamam: %d bar yüklendi (%s -> %s)", len(self.klines_buf), self.klines_buf.index.min(), self.klines_buf.index.max())

        try:
            oi_hist = await fetch_open_interest_hist(self.symbol, period="1h", start_time_ms=start_ms, end_time_ms=end_ms)
            if not oi_hist.empty:
                self.oi_series = oi_hist["sumOpenInterest"]
                logger.info("OI geçmişi yüklendi: %d nokta", len(self.oi_series))
        except Exception as e:  # noqa: BLE001
            logger.warning("OI geçmişi alınamadı (devam ediliyor, oi_cvd bileşeni ısınana kadar nötr olacak): %s", e)

        async with BinanceRestClient() as client:
            ob = await client.get_order_book(self.symbol, limit=100)
            self.latest_bids, self.latest_asks = ob["bids"], ob["asks"]
        self.last_price = float(self.klines_buf["close"].iloc[-1])
        await self.notifier.send(f"exitpump_bot başladı (paper mode). Sembol={self.symbol}, ısınma={len(self.klines_buf)} bar.")

    # ------------------------------------------------------------- handlers

    def _on_agg_trade(self, data: dict) -> None:
        qty = float(data["q"])
        buyer_is_maker = bool(data["m"])
        signed = qty if not buyer_is_maker else -qty
        self.cvd_running += signed
        self._bar_delta_accum += signed

    def _on_depth(self, data: dict) -> None:
        bids = data.get("b") or data.get("bids") or []
        asks = data.get("a") or data.get("asks") or []
        if bids:
            self.latest_bids = pd.DataFrame(bids, columns=["price", "qty"]).astype(float)
        if asks:
            self.latest_asks = pd.DataFrame(asks, columns=["price", "qty"]).astype(float)

    async def _on_mark_price(self, data: dict) -> None:
        price = float(data["p"])
        self.last_price = price
        now = datetime.now(timezone.utc)
        closed = self.broker.update_mark(price, now)
        for trade in closed:
            self.daily_tracker.register_closed_trade(now, self.broker.equity, trade.pnl_usdt)
            self.last_trade_close_time = trade.closed_at
            await self.notifier.send(
                f"[KAPANDI] {trade.direction} {self.symbol} entry={trade.entry_price:.1f} "
                f"exit={trade.exit_price:.1f} ({trade.exit_reason}) pnl={trade.pnl_usdt:+.2f} USDT"
            )

    async def _on_kline_close(self, k: dict) -> None:
        open_time = pd.Timestamp(int(k["t"]), unit="ms", tz="UTC")
        row = {
            "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
            "close": float(k["c"]), "volume": float(k["v"]), "taker_buy_base": float(k["V"]),
        }
        self.klines_buf.loc[open_time] = row
        max_len = self.warmup_days * 1440 + 1440  # primary_tf=1m varsayımıyla kabaca sınır
        if len(self.klines_buf) > max_len:
            self.klines_buf = self.klines_buf.iloc[-max_len:]

        # Bu bar'ın delta'sını CVD serisine işle
        self.cvd_series.loc[open_time] = self.cvd_running
        self._bar_delta_accum = 0.0

        await self._evaluate_and_maybe_trade(open_time)

    # ----------------------------------------------------------- strategy

    async def _evaluate_and_maybe_trade(self, ts: pd.Timestamp) -> None:
        cfg = self.cfg
        if len(self.klines_buf) < 200:
            logger.info("Henüz yeterli bar yok (%d/200), bekleniyor...", len(self.klines_buf))
            return

        price = float(self.klines_buf["close"].iloc[-1])

        day_key = ts.floor("D")
        if day_key not in self.vp_cache:
            try:
                self.vp_cache[day_key] = build_volume_profile(
                    self.klines_buf, lookback_days=cfg["volume_profile"]["lookback_days"],
                    price_bins=cfg["volume_profile"]["price_bins"],
                    value_area_pct=cfg["volume_profile"]["value_area_pct"],
                )
                self.vp_cache = {day_key: self.vp_cache[day_key]}  # sadece güncel günü tut (bellek)
            except ValueError:
                return
        vp = self.vp_cache[day_key]

        periods = cfg["vwap"]["rolling_periods_days"]
        rv_fast = rolling_vwap(self.klines_buf, periods[0]).iloc[-1]
        rv_slow = rolling_vwap(self.klines_buf, periods[1]).iloc[-1]

        lookback = cfg["cvd"]["divergence_lookback_bars"]
        tail_n = max(lookback * 6, 300)
        price_tail = self.klines_buf["close"].iloc[-tail_n:]
        cvd_tail = self.cvd_series.reindex(price_tail.index, method="ffill").bfill()
        divs = detect_divergences(price_tail, cvd_tail, lookback_bars=lookback)
        latest_div = divs[-1].kind if divs else None

        oi_available = len(self.oi_series.dropna()) > cfg["open_interest"]["rsi_period"] + 5
        if oi_available:
            oi_tail = self.oi_series.reindex(price_tail.index, method="ffill")
            rsi_tail = oi_rsi(oi_tail, cfg["open_interest"]["rsi_period"])
            table = price_oi_cvd_table(price_tail, oi_tail, cvd_tail, window=5)
            price_oi_state = table["state"].iloc[-1] if not pd.isna(rsi_tail.iloc[-1]) else "flat"
            confirmed_short_squeeze = bool(table["confirmed_short_squeeze"].iloc[-1])
            confirmed_long_unwind = bool(table["confirmed_long_unwind_risk"].iloc[-1])
        else:
            price_oi_state, confirmed_short_squeeze, confirmed_long_unwind = "flat", False, False

        mid = price
        depth_cfg_pct = cfg["order_book"]["reversal_signal_depth_pct"]
        if not self.latest_bids.empty and not self.latest_asks.empty:
            dd = depth_delta(self.latest_bids, self.latest_asks, mid, depth_cfg_pct)
            depth_bias = classify_liquidity_bias(dd)
        else:
            depth_bias = "neutral"

        state = MarketState(
            timestamp=ts.to_pydatetime(), price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            depth_bias=depth_bias, latest_cvd_divergence=latest_div, price_oi_state=price_oi_state,
            confirmed_short_squeeze=confirmed_short_squeeze, confirmed_long_unwind_risk=confirmed_long_unwind,
            rvwap_30d=float(rv_fast) if not pd.isna(rv_fast) else price,
            rvwap_90d=float(rv_slow) if not pd.isna(rv_slow) else price,
        )
        sig = evaluate(state, cfg, self.last_trade_close_time)
        logger.info(
            "Bar %s price=%.1f VAH=%.1f VAL=%.1f -> sinyal=%s skor=%d/%d %s",
            ts, price, vp.vah, vp.val, sig.direction, sig.score, sig.max_score,
            f"(bloklandı: {sig.blocked_reason})" if sig.blocked_reason else "",
        )

        can_trade, block_reason = self.daily_tracker.can_trade(ts.to_pydatetime(), self.broker.equity)
        if not can_trade:
            logger.warning(block_reason)
            return

        if sig.is_actionable and self.broker.can_open_new():
            await self._open_trade(sig, price_tail)

    async def _open_trade(self, sig, price_tail: pd.Series) -> None:
        cfg = self.cfg
        try:
            avwap_price = None
            if cfg["risk"]["stop_method"] == "avwap":
                lookback = cfg["cvd"]["divergence_lookback_bars"]
                pivots_window = self.klines_buf["close"].iloc[-max(lookback * 10, 500):]
                from src.indicators.cvd import find_pivots
                piv = find_pivots(pivots_window, lookback)
                target_flag = -1 if sig.direction == "long" else 1
                matches = piv[piv == target_flag]
                if len(matches):
                    anchor_time = matches.index[-1]
                    avwap_series = anchored_vwap(self.klines_buf, anchor_time)
                    avwap_price = float(avwap_series.iloc[-1])

            if avwap_price is not None:
                st = calc_stop_target(sig.direction, sig.entry_price, cfg["risk"]["reward_risk_ratio"], method="avwap", avwap_price=avwap_price)
            else:
                atr_val = compute_atr(self.klines_buf, cfg["risk"]["atr_period"]).iloc[-1]
                if pd.isna(atr_val) or atr_val <= 0:
                    logger.warning("ATR hesaplanamadı, işlem atlandı")
                    return
                st = calc_stop_target(sig.direction, sig.entry_price, cfg["risk"]["reward_risk_ratio"], method="atr", atr_value=float(atr_val), atr_multiplier=cfg["risk"]["atr_multiplier"])

            size = position_size(self.broker.equity, cfg["risk"]["risk_per_trade_pct"], st)
            pos = self.broker.open_position(sig.direction, st, size["qty"], size["notional_usdt"], sig.timestamp, sig.reasons)
            logger.info("YENİ POZİSYON: %s", pos)
            await self.notifier.send(
                f"[AÇILDI] {sig.direction.upper()} {self.symbol} @ {st.entry:.1f} "
                f"stop={st.stop:.1f} target={st.target:.1f} skor={sig.score}/{sig.max_score}\n"
                f"Nedenler: {'; '.join(sig.reasons)}"
            )
        except (ValueError, ZeroDivisionError, RuntimeError) as e:
            logger.warning("Pozisyon açılamadı: %s", e)

    # ------------------------------------------------------------- run loop

    async def run(self) -> None:
        await self.warmup()
        ws = BinanceWSClient(self.symbol, primary_interval=self.primary_tf, base_url=self.cfg["exchange"]["base_url_ws"])
        oi_task = asyncio.create_task(self._poll_oi_loop())
        try:
            async for msg in ws.listen():
                try:
                    if msg.channel == "aggTrade":
                        self._on_agg_trade(msg.data)
                    elif msg.channel == "depthUpdate":
                        self._on_depth(msg.data)
                    elif msg.channel == "markPriceUpdate":
                        await self._on_mark_price(msg.data)
                    elif msg.channel == "kline":
                        k = msg.data["k"]
                        if k.get("x"):  # sadece KAPANMIŞ mumlar
                            await self._on_kline_close(k)
                except Exception as e:  # noqa: BLE001 - tek bir mesajdaki hata döngüyü durdurmasın
                    logger.exception("Mesaj işlenirken hata: %s", e)
        finally:
            oi_task.cancel()

    async def _poll_oi_loop(self) -> None:
        interval = self.cfg["open_interest"]["poll_interval_sec"]
        async with BinanceRestClient() as client:
            while True:
                try:
                    oi = await client.get_open_interest(self.symbol)
                    self.oi_series.loc[oi["time"]] = oi["open_interest"]
                    if len(self.oi_series) > 20000:
                        self.oi_series = self.oi_series.iloc[-20000:]
                except Exception as e:  # noqa: BLE001
                    logger.warning("OI polling hatası: %s", e)
                await asyncio.sleep(interval)


async def main() -> None:
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    if cfg["execution"]["mode"] != "paper":
        raise SystemExit(
            "Bu betik (main_live.py) sadece execution.mode='paper' için implemente edilmiştir. "
            "'testnet'/'live' modları bu projede YOKTUR -- bkz. README 'Canlıya geçiş' bölümü."
        )
    bot = LiveTradingBot(cfg)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
