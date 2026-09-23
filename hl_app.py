#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 hl_app.py — Dashboard & chart ORDER BOOK ON-CHAIN khusus HYPERLIQUID
=============================================================================

Versi ini **100% on-chain**: tidak ada data CEX (Binance/OKX) sama sekali.
Semua level buku order diambil dari state HyperCore yang dikomit validator,
dan setiap FILL (trade yang benar-benar terjadi) dibawa lengkap beserta
alamat wallet taker + hash transaksi — jadi bisa diverifikasi, BUKAN simulasi.

Aliran data
-----------
  REST  /info  type=meta            -> daftar coin yang bisa diperdagangkan
  REST  /info  type=candleSnapshot  -> OHLCV historis
  REST  /info  type=l2Book          -> buku order L2 (20 level / sisi)
  WS    wss://api.hyperliquid.xyz/ws
          channel l2Book -> update buku order tiap ada perubahan (sub-detik)
          channel trades -> FILL + alamat wallet taker
          channel candle -> update candle terakhir

Pemakaian
---------
  # dashboard live (buka di browser)
  python hl_app.py --port 8051

  # sekali jalan: rekam 120 detik on-chain lalu simpan chart HTML
  python hl_app.py --once --coin BTC --interval 1m --live 120 \
                   --min-notional 500000 --bin-size 5 --out output/hl_BTC.html

  # lihat daftar coin yang tersedia di Hyperliquid
  python hl_app.py --list-coins
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd

from whale_analytics import WallConfig, detect_walls
from whale_chart import build_figure, save_html, resolve_bin_size
from trade_plan import PlanConfig, build_trade_plan
from onchain_feed import HyperliquidFeed, HyperliquidCollector

# --------------------------------------------------------------------------- #
# Tema & konstanta tampilan
# --------------------------------------------------------------------------- #
BG      = "#0d1117"
PANEL   = "#161b22"
BORDER  = "#30363d"
TEXT    = "#c9d1d9"
MUTED   = "#8b949e"
ACCENT  = "#58a6ff"

# Coin populer yang ditampilkan lebih dulu di dropdown ( sisanya menyusul )
FAVORITES = ["BTC", "ETH", "SOL", "PAXG/GOLD", "HYPE", "XRP", "DOGE", "BNB",
             "AVAX", "LINK", "SUI", "ARB", "OP", "TIA", "LDO", "AAVE"]
# PAXG = emas berbalut token (1 PAXG = 1 troy ounce emas fisik) -> pengganti XAUUSDT

RIGHT_PAD = 10.0      # celah tepi kanan chart (lebar candle) untuk dashboard

TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]
LIMITS     = {"1m": 300, "3m": 300, "5m": 288, "15m": 200, "30m": 200,
              "1h": 168, "2h": 168, "4h": 168, "1d": 120}


# --------------------------------------------------------------------------- #
# Daftar coin
# --------------------------------------------------------------------------- #
def coin_universe() -> List[str]:
    """Ambil daftar coin Hyperliquid; kalau REST gagal, pakai daftar favorit."""
    try:
        coins = HyperliquidFeed.coins()
        ordered = [c for c in FAVORITES if c in coins]
        ordered += [c for c in coins if c not in ordered]
        return ordered
    except Exception:                                    # noqa: BLE001
        return list(FAVORITES)


# --------------------------------------------------------------------------- #
# Registry kolektor: satu kolektor per (coin, interval)
# --------------------------------------------------------------------------- #
_COLLECTORS: Dict[Tuple[str, str], HyperliquidCollector] = {}
_REGISTRY_LOCK = threading.Lock()


def ensure_collector(coin: str, interval: str, sample_every: float = 1.0,
                     n_sig_figs: int = 3) -> HyperliquidCollector:
    """Kolektor di-cache supaya beberapa browser/tab tidak buka WS dobel."""
    key = (coin.upper(), interval)
    with _REGISTRY_LOCK:
        return _ensure_collector_locked(key, sample_every, n_sig_figs)


