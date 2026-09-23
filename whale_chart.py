"""
whale_chart.py
==============
Layer VISUALISASI + CLI. Menggabungkan:
  * candle OHLCV                       (harga)
  * HEATMAP likuiditas order book      (limit Bid = hijau di bawah, Ask = merah di atas)
  * garis/band "whale wall"            (semakin tebal & terang = semakin besar volume)
  * panel volume & order book imbalance di bawahnya

Output utama: file HTML interaktif mandiri (bisa di-zoom, hover, pan — Plotly).
Chart dikembalikan juga sebagai objek `go.Figure` supaya bisa dipakai Dash (dashboard.py).

Cara pakai (lihat README.md untuk detail):
------------------------------------------
# 1) Snapshot historis + order book SAAT INI (cepat, 1 kali jalan)
python whale_chart.py --symbol BTCUSDT --interval 5m --limit 288

# 2) REAL-TIME: kumpulkan order book via WebSocket selama 3 menit, lalu simpan heatmap
python whale_chart.py --symbol BTCUSDT --interval 1m --limit 180 --live 180

# 3) REAL-TIME berkelanjutan: tulis ulang HTML tiap 10 detik (Ctrl+C untuk berhenti)
python whale_chart.py --symbol ETHUSDT --interval 1m --refresh 10
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from whale_analytics import (
    WallConfig,
    detect_walls,
    liquidity_matrix,
    nice_bin_size,
    normalize_log,
    price_grid,
    summarize,
    volume_profile_matrix,
)
from trade_plan import PlanConfig, TradePlan, build_trade_plan, format_plan
from whale_feed import (DepthSnapshot, ExchangeFeed, LiveCollector,
                        list_symbols, normalize_symbol, symbol_exists)

# --------------------------------------------------------------------------- #
# Palet warna (tema gelap)
# --------------------------------------------------------------------------- #
BG = "#0b0e14"
GRID = "#1b2130"
BULL = "#26a69a"   # hijau
BEAR = "#ef5350"   # merah
ACCENT = "#ffd54a" # kuning (mid price)

# Skala warna heatmap DIVERGEN:
#   -1 = limit BID tebal (hijau terang) | 0 = sepi/transparan | +1 = limit ASK tebal (merah terang)
# Warna memakai rgba supaya sel yang kosong benar-benar transparan (terlihat background gelap).
DIVERGING_SCALE = [
    [0.00, "rgba(80,255,200,0.90)"],   # z = -1  -> whale BID (support)
    [0.18, "rgba(38,220,170,0.55)"],
    [0.36, "rgba(38,166,154,0.22)"],
    [0.50, "rgba(0,0,0,0.00)"],        # z =  0  -> tidak ada likuiditas
    [0.64, "rgba(239,83,80,0.22)"],
    [0.82, "rgba(255,110,100,0.55)"],
    [1.00, "rgba(255,150,140,0.90)"],  # z = +1  -> whale ASK (resistance)
]

# Skala untuk heatmap VOLUME-AT-PRICE (mode historis): amber transparan -> putih terang
VOLUME_SCALE = [
    [0.00, "rgba(255,183,77,0.00)"],
    [0.15, "rgba(255,183,77,0.10)"],
    [0.40, "rgba(255,193,100,0.26)"],
    [0.70, "rgba(255,213,145,0.48)"],
    [1.00, "rgba(255,245,225,0.85)"],
]


def _pick_bin_size(mid: float, lo: float, hi: float, target_bins: int, bps: float,
                   step: Optional[float] = None, hard_cap: int = 1000,
                   max_rows: int = 600) -> tuple[float, np.ndarray]:
    """Pilih lebar bin harga.

    Kalau `step` diisi (mis. 5), ukuran itu dipakai apa adanya — hanya diperbesar bila
    jumlah baris melewati `hard_cap` (menjaga chart tetap ringan).
    Tanpa `step`, ukuran diturunkan dari `bps` lalu disesuaikan supaya jumlah bin wajar.
    """
    size = nice_bin_size(mid, bps, step)
    edges = price_grid(lo, hi, size)
    cap = hard_cap if step else max_rows
    for _ in range(40):             # pengaman: pastikan loop berhenti
        if len(edges) - 1 <= cap:
            break
        size *= 2                   # terlalu rapat -> perbesar bin (tetap kelipatan rapi)
        edges = price_grid(lo, hi, size)
    return size, edges


def resolve_bin_size(df: pd.DataFrame, mid: float, price_bins: int = 170,
                     bin_bps: float = 0.5, step: Optional[float] = None,
                     max_rows: int = 600) -> float:
    """Ukuran grid harga yang BENAR-BENAR dipakai chart (bisa dipakai ulang di UI/CLI)."""
    lo = float(df["low"].min()) * 0.9995
    hi = float(df["high"].max()) * 1.0005
    return _pick_bin_size(mid, lo, hi, price_bins, bin_bps, step=step,
                          max_rows=max_rows)[0]


# --------------------------------------------------------------------------- #
# Builder utama
# --------------------------------------------------------------------------- #
def build_figure(
    df: pd.DataFrame,
    snapshot: Optional[DepthSnapshot] = None,
    snapshots: Optional[List[DepthSnapshot]] = None,
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    provider: str = "binance",
    wall_cfg: Optional[WallConfig] = None,
    price_bins: int = 170,
    bin_bps: Optional[float] = None,   # None -> ikut wall_cfg (default 0.5 bps)
    show_walls: bool = True,
    bin_size: Optional[float] = None,    # lebar bin harga absolut (mis. 5 -> 4300,4305,4310)
    show_last_price: bool = True,        # gambar garis putus-putus + label harga terakhir
    right_pad_candles: float = 10.0,     # celah kosong di kanan (dalam satuan lebar candle)
    max_rows: int = 600,                 # batas baris heatmap sebelum grid diperbesar
    heat_source: Optional[str] = None,   # 'book' (heatmap asli) | 'profile' (proxy volume)
    plan: Optional[TradePlan] = None,    # rencana entry/SL/TP (dari trade_plan.py)
    show_plan: bool = True,
    height: int = 950,
) -> go.Figure:
    """Bangun figure Plotly berisi candlestick + heatmap likuiditas + whale wall.

    Parameters
    ----------
    df        : DataFrame OHLCV (index datetime UTC, kolom open/high/low/close/volume)
    snapshot  : order book dalam TERAKHIR (untuk deteksi whale wall & DOM)
    snapshots : kumpulan snapshot historis (untuk heatmap order book ASLI / real-time)
    heat_source: 'book' -> pakai `snapshots`; 'profile' -> proksi volume dari OHLCV;
                 None -> otomatis (book jika snapshot >= 5, selain itu profile)
    """
    wall_cfg = wall_cfg or WallConfig()
    if bin_bps is None:               # grid heatmap WAJIB sama dengan grid deteksi wall
        bin_bps = wall_cfg.bin_bps
    if bin_size:                      # konsisten antara heatmap & deteksi wall
        wall_cfg.bin_size = bin_size
    if df is None or df.empty:
        raise ValueError("DataFrame OHLCV kosong — gagal mengambil data harga.")

    mid = snapshot.mid if snapshot is not None else float(df["close"].iloc[-1])
    lo = float(df["low"].min()) * 0.9995
    hi = float(df["high"].max()) * 1.0005
    bin_size, edges = _pick_bin_size(mid, lo, hi, price_bins, bin_bps, step=bin_size,
                                     max_rows=max_rows)

    # ---------------- 1) Matriks heatmap ---------------------------------- #
    if heat_source is None:
        heat_source = "book" if (snapshots and len(snapshots) >= 5) else "profile"

    raw_bid = raw_ask = None
    if heat_source == "book" and snapshots:
        # === Heatmap order book ASLI: tiap kolom = 1 bucket waktu, tiap sel = limit order ===
        t_centers, price_centers, bid_m, ask_m = liquidity_matrix(
            snapshots, edges, max_time_buckets=180)
        z_bid = normalize_log(bid_m, 96)
        z_ask = normalize_log(ask_m, 96)
        raw_bid, raw_ask = bid_m, ask_m
        z = np.clip(z_ask - z_bid, -1, 1)     # divergen: -1 = bid tebal, +1 = ask tebal
        zmin, zmax, scale = -1.0, 1.0, DIVERGING_SCALE
        cbar = dict(tickvals=[-1, -0.5, 0, 0.5, 1],
                    ticktext=["bid tebal", "", "tipis", "", "ask tebal"])
        focus_window = True                      # zoom awal ke jendela heatmap
        src_label = f"Order book REAL-TIME ({len(snapshots)} snapshot WS)"
    else:
        # === Heatmap historis (proxy): volume yang TEREKSEKUSI per level harga. ===
        # REST publik tidak menyimpan histori order book, jadi yang bisa digambar
        # hanyalah "volume-at-price" (total volume, tidak bisa dipisah buy/sell per level).
        heat_source = "profile"
        t_centers, buy_m, sell_m = volume_profile_matrix(df, edges)
        price_centers = (edges[:-1] + edges[1:]) / 2
        z = normalize_log(buy_m + sell_m, 97)  # 0 = sepi, 1 = volume terpadat
        zmin, zmax, scale = 0.0, 1.0, VOLUME_SCALE
        cbar = dict(tickvals=[0, 0.5, 1], ticktext=["sepi", "sedang", "padat"])
        raw_bid, raw_ask = sell_m, buy_m
        focus_window = False
        src_label = "Volume-at-Price historis (proxy dari OHLCV)"

    # Samakan tipe sumbu waktu (datetime64[ms]) supaya aman untuk ekspor PNG/JSON
    t_centers = pd.DatetimeIndex(t_centers).to_numpy(dtype="datetime64[ms]")
    x_candle = pd.DatetimeIndex(df.index).to_numpy(dtype="datetime64[ms]")
    # Lebar 1 candle (dipakai untuk memberi ruang di tepi kanan)
    if len(x_candle) > 2:
        d = np.diff(x_candle).astype("timedelta64[ms]").astype("int64")
        d = d[d > 0]
        step = np.timedelta64(int(np.median(d)) if len(d) else 60_000, "ms")
    else:
        step = np.timedelta64(60_000, "ms")

    t_first = np.min(x_candle)
    t_last  = np.max(x_candle)
    if len(t_centers):
        t_first = min(t_first, t_centers[0])
        t_last  = max(t_last,  t_centers[-1])
    span = t_last - t_first

    # Ruang ekstra di KANAN supaya candle terakhir (yang masih terbentuk) punya
    # celah jelas dari tepi chart / angka harga di sumbu kanan.
    # Default 10 lebar candle (bisa diubah lewat --right-pad).
    right_pad = max(step * right_pad_candles, span * 0.02)
    left_pad  = span * 0.004

    if focus_window:
        # zoom awal ke jendela heatmap, tapi sisakan konteks candle sebelumnya
        ctx = max(span * 2, np.timedelta64(15, "m"))
        x_range = [t_centers[0] - ctx, t_last + right_pad]
    else:
        x_range = [t_first - left_pad, t_last + right_pad]

    base = symbol.upper().replace("USDT", "").replace("-", "")

    # ---------------- 2) Kerangka subplot ---------------------------------- #
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.02,
        row_heights=[0.78, 0.22],
        specs=[[{}], [{"secondary_y": True}]],
    )

    # ---------------- 3) HEATMAP (paling belakang) ------------------------- #
    # NOTE: urutan penambahan trace menentukan z-order -> heatmap ditambahkan pertama
    # supaya berada di BELAKANG candle.
    heat_custom = np.stack([raw_bid, raw_ask], axis=-1)  # (n_price, n_time, 2)
    l_bid, l_ask = ("Limit bid", "Limit ask") if heat_source == "book" else \
                   ("Vol jual", "Vol beli")
    cbar_title = "Limit order" if heat_source == "book" else "Volume"
    fig.add_trace(go.Heatmap(
        x=t_centers, y=price_centers, z=z, customdata=heat_custom,
        colorscale=scale, zmin=zmin, zmax=zmax, zsmooth=False,
        xgap=0, ygap=0, name="Heatmap likuiditas", showscale=True,
        colorbar=dict(title=dict(text=cbar_title, font=dict(size=10)),
                      x=1.012, y=0.80, len=0.36, thickness=12, **cbar),
        hovertemplate=(f"<b>%{{y:,.2f}}</b><br>%{{x|%H:%M:%S}}<br>"
                       f"{l_bid}: %{{customdata[0]:.4f}} {base}<br>"
                       f"{l_ask}: %{{customdata[1]:.4f}} {base}<extra></extra>"),
    ), row=1, col=1)

    # ---------------- 4) Candlestick --------------------------------------- #
    fig.add_trace(go.Candlestick(
        x=x_candle, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name=f"{symbol} · {interval}",
        increasing=dict(line=dict(color=BULL, width=1), fillcolor="rgba(38,166,154,0.45)"),
        decreasing=dict(line=dict(color=BEAR, width=1), fillcolor="rgba(239,83,80,0.45)"),
        whiskerwidth=0.4,
    ), row=1, col=1)

    # ---------------- 5) Whale wall (band + garis horizontal) -------------- #
    walls = detect_walls(snapshot, wall_cfg) if (snapshot is not None and show_walls) else \
        pd.DataFrame(columns=["side", "price", "price_lo", "price_hi", "qty",
                              "notional", "dist_bps", "strength"])
    for _, w in walls.iterrows():
        is_bid = w["side"] == "bid"
        rgb = "38,220,170" if is_bid else "255,110,100"
        s = float(w["strength"])
        # band semi-transparan selebar rentang harga wall-nya
        fig.add_hrect(
            y0=w["price_lo"], y1=w["price_hi"], row=1, col=1, layer="below",
            fillcolor=f"rgba({rgb},{0.06 + 0.22 * s:.3f})", line_width=0,
        )
        # garis tegas di tengah wall: makin tebal & terang = makin besar order-nya
        fig.add_hline(
            y=w["price"], row=1, col=1, layer="above",
            line=dict(color=f"rgba({rgb},{0.45 + 0.5 * s:.3f})", width=1.0 + 6.0 * s),
            annotation_text=f"{'BID' if is_bid else 'ASK'} ${w['notional']/1e6:.2f}M · {w['qty']:,.3f} {base}",
            annotation_position="right",
            annotation=dict(font=dict(size=9, color=f"rgba({rgb},0.95)"),
                            bgcolor="rgba(11,14,20,0.55)", borderpad=2,
                            xanchor="left", yshift=0),
        )

    # ---------------- 5b) Rencana order (entry / SL / TP) ------------------ #
    if plan is not None and show_plan:
        is_long = plan.side == "long"
        col_entry, col_sl = "#4dd0e1", "#ef5350"
        col_tp = "#26a69a"
        # zona risiko (entry -> SL) dan zona target (entry -> TP)
        fig.add_hrect(y0=min(plan.entry, plan.stop_loss), y1=max(plan.entry, plan.stop_loss),
                      row=1, col=1, layer="below",
                      fillcolor="rgba(239,83,80,0.10)", line_width=0)
        fig.add_hrect(y0=min(plan.entry, plan.take_profit), y1=max(plan.entry, plan.take_profit),
                      row=1, col=1, layer="below",
                      fillcolor="rgba(38,166,154,0.10)", line_width=0)
        fig.add_hline(y=plan.entry, row=1, col=1, layer="above",
                      line=dict(color=col_entry, width=2),
                      annotation_text=f"{'LIMIT BUY' if is_long else 'LIMIT SELL'} "
                                      f"{plan.entry:,.4f} ({plan.entry_offset_bps:+.0f} bps)",
                      annotation_position="left",
                      annotation=dict(font=dict(size=10, color=col_entry),
                                      bgcolor="rgba(11,14,20,0.75)", borderpad=2))
        fig.add_hline(y=plan.stop_loss, row=1, col=1, layer="above",
                      line=dict(color=col_sl, width=1.5, dash="dash"),
                      annotation_text=f"SL {plan.stop_loss:,.4f} (-{plan.sl_pct:.2f}%)",
                      annotation_position="left",
                      annotation=dict(font=dict(size=9, color=col_sl),
                                      bgcolor="rgba(11,14,20,0.6)", borderpad=2))
        fig.add_hline(y=plan.take_profit, row=1, col=1, layer="above",
                      line=dict(color=col_tp, width=1.5, dash="dash"),
                      annotation_text=f"TP {plan.take_profit:,.4f} (+{plan.tp_pct:.2f}%) "
                                      f"· R:R {plan.rr:.2f}",
                      annotation_position="left",
                      annotation=dict(font=dict(size=9, color=col_tp),
                                      bgcolor="rgba(11,14,20,0.6)", borderpad=2))

    # ---------------- 6) Garis mid price ----------------------------------- #
    fig.add_hline(y=mid, row=1, col=1, layer="above",
                  line=dict(color=ACCENT, width=1, dash="dot"),
                  annotation_text=f"mid {mid:,.2f}", annotation_position="left",
                  annotation=dict(font=dict(size=10, color=ACCENT),
                                  bgcolor="rgba(11,14,20,0.6)"))

    # ---------------- 7) Panel bawah: volume + imbalance ------------------- #
    colors = np.where(df["close"].values >= df["open"].values, BULL, BEAR)
    fig.add_trace(go.Bar(
        x=x_candle, y=df["volume"], name="Volume",
        marker=dict(color=colors, line=dict(width=0)),
        opacity=0.55, hovertemplate="Vol %{y:,.4f}<extra></extra>",
    ), row=2, col=1)

    if raw_bid is not None and raw_bid.shape[1] > 1:
        b, a = raw_bid.sum(axis=0), raw_ask.sum(axis=0)
        denom = (b + a)
        denom[denom == 0] = np.nan
        imb = (b - a) / denom * 100
        fig.add_trace(go.Scatter(
            x=t_centers, y=imb,
            name=("Imbalance order book %" if heat_source == "book" else "Net taker flow %"),
            mode="lines", line=dict(color="#7aa2f7", width=1.4),
            hovertemplate="Imbalance %{y:.1f}%<extra></extra>",
        ), row=2, col=1, secondary_y=True)
        fig.add_hline(y=0, row=2, col=1, secondary_y=True,
                      line=dict(color=GRID, width=1))

    # ---------------- 7b) Penanda harga terakhir --------------------------- #
    if show_last_price and len(df):
        last_close = float(df["close"].iloc[-1])
        fig.add_hline(
            y=last_close, row=1, col=1,
            line=dict(color="#e6edf3", width=1, dash="dot"),
            annotation_text=f" {last_close:,.4f} ",
            annotation_position="left",   # sumbu harga ada di kanan -> aman di kiri
            annotation_font=dict(size=11, color="#0d1117"),
            annotation_bgcolor="#e6edf3",
        )

    # ---------------- 8) Info box & layout --------------------------------- #
    info = summarize(snapshot, walls)
    if info:
        txt = (
            f"<b>mid</b> {info['mid']:,.2f} &nbsp;·&nbsp; "
            f"<b>spread</b> {info['spread_bps']:.2f} bps<br>"
            f"<b>imb ±2%</b> {info['imbalance']:+.1%} &nbsp;·&nbsp; "
            f"<b>bid</b> {info['bid_liq']:,.2f} / <b>ask</b> {info['ask_liq']:,.2f} {base}<br>"
            f"<b>wall terbesar</b> bid ${info['top_bid_wall']/1e6:.2f}M · "
            f"ask ${info['top_ask_wall']/1e6:.2f}M"
            + (f"<br><b>rencana {plan.side.upper()}</b> "
               f"{'limit buy' if plan.side == 'long' else 'limit sell'} "
               f"{plan.entry:,.4f} · SL {plan.stop_loss:,.4f} · TP {plan.take_profit:,.4f} "
               f"· R:R {plan.rr:.2f}" if (plan is not None and show_plan) else "")
        )
        fig.add_annotation(
            text=txt, xref="paper", yref="paper", x=0.005, y=1.30, xanchor="left",
            yanchor="top", align="left", showarrow=False,
            font=dict(size=11, color="#c9d1d9"),
            bgcolor="rgba(13,17,23,0.72)", bordercolor="#30363d", borderpad=6,
        )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    fig.update_layout(
        height=height,
        template="plotly_dark",
        paper_bgcolor=BG, plot_bgcolor=BG,
        margin=dict(l=60, r=150, t=95, b=40),
        title=dict(
            text=(f"<b>{symbol.upper()} · {interval}</b> — Crypto Order Book Heatmap & Whale Walls"
                  f"<br><sup><span style='color:#8b949e'>sumber: {provider} · {src_label} · "
                  f"{len(walls)} wall terdeteksi · grid harga {bin_size:g} · update {stamp}<br>"
                  f"heatmap: hijau = BID/support, merah = ASK/resistance"
                  f"{'' if heat_source == 'book' else ' (mode historis: amber = volume)'}"
                  f"</span></sup>"),
            x=0.005, xanchor="left", font=dict(size=17),
        ),
        hovermode="x unified",
        legend=dict(orientation="h", y=1.015, x=0.005, xanchor="left",
                    bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        xaxis_rangeslider_visible=False,
        bargap=0.15,
    )
    fig.update_xaxes(gridcolor=GRID, showspikes=True, spikemode="across",
                     spikethickness=1, spikecolor="#5c6370",
                     range=x_range, row=1, col=1)
    fig.update_xaxes(gridcolor=GRID, row=2, col=1)
    fig.update_yaxes(gridcolor=GRID, side="right", row=1, col=1,
                     title_text="Harga (USDT)",
                     tickformat=",.8g" if bin_size and bin_size < 1 else ",.2f")
    fig.update_yaxes(gridcolor=GRID, side="right", row=2, col=1, title_text="Vol")
    fig.update_yaxes(title_text="Imbalance %", secondary_y=True, row=2, col=1,
                     gridcolor=GRID, zerolinecolor=GRID, range=[-100, 100])
    return fig


def save_html(fig: go.Figure, out: str, cdn: bool = False) -> str:
    """Simpan figure ke HTML mandiri.

    `cdn=False` (default) menanamkan plotly.js di dalam file -> tetap interaktif
    walau dibuka offline / di preview tanpa akses internet.
    """
    fig.write_html(
        out,
        include_plotlyjs="cdn" if cdn else True,  # True == inline
        full_html=True,
        config={"scrollZoom": True, "displaylogo": False,
                "modeBarButtonsToAdd": ["drawline", "eraseshape"]},
    )
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Chart kripto interaktif + heatmap order book (whale wall), "
                    "real-time maupun historis.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--symbol", default="BTCUSDT", help="Pasar, mis. BTCUSDT / ETHUSDT / SOLUSDT")
    p.add_argument("--interval", default="5m", help="Interval candle: 1m,3m,5m,15m,1h,4h,1d")
    p.add_argument("--limit", type=int, default=288, help="Jumlah candle (288x5m = 24 jam)")
    p.add_argument("--depth-limit", type=int, default=5000,
                   help="Jumlah level order book (REST): 5/10/20/50/100/500/1000/5000")
    p.add_argument("--out", default="output/{symbol}_{interval}_whalemap.html",
                   help="Path file HTML keluaran")
    p.add_argument("--live", type=float, default=0,
                   help="Kumpulkan order book via WebSocket selama N detik sebelum menyimpan")
    p.add_argument("--refresh", type=float, default=0,
                   help="Mode berkelanjutan: tulis ulang HTML tiap N detik (0 = sekali)")
    p.add_argument("--min-notional", type=float, default=250_000,
                   help="Nilai minimum (USDT) agar sebuah level dianggap whale wall; 0 = auto")
    p.add_argument("--top-n", type=int, default=6, help="Jumlah wall per sisi")
    p.add_argument("--max-distance-pct", type=float, default=0.03,
                   help="Cari wall hanya dalam ±X%% dari mid price")
    p.add_argument("--bin-bps", type=float, default=0.5,
                   help="Lebar bin harga relatif (basis point): 0.5 -> BTC 5, emas 0.25")
    p.add_argument("--bin-size", type=float, default=None,
                   help="Lebar bin harga ABSOLUT, mis. 5 -> grid 4300/4305/4310 (override --bin-bps)")
    p.add_argument("--right-pad", type=float, default=10.0,
                   help="Celah kosong di tepi KANAN chart, dalam satuan lebar candle")
    p.add_argument("--price-bins", type=int, default=170, help="Target jumlah baris heatmap")
    p.add_argument("--no-walls", action="store_true", help="Sembunyikan garis whale wall")
    p.add_argument("--cdn", action="store_true", help="Pakai plotly.js dari CDN (file lebih kecil)")
    p.add_argument("--plan", action="store_true",
                   help="Hitung & gambar rencana limit order (entry/SL/TP) mengikuti whale wall")
    p.add_argument("--side", default="auto", choices=["auto", "long", "short"],
                   help="Arah rencana order (auto = pilih skor tertinggi)")
    p.add_argument("--balance", type=float, default=None, help="Modal USDT (untuk ukuran posisi)")
    p.add_argument("--risk-pct", type=float, default=1.0, help="%% modal yang dipertaruhkan per trade")
    p.add_argument("--buffer-bps", type=float, default=6.0,
                   help="Jarak limit order dari tepi wall (bps)")
    p.add_argument("--sl-buffer-bps", type=float, default=12.0, help="Jarak SL dari sisi luar wall (bps)")
    p.add_argument("--min-rr", type=float, default=1.5, help="Risk/Reward minimum agar rencana valid")
    p.add_argument("--max-entry-distance-pct", type=float, default=0.015,
                   help="Jarak maksimal level entry dari harga (rencana order)")
    p.add_argument("--list-symbols", nargs="?", const="", metavar="FILTER",
                   help="Tampilkan daftar pair yang tersedia (opsional filter, mis. SOL atau XAU) lalu keluar")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # --- daftar pair (mode informasi) ---
    if args.list_symbols is not None:
        filt = args.list_symbols.upper()
        syms = list_symbols(top=400)
        if filt:
            syms = [s for s in syms if filt in s]
        print(f"{len(syms)} pair ditemukan" + (f" untuk filter '{filt}'" if filt else "") + ":")
        for i in range(0, len(syms), 10):
            print("  " + "  ".join(f"{x:<12}" for x in syms[i:i + 10]))
        print("\nTip: XAU (emas) tidak punya pair spot -> pakai XAUTUSDT atau PAXGUSDT.")
        return 0

    # --- normalisasi & pengecekan pair ---
    args.symbol, note = normalize_symbol(args.symbol)
    if note:
        print(f"[pair] {note}")
    if not symbol_exists(args.symbol):
        print(f"[pair] PERINGATAN: {args.symbol} tidak ditemukan di exchange — "
              f"coba jalankan: python whale_chart.py --list-symbols")
    out = args.out.format(symbol=args.symbol.upper(), interval=args.interval)
    # --min-notional 0 -> mode AUTO: ambang disesuaikan ketebalan buku tiap pair
    cfg = WallConfig(min_notional=args.min_notional, top_n=args.top_n,
                     max_distance_pct=args.max_distance_pct, bin_bps=args.bin_bps,
                     bin_size=args.bin_size)

    # Data historis selalu diambil dulu supaya chart langsung berisi candle
    feed = ExchangeFeed(args.symbol)
    df = feed.fetch_klines(args.interval, args.limit)
    print(f"[data] {len(df)} candle {args.symbol} @ {args.interval} dari {feed.provider_name}")

    collector: Optional[LiveCollector] = None
    if args.live > 0 or args.refresh > 0:
        collector = LiveCollector(symbol=args.symbol, interval=args.interval,
                                  limit=args.limit, depth_limit=args.depth_limit)
        collector.start()
        # tunggu data pertama siap (maks ~20 detik) sebelum mulai menggambar
        for _ in range(100):
            if collector.df is not None and collector.deep is not None:
                break
            time.sleep(0.2)
        if args.live > 0:
            print(f"[live] mengumpulkan order book via WebSocket selama {args.live:.0f} detik...")
            t0 = time.time()
            while time.time() - t0 < args.live:
                time.sleep(5)
                print(f"       ... {time.time() - t0:5.0f}s | {len(collector.snapshot_stream())} snapshot",
                      end="\r", flush=True)
            print()

    def render_once() -> None:
        # kalau kolektor belum siap, pakai data historis yang sudah diambil di awal
        df_now = collector.snapshot_df() if collector else None
        if df_now is None:
            df_now = df
        snap = collector.snapshot_deep() if collector else feed.fetch_depth(args.depth_limit)
        snaps = collector.snapshot_stream() if collector else None

        # --- rencana limit order (opsional) ---
        plan = None
        if args.plan:
            plan_cfg = PlanConfig(min_notional=args.min_notional, buffer_bps=args.buffer_bps,
                                  sl_buffer_bps=args.sl_buffer_bps, min_rr=args.min_rr,
                                  max_entry_distance_pct=args.max_entry_distance_pct,
                                  bin_bps=args.bin_bps, bin_size=args.bin_size)
            plan = build_trade_plan(df_now, snap, None, plan_cfg, side=args.side,
                                    balance=args.balance, risk_pct=args.risk_pct)

        fig = build_figure(df_now, snap, snaps, symbol=args.symbol, interval=args.interval,
                           provider=feed.provider_name, wall_cfg=cfg,
                           price_bins=args.price_bins, bin_bps=args.bin_bps,
                           bin_size=args.bin_size,
                           right_pad_candles=args.right_pad,
                           show_walls=not args.no_walls, plan=plan)
        save_html(fig, out, cdn=args.cdn)
        n_snap = len(snaps) if snaps else 0
        mid_txt = f"mid={snap.mid:,.2f}" if snap is not None else "mid=—"
        grid = resolve_bin_size(df_now, snap.mid if snap else float(df_now["close"].iloc[-1]),
                                args.price_bins, args.bin_bps, args.bin_size)
        print(f"[ok] {out} | candle={len(df_now)} snapshot={n_snap} {mid_txt} | grid harga {grid:g}")
        if args.plan:
            print()
            print(format_plan(plan, args.symbol, snap.mid if snap else float(df_now["close"].iloc[-1]),
                              args.balance, args.risk_pct))

    if args.refresh > 0:
        print(f"[live] mode refresh tiap {args.refresh:.0f}s — Ctrl+C untuk berhenti")
        try:
            while True:
                render_once()
                time.sleep(args.refresh)
        except KeyboardInterrupt:
            print("\n[stop] dihentikan pengguna")
        finally:
            if collector:
                collector.stop()
    else:
        render_once()

    return 0


if __name__ == "__main__":
    sys.exit(main())
