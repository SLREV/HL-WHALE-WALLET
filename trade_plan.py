"""
trade_plan.py
=============
Layer EKSEKUSI — mengubah whale wall menjadi RENCANA ORDER yang bisa ditindaklanjuti:
di mana pasang limit order, di mana stop loss, di mana take profit, dan berapa ukuran posisi.

Logika inti (mengikuti "smart money" = pembuat likuiditas besar)
----------------------------------------------------------------
1. Wall BID besar di bawah harga = dukungan (support). Kita ingin **ikut di belakangnya**:
   pasang LIMIT BUY beberapa bps DI ATAS batas atas wall supaya kita
   - ikut terisi (fill) SEBELUM order whale tereksekusi (posisi antrean lebih depan), dan
   - kalau wall benar-benar menahan, harga memantul setelah kita terisi.
2. Stop loss diletakkan DI BAWAH batas bawah wall (+buffer).
   Alasan: selama wall utuh, harga sulit menembusnya. Kalau tembus, berarti wall
   terserap habis / ditarik → alasan kita masuk sudah tidak valid.
3. Take profit diletakkan DI BAWAH wall ASK terdekat (resistance) supaya kita
   menjual KE likuiditas mereka, bukan melawannya.
4. Semua level dicek dengan Risk/Reward minimal (default 1.5) dan jarak minimal
   berbasis ATR supaya tidak kena noise.

PENTING: wall bisa di-spoof (ditarik sebelum tersentuh) atau iceberg (diisi ulang).
Modul ini memberi LEVEL, bukan kepastian. Selalu pakai manajemen risiko.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict, field
from typing import List, Optional

import numpy as np
import pandas as pd

from whale_analytics import WallConfig, detect_walls
from whale_feed import DepthSnapshot, ExchangeFeed, normalize_symbol


# --------------------------------------------------------------------------- #
# Konfigurasi rencana
# --------------------------------------------------------------------------- #
@dataclass
class PlanConfig:
    """Parameter penyusunan rencana order — sesuaikan dengan gaya trading Anda."""

    max_entry_distance_pct: float = 0.015  # wall entry maksimal 1.5% dari harga
    bin_bps: float = 0.5                  # lebar bin harga relatif
    bin_size: Optional[float] = None      # lebar bin harga absolut (mis. 5)
    min_entry_offset_bps: float = 3.0      # entry minimal 3 bps dari harga (hindari "nempel" di mid)
    buffer_bps: float = 6.0                # jarak limit order dari tepi wall (bps)
    sl_buffer_bps: float = 12.0            # jarak stop loss dari sisi luar wall (bps)
    atr_mult_sl: float = 0.5               # jarak SL minimal = atr_mult_sl x ATR
    min_rr: float = 1.5                    # Risk/Reward minimal agar setup valid
    rr_fallback: float = 2.0               # TP = rr_fallback x risk bila tak ada wall lawan
    min_notional: float = 250_000.0        # nilai wall minimum (USDT)
    atr_period: int = 14


@dataclass
class TradePlan:
    """Satu rencana order (long atau short)."""

    side: str                    # "long" | "short"
    valid: bool
    entry: float
    stop_loss: float
    take_profit: float
    risk: float                  # jarak entry -> SL (harga)
    reward: float                # jarak entry -> TP (harga)
    rr: float                    # risk/reward
    entry_dist_bps: float        # jarak absolut entry dari harga sekarang
    entry_offset_bps: float      # jarak entry searah rencana (+ = makin menguntungkan)
    sl_pct: float                # % risiko dari entry
    tp_pct: float                # % target dari entry
    wall_price: float            # harga tengah wall acuan
    wall_notional: float         # nilai wall acuan (USDT)
    wall_range: tuple[float, float]
    tp_wall_price: Optional[float] = None     # wall lawan yang jadi target (jika ada)
    tp_wall_notional: Optional[float] = None
    qty: Optional[float] = None              # ukuran posisi (butuh balance & risk_pct)
    position_notional: Optional[float] = None
    score: float = 0.0                       # skor keyakinan 0-100
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["wall_range"] = list(self.wall_range)
        return d


# --------------------------------------------------------------------------- #
# Indikator pendukung
# --------------------------------------------------------------------------- #
def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range — dipakai agar SL tidak terlalu dekat (kena noise)."""
    if df is None or len(df) < period + 1:
        return float("nan")
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


