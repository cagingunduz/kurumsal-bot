#!/usr/bin/env bash
# Binance Data Vision'dan (data.binance.vision) GÜNLÜK kline + aggTrades zip'lerini indirir.
#
# Bu tamamen ÜCRETSİZ, API key GEREKTİRMEYEN, statik dosya indirmedir (Binance'in
# kendi CDN'inden). Python'un kendi ağ/SSL sorunu yaşadığı makinelerde işe yarar,
# çünkü indirme işini burada `curl` yapıyor (senin makinende zaten çalıştığını
# doğruladık) -- bot ise sadece diske inmiş dosyaları okuyacak, hiç HTTPS isteği
# atmayacak.
#
# Kullanım:
#   ./scripts/download_data_vision.sh BTCUSDT 2026-08-18 2026-09-16 ./data_cache/raw
#
# Argümanlar: SYMBOL START_DATE(YYYY-MM-DD) END_DATE(YYYY-MM-DD) [OUT_DIR]
#
# Not: aggTrades indirmek istemezsen üçüncü pozisyonel argümandan sonra
# SKIP_AGGTRADES=1 ./scripts/download_data_vision.sh ... şeklinde çalıştırabilirsin
# (aggTrades dosyaları klines'a göre çok daha büyüktür, ilk denemede atlamak isteyebilirsin).

set -euo pipefail

SYMBOL="${1:?Kullanım: $0 SYMBOL START_DATE END_DATE [OUT_DIR]}"
START_DATE="${2:?START_DATE (YYYY-MM-DD) gerekli}"
END_DATE="${3:?END_DATE (YYYY-MM-DD) gerekli}"
OUT_DIR="${4:-./data_cache/raw}"
MARKET="um"      # USDT-margined futures (BTCUSDT gibi perp'ler için doğru olan bu)
INTERVAL="1m"
SKIP_AGGTRADES="${SKIP_AGGTRADES:-0}"

BASE="https://data.binance.vision/data/futures/${MARKET}/daily"
KLINES_DIR="${OUT_DIR}/klines/${SYMBOL}"
AGG_DIR="${OUT_DIR}/aggTrades/${SYMBOL}"
mkdir -p "$KLINES_DIR" "$AGG_DIR"

# macOS'un tarih komutu (BSD date) GNU date'ten farklı; ikisini de destekle.
next_day() {
  if date -j -f "%Y-%m-%d" "$1" "+%Y-%m-%d" >/dev/null 2>&1; then
    date -j -v+1d -f "%Y-%m-%d" "$1" "+%Y-%m-%d"          # macOS (BSD date)
  else
    date -d "$1 + 1 day" "+%Y-%m-%d"                        # Linux (GNU date)
  fi
}

echo "== Binance Data Vision indirme: $SYMBOL $START_DATE -> $END_DATE =="
echo "   Klines hedefi:    $KLINES_DIR"
echo "   aggTrades hedefi: $AGG_DIR (SKIP_AGGTRADES=$SKIP_AGGTRADES)"

d="$START_DATE"
total=0
failed=0
while [[ "$d" < "$END_DATE" || "$d" == "$END_DATE" ]]; do
  kline_zip="${SYMBOL}-${INTERVAL}-${d}.zip"
  kline_url="${BASE}/klines/${SYMBOL}/${INTERVAL}/${kline_zip}"
  if [[ ! -f "${KLINES_DIR}/${SYMBOL}-${INTERVAL}-${d}.csv" ]]; then
    echo "-> kline $d indiriliyor..."
    if curl -sSf -o "${KLINES_DIR}/${kline_zip}" "$kline_url"; then
      unzip -oq "${KLINES_DIR}/${kline_zip}" -d "$KLINES_DIR"
      rm -f "${KLINES_DIR}/${kline_zip}"
      total=$((total+1))
    else
      echo "   UYARI: $kline_url indirilemedi (o gün için veri olmayabilir)"
      failed=$((failed+1))
    fi
  fi

  if [[ "$SKIP_AGGTRADES" != "1" ]]; then
    agg_zip="${SYMBOL}-aggTrades-${d}.zip"
    agg_url="${BASE}/aggTrades/${SYMBOL}/${agg_zip}"
    if [[ ! -f "${AGG_DIR}/${SYMBOL}-aggTrades-${d}.csv" ]]; then
      echo "-> aggTrades $d indiriliyor (büyük olabilir)..."
      if curl -sSf -o "${AGG_DIR}/${agg_zip}" "$agg_url"; then
        unzip -oq "${AGG_DIR}/${agg_zip}" -d "$AGG_DIR"
        rm -f "${AGG_DIR}/${agg_zip}"
      else
        echo "   UYARI: $agg_url indirilemedi"
      fi
    fi
  fi

  d="$(next_day "$d")"
done

echo "== Bitti: $total yeni kline dosyası indirildi, $failed hata =="
echo "Şimdi şunu çalıştırabilirsin:"
echo "  python -m src.main_backtest --days 30 --data-dir ${OUT_DIR}"
