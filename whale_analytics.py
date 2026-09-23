"""
whale_analytics.py
==================
Layer ANALITIK — mengubah order book mentah menjadi hal yang bisa digambar:

1. `detect_walls()`        -> deteksi "whale wall" (tembok limit order besar)
2. `liquidity_matrix()`    -> matriks likuiditas order book (sumbu harga x waktu) untuk HEATMAP ASLI
3. `volume_profile_matrix()` -> matriks VOLUME-AT-PRICE dari OHLCV (proxy heatmap historis,
                                karena REST publik tidak menyimpan riwayat order book gratis)
4. `normalize_log()`       -> normalisasi nilai ke 0..1 agar warna heatmap konsisten

Konsep singkat
--------------
* Limit BID (buy) menumpuk di bawah harga  -> potensi SUPPORT  (warna hijau)
* Limit ASK (sell) menumpuk di atas harga  -> potensi RESISTANCE (warna merah)
* "Whale wall" = kumpulan limit order dalam satu rentang harga sempit yang
  volumenya jauh di atas rata-rata (biasanya > 3x median) dan bernilai besar (USDT).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from whale_feed import DepthSnapshot

# --------------------------------------------------------------------------- #
# Utilitas harga / binning
# --------------------------------------------------------------------------- #
def nice_bin_size(price: float, bps: float = 0.5, step: Optional[float] = None) -> float:
    """Ukuran 1 bin harga yang 'rapi'.

    bps  = basis point terhadap harga (aturan relatif, default 0.5 bps):
            85000 x 0.5 bps = 4.25  -> dibulatkan ke **5**   (BTC)
            4320  x 0.5 bps = 0.216 -> dibulatkan ke 0.25    (emas)
    step = ukuran ABSOLUT yang dipakai apa adanya, mis. 5 -> grid 4300, 4305, 4310, ...
           (kalau diisi, `bps` diabaikan)
    """
    if step:                       # override manual: pakai persis seperti diminta
        return float(step)
    raw = price * bps / 10_000.0
    if raw <= 0:
        return 1e-8
    magnitude = 10 ** np.floor(np.log10(raw))
    for m in (1, 2, 2.5, 5, 10):  # pembulatan ke angka "enak dilihat"
        if raw <= magnitude * m:
            return float(magnitude * m)
    return float(magnitude * 10)


def price_grid(lo: float, hi: float, bin_size: float) -> np.ndarray:
    """Edge (batas) bin harga dari lo..hi dengan lebar seragam `bin_size`."""
    lo = np.floor(lo / bin_size) * bin_size
    hi = np.ceil(hi / bin_size) * bin_size
    n = max(int(round((hi - lo) / bin_size)), 1)
    return lo + np.arange(n + 1) * bin_size


def normalize_log(z: np.ndarray, hi_pct: float = 97.0) -> np.ndarray:
    """Normalisasi ke 0..1 memakai skala log (likuiditas itu sangat miring/skewed).

    Nilai > persentil `hi_pct` di-clip ke 1 supaya beberapa whale wall ekstrem
    tidak "menggelapkan" seluruh heatmap.
    """
    z = np.asarray(z, dtype=float)
    if z.size == 0 or not np.isfinite(z).any():
        return np.zeros_like(z)
    pos = z[z > 0]
    if pos.size == 0:
        return np.zeros_like(z)
    ref = np.percentile(pos, hi_pct)
    if ref <= 0:
        ref = pos.max()
    out = np.log1p(z) / np.log1p(ref)
    return np.clip(out, 0, 1)


def _hist_liquidity(prices: np.ndarray, qtys: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Jumlahkan qty per bin harga (np.histogram dengan bobot qty)."""
    if prices.size == 0:
        return np.zeros(len(edges) - 1)
    return np.histogram(prices, bins=edges, weights=qtys)[0]