# --------------------------------------------------------------------------- #
# Penyusun rencana
# --------------------------------------------------------------------------- #
def _pick_wall(walls: pd.DataFrame, side: str, mid: float, cfg: PlanConfig,
               imbalance: float) -> Optional[pd.Series]:
    """Pilih wall terbaik untuk entry pada sisi tertentu.

    Skor = 50% kekuatan wall (nilai relatif) + 30% kedekatan + 20% kesesuaian imbalance.
    """
    if walls is None or walls.empty:
        return None
    max_dist_bps = cfg.max_entry_distance_pct * 10_000
    buf = 1.0 + cfg.buffer_bps / 10_000
    # entry HARUS menguntungkan arah rencana:
    #   long  -> limit buy DI BAWAH mid   |   short -> limit sell DI ATAS mid
    # (kalau tidak, order akan langsung tereksekusi seperti market order)
    if side == "bid":
        reachable = walls["price_hi"] * buf <= mid * (1 - cfg.min_entry_offset_bps / 10_000)
    else:
        reachable = walls["price_lo"] / buf >= mid * (1 + cfg.min_entry_offset_bps / 10_000)
    c = walls[(walls["side"] == side) & reachable &
              (walls["dist_bps"] <= max_dist_bps) &
              (walls["notional"] >= cfg.min_notional)].copy()
    if c.empty:  # longgarkan nilai wall, TAPI syarat arah entry tetap wajib
        c = walls[(walls["side"] == side) & reachable &
                  (walls["dist_bps"] <= max_dist_bps)].copy()
    if c.empty:
        return None

    prox = 1.0 - (c["dist_bps"] / max_dist_bps).clip(0, 1)
    align = c["side"].map(lambda s: max(0.0, imbalance) if s == "bid" else max(0.0, -imbalance))
    c["score"] = 0.5 * c["strength"] + 0.3 * prox + 0.2 * align
    return c.sort_values("score", ascending=False).iloc[0]


def _target_wall(walls: pd.DataFrame, side: str, entry: float, risk: float,
                 cfg: PlanConfig) -> Optional[pd.Series]:
    """Cari wall lawan terdekat yang memberi R:R >= min_rr (target profit).

    side = sisi entry ("bid" untuk long -> target wall ASK; "ask" untuk short -> target wall BID)
    """
    if walls is None or walls.empty:
        return None
    opposite = "ask" if side == "bid" else "bid"
    c = walls[(walls["side"] == opposite) & (walls["notional"] >= cfg.min_notional)].copy()
    if c.empty:
        return None
    if opposite == "ask":
        c = c[c["price_lo"] > entry]                       # resistance di atas entry
        c["reward"] = c["price_lo"] * (1 - cfg.buffer_bps / 10_000) - entry
    else:
        c = c[c["price_hi"] < entry]                       # support di bawah entry
        c["reward"] = entry - c["price_hi"] * (1 + cfg.buffer_bps / 10_000)
    c = c[c["reward"] >= cfg.min_rr * risk]
    if c.empty:
        return None
    return c.sort_values("reward").iloc[0]                 # ambil target TERDEKAT yang lolos R:R


def _score_plan(wall_notional: float, rr: float, dist_bps: float,
                imbalance: float, side: str, cfg: PlanConfig) -> float:
    """Skor keyakinan 0-100 (bukan janji profit — hanya pembanding antar setup)."""
    s = 0.0
    s += 30.0 * min(wall_notional / max(cfg.min_notional * 4, 1e-9), 1.0)   # tebalnya wall
    s += 25.0 * min(rr / 3.0, 1.0)                                          # kualitas R:R
    s += 20.0 * (1.0 - min(dist_bps / (cfg.max_entry_distance_pct * 10_000), 1.0))  # dekat
    align = imbalance if side == "long" else -imbalance
    s += 25.0 * max(0.0, min(align, 1.0))                                   # searah imbalance
    return float(np.clip(s, 0, 100))