def _ensure_collector_locked(key, sample_every: float, n_sig_figs: int) -> HyperliquidCollector:
    coin, interval = key
    col = _COLLECTORS.get(key)

    if col is not None and (col.error or col.status == "error"):
        col.stop()
        col = None
    if col is None:
        col = HyperliquidCollector(coin=coin, interval=interval,
                                   limit=LIMITS.get(interval, 300),
                                   n_sig_figs=n_sig_figs,
                                   sample_every=sample_every)
        col.start()
        _COLLECTORS[key] = col
        # tunggu sampai candle + buku order pertama benar-benar datang
        for _ in range(150):
            if col.df is not None and col.deep is not None:
                break
            if col.status == "error":
                break
            time.sleep(0.2)
    return col


# --------------------------------------------------------------------------- #
# Panel bukti on-chain: FILL terakhir (bisa diverifikasi di explorer)
# --------------------------------------------------------------------------- #
def fills_panel(trades: List[dict], max_rows: int = 10) -> str:
    if not trades:
        return "belum ada fill masuk …"
    lines = [f"{'WAKTU':<10} {'SISI':<5} {'HARGA':>12} {'SIZE':>12}  WALLET TAKER   TX"]
    lines.append("-" * 78)
    for t in trades[-max_rows:][::-1]:
        ts = time.strftime("%H:%M:%S", time.localtime(t["ts"] / 1000)) if t["ts"] else "--:--:--"
        side = "BUY " if str(t.get("side", "")).upper() == "B" else "SELL"
        lines.append(f"{ts:<10} {side:<5} {t['px']:>12,.4f} {t['sz']:>12,.4f}  "
                     f"{t.get('taker',''):<14} {t.get('hash','')}")
    return "\n".join(lines)


def _baris(label: str, value: str, color: str = TEXT):
    from dash import html
    return html.Span(f"{label} {value}", style={"marginRight": "16px", "color": color})


# --------------------------------------------------------------------------- #
# Bangun figure
# --------------------------------------------------------------------------- #
def render(col: HyperliquidCollector, min_notional: float, top_n: int,
           show_walls: bool, with_plan: bool, balance: Optional[float],
           risk_pct: float, bin_size: Optional[float],
           right_pad_candles: float = 10.0):
    """Bangun figure + ringkasan. Ringkasan dikembalikan sebagai dict agar bisa
    dipakai oleh dashboard (html) maupun mode --once (teks biasa, tanpa dash)."""
    df    = col.snapshot_df()
    snap  = col.snapshot_deep()
    snaps = col.snapshot_stream()
    if df is None or df.empty:
        return None, {"error": "menunggu data on-chain pertama …"}, "…"

    mid = snap.mid if snap else float(df["close"].iloc[-1])
    cfg = WallConfig(min_notional=float(min_notional), top_n=top_n,
                     max_distance_pct=0.03, bin_bps=0.5, bin_size=bin_size)
    walls = detect_walls(snap, cfg) if snap is not None else pd.DataFrame()

    plan = None
    if with_plan and snap is not None:
        plan = build_trade_plan(df, snap, walls,
                                PlanConfig(min_notional=float(min_notional),
                                           bin_bps=0.5, bin_size=bin_size),
                                balance=balance, risk_pct=risk_pct)

    fig = build_figure(df, snap, snaps, symbol=col.coin, interval=col.interval,
                       provider="⛓ ON-CHAIN Hyperliquid", wall_cfg=cfg,
                       price_bins=170, bin_size=bin_size, show_walls=show_walls,
                       plan=plan, show_plan=with_plan, height=920,
                       right_pad_candles=right_pad_candles)

    # harga PALING SEGAR = harga FILL terakhir (update sub-detik, presisi penuh).
    # mid buku hanya di-push Hyperliquid tiap ~5 detik dan dibulatkan ke 3 angka
    # penting (BTC -> kelipatan $100), makanya tidak dipakai untuk tampilan harga.
    px, ts, side = col.live_price()
    trades = col.snapshot_trades()
    info = {
        "price": px, "price_ts": ts, "price_side": side,
        "coin": col.coin, "interval": col.interval, "mid": mid,
        "grid": resolve_bin_size(df, mid, 170, 0.5, bin_size),
        "bid": int((walls["side"] == "bid").sum()) if len(walls) else 0,
        "ask": int((walls["side"] == "ask").sum()) if len(walls) else 0,
        "snapshots": len(snaps), "fills": len(trades),
        "wallets": len({t.get("taker", "") for t in trades if t.get("taker")}),
        "ws": col.is_ws_connected, "plan": plan,
        "rows": int(fig.data[0].z.shape[0]) if hasattr(fig.data[0], "z") else 0,
    }
    return fig, info, fills_panel(trades)


