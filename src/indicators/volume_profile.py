"""
Auction Market Theory (AMT) / Volume Profile göstergeleri -- exitpump'ın "AMT Guide" makalesi.

Hesaplananlar:
  - POC (Point of Control): en çok hacmin işlem gördüğü fiyat seviyesi
  - VAH / VAL (Value Area High/Low): hacmin ~%68-70'inin işlem gördüğü aralığın sınırları
  - Balance / Imbalance sınıflandırması: fiyat VAH-VAL arasındaysa "balance" (dengede,
    iki yönlü işlem / kabul), dışındaysa "imbalance" (keşif / yönlü hareket)

exitpump'ın "A+ setup"ı: VAH'ta short, VAL'de long ara ("ispatlanana kadar" - yani
fiyat değer alanını net kırıp yeni bir alan oluşturana dek bu mean-reversion geçerli).
"10 Kural"dan öne çıkanlar burada da fonksiyon olarak yer alıyor (bkz. law_* fonksiyonları).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


@dataclass
class VolumeProfileResult:
    poc: float
    vah: float
    val: float
    price_bins: np.ndarray
    volume_by_bin: np.ndarray


def build_volume_profile(
    df: pd.DataFrame, lookback_days: int | None = None, price_bins: int = 100,
    value_area_pct: float = 0.70,
) -> VolumeProfileResult:
    """df: 'high','low','close','volume' kolonlu OHLCV. lookback_days verilirse
    son N günün verisiyle sınırlanır (exitpump'ın '9 günlük composite' örneği gibi).
    """
    window = df if lookback_days is None else df.loc[df.index >= df.index.max() - pd.Timedelta(days=lookback_days)]
    if window.empty:
        raise ValueError("Volume profile için boş veri penceresi")

    lo, hi = window["low"].min(), window["high"].max()
    if hi <= lo:
        hi = lo + 1e-8
    edges = np.linspace(lo, hi, price_bins + 1)
    vol_per_bin = np.zeros(price_bins)

    # Her mumun hacmini, o mumun high-low aralığına düşen bin'lere eşit dağıt
    # (basitleştirilmiş TPO/Volume Profile yaklaşımı -- tick-level veri gerektirmez)
    highs = window["high"].to_numpy()
    lows = window["low"].to_numpy()
    vols = window["volume"].to_numpy()
    bin_width = (hi - lo) / price_bins

    for h, l, v in zip(highs, lows, vols):
        lo_bin = max(0, int((l - lo) / bin_width))
        hi_bin = min(price_bins - 1, int((h - lo) / bin_width))
        n_bins = hi_bin - lo_bin + 1
        if n_bins <= 0:
            continue
        vol_per_bin[lo_bin : hi_bin + 1] += v / n_bins

    poc_idx = int(np.argmax(vol_per_bin))
    poc_price = (edges[poc_idx] + edges[poc_idx + 1]) / 2.0

    # Value area'yı POC'tan dışa doğru genişleterek bul (klasik Market Profile yöntemi)
    total_vol = vol_per_bin.sum()
    target_vol = total_vol * value_area_pct
    included = {poc_idx}
    acc_vol = vol_per_bin[poc_idx]
    lo_i, hi_i = poc_idx, poc_idx
    while acc_vol < target_vol and (lo_i > 0 or hi_i < price_bins - 1):
        vol_below = vol_per_bin[lo_i - 1] if lo_i > 0 else -1
        vol_above = vol_per_bin[hi_i + 1] if hi_i < price_bins - 1 else -1
        if vol_above >= vol_below:
            hi_i += 1
            acc_vol += vol_per_bin[hi_i]
            included.add(hi_i)
        else:
            lo_i -= 1
            acc_vol += vol_per_bin[lo_i]
            included.add(lo_i)

    val_price = edges[lo_i]
    vah_price = edges[hi_i + 1]

    return VolumeProfileResult(
        poc=poc_price, vah=vah_price, val=val_price,
        price_bins=edges, volume_by_bin=vol_per_bin,
    )


def classify_balance_imbalance(price: float, vah: float, val: float) -> Literal["balance", "imbalance_above", "imbalance_below"]:
    if price > vah:
        return "imbalance_above"
    if price < val:
        return "imbalance_below"
    return "balance"


def acceptance_or_rejection(
    prices: pd.Series, level: float, hold_bars: int = 3, tolerance_pct: float = 0.1
) -> Literal["acceptance", "rejection", "undetermined"]:
    """Fiyat bir seviyeyi (VAH/VAL/POC) kırdıktan sonra 'hold_bars' boyunca orada
    kalıyorsa 'acceptance' (kabul), hızla geri dönüyorsa 'rejection' (red) sayılır."""
    if len(prices) < hold_bars + 1:
        return "undetermined"
    broke_above = prices.iloc[0] > level
    recent = prices.iloc[1 : hold_bars + 1]
    tol = level * tolerance_pct / 100.0
    if broke_above:
        held = (recent > level - tol).all()
    else:
        held = (recent < level + tol).all()
    return "acceptance" if held else "rejection"


# --- "10 Kural"dan pratik, kodlanabilir olanlar -------------------------------------------


def law1_traverse_value_area(price: float, vah: float, val: float, prior_close_outside: bool) -> bool:
    """Kural 1: Fiyat değer alanına (yeniden) kabul edilirse, tüm alanı (VAL<->VAH) kat etmeyi dener."""
    return prior_close_outside and (val <= price <= vah)


def law2_breakout_continuation(price: float, vah: float, val: float, accepted_outside: bool) -> Literal["bullish_continuation", "bearish_continuation", "none"]:
    """Kural 2: Değer alanı dışında kabul (acceptance) -> kırılımın devamı beklenir."""
    if not accepted_outside:
        return "none"
    if price > vah:
        return "bullish_continuation"
    if price < val:
        return "bearish_continuation"
    return "none"


def law6_choppy_middle(price: float, vah: float, val: float, band_pct: float = 20.0) -> bool:
    """Kural 6: Değer alanının tam ortası aşırı whipsaw/choppy -> burada işlem açma."""
    mid = (vah + val) / 2.0
    half_range = (vah - val) / 2.0
    return abs(price - mid) <= half_range * (band_pct / 100.0)