def build_trade_plan(
    df: pd.DataFrame,
    snapshot: Optional[DepthSnapshot],
    walls: Optional[pd.DataFrame] = None,
    cfg: Optional[PlanConfig] = None,
    side: str = "auto",
    wall_cfg: Optional[WallConfig] = None,
    balance: Optional[float] = None,
    risk_pct: float = 1.0,
) -> Optional[TradePlan]:
    """Susun rencana LONG/SHORT dari whale wall terbaik.

    side: "long" (limit buy di atas wall BID) | "short" (limit sell di bawah wall ASK)
          | "auto" -> pilih yang skornya lebih tinggi.
    """
    cfg = cfg or PlanConfig()
    if snapshot is None or not len(snapshot.bids) or not len(snapshot.asks):
        return None
    if walls is None:
        wall_cfg = wall_cfg or WallConfig(
            min_notional=cfg.min_notional, top_n=8,
            max_distance_pct=max(cfg.max_entry_distance_pct * 3, 0.05),
            bin_bps=cfg.bin_bps, bin_size=cfg.bin_size)
        walls = detect_walls(snapshot, wall_cfg)

    mid = snapshot.mid
    imb = snapshot.imbalance(0.02)
    a = atr(df, cfg.atr_period)
    if not np.isfinite(a) or a <= 0:
        a = mid * 0.001  # fallback 10 bps

    candidates = ["long", "short"] if side == "auto" else [side]
    best: Optional[TradePlan] = None

    for sd in candidates:
        entry_side = "bid" if sd == "long" else "ask"
        w = _pick_wall(walls, entry_side, mid, cfg, imb)
        if w is None:
            continue

        notes: List[str] = []
        wall_lo, wall_hi = float(w["price_lo"]), float(w["price_hi"])

        if sd == "long":
            # limit buy SEDIKIT DI ATAS batas atas wall -> antrean lebih depan dari whale
            entry = wall_hi * (1 + cfg.buffer_bps / 10_000)
            sl = wall_lo * (1 - cfg.sl_buffer_bps / 10_000)
            sl = min(sl, entry - cfg.atr_mult_sl * a)        # SL tidak boleh lebih dekat dari ATR
            risk = entry - sl
            if risk <= 0:
                continue
            tw = _target_wall(walls, entry_side, entry, risk, cfg)
            if tw is not None:
                tp = float(tw["price_lo"]) * (1 - cfg.buffer_bps / 10_000)
                tp_wall, tp_notional = float(tw["price"]), float(tw["notional"])
                notes.append(f"TP di bawah wall ASK ${tw['notional']/1e6:.2f}M @ {tw['price']:,.2f}")
            else:
                tp = entry + cfg.rr_fallback * risk
                tp_wall = tp_notional = None
                notes.append("tidak ada wall ASK yang memberi R:R minimum -> TP memakai "
                             f"{cfg.rr_fallback}x risk")
        else:
            # limit sell SEDIKIT DI BAWAH batas bawah wall ASK -> antrean lebih depan
            entry = wall_lo * (1 - cfg.buffer_bps / 10_000)
            sl = wall_hi * (1 + cfg.sl_buffer_bps / 10_000)
            sl = max(sl, entry + cfg.atr_mult_sl * a)
            risk = sl - entry
            if risk <= 0:
                continue
            tw = _target_wall(walls, entry_side, entry, risk, cfg)
            if tw is not None:
                tp = float(tw["price_hi"]) * (1 + cfg.buffer_bps / 10_000)
                tp_wall, tp_notional = float(tw["price"]), float(tw["notional"])
                notes.append(f"TP di atas wall BID ${tw['notional']/1e6:.2f}M @ {tw['price']:,.2f}")
            else:
                tp = entry - cfg.rr_fallback * risk
                tp_wall = tp_notional = None
                notes.append("tidak ada wall BID yang memberi R:R minimum -> TP memakai "
                             f"{cfg.rr_fallback}x risk")

        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0.0
        valid = rr >= cfg.min_rr
        dist_bps = abs(entry - mid) / mid * 10_000

        # ukuran posisi berdasar risiko akun (opsional)
        qty = position_notional = None
        if balance and risk > 0:
            risk_money = balance * risk_pct / 100.0
            qty = risk_money / risk
            position_notional = qty * entry

        notes.insert(0, f"wall {entry_side.upper()} ${w['notional']/1e6:.2f}M di "
                        f"{wall_lo:,.2f}–{wall_hi:,.2f} (jarak {w['dist_bps']:.0f} bps)")
        notes.append(f"spread {snapshot.spread_bps:.3f} bps · imbalance ±2% {imb:+.1%} · ATR {a:,.2f}")
        if not valid:
            notes.append(f"R:R {rr:.2f} di bawah minimum {cfg.min_rr} -> setup dilewati")

        plan = TradePlan(
            side=sd, valid=valid, entry=float(entry), stop_loss=float(sl),
            take_profit=float(tp), risk=float(risk), reward=float(reward), rr=float(rr),
            entry_dist_bps=float(dist_bps),
            entry_offset_bps=float((mid - entry) / mid * 10_000 if sd == "long"
                                   else (entry - mid) / mid * 10_000),
            sl_pct=float(abs(sl - entry) / entry * 100),
            tp_pct=float(abs(tp - entry) / entry * 100),
            wall_price=float(w["price"]), wall_notional=float(w["notional"]),
            wall_range=(wall_lo, wall_hi),
            tp_wall_price=tp_wall, tp_wall_notional=tp_notional,
            qty=None if qty is None else float(qty),
            position_notional=None if position_notional is None else float(position_notional),
            score=_score_plan(float(w["notional"]), rr, dist_bps, imb, sd, cfg),
            notes=notes,
        )
        if best is None or plan.score > best.score:
            best = plan

    return best