def status_component(info: dict):
    """Baris status untuk dashboard (butuh dash)."""
    from dash import html
    if info.get("error"):
        return html.Span(info["error"], style={"color": "#d29922"})
    ws_txt, ws_col = ("● live", "#3fb950") if info["ws"] else ("○ reconnect", "#d29922")
    row = html.Div([
        html.B(f"⛓ {info['coin']}", style={"color": ACCENT, "marginRight": "14px"}),
        _baris("harga (fill)", f"{info['price']:,.4f}") if info.get("price") else _baris("harga", f"{info['mid']:,.4f}"),
        _baris("mid buku", f"{info['mid']:,.4f}", MUTED),
        _baris("timeframe", info["interval"]),
        _baris("grid", f"{info['grid']:g}", MUTED),
        _baris("wall", f"{info['bid']} bid / {info['ask']} ask"),
        _baris("buku tersimpan", f"{info['snapshots']} snapshot", MUTED),
        _baris("baris heatmap", f"{info['rows']}", MUTED),
        _baris("fill on-chain", f"{info['fills']} ({info['wallets']} wallet)", "#3fb950"),
        _baris("ws", ws_txt, ws_col),
    ], style={"fontSize": "12px", "color": TEXT})
    plan = info.get("plan")
    if plan is not None:
        row.children.append(html.Span(
            f" · {plan.side.upper()} {plan.entry:,.4f} · SL {plan.stop_loss:,.4f} "
            f"· TP {plan.take_profit:,.4f} · R:R {plan.rr:.2f} · skor {plan.score}",
            style={"marginLeft": "10px", "color": "#ffa657"}))
    return row