# --------------------------------------------------------------------------- #
# 1. Deteksi whale wall
# --------------------------------------------------------------------------- #
@dataclass
class WallConfig:
    """Parameter deteksi whale wall — satu tempat supaya mudah di-tuning."""

    max_distance_pct: float = 0.03   # hanya cari wall dalam ±3% dari mid price
    bin_bps: float = 0.5             # lebar bin harga (basis point) -> BTC ~5, emas ~0.25
    bin_size: Optional[float] = None # lebar bin ABSOLUT (mis. 5); kalau diisi, bin_bps diabaikan
    min_notional: float = 250_000.0  # nilai minimum sebuah wall (USD/USDT); 0 = auto
    score_mult: float = 3.0          # wall = >= 3x median likuiditas per bin
    top_n: int = 6                   # jumlah wall per sisi yang ditampilkan
    adaptive: bool = True            # kalau tidak ada wall, longgarkan ambang otomatis


def detect_walls(snapshot: DepthSnapshot, cfg: Optional[WallConfig] = None) -> pd.DataFrame:
    """Temukan whale wall (akumulasi limit order besar) pada kedua sisi book.

    Algoritma
    ---------
    1. Potong book ke rentang harga `mid ± max_distance_pct` (wall jauh tidak relevan).
    2. Agregasi qty per bin harga -> hitung notional (price x qty).
    3. Tandai bin yang notional-nya >= max(min_notional, score_mult x median bin).
    4. Gabungkan bin berturut-turut yang sama-sama "besar" menjadi 1 wall utuh
       (penting: whale sering memecah order ke beberapa level harga berdekatan).
    5. Ambil `top_n` wall terbesar per sisi.

    Returns
    -------
    DataFrame kolom: side, price, price_lo, price_hi, qty, notional, dist_bps, strength
    """
    cfg = cfg or WallConfig()
    if snapshot is None or not len(snapshot.bids) or not len(snapshot.asks):
        return pd.DataFrame(columns=["side", "price", "price_lo", "price_hi",
                                     "qty", "notional", "dist_bps", "strength"])

    mid = snapshot.mid
    bin_size = nice_bin_size(mid, cfg.bin_bps, cfg.bin_size)
    lo = mid * (1 - cfg.max_distance_pct)
    hi = mid * (1 + cfg.max_distance_pct)

    rows = []
    for side, book in (("bid", snapshot.bids), ("ask", snapshot.asks)):
        p, q = book[:, 0], book[:, 1]
        # sisi bid hanya di bawah mid, sisi ask hanya di atas mid
        keep = (p >= lo) & (p <= mid) if side == "bid" else (p <= hi) & (p >= mid)
        p, q = p[keep], q[keep]
        if p.size == 0:
            continue

        edges = price_grid(p.min() - bin_size, p.max() + bin_size, bin_size)
        qty_bin = _hist_liquidity(p, q, edges)
        centers = (edges[:-1] + edges[1:]) / 2
        notional = qty_bin * centers

        # --- ambang batas "besar" (beberapa kandidat, dari yang paling ketat) ---
        nonzero = notional[notional > 0]
        if nonzero.size == 0:
            continue
        base = float(np.median(nonzero)) * cfg.score_mult
        candidates = [base, float(np.percentile(nonzero, 92))]
        if cfg.min_notional and cfg.min_notional > 0:
            candidates.append(max(cfg.min_notional, base))   # 0 / negatif = mode AUTO
        if not cfg.adaptive:
            candidates = candidates[:1]
        # urutkan menurun supaya kita mencoba kriteria paling ketat lebih dulu
        candidates = sorted({round(c, 6) for c in candidates}, reverse=True)

        groups_found = []
        for thr in candidates:
            idx = np.flatnonzero(notional >= thr)
            groups_found = [g for g in np.split(idx, np.flatnonzero(np.diff(idx) != 1) + 1)
                            if g.size]
            if groups_found:  # kriteria ketat sudah menghasilkan wall -> stop
                break

        # --- gabungkan bin berturut-turut yang masuk kriteria jadi 1 wall utuh ---
        for group in groups_found:
            qty = float(qty_bin[group].sum())
            notional_sum = float(notional[group].sum())
            if notional_sum <= 0 or qty <= 0:
                continue
            price_wavg = notional_sum / qty  # harga rata-rata berbobot nilai
            rows.append({
                "side": side,
                "price": price_wavg,
                "price_lo": float(edges[group[0]]),
                "price_hi": float(edges[group[-1] + 1]),
                "qty": qty,
                "notional": notional_sum,
                "dist_bps": (price_wavg - mid) / mid * 10_000,
            })

    if not rows:
        return pd.DataFrame(columns=["side", "price", "price_lo", "price_hi",
                                     "qty", "notional", "dist_bps", "strength"])
    walls = pd.DataFrame(rows)
    walls["dist_bps"] = walls["dist_bps"].abs()
    walls = walls.sort_values("notional", ascending=False).reset_index(drop=True)
    # kekuatan relatif 0..1 terhadap wall terbesar (dipakai untuk ketebalan & opacity garis)
    walls["strength"] = (walls["notional"] / walls["notional"].max()).clip(0, 1)
    walls = (walls.groupby("side", group_keys=False)
                  .head(cfg.top_n)
                  .sort_values(["side", "notional"], ascending=[True, False])
                  .reset_index(drop=True))
    return walls