# --------------------------------------------------------------------------- #
# Format tampilan
# --------------------------------------------------------------------------- #
def format_plan(plan: Optional[TradePlan], symbol: str, mid: float,
                balance: Optional[float] = None, risk_pct: float = 1.0) -> str:
    """Teks ringkas siap dibaca di terminal."""
    if plan is None:
        return (f"{symbol}: tidak ada rencana — order book kosong atau tidak ada wall "
                f"dalam jarak yang diizinkan.")
    head = "🟢 LIMIT BUY (long)" if plan.side == "long" else "🔴 LIMIT SELL (short)"
    lines = [
        "════════ {} · {} ════════".format(symbol, head),
        f"harga sekarang (mid) : {mid:,.4f}",
        "",
        f"  PASANG LIMIT   : {plan.entry:,.4f}   ({plan.entry_offset_bps:+.0f} bps "
        f"{'di bawah' if plan.side == 'long' else 'di atas'} mid)",
        f"  STOP LOSS      : {plan.stop_loss:,.4f}   ({plan.sl_pct:.2f}% dari entry)",
        f"  TAKE PROFIT    : {plan.take_profit:,.4f}   ({plan.tp_pct:.2f}% dari entry)",
        "",
        f"  risk/reward    : {plan.rr:.2f}  |  skor keyakinan {plan.score:.0f}/100",
    ]
    if plan.qty is not None:
        lines.append(f"  ukuran posisi  : {plan.qty:,.4f} {symbol.replace('USDT','')} "
                     f"(≈ ${plan.position_notional:,.0f}, risiko ${balance * risk_pct / 100:,.2f} "
                     f"= {risk_pct}% dari ${balance:,.0f})")
    lines += ["", "  alasan / catatan:"]
    lines += [f"   • {n}" for n in plan.notes]
    if not plan.valid:
        lines += ["", "  ⚠️  R:R di bawah minimum — pertimbangkan menunggu setup lain."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Susun rencana limit order mengikuti whale wall (smart money).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--depth-limit", type=int, default=5000)
    ap.add_argument("--side", default="auto", choices=["auto", "long", "short"])
    ap.add_argument("--min-notional", type=float, default=250_000)
    ap.add_argument("--buffer-bps", type=float, default=6.0,
                    help="jarak limit order dari tepi wall")
    ap.add_argument("--sl-buffer-bps", type=float, default=12.0)
    ap.add_argument("--min-rr", type=float, default=1.5)
    ap.add_argument("--max-entry-distance-pct", type=float, default=0.015)
    ap.add_argument("--bin-size", type=float, default=None,
                    help="Lebar bin harga absolut, mis. 5 (override --bin-bps)")
    ap.add_argument("--bin-bps", type=float, default=0.5, help="Lebar bin harga relatif (bps)")
    ap.add_argument("--balance", type=float, default=None,
                    help="modal USDT untuk hitung ukuran posisi")
    ap.add_argument("--risk-pct", type=float, default=1.0,
                    help="persen modal yang dipertaruhkan per trade")
    ap.add_argument("--json", action="store_true", help="keluaran JSON (untuk bot/otomasi)")
    a = ap.parse_args()

    symbol, note = normalize_symbol(a.symbol)
    if note:
        print(f"[pair] {note}")

    feed = ExchangeFeed(symbol)
    df = feed.fetch_klines(a.interval, a.limit)
    snap = feed.fetch_depth(a.depth_limit)

    cfg = PlanConfig(min_notional=a.min_notional, buffer_bps=a.buffer_bps,
                     sl_buffer_bps=a.sl_buffer_bps, min_rr=a.min_rr,
                     max_entry_distance_pct=a.max_entry_distance_pct,
                     bin_bps=a.bin_bps, bin_size=a.bin_size)
    walls = detect_walls(snap, WallConfig(min_notional=a.min_notional, top_n=8,
                                          max_distance_pct=max(a.max_entry_distance_pct * 3, 0.05),
                                          bin_bps=a.bin_bps, bin_size=a.bin_size))
    plan = build_trade_plan(df, snap, walls, cfg, side=a.side,
                            balance=a.balance, risk_pct=a.risk_pct)

    if plan is None and not a.json:
        print("Tidak ada setup valid: tidak ada wall di sisi yang menguntungkan dalam "
              f"jarak {a.max_entry_distance_pct:.2%}.")
        print("Coba: naikkan --max-entry-distance-pct, turunkan --min-notional, "
              "atau paksa arah dengan --side long / --side short.")

    if a.json:
        print(json.dumps({"symbol": symbol, "mid": snap.mid,
                          "plan": plan.to_dict() if plan else None}, indent=2, default=str))
    else:
        print(f"[data] {feed.provider_name} · {len(df)} candle {symbol} @ {a.interval} "
              f"· {len(walls)} wall terdeteksi\n")
        print(format_plan(plan, symbol, snap.mid, a.balance, a.risk_pct))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