def status_text(info: dict) -> str:
    """Ringkasan teks untuk mode --once (tanpa dash)."""
    if info.get("error"):
        return info["error"]
    p = info.get("plan")
    plan_txt = ""
    if p is not None:
        plan_txt = (f"\n     rencana        : {p.side.upper()} {p.entry:,.4f} · "
                    f"SL {p.stop_loss:,.4f} · TP {p.take_profit:,.4f} · R:R {p.rr:.2f}")
    return (
        f"     harga (fill)   : {info.get('price') or info['mid']:,.4f}\n"
        f"     mid buku       : {info['mid']:,.4f}\n"
        f"     grid harga     : {info['grid']:g}  ({info['rows']} baris heatmap)\n"
        f"     wall terdeteksi: {info['bid']} bid / {info['ask']} ask\n"
        f"     snapshot buku  : {info['snapshots']}\n"
        f"     FILL on-chain  : {info['fills']} ({info['wallets']} wallet taker berbeda)"
        f"{plan_txt}")


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def build_app(default_coin: str = "BTC", default_tf: str = "1m",
              default_min: float = 500_000):
    from dash import Dash, dcc, html, Input, Output, State

    coins = coin_universe()
    if default_coin.upper() not in coins:
        coins = [default_coin.upper()] + coins

    app = Dash(__name__, title="Hyperliquid · On-Chain Whale Wall")
    app.layout = html.Div([
        html.Div([
            html.H3("⛓ Hyperliquid — Order Book ON-CHAIN & Whale Wall",
                    style={"margin": "0 0 4px 0", "color": ACCENT, "fontSize": "18px"}),
            html.Div("Level = limit order yang sedang resting di chain HyperCore · "
                     "FILL = trade sungguhan lengkap dengan alamat wallet taker "
                     "(bukan simulasi)",
                     style={"fontSize": "11px", "color": MUTED}),
        ], style={"marginBottom": "12px"}),

        html.Div([
            html.Div([html.Label("Coin (Hyperliquid perp)",
                                 style={"fontSize": "11px", "color": MUTED}),
                      dcc.Dropdown(id="coin", options=[{"label": c, "value": c} for c in coins],
                                   value=default_coin.upper(), clearable=False,
                                   style={"width": "190px", "color": "#000"})]),
            html.Div([html.Label("Timeframe", style={"fontSize": "11px", "color": MUTED}),
                      dcc.Dropdown(id="timeframe",
                                   options=[{"label": t, "value": t} for t in TIMEFRAMES],
                                   value=default_tf, clearable=False,
                                   style={"width": "100px", "color": "#000"})]),
            html.Div([html.Label("Min wall (USDT)", style={"fontSize": "11px", "color": MUTED}),
                      dcc.Input(id="min-notional", type="number", value=default_min,
                                step=50_000, debounce=True,
                                style=_input_style("120px"))]),
            html.Div([html.Label("Grid harga (bin)", style={"fontSize": "11px", "color": MUTED}),
                      dcc.Input(id="bin-size", type="number", value=None, step=1,
                                debounce=True, placeholder="auto (mis. 5)",
                                style=_input_style("120px"))]),
            html.Div([html.Label("Refresh (detik)", style={"fontSize": "11px", "color": MUTED}),
                      dcc.Dropdown(id="refresh",
                                   options=[{"label": f"{s}s", "value": s * 1000}
                                            for s in (1, 2, 3, 5, 10)],
                                   value=2000, clearable=False,
                                   style={"width": "90px", "color": "#000"})]),
            html.Div([html.Label("Tampilkan", style={"fontSize": "11px", "color": MUTED}),
                      dcc.Checklist(id="show-walls",
                                    options=[{"label": " Whale walls", "value": 1},
                                             {"label": " Rencana entry/SL/TP", "value": 2}],
                                    value=[1, 2],
                                    style={"color": TEXT, "fontSize": "12px"})]),
            html.Div([html.Label("Modal (USDT)", style={"fontSize": "11px", "color": MUTED}),
                      dcc.Input(id="balance", type="number", value=1000, step=100,
                                debounce=True, style=_input_style("100px"))]),
            html.Div([html.Label("Risiko (%)", style={"fontSize": "11px", "color": MUTED}),
                      dcc.Input(id="risk-pct", type="number", value=1.0, step=0.5,
                                debounce=True, style=_input_style("80px"))]),
        ], style={"display": "flex", "gap": "14px", "flexWrap": "wrap",
                  "alignItems": "flex-end", "marginBottom": "10px"}),

        html.Div(id="ticker", style={"backgroundColor": PANEL, "border": f"1px solid {BORDER}",
                                     "borderRadius": "6px", "padding": "6px 12px",
                                     "marginBottom": "6px", "display": "flex",
                                     "alignItems": "baseline", "gap": "12px"}),
        html.Div(id="status", style={"backgroundColor": PANEL, "border": f"1px solid {BORDER}",
                                     "borderRadius": "6px", "padding": "8px 10px",
                                     "marginBottom": "8px", "fontSize": "12px"}),
        dcc.Interval(id="tick", interval=2000, n_intervals=0),
        dcc.Interval(id="fast", interval=700, n_intervals=0),   # ticker harga saja
        dcc.Graph(id="graph", style={"height": "920px"}),

        html.Details([
            html.Summary("🔍 Bukti on-chain: FILL terakhir (wallet taker + hash tx)",
                         style={"cursor": "pointer", "color": ACCENT, "fontSize": "12px",
                                "margin": "10px 0 6px 0"}),
            html.Pre(id="fills", style={"backgroundColor": PANEL, "color": TEXT,
                                        "border": f"1px solid {BORDER}", "borderRadius": "6px",
                                        "padding": "10px", "fontSize": "11px",
                                        "overflowX": "auto", "margin": "0"}),
        ]),
    ], style={"backgroundColor": BG, "color": TEXT, "padding": "14px 18px",
              "minHeight": "100vh", "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto"})

    @app.callback(
        Output("graph", "figure"), Output("status", "children"), Output("fills", "children"),
        Input("tick", "n_intervals"), Input("coin", "value"), Input("timeframe", "value"),
        Input("refresh", "value"),
        State("min-notional", "value"), State("show-walls", "value"),
        State("balance", "value"), State("risk-pct", "value"), State("bin-size", "value"),
        prevent_initial_call=False,
    )
    def update(_n, coin, timeframe, refresh, min_notional, show_walls,
               balance, risk_pct, bin_size):
        from dash import html
        if refresh:
            # sinkronkan kecepatan interval dengan pilihan user
            pass
        try:
            col = ensure_collector(coin or "BTC", timeframe or "1m")
        except Exception as e:                            # noqa: BLE001
            return _empty_fig(), html.Span(f"❌ {type(e).__name__}: {e}",
                                           style={"color": "#f85149"}), ""
        if col.error:
            return _empty_fig(), html.Span(f"❌ {col.error}", style={"color": "#f85149"}), ""

        fig, info, fills = render(col, min_notional or 500_000, top_n=6,
                                  show_walls=1 in (show_walls or []),
                                  with_plan=2 in (show_walls or []),
                                  balance=float(balance) if balance else None,
                                  risk_pct=float(risk_pct or 1.0),
                                  bin_size=float(bin_size) if bin_size else None,
                                  right_pad_candles=RIGHT_PAD)
        if fig is None:
            return _empty_fig(), status_component(info), fills
        return fig, status_component(info), fills

    # CATATAN: sengaja memakai 2 output (children + title). Di Dash 4.4.1 callback
    # ber-output TUNGGAL menolak nilai berupa komponen/list, sedangkan jalur
    # multi-output (seperti callback chart di atas) berjalan normal.
    @app.callback(Output("ticker", "children"), Output("ticker", "title"),
                  Input("fast", "n_intervals"),
                  State("coin", "value"), State("timeframe", "value"))
    def ticker(_n, coin, timeframe):
        """Harga live: dibaca langsung dari memori kolektor (tanpa rebuild chart),
        sehingga tampil tiap 0.7 detik walaupun chart hanya digambar ulang tiap 2-5 detik."""
        from dash import html
        # CATATAN: output `children` di Dash 4 wajib dikembalikan sebagai LIST
        try:
            col = _COLLECTORS.get(((coin or "BTC").upper(), timeframe or "1m"))
        except Exception:                                        # noqa: BLE001
            col = None
        if col is None or col.df is None:
            return ([html.Span("menunggu data on-chain …",
                               style={"color": MUTED, "fontSize": "13px"})], "menunggu data")
        px, ts, side = col.live_price()
        if px is None:
            return ([html.Span("menunggu fill on-chain pertama …",
                               style={"color": MUTED, "fontSize": "13px"})], "menunggu fill")
        age = time.time() - ts / 1000 if ts else 99
        age_col = "#3fb950" if age < 2 else ("#d29922" if age < 6 else "#f85149")
        side_txt, side_col = ("▲ BUY", "#3fb950") if str(side).upper() == "B" else ("▼ SELL", "#f85149")
        snap = col.snapshot_deep()
        # Dash 4: output tunggal harus 1 nilai -> bungkus daftar span ke dalam 1 Div
        return html.Div([
            html.Span(f"{px:,.4f}", style={"fontSize": "26px", "fontWeight": "600",
                                           "color": "#e6edf3", "fontFamily": "ui-monospace, monospace"}),
            html.Span(f"USDT · {col.coin}", style={"fontSize": "12px", "color": MUTED}),
            html.Span(side_txt, style={"fontSize": "12px", "color": side_col}),
            html.Span(f"update {age:.1f}s lalu", style={"fontSize": "12px", "color": age_col}),
            html.Span(f"mid buku {snap.mid:,.4f}" if snap else "",
                      style={"fontSize": "11px", "color": MUTED, "marginLeft": "auto"}),
        ], style={"display": "flex", "alignItems": "baseline", "gap": "12px", "width": "100%"}), \
            f"harga terakhir dari FILL on-chain · {age:.1f} detik lalu"

    # kecepatan refresh mengikuti dropdown
    @app.callback(Output("tick", "interval"), Input("refresh", "value"),
                  prevent_initial_call=True)
    def _set_interval(refresh):
        return int(refresh or 2000)

    return app