# --------------------------------------------------------------------------- #
# 2. Matriks likuiditas dari RANGKAIAN snapshot order book (heatmap asli)
# --------------------------------------------------------------------------- #
def liquidity_matrix(
    snapshots: Sequence[DepthSnapshot],
    price_edges: np.ndarray,
    max_time_buckets: int = 180,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Bangun matriks likuiditas (harga x waktu) dari banyak snapshot order book.

    Dipakai untuk heatmap real-time bergaya "Bookmap": setiap kolom = 1 bucket waktu,
    setiap sel = jumlah limit order (dalam base asset) pada level harga tersebut.

    Returns
    -------
    (time_centers, price_centers, bid_matrix, ask_matrix)
    """
    n_price = len(price_edges) - 1
    if not snapshots:
        return (np.array([]), (price_edges[:-1] + price_edges[1:]) / 2,
                np.zeros((n_price, 0)), np.zeros((n_price, 0)))

    times = np.array([s.ts for s in snapshots], dtype=float)
    n_buckets = int(min(max_time_buckets, max(len(snapshots), 1)))
    t_edges = np.linspace(times.min() - 1, times.max() + 1, n_buckets + 1)
    t_centers = pd.to_datetime((t_edges[:-1] + t_edges[1:]) / 2, unit="ms", utc=True)

    bid_m = np.zeros((n_price, n_buckets))
    ask_m = np.zeros((n_price, n_buckets))
    counts = np.zeros(n_buckets)

    for s in snapshots:
        b = np.clip(np.searchsorted(t_edges, s.ts, side="right") - 1, 0, n_buckets - 1)
        counts[b] += 1
        bid_m[:, b] += _hist_liquidity(s.bids[:, 0], s.bids[:, 1], price_edges)
        ask_m[:, b] += _hist_liquidity(s.asks[:, 0], s.asks[:, 1], price_edges)

    # rata-rata per bucket (bukan total) supaya bucket dengan lebih banyak
    # snapshot tidak tampak "lebih tebal" hanya karena sampling
    counts[counts == 0] = 1
    bid_m /= counts
    ask_m /= counts

    price_centers = (price_edges[:-1] + price_edges[1:]) / 2
    return t_centers, price_centers, bid_m, ask_m


# --------------------------------------------------------------------------- #
# 3. Matriks volume-at-price dari OHLCV (PROXY heatmap historis)
# --------------------------------------------------------------------------- #
def volume_profile_matrix(
    df: pd.DataFrame,
    price_edges: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Estimasi distribusi volume per level harga dari candle OHLCV (footprint proxy).

    Histori order book TIDAK tersedia gratis di REST publik, jadi untuk mode historis
    kita pakai volume perdagangan yang terserap (executed) sebagai proksi likuiditas:
      * volume BUY  (taker buy) -> digambar di sisi ASK (offer yang "diangkat")
      * volume SELL (sisanya)   -> digambar di sisi BID  (bid yang "dihantam")

    Volume setiap candle disebar merata ke seluruh level harga antara low..high.

    Returns
    -------
    (time_index, buy_matrix, sell_matrix)  — masing-masing matriks (n_price, n_time)
    """
    n_price = len(price_edges) - 1
    buy_m = np.zeros((n_price, len(df)))
    sell_m = np.zeros((n_price, len(df)))

    bin_lo, bin_hi = price_edges[:-1], price_edges[1:]
    has_taker = "taker_buy_base" in df.columns and df["taker_buy_base"].notna().any()

    for j, (_, row) in enumerate(df.iterrows()):
        lo, hi = float(row["low"]), float(row["high"])
        vol = float(row["volume"])
        if vol <= 0 or hi <= lo:
            # candle datar (doji sempit) -> taruh semua volume di harga close
            k = np.clip(np.searchsorted(price_edges, row["close"], side="right") - 1, 0, n_price - 1)
            buy_m[k, j] += vol / 2
            sell_m[k, j] += vol / 2
            continue

        # rasio volume beli: pakai taker_buy jika ada, kalau tidak pakai posisi close
        if has_taker and np.isfinite(row["taker_buy_base"]):
            buy_ratio = float(np.clip(row["taker_buy_base"] / vol, 0, 1))
        else:
            buy_ratio = float(np.clip((row["close"] - lo) / (hi - lo), 0, 1))

        # overlap candle dengan tiap bin harga -> bobot penyebaran volume
        overlap = np.clip(np.minimum(hi, bin_hi) - np.maximum(lo, bin_lo), 0, None)
        total = overlap.sum()
        if total <= 0:
            continue
        w = overlap / total
        buy_m[:, j] += vol * buy_ratio * w
        sell_m[:, j] += vol * (1 - buy_ratio) * w

    return df.index, buy_m, sell_m


# --------------------------------------------------------------------------- #
# Ringkasan teks untuk header chart
# --------------------------------------------------------------------------- #
def summarize(snapshot: Optional[DepthSnapshot], walls: pd.DataFrame, pct: float = 0.02):
    """Ringkasan singkat kondisi book (dipakai untuk info box di chart)."""
    if snapshot is None:
        return {}
    bid_liq = snapshot.side_liquidity("bid", pct)
    ask_liq = snapshot.side_liquidity("ask", pct)
    top_bid = walls[(walls["side"] == "bid")]["notional"].max() if len(walls) else 0.0
    top_ask = walls[(walls["side"] == "ask")]["notional"].max() if len(walls) else 0.0
    return {
        "mid": snapshot.mid,
        "spread_bps": snapshot.spread_bps,
        "bid_liq": bid_liq,
        "ask_liq": ask_liq,
        "imbalance": snapshot.imbalance(pct),
        "top_bid_wall": 0.0 if np.isnan(top_bid) else float(top_bid),
        "top_ask_wall": 0.0 if np.isnan(top_ask) else float(top_ask),
    }


# --------------------------------------------------------------------------- #
# Uji mandiri: python whale_analytics.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from whale_feed import ExchangeFeed

    feed = ExchangeFeed("BTCUSDT")
    df = feed.fetch_klines("5m", 200)
    snap = feed.fetch_depth(1000)

    walls = detect_walls(snap, WallConfig(min_notional=250_000, top_n=5))
    pd.set_option("display.width", 140, "display.float_format", lambda v: f"{v:,.2f}")
    print(f"mid = {snap.mid:,.2f} | spread = {snap.spread_bps:.2f} bps\n")
    print(walls[["side", "price", "qty", "notional", "dist_bps", "strength"]])

    edges = price_grid(df["low"].min() * 0.999, df["high"].max() * 1.001,
                       nice_bin_size(snap.mid, 2.0))
    _, buy_m, sell_m = volume_profile_matrix(df, edges)
    print(f"\nvolume-profile matrix: buy={buy_m.shape} sell={sell_m.shape} "
          f"total_vol={buy_m.sum() + sell_m.sum():,.3f}")
