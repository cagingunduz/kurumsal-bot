"""
Backtest CLI. Örnek kullanım:

    python -m src.main_backtest --days 30
    python -m src.main_backtest --start 2026-06-01 --end 2026-07-01 --with-agg-trades
    python -m src.main_backtest --days 20 --min-score 2   # config'i geçici override eder

--with-agg-trades: gerçek CVD için aggTrade indirir (yavaş olabilir, uzun aralıklarda
küçük parçalara böl). Verilmezse kline tabanlı CVD proxy kullanılır (bkz. engine.py).
--with-oi: Binance'in tuttuğu (yakın geçmiş, ~30 gün) OI verisini indirir.
"""
from __future__ import annotations

try:
    # macOS'ta (ve bazı kurumsal/antivirüs ağlarında) Python'un kendi sertifika
    # listesi (certifi) sistem anahtarlığından habersizdir; bir güvenlik/VPN
    # yazılımı HTTPS trafiğine kendi sertifikasıyla araya giriyorsa (Safari/Chrome/
    # curl bunu sistem anahtarlığından güvenilir bulur ama Python bulmaz) bu,
    # "self-signed certificate in certificate chain" hatasına yol açar. truststore,
    # Python'un ssl modülünü işletim sisteminin GERÇEK güven deposunu kullanacak
    # şekilde yamalar -- curl/tarayıcı ile aynı sertifikalara güvenmesini sağlar.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import yaml

from src.backtest.engine import run_backtest
from src.data.history_loader import fetch_agg_trades_range, fetch_klines_range, fetch_open_interest_hist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("exitpump_bot.main_backtest")


def parse_args():
    p = argparse.ArgumentParser(description="exitpump_bot backtest CLI")
    p.add_argument("--days", type=int, default=30, help="Kaç gün geriye gidilecek (start/end verilmezse)")
    p.add_argument("--start", type=str, default=None, help="YYYY-MM-DD (UTC)")
    p.add_argument("--end", type=str, default=None, help="YYYY-MM-DD (UTC)")
    p.add_argument("--symbol", type=str, default=None, help="Varsayılan: config.yaml exchange.symbol")
    p.add_argument("--interval", type=str, default=None, help="Varsayılan: config.yaml timeframes.primary")
    p.add_argument("--with-agg-trades", action="store_true", help="Gerçek aggTrade tabanlı CVD kullan (yavaş)")
    p.add_argument("--with-oi", action="store_true", help="Binance OI geçmişini kullan (~30 gün limit)")
    p.add_argument("--min-score", type=int, default=None, help="config.strategy.min_confluence_score'u geçici override et")
    p.add_argument("--config", type=str, default=str(Path(__file__).resolve().parents[1] / "config.yaml"))
    p.add_argument("--out-dir", type=str, default=str(Path(__file__).resolve().parents[1] / "logs"))
    return p.parse_args()


async def _amain():
    args = parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())

    symbol = args.symbol or cfg["exchange"]["symbol"]
    interval = args.interval or cfg["timeframes"]["primary"]
    provider = cfg["exchange"].get("provider", "binance")
    okx_oi_ccy = cfg["exchange"].get("okx_oi_ccy", "BTC")
    if args.min_score is not None:
        cfg["strategy"]["min_confluence_score"] = args.min_score

    if args.end:
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start_dt = end_dt - timedelta(days=args.days)

    start_ms, end_ms = int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)
    logger.info("[%s] Kline indiriliyor: %s %s [%s -> %s]", provider, symbol, interval, start_dt.date(), end_dt.date())
    klines = await fetch_klines_range(symbol, interval, start_ms, end_ms, provider=provider)
    if klines.empty:
        raise SystemExit("Kline verisi boş döndü -- ağ erişimini ve sembolü kontrol et.")

    agg_trades = None
    if args.with_agg_trades:
        logger.info("Gerçek trade/aggTrade verisi indiriliyor (yavaş olabilir)...")
        agg_trades = await fetch_agg_trades_range(symbol, start_ms, end_ms, provider=provider)
    elif provider == "okx":
        logger.warning(
            "OKX ile --with-agg-trades vermeden backtest alıyorsun: OKX kline'ları "
            "taker_buy_base içermediği için CVD proxy'si NÖTR/DÜZ kalacak. "
            "Gerçek CVD sinyali için --with-agg-trades eklemen şiddetle önerilir."
        )

    oi_hist = None
    if args.with_oi:
        logger.info("Open Interest geçmişi indiriliyor (%s, yakın geçmişle sınırlı)...", provider)
        oi_hist = await fetch_open_interest_hist(
            symbol, period="1h", start_time_ms=start_ms, end_time_ms=end_ms,
            provider=provider, okx_ccy=okx_oi_ccy,
        )

    result = run_backtest(klines, cfg, agg_trades=agg_trades, oi_hist=oi_hist)

    print("\n" + "=" * 70)
    print(f"BACKTEST SONUÇLARI  {symbol} {interval}  [{start_dt.date()} -> {end_dt.date()}]")
    print("=" * 70)
    for k, v in result.metrics.items():
        print(f"  {k:22s}: {v}")
    print(f"\n  Skor dağılımı (bar sayısı): {result.score_histogram}")
    if result.warnings:
        print("\n  UYARILAR:")
        for w in result.warnings:
            print(f"   - {w}")
    print("=" * 70)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not result.equity_curve.empty:
        eq = result.equity_curve.copy()
        eq["timestamp"] = pd.to_datetime(eq["timestamp"])
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(eq["timestamp"], eq["equity"], linewidth=1.2)
        ax.set_title(f"exitpump_bot equity curve -- {symbol} {interval}")
        ax.set_xlabel("Zaman (UTC)")
        ax.set_ylabel("Equity (USDT)")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        out_path = out_dir / "equity_curve.png"
        fig.savefig(out_path, dpi=140)
        logger.info("Equity eğrisi kaydedildi: %s", out_path)

    trades_path = out_dir / "backtest_trades_summary.csv"
    pd.DataFrame([{
        "id": t.id, "direction": t.direction, "entry": t.entry_price, "exit": t.exit_price,
        "qty": t.qty, "opened_at": t.opened_at, "closed_at": t.closed_at,
        "exit_reason": t.exit_reason, "pnl_usdt": t.pnl_usdt, "pnl_pct": t.pnl_pct,
    } for t in result.trades]).to_csv(trades_path, index=False)
    logger.info("İşlem özeti kaydedildi: %s", trades_path)


def main():
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
