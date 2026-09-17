# exitpump_bot

@exitpumpBTC'nin X'te paylaştığı 5 makaleden (VWAP, Order Book/Heatmap, CVD,
Open Interest, Auction Market Theory) çıkarılan metodolojiyi kural-tabanlı bir
confluence (çoklu teyit) sistemine dönüştüren bir **backtest + paper-trading**
botu. **Varsayılan veri kaynağı OKX** (`BTC-USDT-SWAP`, public API, key
gerektirmez) — Binance Futures (BTCUSDT) de `config.yaml → exchange.provider`
ile alternatif olarak seçilebilir.

> **Neden OKX varsayılan?** Binance'e (fapi.binance.com) giden Python
> isteklerinde bazı makinelerde (antivirüs/VPN'in HTTPS trafiğine araya
> girdiği durumlarda) SSL sertifika hatası görülüyor; OKX farklı bir host
> olduğu için bu sorunu bypass edebiliyor. `curl` gibi araçlar zaten
> etkilenmiyordu, sorun Python'un sertifika zinciriyle ilgiliydi.

> **ÖNEMLİ:** Bu proje varsayılan olarak ve şu haliyle **sadece paper (simüle)
> trading** yapar. Gerçek para ile emir göndermez, borsaya gerçek API
> anahtarıyla bağlanmaz. Bu bir yatırım tavsiyesi değildir, finansal
> danışmanlık değildir. Algoritmik trading ciddi sermaye kaybı riski taşır.
> Kodun mantığını, varsayımlarını ve sınırlarını (aşağıda) anlamadan gerçek
> parayla kullanma.

## Neden bu mimari?

exitpump'ın makalelerinin ortak teması **tek bir göstergeye güvenmemek**.
Bot bu yüzden 5 bağımsız "oy"u birleştiren bir confluence motoru olarak
tasarlandı (`src/strategy/confluence.py`):

1. **value_area** — AMT/Volume Profile: fiyat VAH'a mı (short ucu) VAL'e mi
   (long ucu) yakın, yoksa değer alanının "choppy" ortasında mı (Law 6: orta
   bölgede işlem açma).
2. **cvd** — CVD divergence: absorption/exhaustion (tepe/dipte agresyonun
   gerçek olup olmadığı).
3. **order_book** — Order book depth delta (%25 aralık, exitpump'ın makalede
   belirttiği reversal ayarı): üstte satış duvarı mı, altta alış duvarı mı var.
4. **oi_cvd** — Fiyat + Open Interest + CVD "cheat sheet": yeni pozisyon
   girişi mi, likidasyon mu, short/long squeeze mi.
5. **htf_trend** — 30G/90G Rolling VWAP'e göre rejim filtresi.

`config.yaml` → `strategy.min_confluence_score` kaç oy gerektiğini belirler
(varsayılan 3/5). Bu sayı ne kadar yüksekse bot o kadar seyrek ama o kadar
"temkinli" işlem açar — tıpkı exitpump'ın "tek göstergeye güvenme" yaklaşımı
gibi.

## Mimari / dosya yapısı

```
config.yaml                  Tüm ayarlar (semboller, gösterge parametreleri, risk, vs.)
src/
  data/
    okx_client.py             OKX v5 REST + WebSocket istemcisi (VARSAYILAN, public endpoint'ler)
    binance_client.py        Binance Futures REST + WebSocket istemcisi (alternatif, public endpoint'ler)
    data_vision_loader.py     Diske indirilmiş Binance Data Vision CSV'lerini okuyan yerel (ağsız) yükleyici
    history_loader.py        Backtest için tarihsel veri indirme + parquet önbellek (provider="okx"|"binance")
  indicators/
    vwap.py                  Session/Anchored/Rolling/Multi-Period VWAP + bantlar
    orderbook.py              Depth delta, heatmap yardımcıları, büyük emir tespiti
    cvd.py                    CVD hesaplama + absorption/exhaustion divergence tespiti
    open_interest.py          OI RSI/Z-Score, Fiyat+OI+CVD sınıflandırma tablosu
    volume_profile.py         AMT: POC/VAH/VAL, balance/imbalance, "10 kanun"dan pratik olanlar
  strategy/
    confluence.py             5 bileşenli oylama motoru
    signals.py                MarketState -> TradeSignal (makro blackout + cooldown dahil)
    risk.py                   Stop/target (AVWAP ya da ATR), pozisyon boyutlandırma, günlük zarar limiti
  execution/
    paper_broker.py           Sanal (paper) emir yürütme, pozisyon/eşitlik takibi, CSV loglama
  backtest/
    engine.py                 Event-driven backtest (lookahead-güvenli, main_live ile aynı karar zinciri)
    metrics.py                Win rate, profit factor, drawdown, vb.
  notify/
    telegram_bot.py           Opsiyonel Telegram bildirimleri
  main_backtest.py             Backtest CLI
  main_live.py                  Canlı paper-trading ana döngüsü (WebSocket, şu an Binance'e bağlı)
  railway_entrypoint.py         Railway "web" process girişi (deploy'da otomatik backtest + servis canlı kalır)
Procfile / railway.json / .python-version    Railway deploy ayarları (bkz. "Railway'e deploy" bölümü)
.env.example                    Ortam değişkeni şablonu (OKX/Binance/Telegram key'leri)
```

