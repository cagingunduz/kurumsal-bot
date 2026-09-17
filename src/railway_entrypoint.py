"""
Railway "web" process giriş noktası.

Railway servisleri sürekli çalışan bir process bekler (çökerse restart döngüsüne
girer). Bu script:
  1) Deploy anında OTOMATİK bir "sağlama" backtest'i çalıştırır (son 7 gün,
     gerçek trade + OI verisiyle) ve sonucu Railway deploy loglarına basar --
     böylece deploy biter bitmez gerçek bir win rate/metrik seti loglarda görünür.
  2) Sonra süreci canlı tutmak için bekler -- bu sayede `railway run` veya
     Railway dashboard'daki "Run a command" ile istediğin zaman farklı
     parametrelerle (--days, --start/--end, --with-agg-trades, --with-oi,
     --symbol vb.) ek backtest'ler tetiklenebilir, örn:

        railway run python -m src.main_backtest --days 30 --with-agg-trades --with-oi

Ortam değişkeni ile otomatik ilk-koşu backtest'ini özelleştirebilirsin:
  ENTRYPOINT_BACKTEST_DAYS   (varsayılan: 7)
  ENTRYPOINT_SKIP_FIRST_RUN  ("1" ise otomatik ilk koşu atlanır)
"""
from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import asyncio
import logging
import os
import time
from pathlib import Path

import yaml

from src.backtest.engine import run_backtest
from src.data.history_loader import fetch_agg_trades_range, fetch_klines_range, fetch_open_interest_hist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("exitpump_bot.railway_entrypoint")


async def _run_startup_backtest():
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())

    days = int(os.environ.get("ENTRYPOINT_BACKTEST_DAYS", "7"))
    symbol = cfg["exchange"]["symbol"]
    interval = cfg["timeframes"]["primary"]
    provider = cfg["exchange"].get("provider", "okx")
    okx_oi_ccy = cfg["exchange"].get("okx_oi_ccy", "BTC")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    logger.info("=" * 70)
    logger.info("exitpump_bot Railway deploy -- otomatik sağlama backtest'i")
    logger.info("provider=%s symbol=%s interval=%s days=%s", provider, symbol, interval, days)
    logger.info("=" * 70)

    try:
        klines = await fetch_klines_range(symbol, interval, start_ms, end_ms, provider=provider)
        if klines.empty:
            logger.error("Kline verisi boş döndü -- exchange erişimini/sembolü kontrol et.")
            return
        logger.info("Kline: %d bar indirildi (%s -> %s)", len(klines), klines.index.min(), klines.index.max())

        agg_trades = await fetch_agg_trades_range(symbol, start_ms, end_ms, provider=provider)
        logger.info("Gerçek trade: %d satır indirildi", len(agg_trades) if agg_trades is not None else 0)

        oi_hist = await fetch_open_interest_hist(
            symbol, period="1h", start_time_ms=start_ms, end_time_ms=end_ms,
            provider=provider, okx_ccy=okx_oi_ccy,
        )
        logger.info("OI geçmişi: %d satır indirildi", len(oi_hist) if oi_hist is not None else 0)

        result = run_backtest(klines, cfg, agg_trades=agg_trades, oi_hist=oi_hist)

        logger.info("-" * 70)
        logger.info("BACKTEST SONUÇLARI (%s %s, son %s gün)", symbol, interval, days)
        for k, v in result.metrics.items():
            logger.info("  %-22s: %s", k, v)
        logger.info("  Skor dağılımı: %s", result.score_histogram)
        for w in result.warnings:
            logger.warning("  UYARI: %s", w)
        logger.info("-" * 70)
    except Exception:
        logger.exception("Otomatik sağlama backtest'i başarısız oldu -- exchange erişimi/ağ/SSL kontrol et.")


def main():
    if os.environ.get("ENTRYPOINT_SKIP_FIRST_RUN") != "1":
        asyncio.run(_run_startup_backtest())
    else:
        logger.info("ENTRYPOINT_SKIP_FIRST_RUN=1 -- otomatik ilk koşu atlandı.")

    logger.info(
        "Servis ayakta kalıyor. Ek backtest için: "
        "railway run python -m src.main_backtest --days 30 --with-agg-trades --with-oi"
    )
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
