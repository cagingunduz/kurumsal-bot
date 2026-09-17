"""
Event-driven backtest motoru.

TASARIM İLKESİ: main_live.py (canlı paper-trading) ile AYNI karar zincirini
kullanır -- src.strategy.signals.evaluate() + src.strategy.risk + PaperBroker.
Böylece "backtest'te farklı, canlıda farklı davranan bot" hatası engellenir.

LOOKAHEAD (ileriye bakma) güvenliği:
  - rVWAP, CVD (kümülatif), OI RSI: doğaları gereği sadece geçmişe bakar (causal),
    tüm seri üzerinde vektörel önceden hesaplanabilir.
  - Volume Profile (AMT): her gün, O ANA KADAR olan veriyle (lookback_days
    penceresiyle) yeniden hesaplanır -- gelecekteki mumları kullanmaz.
  - CVD divergence (pivot tespiti): pivot tespiti "center=True" rolling
    pencere kullandığından bir pivotun kesinleşmesi için lookback_bars kadar
    GELECEK veri gerekir. Bu yüzden pivot serisi bir kere (vektörel, hızlı)
    hesaplanır ama bar i'deki pivot bilgisi, strateji tarafından ancak
    (i + lookback_bars). bar'da "açığa çıkmış" sayılır (delayed reveal).
    Bu, canlı ortamda doğal olarak oluşan gecikmeyle birebir eşleşir.

VERİ KISITLARI (README'de detaylı):
  - Order book/heatmap geçmişi Binance'te mevcut değil -> backtest'te bu
    confluence bileşeni her zaman "neutral" oy verir (canlıda gerçek veriyle
    çalışır). Bu, backtest sonuçlarının canlıdan biraz daha "temkinli"
    (daha az teyit) olacağı anlamına gelir.
  - Open Interest geçmişi Binance'te ~30 gün ile sınırlıdır; bu pencerenin
    dışında oi_cvd bileşeni nötr kalır.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from src.backtest.metrics import compute_metrics
from src.execution.paper_broker import ClosedTrade, PaperBroker
from src.indicators.cvd import cvd_from_klines_proxy, find_pivots, trades_to_delta_bars
from src.indicators.open_interest import oi_rsi, price_oi_cvd_table
from src.indicators.vwap import anchored_vwap, rolling_vwap
from src.indicators.volume_profile import VolumeProfileResult, build_volume_profile
from src.strategy.risk import DailyLossTracker, calc_stop_target, position_size
from src.strategy.signals import MarketState, evaluate

logger = logging.getLogger("exitpump_bot.backtest")


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    equity_curve: pd.DataFrame
    metrics: dict
    warnings: list[str]
    score_histogram: dict[int, int]
    """Her bar'da ulaşılan HAM confluence skorunun (yön eşiğinden bağımsız) dağılımı.
    min_confluence_score'u anlamlı şekilde ayarlamak için kullan: ör. skor>=3 çok
    nadir çıkıyorsa eşiği düşürmeyi (ya da OI/orderbook verisi eklemeyi) düşün."""


def _latest_divergence_kind(
    price: pd.Series, cvd: pd.Series, price_pivots: pd.Series, reveal_upto_idx: int,
) -> str | None:
    def dedupe(idx: pd.DatetimeIndex, ref_index: pd.Index) -> list:
        if len(idx) == 0:
            return []
        positions = ref_index.get_indexer(idx)
        groups = [[positions[0]]]
        for p in positions[1:]:
            if p - groups[-1][-1] <= 1:
                groups[-1].append(p)
            else:
                groups.append([p])
        return [ref_index[g[len(g) // 2]] for g in groups]

    visible_price_piv = price_pivots.iloc[: reveal_upto_idx + 1]
    highs = dedupe(visible_price_piv[visible_price_piv == 1].index, price.index)
    lows = dedupe(visible_price_piv[visible_price_piv == -1].index, price.index)

    if len(highs) >= 2:
        p_prev, p_curr = price.loc[highs[-2]], price.loc[highs[-1]]
        c_prev, c_curr = cvd.loc[highs[-2]], cvd.loc[highs[-1]]
        if p_curr < p_prev and c_curr > c_prev:
            return "absorption_top"
        if p_curr > p_prev and c_curr < c_prev:
            return "exhaustion_top"
    if len(lows) >= 2:
        p_prev, p_curr = price.loc[lows[-2]], price.loc[lows[-1]]
        c_prev, c_curr = cvd.loc[lows[-2]], cvd.loc[lows[-1]]
        if p_curr > p_prev and c_curr < c_prev:
            return "absorption_bottom"
        if p_curr < p_prev and c_curr > c_prev:
            return "exhaustion_bottom"
    return None


def run_backtest(
    klines: pd.DataFrame, cfg: dict,
    agg_trades: pd.DataFrame | None = None, oi_hist: pd.DataFrame | None = None,
    verbose_every: int = 5000,
) -> BacktestResult:
    warnings: list[str] = []
    primary_tf = cfg["timeframes"]["primary"]

    if len(klines) < 500:
        raise ValueError("Backtest için en az birkaç günlük (>=500 bar) veri gerekli")

    close = klines["close"]

    # --- Vektörel, causal göstergeler ---
    periods = cfg["vwap"]["rolling_periods_days"]
    rv_fast = rolling_vwap(klines, periods[0])
    rv_slow = rolling_vwap(klines, periods[1])

    if agg_trades is not None and not agg_trades.empty:
        bars = trades_to_delta_bars(agg_trades, primary_tf)
        cvd_series = bars["cvd"].reindex(klines.index, method="ffill").bfill().fillna(0)
    else:
        warnings.append("aggTrade verisi verilmedi -> CVD proxy (kline taker_buy_base bazlı) kullanıldı")
        cvd_series = cvd_from_klines_proxy(klines)

    oi_available = oi_hist is not None and not oi_hist.empty
    if oi_available:
        oi_series = oi_hist["sumOpenInterest"].reindex(klines.index, method="ffill")
        oi_rsi_series = oi_rsi(oi_series, cfg["open_interest"]["rsi_period"])
        oi_cvd_table = price_oi_cvd_table(close, oi_series, cvd_series, window=5)
    else:
        warnings.append(
            "Open Interest geçmişi verilmedi/boş (Binance ~30 gün ile sınırlı) -> "
            "oi_cvd confluence bileşeni bu backtest boyunca nötr kaldı"
        )
        oi_cvd_table = pd.DataFrame(index=klines.index, data={
            "state": "flat", "confirmed_short_squeeze": False, "confirmed_long_unwind_risk": False,
        })

    warnings.append(
        "Tarihsel order book/heatmap snapshot'ı yok -> order_book confluence bileşeni "
        "bu backtest boyunca her zaman nötr (canlı paper-trading'de gerçek veriyle çalışır)"
    )

    lookback = cfg["cvd"]["divergence_lookback_bars"]
    price_pivots = find_pivots(close, lookback)

    # --- Adaptif confluence eşiği -------------------------------------------------
    # order_book her zaman, oi_cvd ise OI verisi yoksa nötr kalıyor. Bu durumda
    # config'teki min_confluence_score'a asla ulaşılamayabilir (skor hep 0 kalır,
    # sonsuza dek 0 işlem üretir). Bunu sessizce yaşamak yerine, backtest için
    # ulaşılabilir maksimuma göre bir efektif eşik kullanıyoruz ve durumu açıkça
    # logluyoruz. CANLI modda (main_live.py) 5 bileşen de gerçek veriyle çalışır,
    # dolayısıyla config değeri orada AYNEN uygulanır -- bu ayarlama sadece backtest'e özeldir.
    disabled_components = 1 + (0 if oi_available else 1)  # order_book + (oi_cvd)
    max_achievable_score = 5 - disabled_components
    configured_min_score = cfg["strategy"]["min_confluence_score"]
    effective_min_score = min(configured_min_score, max_achievable_score)
    if effective_min_score < configured_min_score:
        warnings.append(
            f"config.strategy.min_confluence_score={configured_min_score} bu backtest'te "
            f"asla ulaşılamaz (devre dışı bileşenler nedeniyle max mümkün skor={max_achievable_score}). "
            f"Backtest bu çalıştırmada efektif eşik olarak {effective_min_score} kullandı. "
            f"Canlı paper-trading'de (main_live.py) 5 bileşen de aktif olduğundan config değeri aynen geçerlidir."
        )
    cfg_for_eval = {**cfg, "strategy": {**cfg["strategy"], "min_confluence_score": effective_min_score}}

    # --- Broker / risk state ---
    broker = PaperBroker(
        starting_equity=cfg["risk"]["account_equity_usdt"],
        trade_log_csv=cfg["logging"]["trade_log_csv"],
        equity_log_csv=cfg["logging"]["equity_log_csv"],
        max_concurrent_positions=cfg["strategy"]["max_concurrent_positions"],
    )
    daily_tracker = DailyLossTracker(cfg["risk"]["account_equity_usdt"], cfg["risk"]["max_daily_loss_pct"])
    last_trade_close_time = None

    vp_cache: dict[pd.Timestamp, VolumeProfileResult] = {}
    min_bars_for_vp = 200
    n = len(klines)
    score_histogram: dict[int, int] = {i: 0 for i in range(6)}

    for i in range(n):
        ts = klines.index[i]
        row = klines.iloc[i]
        price = float(row["close"])

        # 1) Açık pozisyonu bar içi high/low ile kontrol et (close yerine -- daha gerçekçi)
        if broker.open_positions:
            pos = broker.open_positions[0]
            bar_high, bar_low = float(row["high"]), float(row["low"])
            if pos.direction == "long":
                if bar_low <= pos.stop:
                    closed = broker.force_close(pos, pos.stop, ts.to_pydatetime(), "stop")
                    daily_tracker.register_closed_trade(ts.to_pydatetime(), broker.equity, closed.pnl_usdt)
                    last_trade_close_time = closed.closed_at
                elif bar_high >= pos.target:
                    closed = broker.force_close(pos, pos.target, ts.to_pydatetime(), "target")
                    daily_tracker.register_closed_trade(ts.to_pydatetime(), broker.equity, closed.pnl_usdt)
                    last_trade_close_time = closed.closed_at
            else:
                if bar_high >= pos.stop:
                    closed = broker.force_close(pos, pos.stop, ts.to_pydatetime(), "stop")
                    daily_tracker.register_closed_trade(ts.to_pydatetime(), broker.equity, closed.pnl_usdt)
                    last_trade_close_time = closed.closed_at
                elif bar_low <= pos.target:
                    closed = broker.force_close(pos, pos.target, ts.to_pydatetime(), "target")
                    daily_tracker.register_closed_trade(ts.to_pydatetime(), broker.equity, closed.pnl_usdt)
                    last_trade_close_time = closed.closed_at
        broker.log_equity(price, ts.to_pydatetime())

        if i < min_bars_for_vp:
            continue

        # 2) Volume profile: günde bir kez, O ANA KADARKİ veriyle yeniden hesapla (causal)
        day_key = ts.floor("D")
        if day_key not in vp_cache:
            hist_slice = klines.iloc[: i + 1]
            try:
                vp_cache[day_key] = build_volume_profile(
                    hist_slice, lookback_days=cfg["volume_profile"]["lookback_days"],
                    price_bins=cfg["volume_profile"]["price_bins"],
                    value_area_pct=cfg["volume_profile"]["value_area_pct"],
                )
            except ValueError:
                continue
        vp = vp_cache[day_key]

        # 3) Divergence (delayed reveal: sadece i-lookback'e kadar açığa çıkmış pivotlar)
        reveal_idx = i - lookback
        latest_div = None
        if reveal_idx >= 0:
            latest_div = _latest_divergence_kind(close, cvd_series, price_pivots, reveal_idx)

        # 4) OI+CVD durumu
        if oi_available and i < len(oi_cvd_table) and not pd.isna(oi_rsi_series.iloc[i]):
            price_oi_state = oi_cvd_table["state"].iloc[i]
            confirmed_short_squeeze = bool(oi_cvd_table["confirmed_short_squeeze"].iloc[i])
            confirmed_long_unwind = bool(oi_cvd_table["confirmed_long_unwind_risk"].iloc[i])
        else:
            price_oi_state, confirmed_short_squeeze, confirmed_long_unwind = "flat", False, False

        state = MarketState(
            timestamp=ts.to_pydatetime(), price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            depth_bias="neutral",  # bkz. modül docstring -- backtest'te tarihsel orderbook yok
            latest_cvd_divergence=latest_div, price_oi_state=price_oi_state,
            confirmed_short_squeeze=confirmed_short_squeeze, confirmed_long_unwind_risk=confirmed_long_unwind,
            rvwap_30d=float(rv_fast.iloc[i]) if not pd.isna(rv_fast.iloc[i]) else price,
            rvwap_90d=float(rv_slow.iloc[i]) if not pd.isna(rv_slow.iloc[i]) else price,
        )

        sig = evaluate(state, cfg_for_eval, last_trade_close_time)
        score_histogram[sig.score] = score_histogram.get(sig.score, 0) + 1
        can_trade, _ = daily_tracker.can_trade(ts.to_pydatetime(), broker.equity)

        if sig.is_actionable and can_trade and broker.can_open_new():
            # AVWAP anchor: son açığa çıkmış ZIT yönlü pivota anchor'la (uzun için son dip, kısa için son tepe)
            anchor_time = None
            if reveal_idx >= 0:
                visible = price_pivots.iloc[: reveal_idx + 1]
                target_flag = -1 if sig.direction == "long" else 1
                matches = visible[visible == target_flag]
                if len(matches):
                    anchor_time = matches.index[-1]

            try:
                if anchor_time is not None and cfg["risk"]["stop_method"] == "avwap":
                    avwap_series = anchored_vwap(klines.iloc[: i + 1], anchor_time)
                    avwap_price = float(avwap_series.iloc[-1])
                    st = calc_stop_target(
                        sig.direction, price, cfg["risk"]["reward_risk_ratio"],
                        method="avwap", avwap_price=avwap_price,
                    )
                else:
                    from src.strategy.risk import compute_atr
                    atr_val = compute_atr(klines.iloc[: i + 1], cfg["risk"]["atr_period"]).iloc[-1]
                    if pd.isna(atr_val) or atr_val <= 0:
                        continue
                    st = calc_stop_target(
                        sig.direction, price, cfg["risk"]["reward_risk_ratio"],
                        method="atr", atr_value=float(atr_val), atr_multiplier=cfg["risk"]["atr_multiplier"],
                    )
                size = position_size(broker.equity, cfg["risk"]["risk_per_trade_pct"], st)
                broker.open_position(sig.direction, st, size["qty"], size["notional_usdt"], ts.to_pydatetime(), sig.reasons)
            except (ValueError, ZeroDivisionError) as e:
                logger.debug("Trade açılamadı bar=%s: %s", ts, e)

        if verbose_every and i % verbose_every == 0 and i > 0:
            logger.info("Backtest %d/%d (%s) equity=%.2f", i, n, ts, broker.equity)

    broker.close_all(float(close.iloc[-1]), klines.index[-1].to_pydatetime(), reason="backtest_end")
    metrics = compute_metrics(broker.closed_trades, broker.equity, cfg["risk"]["account_equity_usdt"])
    equity_curve = pd.read_csv(cfg["logging"]["equity_log_csv"])

    bars_at_or_above_threshold = sum(c for score, c in score_histogram.items() if score >= effective_min_score)
    if bars_at_or_above_threshold == 0:
        warnings.append(
            f"Bu veri penceresinde HİÇBİR bar efektif eşiğe (skor>={effective_min_score}) ulaşmadı -> "
            f"0 işlem üretildi. Skor dağılımı: {score_histogram}. Daha fazla işlem görmek istersen "
            f"config.strategy.min_confluence_score değerini düşür ya da daha uzun/volatil bir dönem dene."
        )

    return BacktestResult(
        trades=broker.closed_trades, equity_curve=equity_curve, metrics=metrics,
        warnings=warnings, score_histogram=score_histogram,
    )