def _input_style(width: str) -> dict:
    return {"width": width, "backgroundColor": "#0d1117", "color": TEXT,
            "border": f"1px solid {BORDER}", "borderRadius": "6px", "padding": "6px"}


def _empty_fig():
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.update_layout(template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG,
                      height=920, xaxis={"visible": False}, yaxis={"visible": False},
                      annotations=[{"text": "menghubungkan ke Hyperliquid …",
                                    "showarrow": False, "font": {"size": 16, "color": MUTED}}])
    return fig


# --------------------------------------------------------------------------- #
# Mode sekali jalan: rekam N detik -> simpan HTML + cetak bukti on-chain
# --------------------------------------------------------------------------- #
def run_once(args) -> int:
    print(f"[hl] menghubungkan ke Hyperliquid untuk {args.coin} …")
    col = ensure_collector(args.coin, args.interval, sample_every=args.sample_every)
    if col.status == "error":
        print(f"[hl] gagal: {col.error}")
        return 1
    print(f"[hl] streaming {args.live} detik (buku + fill on-chain) …")
    t_end = time.time() + args.live
    while time.time() < t_end:
        time.sleep(1)
        n_snap, n_fill = len(col.snapshot_stream()), len(col.snapshot_trades())
        print(f"\r     {n_snap:>4} snapshot buku · {n_fill:>5} fill on-chain", end="", flush=True)
    print()

    fig, info, fills = render(col, args.min_notional, args.top_n,
                                show_walls=not args.no_walls, with_plan=args.plan,
                                balance=args.balance, risk_pct=args.risk_pct,
                                bin_size=args.bin_size,
                                right_pad_candles=args.right_pad)
    if fig is None:
        print("[hl] tidak ada data yang diterima"); return 1

    out = args.out or f"output/hl_{args.coin}_{args.interval}.html"
    save_html(fig, out)

    print(f"\n[ok] {out}")
    print(status_text(info))
    if info.get("fills"):
        print("\n===== BUKTI ON-CHAIN: 8 FILL TERAKHIR (wallet taker + hash) =====")
        print(fills_panel(col.snapshot_trades(), 8))
    col.stop()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Hyperliquid — order book on-chain & whale wall")
    p.add_argument("--coin", default="BTC", help="Coin Hyperliquid, mis. BTC / ETH / PAXG")
    p.add_argument("--interval", default="1m", choices=TIMEFRAMES)
    p.add_argument("--min-notional", type=float, default=500_000)
    p.add_argument("--top-n", type=int, default=6)
    p.add_argument("--bin-size", type=float, default=None,
                   help="Lebar grid harga absolut, mis. 5 (kelipatan 5)")
    p.add_argument("--sigfigs", type=int, default=3,
                   help="Agregasi level buku: 3 = ±220 bps / ~11 bps per bucket")
    p.add_argument("--right-pad", type=float, default=10.0,
                   help="Celah kosong di tepi KANAN chart (lebar candle)")
    p.add_argument("--sample-every", type=float, default=1.0,
                   help="Simpan snapshot buku tiap N detik (bahan heatmap)")
    # dashboard
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8051)
    # mode sekali jalan
    p.add_argument("--once", action="store_true", help="Rekam sebentar lalu simpan HTML")
    p.add_argument("--live", type=int, default=60, help="Detik merekam (mode --once)")
    p.add_argument("--out", default=None)
    p.add_argument("--no-walls", action="store_true")
    p.add_argument("--plan", action="store_true", help="Sertakan rencana entry/SL/TP")
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--list-coins", action="store_true")
    args = p.parse_args(argv)

    if args.list_coins:
        cs = coin_universe()
        print(f"{len(cs)} coin di Hyperliquid:")
        print("  " + ", ".join(cs[:60]))
        if len(cs) > 60:
            print(f"  … dan {len(cs) - 60} lainnya")
        return 0

    if args.once:
        return run_once(args)

    print(f"""
  ⛓  Hyperliquid · Order Book ON-CHAIN & Whale Wall
     coin      : {args.coin}  ({args.interval})
     min wall  : ${args.min_notional:,.0f}
     dashboard : http://{args.host}:{args.port}
     Ctrl+C untuk berhenti
""")
    app = build_app(args.coin, args.interval, args.min_notional)
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