**Tasarım ilkesi:** `main_backtest.py` ve `main_live.py` AYNI karar zincirini
(`indicators/* → strategy/confluence.py + signals.py → strategy/risk.py →
execution/paper_broker.py`) kullanır. Bu, "backtest'te iyi görünüp canlıda
farklı davranan bot" hatasını yapısal olarak engeller.

## Kurulum

```bash
cd exitpump_bot
python3 -m venv .venv && source .venv/bin/activate   # opsiyonel ama önerilir
pip install -r requirements.txt
```

Python 3.10+ gerekli (`X | Y` tip union syntax'ı kullanılıyor).

## Backtest çalıştırma

```bash
python -m src.main_backtest --days 30
python -m src.main_backtest --start 2026-06-01 --end 2026-07-01 --with-agg-trades --with-oi
python -m src.main_backtest --days 14 --min-score 2   # daha fazla işlem görmek için eşiği geçici düşür
```

**OKX ile `--with-agg-trades` neredeyse zorunlu:** OKX'in kline (candle)
verisi Binance'in aksine alıcı/satıcı hacim ayrımı (`taker_buy_base`)
vermez, bu yüzden `--with-agg-trades` verilmeden OKX ile backtest alırsan
CVD sinyali nötr/düz kalır (bot bunu bir uyarı olarak loglar). `okx_client.py`
gerçek trade'leri (`side` alanı doğrudan `buy`/`sell`) kullanarak daha
isabetli bir CVD üretir — bu yüzden OKX'te gerçek kullanım şu şekilde olmalı:

```bash
python -m src.main_backtest --days 14 --with-agg-trades --with-oi
```

Binance'e dönmek istersen `config.yaml`: `exchange.provider: binance`,
`exchange.symbol: BTCUSDT` yap.

Çıktı: konsolda metrikler (win rate, profit factor, max drawdown, ...),
`logs/equity_curve.png`, `logs/backtest_trades_summary.csv`,
`logs/paper_trades.csv`, `logs/equity_curve.csv`.

### Skor histogramı ve "0 işlem" durumu

Backtest çıktısındaki `score_histogram` her bar'da ulaşılan HAM confluence
skorunun dağılımını gösterir. `order_book` bileşeni backtest'te veri
eksikliği yüzünden **her zaman nötr**dür (bkz. "Veri kısıtları"), bu yüzden
bot otomatik olarak ulaşılabilir maksimuma göre bir **efektif eşik**
kullanır ve bunu uyarı olarak yazdırır. Yine de 0 işlem görürsen `--min-score`
ile eşiği düşürüp dene, ya da daha volatil/uzun bir dönem seç.

## Canlı paper-trading çalıştırma

```bash
python -m src.main_live
```

Başlangıçta `config.yaml → vwap.rolling_periods_days` içindeki en uzun
periyoda göre (varsayılan 365 gün) tarihsel veri indirir (**bu işlem
büyük veri hacmi yüzünden uzun sürebilir ve disk/bellek kullanır** — daha
hafif bir kurulum istersen `rolling_periods_days`'i örn. `[7, 30]` yap).
Sonrasında Binance Futures WebSocket'ine bağlanıp her 1 dakikalık mum
kapanışında sinyal üretir, uygunsa sanal (paper) pozisyon açar/kapatır ve
(varsa) Telegram'a bildirim gönderir.

Durdurmak için `Ctrl+C`.

## Railway'e deploy etme

Bu proje Railway'de bir "web" servisi olarak çalışacak şekilde hazırlandı
(`Procfile`, `railway.json`, `.python-version`). Adımlar:

1. **Kodu bir GitHub reposuna it** (Railway'in en pratik deploy yolu GitHub
   entegrasyonu üzerinden). Boş bir repo oluşturup bu klasörü push et:
   ```bash
   cd exitpump_bot
   git init && git add . && git commit -m "exitpump_bot ilk sürüm"
   git branch -M main
   git remote add origin <senin-repo-url'in>
   git push -u origin main
   ```
2. Railway dashboard'unda **New → Deploy from GitHub repo** ile bu reponu
   seç. Railway, `railway.json`'daki Nixpacks build ayarlarını ve
   `Procfile`'daki `web: python -m src.railway_entrypoint` başlangıç
   komutunu otomatik algılar.
3. **Variables** sekmesinde (backtest için ZORUNLU DEĞİL ama ileride
   canlıya geçersen gerekir) `.env.example`'daki değişkenleri gir —
   özellikle OKX'ten aldığın key'i **koda değil buraya**:
   ```
   OKX_API_KEY=...
   OKX_API_SECRET=...
   OKX_API_PASSPHRASE=...
   ```
   Not: Bu bot şu an sadece OKX'in PUBLIC market data endpoint'lerini
   kullanıyor (backtest/paper mod), yani bu değişkenler olmadan da
   backtest çalışır. Onları girmen sadece ileride private/live
   endpoint'lere geçilirse işe yarar.
4. Deploy tamamlandığında `railway_entrypoint.py` otomatik olarak **son 7
   günlük bir sağlama backtest'i** çalıştırıp sonucu (win rate, profit
   factor, vb.) **Deploy Logs**'a basar, sonra servisi ayakta tutmak için
   bekler. Farklı bir aralık/parametre ile ek backtest çalıştırmak için:
   ```bash
   railway run python -m src.main_backtest --days 30 --with-agg-trades --with-oi
   ```
   ya da Railway dashboard'daki servis sayfasında **"Run a command"**
   seçeneğini kullan. Otomatik ilk-koşu backtest'inin gün sayısını
   `ENTRYPOINT_BACKTEST_DAYS` env var'ı ile değiştirebilirsin.
5. Sonuçları görmek için: Railway dashboard → servis → **Deploy Logs**
   (otomatik ilk koşu) veya **Logs** (manuel `railway run` çıktısı).

## Telegram bildirimleri (opsiyonel)

```yaml
notify:
  telegram:
    enabled: true
```

ve ortam değişkenleri:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

## Config'in önemli alanları (`config.yaml`)

- `strategy.min_confluence_score`: kaç bileşen aynı yönde oy vermeli (1-5).
- `risk.risk_per_trade_pct`, `risk.max_daily_loss_pct`: sermaye koruması.
- `risk.stop_method`: `"avwap"` (son zıt yönlü swing pivotuna anchor'lı VWAP,
  makaledeki yöntem) ya da `"atr"` (klasik volatilite stopu, AVWAP hesaplanamazsa
  otomatik fallback).
- `macro_blackout.events_utc`: FOMC gibi olayların UTC zaman damgalarını
  buraya elle ekle — bot bu pencerede yeni sinyal üretmez (exitpump'ın
  "FOMC öncesi pozisyon almam" davranışının karşılığı). **Otomatik bir
  ekonomik takvim entegrasyonu YOKTUR**, elle güncellemen gerekir.

## Veri kısıtları (dürüstçe, önemli)

- **Order book/heatmap geçmişi**: Ne Binance ne OKX geçmiş order book
  snapshot'larını saklamaz/sunmaz. Backtest'te `order_book` bileşeni bu
  yüzden her zaman nötr oy verir; sadece `main_live.py` çalışırken gerçek,
  anlık order book verisiyle çalışır. Bu, backtest sonuçlarının canlı
  davranıştan biraz farklı (daha az teyit imkânı) olabileceği anlamına gelir.
- **Open Interest geçmişi**:
  - Binance'in `openInterestHist` endpoint'i genelde yalnızca son ~30 günü tutar.
  - OKX'in geçmiş OI istatistik ucu (`/api/v5/rubik/stat/contracts/open-interest-volume`)
    belirli bir enstrümana (`BTC-USDT-SWAP`) değil, **`ccy` (ör. "BTC")
    bazında TÜM sözleşmelerin toplam OI'sini** döndürür — yani Binance'in
    endpoint'iyle birebir eşdeğer değildir, biraz daha kaba bir yaklaşımdır.
  - Her iki durumda da daha eski dönemler için `oi_cvd` bileşeni backtest'te nötr kalır.
- **CVD**: `--with-agg-trades` verilmezse gerçek trade-bazlı CVD yerine
  kline'lardan türetilen bir **yaklaşık proxy** kullanılır. Binance'te bu
  proxy kline'ın kendi `taker_buy_base` kolonunu kullanır (orta düzeyde
  isabetli); **OKX'te ise kline'da bu ayrım hiç yok**, bu yüzden proxy
  `taker_buy_base = volume/2` ile tamamen NÖTR/DÜZ bırakılır — OKX ile
  anlamlı bir CVD sinyali için `--with-agg-trades` kullanmak neredeyse
  zorunludur (bkz. `okx_client.py` docstring'i).
- **AVWAP anchor (otomatik)**: Makalede "swing high/low'a anchor'la" deniyor;
  bot bunu otomatikleştirmek için son *açığa çıkmış* (lookahead-güvenli)
  zıt yönlü fiyat pivotunu kullanıyor. Bu, insan bir trader'ın gözle
  seçtiği "önemli" swing noktasından daha mekanik/gürültülü olabilir.
- **OKX istemcisi canlı test edilemedi**: Bu geliştirme ortamının (sandbox)
  ağ erişimi hem Binance'e hem OKX'e hem Railway'e kapalı (kurumsal
  allowlist) — yani `okx_client.py` **gerçek bir OKX isteğiyle uçtan uca
  test edilemedi**. Alan adları/pagination mantığı OKX'in genel v5 şemasına
  göre yazıldı, `tests/test_okx_client.py` ile sabit (fixture) JSON
  şekilleri üzerinden parse mantığı doğrulandı, ve sentetik OKX-şekilli veri
  `run_backtest`'e uçtan uca başarıyla beslendi (bkz. `tests/`) — ama gerçek
  API yanıtındaki alan adlarında ufak bir uyuşmazlık çıkma ihtimali var.
  Railway'de (gerçek ağ erişimi olan bir ortamda) ilk çalıştırmada küçük bir
  aralıkla (`--days 2`) dene; bir `KeyError` ya da beklenmeyen şema
  hatası görürsen loglardaki hata mesajını bana yapıştır, hemen düzeltirim.
  Binance tarafı ise gerçek borsa bağlantısıyla test edilemedi ama en
  azından kullanıcı `curl` ile `fapi.binance.com`'a başarıyla bağlandığını
  doğrulamıştı (sorun sadece Python'un yerel SSL zincirindeydi).

## Canlıya geçiş (testnet/gerçek para) — YOK, bilinçli olarak

Bu proje **gerçek emir gönderen bir execution katmanı içermez**. `config.yaml`
içindeki `execution.mode` sadece `"paper"` değeriyle çalışır;
`main_live.py` başka bir değer görürse çalışmayı reddeder. Bunun nedenleri:

1. Gerçek para ile trading, API anahtarı güvenliği, kaldıraç/likidasyon
   riski, borsa emir tipi nüansları (post-only, reduce-only, slippage) gibi
   konularda ayrıca dikkatli bir mühendislik ve **senin bilinçli
   onayın** gerektiriyor.
2. Buradaki confluence eşiği/parametreler henüz gerçek geçmiş veriyle
   (özellikle gerçek order book + uzun OI geçmişiyle) doğrulanmadı.

Eğer ileride testnet/gerçek moda geçmek istersen: `src/execution/` altına
`binance_testnet.py` / `binance_live.py` gibi, `PaperBroker` ile aynı arayüze
(`open_position`, `update_mark`, `force_close`) sahip yeni bir broker sınıfı
eklemen, `ccxt` ya da Binance'in resmi `futures` REST imzalı endpoint'lerini
kullanman ve **önce haftalarca testnet'te, sonra küçük gerçek sermayeyle**
test etmen gerekir. Bunu yaparken en azından: pozisyon boyutu üst sınırı,
API hata/timeout durumunda "fail-safe" (pozisyon açmama) davranışı, ve
borsa tarafında da bir stop-loss emri (sadece bot içi mantığa güvenme)
eklemeni şiddetle öneririm.

## Sınırlamalar / bilinen basitleştirmeler

- Divergence (pivot) tespiti kural-tabanlı basit bir yöntemdir, akademik bir
  swing-detection algoritması değildir.
- Volume Profile, tick-level veri yerine her mumun hacmini kendi high-low
  aralığına eşit dağıtarak yaklaşık hesaplanır (tam TPO/tick verisi değil).
- Tek sembol (varsayılan BTCUSDT), tek eşzamanlı pozisyon (config'ten
  değiştirilebilir) için tasarlandı; çoklu sembol/pozisyon desteği yoktur.
- Ekonomik takvim entegrasyonu yok, `macro_blackout.events_utc` elle
  doldurulmalı.

## Lisans / sorumluluk reddi

Bu kod eğitim ve araştırma amaçlıdır. Yatırım tavsiyesi değildir. Yazar(lar)
bu kodun kullanımından doğacak hiçbir finansal kayıptan sorumlu tutulamaz.
