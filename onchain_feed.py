"""
onchain_feed.py
===============
Sumber data **ON-CHAIN** (berlawanan dengan whale_feed.py yang mengambil order book CEX).

Tiga provider yang semuanya gratis tanpa API key, dengan sifat yang berbeda:

| Provider | Jenis | Apa yang on-chain | Bisa di-spoof? |
|---|---|---|---|
| **Hyperliquid**  | CLOB (buku limit) di appchain HyperCore | State buku order & trade dikomit ke chain validator | Bisa — order bisa dibatalkan kapan saja (seperti CEX), tapi state-nya bisa diverifikasi |
| **dYdX v4**      | CLOB di appchain Cosmos | Settlements on-chain; buku order hidup di memori validator & dipublikasikan indexer | Bisa dibatalkan, tapi matching & settlement on-chain |
| **Uniswap V3**   | AMM (bukan limit order) | Likuiditas **terkunci di kontrak** — dibaca langsung dari state Ethereum | **Tidak bisa spoof** — dananya nyata ada di pool; LP bisa menarik kapan saja |

Kenapa Uniswap bisa tetap digambar sebagai "order book"?
  Rentang likuiditas DI BAWAH harga sekarang -> posisi itu 100% berisi token QUOTE
  (mis. USDC) yang siap MEMBELI base token saat harga turun  -> ekuivalen **BID**.
  Rentang DI ATAS harga -> posisi 100% berisi token BASE yang dijual saat harga naik
  -> ekuivalen **ASK**. Inilah "tembok likuiditas" versi DEX: bukan janji, tapi modal nyata.

Semua provider mengembalikan (DataFrame OHLCV, DepthSnapshot) — struktur yang sama
persis dengan whale_feed.py, jadi whale_analytics / whale_chart / trade_plan tidak berubah.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests

from whale_feed import DepthSnapshot, _to_book

TIMEOUT = 25

# =========================================================================== #
# Utilitas JSON-RPC Ethereum
# =========================================================================== #
# Selector fungsi Uniswap V3 / ERC20 (4 byte pertama keccak signature)
SEL_SLOT0 = "0x3850c7bd"        # slot0() -> (sqrtPriceX96, tick, ...)
SEL_TICKSPACING = "0xd0c93a7c"  # tickSpacing() -> int24
SEL_LIQUIDITY = "0x1a686502"    # liquidity() -> uint128
SEL_TICKS = "0xf30dba93"        # ticks(int24) -> (liquidityGross, liquidityNet, ...)
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"

TOPIC_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SECONDS_PER_BLOCK = 12          # Ethereum pasca-merge


class JsonRpc:
    """Klien JSON-RPC minimal dengan dukungan **batching** (1 HTTP request = banyak call)."""

    def __init__(self, url: str = "https://ethereum-rpc.publicnode.com", timeout: int = TIMEOUT):
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()

    def call(self, to: str, data: str) -> str:
        r = self.session.post(self.url, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"]}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("result", "0x")

    def batch(self, calls: Sequence[Tuple[str, str]], chunk: int = 80) -> List[str]:
        """Kirim banyak eth_call sekaligus; fallback ke request tunggal bila batch ditolak."""
        out: List[str] = []
        for i in range(0, len(calls), chunk):
            part = calls[i:i + chunk]
            payload = [{"jsonrpc": "2.0", "id": j, "method": "eth_call",
                        "params": [{"to": t, "data": d}, "latest"]}
                       for j, (t, d) in enumerate(part)]
            try:
                r = self.session.post(self.url, json=payload, timeout=self.timeout)
                body = r.json()
                if isinstance(body, list):
                    out.extend(x.get("result", "0x") for x in body)
                    continue
            except Exception:  # noqa: BLE001
                pass
            # fallback: satu per satu
            for t, d in part:
                out.append(self.call(t, d))
        return out

    def block_number(self) -> int:
        r = self.session.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                                              "method": "eth_blockNumber", "params": []},
                              timeout=self.timeout)
        return int(r.json()["result"], 16)

    def get_logs(self, address: str, topics: Sequence[str], from_block: int,
                 to_block: int) -> List[dict]:
        r = self.session.post(self.url, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
            "params": [{"address": address, "topics": list(topics),
                        "fromBlock": hex(from_block), "toBlock": hex(to_block)}]},
            timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("result", [])

    def block_timestamps(self, blocks: Sequence[int], chunk: int = 50) -> Dict[int, int]:
        """Ambil timestamp blok pakai batching (fallback: estimasi 12 detik/blok)."""
        out: Dict[int, int] = {}
        for i in range(0, len(blocks), chunk):
            part = list(blocks[i:i + chunk])
            payload = [{"jsonrpc": "2.0", "id": j, "method": "eth_getBlockByNumber",
                        "params": [hex(b), False]} for j, b in enumerate(part)]
            try:
                r = self.session.post(self.url, json=payload, timeout=self.timeout)
                body = r.json()
                if isinstance(body, list):
                    for b, res in zip(part, body):
                        ts = (res or {}).get("result") or {}
                        if ts.get("timestamp"):
                            out[b] = int(ts["timestamp"], 16)
                    continue
            except Exception:  # noqa: BLE001
                pass
        return out


def _int_from_word(word: str, signed: bool = False) -> int:
    v = int(word, 16)
    if signed and v >> 255:
        v -= 1 << 256
    return v


def _decode_string(hexdata: str) -> str:
    """Decode string ABI (bytes32 offset, length, lalu data)."""
    d = hexdata[2:]
    if len(d) < 128:
        return ""
    length = int(d[64:128], 16)
    raw = bytes.fromhex(d[128:128 + length * 2])
    return raw.decode("utf-8", errors="ignore").strip("\x00")


# =========================================================================== #
# 1) Uniswap V3 — likuiditas nyata di kontrak Ethereum
# =========================================================================== #
POOLS: Dict[str, str] = {
    # nama yang mudah diingat -> alamat pool Uniswap V3 (Ethereum mainnet)
    "ETH/USDC-0.05%": "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
    "WBTC/USDC-0.05%": "0x99ac8cA7087fA4A2A1FB6357269965A2014ABc35",
    "WBTC/ETH-0.3%": "0xCBCdF9626bC03E24f779434178A73a0B4bad62eD",
}
STABLES = {"USDC", "USDT", "DAI", "USDE", "FDUSD", "TUSD"}


@dataclass
class PoolInfo:
    address: str
    token0: str
    token1: str
    symbol0: str
    symbol1: str
    dec0: int
    dec1: int
    spacing: int
    quote_is_token0: bool
    base_symbol: str
    quote_symbol: str

    @property
    def base_decimals(self) -> int:
        return self.dec0 if not self.quote_is_token0 else self.dec1

    @property
    def pair(self) -> str:
        return f"{self.base_symbol}/{self.quote_symbol}"


class UniswapV3Feed:
    """Membaca state pool Uniswap V3 langsung dari Ethereum (likuiditas per tick)."""

    name = "uniswap-v3"

    def __init__(self, pool: str = "ETH/USDC-0.05%", rpc_url: str = JsonRpc.__init__.__defaults__[0],
                 window_bps: float = 300.0):
        self.rpc = JsonRpc(rpc_url)
        self.pool_address = POOLS.get(pool, pool)          # boleh kirim alamat langsung
        self.window_bps = window_bps                        # lebar jendela pindai (± bps)
        self.info = self._load_pool_info()

    # ---------------- metadata pool ------------------------------------- #
    def _load_pool_info(self) -> PoolInfo:
        addr = self.pool_address
        res = self.rpc.batch([
            (addr, SEL_TOKEN0), (addr, SEL_TOKEN1), (addr, SEL_TICKSPACING),
            (addr, SEL_SYMBOL),  # symbol token0 diisi setelah tahu alamat token
        ])
        t0 = "0x" + res[0][-40:]
        t1 = "0x" + res[1][-40:]
        spacing = _int_from_word(res[2])
        meta = self.rpc.batch([(t0, SEL_SYMBOL), (t1, SEL_SYMBOL),
                               (t0, SEL_DECIMALS), (t1, SEL_DECIMALS)])
        s0, s1 = _decode_string(meta[0]), _decode_string(meta[1])
        d0, d1 = _int_from_word(meta[2]), _int_from_word(meta[3])
        quote_is_token0 = s0.upper() in STABLES and s1.upper() not in STABLES
        return PoolInfo(addr, t0, t1, s0, s1, d0, d1, spacing, quote_is_token0,
                        base_symbol=s1 if quote_is_token0 else s0,
                        quote_symbol=s0 if quote_is_token0 else s1)

    # ---------------- harga --------------------------------------------- #
    def tick_to_price(self, tick: float) -> float:
        """Harga manusia: jumlah QUOTE per 1 BASE."""
        p_raw = 1.0001 ** tick                     # raw token1 per raw token0
        if self.info.quote_is_token0:
            return (10 ** (self.info.dec1 - self.info.dec0)) / p_raw
        return p_raw * 10 ** (self.info.dec0 - self.info.dec1)

    def current_tick(self) -> int:
        res = self.rpc.call(self.pool_address, SEL_SLOT0)
        d = res[2:]
        return int(d[64:128], 16)                  # word ke-2 = tick (int24)

    def active_liquidity(self) -> int:
        """Likuiditas aktif pada tick saat ini (state on-chain)."""
        return _int_from_word(self.rpc.call(self.pool_address, SEL_LIQUIDITY))

    def balances(self) -> Tuple[float, float]:
        """Saldo NYATA token0 & token1 yang terkunci di kontrak pool (untuk validasi)."""
        def bal_of(token: str) -> str:
            return "0x70a08231" + "0" * 24 + self.pool_address.lower().replace("0x", "")
        r = self.rpc.batch([(self.info.token0, bal_of(self.info.token0)),
                            (self.info.token1, bal_of(self.info.token1))])
        return (_int_from_word(r[0]) / 10 ** self.info.dec0,
                _int_from_word(r[1]) / 10 ** self.info.dec1)

    # ---------------- konversi likuiditas -> jumlah token ---------------- #
    def _amounts(self, tick_lo: float, tick_hi: float, liquidity: float) -> Tuple[float, float]:
        """Jumlah token0 & token1 (sudah disesuaikan desimal) untuk rentang tick tertentu."""
        sqrt_lo = 1.0001 ** (tick_lo / 2.0)
        sqrt_hi = 1.0001 ** (tick_hi / 2.0)
        amt0_raw = liquidity * (sqrt_hi - sqrt_lo) / (sqrt_lo * sqrt_hi)
        amt1_raw = liquidity * (sqrt_hi - sqrt_lo)
        return amt0_raw / 10 ** self.info.dec0, amt1_raw / 10 ** self.info.dec1

    # ---------------- "order book" dari likuiditas terkonsentrasi -------- #
    def fetch_depth(self, limit: int = 1000) -> DepthSnapshot:
        """Bangun DepthSnapshot dari likuiditas on-chain per rentang tick.

        - rentang di bawah harga  -> posisi berisi QUOTE (siap beli) -> **BID**
        - rentang di atas harga   -> posisi berisi BASE (siap jual)  -> **ASK**
        """
        cur = self.current_tick()
        step = max(self.info.spacing, 1)
        span_ticks = int(self.window_bps)          # 1 tick = 1 bps (0.01%)
        lo_tick = math.floor((cur - span_ticks) / step) * step
        hi_tick = math.ceil((cur + span_ticks) / step) * step
        grid = list(range(lo_tick, hi_tick + 1, step))
        if cur not in grid:
            grid = sorted(grid + [cur])

        # baca liquidityNet tiap tick (batching -> sedikit request HTTP)
        calls = [(self.pool_address, SEL_TICKS + f"{t & ((1 << 256) - 1):064x}") for t in grid]
        results = self.rpc.batch(calls)
        nets = {}
        for t, res in zip(grid, results):
            d = res[2:]
            nets[t] = _int_from_word(d[64:128], signed=True) if len(d) >= 128 else 0

        bids: List[Tuple[float, float]] = []
        asks: List[Tuple[float, float]] = []
        cur_price = self.tick_to_price(cur)
        # Referensi = likuiditas aktif on-chain di tick saat ini. Dari situ kita
        # "berjalan" naik/turun: lewati tick t ke atas -> +net(t), ke bawah -> -net(t).
        l_ref = self.active_liquidity()
        for i in range(len(grid) - 1):
            t_lo, t_hi = grid[i], grid[i + 1]
            if t_lo >= cur:
                liquidity_acc = l_ref + sum(n for t, n in nets.items() if cur < t <= t_lo)
            else:
                liquidity_acc = l_ref - sum(n for t, n in nets.items() if t_lo < t <= cur)
            if liquidity_acc <= 0:
                continue
            # pecah rentang yang mengapung di harga sekarang
            segments = [(t_lo, t_hi)] if not (t_lo < cur < t_hi) else [(t_lo, cur), (cur, t_hi)]
            for a, b in segments:
                amt0, amt1 = self._amounts(a, b, liquidity_acc)
                price = self.tick_to_price((a + b) / 2.0)
                quote_amt, base_amt = (amt0, amt1) if self.info.quote_is_token0 else (amt1, amt0)
                # PENTING: bandingkan HARGA (bukan tick) — arah tick terhadap harga
                # bergantung pada token mana yang jadi quote.
                #   harga < harga sekarang -> posisi memegang QUOTE (siap beli) -> BID
                #   harga > harga sekarang -> posisi memegang BASE  (siap jual) -> ASK
                if price <= cur_price:
                    qty = quote_amt / price if price > 0 else 0.0
                    if qty > 0:
                        bids.append((price, qty))
                else:
                    if base_amt > 0:
                        asks.append((price, base_amt))

        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        return DepthSnapshot(
            ts=int(time.time() * 1000),
            bids=np.array(bids, dtype=float).reshape(-1, 2),
            asks=np.array(asks, dtype=float).reshape(-1, 2),
            update_id=-1,
        )

    # ---------------- OHLCV dari event Swap on-chain --------------------- #
    def fetch_klines(self, interval: str = "5m", limit: int = 72) -> pd.DataFrame:
        """Susun candle dari log event `Swap` — trade yang benar-benar terjadi on-chain."""
        secs = _interval_seconds(interval)
        head = self.rpc.block_number()
        bpc = max(int(secs / SECONDS_PER_BLOCK), 1)
        from_block = max(head - limit * bpc, 0)

        logs: List[dict] = []
        CHUNK = 300
        for start in range(from_block, head + 1, CHUNK):
            logs.extend(self.rpc.get_logs(self.pool_address, [TOPIC_SWAP],
                                          start, min(start + CHUNK - 1, head)))
        if not logs:
            raise RuntimeError("Tidak ada event Swap pada rentang blok yang diminta.")

        rows = []
        for lg in logs:
            blk = int(lg["blockNumber"], 16)
            d = lg["data"][2:]
            words = [d[i:i + 64] for i in range(0, len(d), 64)]
            a0 = _int_from_word(words[0], signed=True)
            a1 = _int_from_word(words[1], signed=True)
            if a0 == 0:
                continue
            price = abs(self._price_from_amounts(a0, a1))
            base_vol = abs(a1 if self.info.quote_is_token0 else a0) / 10 ** self.info.base_decimals
            rows.append((blk, price, base_vol))

        # timestamp blok (batch) -> kelompokkan per candle
        blocks = sorted({b for b, _, _ in rows})
        stamps = self.rpc.block_timestamps(blocks)
        head_ts = stamps.get(head, int(time.time()))
        rows.sort(key=lambda r: r[0])
        data = []
        for blk, price, vol in rows:
            ts = stamps.get(blk, head_ts - (head - blk) * SECONDS_PER_BLOCK)
            data.append((int(ts // secs) * secs, price, vol))

        df = pd.DataFrame(data, columns=["ts", "price", "vol"])
        g = df.groupby("ts")
        out = pd.DataFrame({
            "open": g["price"].first(), "high": g["price"].max(),
            "low": g["price"].min(), "close": g["price"].last(),
            "volume": g["vol"].sum(), "taker_buy_base": np.nan,
        })
        out.index = pd.to_datetime(out.index, unit="s", utc=True)
        out.index.name = "time"
        return out.tail(limit)

    def _price_from_amounts(self, a0: int, a1: int) -> float:
        """Harga quote/base dari rasio jumlah token dalam satu swap."""
        if a0 == 0:
            return float("nan")
        if self.info.quote_is_token0:
            return (abs(a0) / 10 ** self.info.dec0) / (abs(a1) / 10 ** self.info.dec1)
        return (abs(a1) / 10 ** self.info.dec1) / (abs(a0) / 10 ** self.info.dec0)


# =========================================================================== #
# 2) Hyperliquid — CLOB on-chain (appchain HyperCore)
# =========================================================================== #
class HyperliquidFeed:
    """Buku order + candle dari Hyperliquid (state dikomit oleh validator on-chain)."""

    name = "hyperliquid"
    API = "https://api.hyperliquid.xyz/info"
    INTERVALS = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
                 "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d"}

    provider_name = "hyperliquid-onchain"     # kompatibel dengan dashboard

    def __init__(self, symbol: str = "BTC", n_sig_figs: int = 3):
        # nSigFigs menentukan agregasi level:
        #   5 (default API) -> buku hanya ±2 bps  (terlalu sempit untuk cari wall)
        #   3                -> ±220 bps, 20 bucket @ ~11 bps  (pas untuk deteksi wall)
        #   2                -> ±2200 bps (terlalu kasar)
        self.symbol = symbol.upper()
        self.n_sig_figs = n_sig_figs

    def fetch_klines(self, interval: str = "5m", limit: int = 288) -> pd.DataFrame:
        iv = self.INTERVALS.get(interval, "5m")
        end = int(time.time() * 1000)
        start = end - int(_interval_seconds(interval) * 1000 * (limit + 2))
        r = requests.post(self.API, json={"type": "candleSnapshot", "req": {
            "coin": self.symbol, "interval": iv, "startTime": start, "endTime": end}},
            timeout=TIMEOUT)
        r.raise_for_status()
        raw = r.json()
        if not raw:
            raise RuntimeError(f"Hyperliquid: tidak ada candle untuk {self.symbol}")
        df = pd.DataFrame([{"open": float(c["o"]), "high": float(c["h"]),
                            "low": float(c["l"]), "close": float(c["c"]),
                            "volume": float(c["v"]), "taker_buy_base": np.nan} for c in raw])
        df.index = pd.to_datetime([int(c["t"]) for c in raw], unit="ms", utc=True)
        df.index.name = "time"
        return df.tail(limit)

    def fetch_depth(self, limit: int = 1000) -> DepthSnapshot:
        r = requests.post(self.API, json={"type": "l2Book", "coin": self.symbol,
                                          "nSigFigs": self.n_sig_figs}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        levels = d.get("levels", [[], []])
        bids = [(float(x["px"]), float(x["sz"])) for x in levels[0]]
        asks = [(float(x["px"]), float(x["sz"])) for x in levels[1]]
        return DepthSnapshot(ts=int(d.get("time", time.time() * 1000)),
                             bids=_to_book(bids), asks=_to_book(asks))

    @classmethod
    def coins(cls) -> List[str]:
        r = requests.post(cls.API, json={"type": "meta"}, timeout=TIMEOUT)
        return [a["name"] for a in r.json()["universe"]]


# =========================================================================== #
# 3) dYdX v4 — CLOB appchain Cosmos (settlement on-chain)
# =========================================================================== #
class DydxFeed:
    """Buku order & candle dYdX v4 lewat indexer publik."""

    name = "dydx-v4"
    API = "https://indexer.dydx.trade/v4"
    RES = {"1m": "1MIN", "5m": "5MINS", "15m": "15MINS", "30m": "30MINS",
           "1h": "1HOUR", "4h": "4HOURS", "1d": "1DAY"}

    def __init__(self, symbol: str = "BTC-USD"):
        sym = symbol.upper()
        self.symbol = sym if "-" in sym else f"{sym[:-4]}-{sym[-4:]}" if sym.endswith("USD") \
            else f"{sym.replace('USDT','')}-USD"

    def fetch_klines(self, interval: str = "5m", limit: int = 288) -> pd.DataFrame:
        r = requests.get(f"{self.API}/candles/perpetualMarkets/{self.symbol}",
                         params={"resolution": self.RES.get(interval, "5MINS"), "limit": limit},
                         timeout=TIMEOUT)
        r.raise_for_status()
        raw = r.json().get("candles", [])
        if not raw:
            raise RuntimeError(f"dYdX: tidak ada candle untuk {self.symbol}")
        raw = sorted(raw, key=lambda c: c["startedAt"])
        df = pd.DataFrame([{"open": float(c["open"]), "high": float(c["high"]),
                            "low": float(c["low"]), "close": float(c["close"]),
                            "volume": float(c["baseTokenVolume"]), "taker_buy_base": np.nan}
                           for c in raw])
        df.index = pd.to_datetime([c["startedAt"] for c in raw], utc=True, format="ISO8601")
        df.index.name = "time"
        return df.tail(limit)

    def fetch_depth(self, limit: int = 1000) -> DepthSnapshot:
        r = requests.get(f"{self.API}/orderbooks/perpetualMarket/{self.symbol}", timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        bids = [(float(x["price"]), float(x["size"])) for x in d.get("bids", [])]
        asks = [(float(x["price"]), float(x["size"])) for x in d.get("asks", [])]
        return DepthSnapshot(ts=int(time.time() * 1000),
                             bids=_to_book(bids), asks=_to_book(asks))


# =========================================================================== #
# 4) Kolektor REAL-TIME on-chain (WebSocket Hyperliquid)
# =========================================================================== #
class HyperliquidCollector(threading.Thread):
    """Kolektor real-time untuk buku order ON-CHAIN Hyperliquid.

    Aliran data (semuanya dari chain HyperCore, bukan simulasi):
      * channel `l2Book` -> snapshot buku order (limit order yang sedang resting)
      * channel `trades` -> FILL yang benar-benar terjadi (lengkap dengan alamat wallet)
      * channel `candle` -> update candle terakhir
    Snapshot buku disimpan tiap `sample_every` detik ke buffer rolling -> inilah bahan
    heatmap order book ASLI (bukan proksi volume).
    """

    WS_URL = "wss://api.hyperliquid.xyz/ws"

    # lebar 1 candle -> freq pandas, dipakai untuk menempatkan FILL ke bucket candle
    FREQ = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
            "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1D"}

    def __init__(self, coin: str = "BTC", interval: str = "1m", limit: int = 300,
                 n_sig_figs: int = 3, sample_every: float = 1.0,
                 max_snapshots: int = 900, max_trades: int = 3000,
                 kline_refresh: float = 60.0):
        super().__init__(daemon=True, name="HLCollector")
        self.coin = coin.upper()
        self.interval = interval
        self.limit = limit
        self.n_sig_figs = n_sig_figs
        self.sample_every = sample_every
        self.kline_refresh = kline_refresh
        self.feed = HyperliquidFeed(self.coin, n_sig_figs)

        self.lock = threading.RLock()
        self.df: Optional[pd.DataFrame] = None
        self.deep: Optional[DepthSnapshot] = None
        self.stream: deque = deque(maxlen=max_snapshots)
        self.trades: deque = deque(maxlen=max_trades)
        self._freq = self.FREQ.get(interval, "1min")

        # harga terakhir dari FILL (lebih segar daripada buku: buku hanya di-push
        # tiap ~5 detik oleh Hyperliquid, sedangkan FILL datang tiap transaksi)
        self.last_price: Optional[float] = None
        self.last_trade_ts: int = 0
        self.last_side: Optional[str] = None

        self.status = "starting"
        self.error: Optional[str] = None
        self._ws_ok = False
        self._stop = threading.Event()

    # ---------- akses thread-safe ----------
    def snapshot_df(self):
        with self.lock:
            return None if self.df is None else self.df.copy()

    def snapshot_deep(self):
        with self.lock:
            return self.deep

    def snapshot_stream(self) -> List[DepthSnapshot]:
        with self.lock:
            return list(self.stream)

    def snapshot_trades(self) -> List[dict]:
        with self.lock:
            return list(self.trades)

    def live_price(self):
        """(harga, timestamp_ms, sisi) dari FILL terakhir — update sub-detik."""
        with self.lock:
            return self.last_price, self.last_trade_ts, self.last_side

    @property
    def is_ws_connected(self) -> bool:
        return self._ws_ok

    def stop(self):
        self._stop.set()

    # ---------- loop utama ----------
    def run(self):
        try:
            self.df = self.feed.fetch_klines(self.interval, self.limit)
            self.deep = self.feed.fetch_depth()
            self.status = "live"
            threading.Thread(target=self._ws_loop, daemon=True, name="HL-WS").start()

            last_s = last_k = time.time()
            last_book_ts = -1
            while not self._stop.is_set():
                now = time.time()
                # Hyperliquid hanya mengirim l2Book tiap ~5 detik; simpan snapshot
                # HANYA kalau timestamp buku berubah (bukan duplikat tiap detik)
                if (self.deep is not None and now - last_s >= self.sample_every
                        and self.deep.ts != last_book_ts):
                    with self.lock:
                        self.stream.append(self.deep)
                    last_book_ts = self.deep.ts
                    last_s = now
                if now - last_k >= self.kline_refresh:
                    self.df = self.feed.fetch_klines(self.interval, self.limit)
                    last_k = now
                time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            self.status = "error"

    # ---------- WebSocket ----------
    def _ws_loop(self):
        import websocket
        while not self._stop.is_set():
            try:
                ws = websocket.WebSocket(timeout=15)
                ws.connect(self.WS_URL)
                for sub in ({"type": "l2Book", "coin": self.coin, "nSigFigs": self.n_sig_figs},
                            {"type": "trades", "coin": self.coin},
                            {"type": "candle", "coin": self.coin, "interval": self.interval}):
                    ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        continue
                    msg = json.loads(raw)
                    ch = msg.get("channel")
                    if ch == "l2Book":
                        self._on_book(msg["data"])
                    elif ch == "trades":
                        self._on_trades(msg["data"])
                    elif ch == "candle":
                        self._on_candle(msg["data"])
                    self._ws_ok = True
            except Exception as e:  # noqa: BLE001 - auto reconnect
                self._ws_ok = False
                self.error = f"WS {type(e).__name__}: {e}"
                time.sleep(3)

    def _on_book(self, d: dict):
        levels = d.get("levels", [[], []])
        bids = [(float(x["px"]), float(x["sz"])) for x in levels[0]]
        asks = [(float(x["px"]), float(x["sz"])) for x in levels[1]]
        snap = DepthSnapshot(ts=int(d.get("time", time.time() * 1000)),
                             bids=_to_book(bids), asks=_to_book(asks))
        with self.lock:
            self.deep = snap

    def _on_trades(self, fills: List[dict]):
        """Simpan FILL + pakai sebagai sumber harga live dan update candle berjalan."""
        for f in fills:
            ts = int(f.get("time", 0))
            px = float(f.get("px", 0) or 0)
            sz = float(f.get("sz", 0) or 0)
            with self.lock:
                self.trades.append({"ts": ts, "side": f.get("side"), "px": px, "sz": sz,
                                    "hash": f.get("hash", "")[:18],
                                    "taker": (f.get("users") or [""])[0][:10]})
                if px > 0:
                    self.last_price, self.last_trade_ts = px, ts
                    self.last_side = f.get("side")
            self._apply_fill(ts, px, sz)

    def _apply_fill(self, ts_ms: int, px: float, sz: float) -> None:
        """Gerakkan candle terakhir memakai FILL yang baru terjadi.

        Tanpa ini, candle baru berubah saat push `candle` dari server (tiap ~1
        menit) — padahal harga sudah bergerak puluhan kali di antaranya.
        """
        if not ts_ms or px <= 0:
            return
        with self.lock:
            if self.df is None or not len(self.df):
                return
            bucket = pd.to_datetime(int(ts_ms), unit="ms", utc=True).floor(self._freq)
            if bucket in self.df.index:
                row = self.df.loc[bucket]
                self.df.loc[bucket, "close"] = px
                self.df.loc[bucket, "high"] = max(float(row["high"]), px)
                self.df.loc[bucket, "low"] = min(float(row["low"]), px)
                if sz:
                    self.df.loc[bucket, "volume"] = float(row["volume"]) + sz
            elif bucket > self.df.index[-1]:
                self.df.loc[bucket] = {"open": px, "high": px, "low": px, "close": px,
                                       "volume": sz or 0.0, "taker_buy_base": np.nan}
                self.df = self.df.iloc[-self.limit:]

    def _on_candle(self, c: dict):
        try:
            ts = pd.to_datetime(int(c["t"]), unit="ms", utc=True)
            row = {"open": float(c["o"]), "high": float(c["h"]), "low": float(c["l"]),
                   "close": float(c["c"]), "volume": float(c["v"]), "taker_buy_base": np.nan}
        except Exception:  # noqa: BLE001
            return
        with self.lock:
            if self.df is None:
                return
            if ts in self.df.index:
                for k, v in row.items():
                    self.df.loc[ts, k] = v
            else:
                self.df.loc[ts] = row
                self.df = self.df.iloc[-self.limit:]


# =========================================================================== #
# Helper & factory
# =========================================================================== #
def _interval_seconds(interval: str) -> int:
    unit = interval[-1].lower()
    mult = int(interval[:-1] or 1)
    return {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 60) * mult


def get_feed(provider: str, symbol: str, **kw):
    """Factory: 'hyperliquid' | 'dydx' | 'uniswap'."""
    p = provider.lower()
    if p in ("hyperliquid", "hl"):
        return HyperliquidFeed(symbol)
    if p in ("dydx", "dydx-v4"):
        return DydxFeed(symbol)
    if p in ("uniswap", "uniswap-v3", "uni"):
        return UniswapV3Feed(symbol, **kw)
    raise ValueError(f"Provider on-chain tidak dikenal: {provider}")


# =========================================================================== #
# Uji mandiri
# =========================================================================== #
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Uji feed on-chain")
    ap.add_argument("--provider", default="hyperliquid",
                    choices=["hyperliquid", "dydx", "uniswap"])
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--list-coins", action="store_true")
    a = ap.parse_args()

    if a.list_coins:
        coins = HyperliquidFeed.coins()
        print(f"{len(coins)} coin Hyperliquid:", ", ".join(coins[:40]), "...")
        print("emas/logam:", [c for c in coins if "PAXG" in c or "XAU" in c or "GOLD" in c])
        raise SystemExit(0)

    feed = get_feed(a.provider, a.symbol)
    df = feed.fetch_klines(a.interval, a.limit)
    snap = feed.fetch_depth()
    print(f"provider : {feed.name}")
    print(f"candle   : {len(df)} x {a.interval} | terakhir close={df['close'].iloc[-1]:,.4f}")
    print(f"book     : {len(snap.bids)} bid / {len(snap.asks)} ask")
    if len(snap.bids) and len(snap.asks):
        print(f"mid={snap.mid:,.4f} | spread={snap.spread_bps:.2f} bps | "
              f"imb ±2%={snap.imbalance(0.02):+.1%}")
        print("bid teratas:", [(round(p, 4), round(q, 4)) for p, q in snap.bids[:3].tolist()])
        print("ask teratas:", [(round(p, 4), round(q, 4)) for p, q in snap.asks[:3].tolist()])
