"""
whale_feed.py
=============
Layer DATA (data source layer) — mengambil OHLCV dan Order Book (depth) dari API publik exchange.

Kenapa dipisah? Supaya layer analitik & visualisasi tidak peduli dari mana data berasal.
Jika nanti Anda mau ganti sumber (Binance -> OKX -> DEX/on-chain), cukup tambahkan class
feed baru dengan 2 method wajib: `fetch_klines()` dan `fetch_depth()`.

Sumber default:
  * REST  : https://data-api.binance.vision  (mirror publik data Binance, tanpa API key)
  * WS    : wss://data-stream.binance.vision:9443 (stream kline + partial depth real-time)
  * Fallback REST: OKX (https://www.okx.com/api/v5/market/...) jika Binance tidak reachable

Catatan "on-chain": data di sini adalah order book CEX (off-chain, tersentralisasi).
Untuk data on-chain/DEX (mis. Uniswap, GMX, Hyperliquid) polanya sama: buat class feed
yang mengembalikan objek `DepthSnapshot` — chart-nya tidak perlu diubah sama sekali.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Sequence

import numpy as np
import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# Konstanta endpoint
# --------------------------------------------------------------------------- #
BINANCE_REST = "https://data-api.binance.vision"
BINANCE_WS = "wss://data-stream.binance.vision:9443"
OKX_REST = "https://www.okx.com"

HTTP_TIMEOUT = 15  # detik


# --------------------------------------------------------------------------- #
# Struktur data
# --------------------------------------------------------------------------- #
@dataclass
class DepthSnapshot:
    """Satu snapshot order book.

    Attributes
    ----------
    ts    : timestamp epoch (millidetik)
    bids  : array (N, 2) -> [[price, qty], ...] terurut harga TERTINGGI lebih dulu
    asks  : array (N, 2) -> [[price, qty], ...] terurut harga TERENDAH lebih dulu
    """

    ts: int
    bids: np.ndarray
    asks: np.ndarray
    update_id: int = -1      # lastUpdateId (Binance) — penanda sinkronisasi diff stream

    # --- metrik cepat ----------------------------------------------------- #
    @property
    def best_bid(self) -> float:
        return float(self.bids[0, 0]) if len(self.bids) else np.nan

    @property
    def best_ask(self) -> float:
        return float(self.asks[0, 0]) if len(self.asks) else np.nan

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_bps(self) -> float:
        """Spread bid-ask dalam basis point (0.01%)."""
        if not len(self.bids) or not len(self.asks):
            return np.nan
        return (self.best_ask - self.best_bid) / self.mid * 10_000

    def side_liquidity(self, side: str, pct: float = 0.02) -> float:
        """Total quantity dalam rentang ±pct dari mid price (default ±2%)."""
        book = self.bids if side == "bid" else self.asks
        if not len(book):
            return 0.0
        mid = self.mid
        lim = pct / 100.0
        mask = np.abs(book[:, 0] - mid) <= mid * lim
        return float(book[mask, 1].sum())

    def imbalance(self, pct: float = 0.02) -> float:
        """Order book imbalance: (bid - ask) / (bid + ask) pada rentang ±pct.

        > 0 -> dinding BID lebih tebal (cenderung support)
        < 0 -> dinding ASK lebih tebal (cenderung resistance)
        """
        b = self.side_liquidity("bid", pct)
        a = self.side_liquidity("ask", pct)
        if b + a == 0:
            return 0.0
        return (b - a) / (b + a)


# --------------------------------------------------------------------------- #
# Penanganan simbol / pair
# --------------------------------------------------------------------------- #
# Beberapa aset tidak punya pair spot dengan nama "klasik"-nya.
# Contoh: XAU (emas) tidak diperdagangkan langsung sebagai XAUUSDT di exchange
# besar — yang ada adalah emas ter-tokenisasi: XAUT (Tether Gold) & PAXG (Paxos Gold).
# Alias di bawah membuat permintaan "XAUUSDT" otomatis diarahkan ke pair yang ada.
SYMBOL_ALIASES = {
    "XAUUSDT": "XAUTUSDT",    # emas  -> Tether Gold
    "GOLDUSDT": "XAUTUSDT",
    "GOLD": "XAUTUSDT",
    "XAU": "XAUTUSDT",
    "XAGUSDT": "PAXGUSDT",    # perak -> tidak ada pair spot; fallback ke emas PAXG
}

# dipakai bila endpoint exchangeInfo/ticker sedang tidak bisa diakses
FALLBACK_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT", "ARBUSDT", "OPUSDT",
    "PEPEUSDT", "SUIUSDT", "XAUTUSDT", "PAXGUSDT",
]

_STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "AEUR", "USD1", "XUSD"}
_SYMBOLS_CACHE = {"ts": 0.0, "data": []}
_EXISTS_CACHE: dict[str, bool] = {}


def normalize_symbol(symbol: str) -> tuple[str, str | None]:
    """Rapikan nama pair & terapkan alias.

    Returns
    -------
    (symbol_resolved, catatan)  — catatan berisi penjelasan bila dialihkan/diperbaiki.
    """
    s = (symbol or "").upper().strip().replace("/", "").replace("-", "").replace("_", "")
    if not s:
        return "BTCUSDT", "simbol kosong -> default BTCUSDT"
    if s in SYMBOL_ALIASES:
        target = SYMBOL_ALIASES[s]
        return target, (f"{s} tidak tersedia sebagai pair spot -> "
                        f"dialihkan ke {target}")
    return s, None


def list_symbols(quote: str = "USDT", top: int = 200, refresh: bool = False) -> List[str]:
    """Daftar pair aktif terpopuler (urut berdasarkan volume 24 jam USDT)."""
    now = time.time()
    if not refresh and _SYMBOLS_CACHE["data"] and now - _SYMBOLS_CACHE["ts"] < 900:
        data = _SYMBOLS_CACHE["data"]
    else:
        data = []
        try:
            r = requests.get(f"{BINANCE_REST}/api/v3/ticker/24hr", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            rows = [x for x in r.json()
                    if x["symbol"].endswith(quote)
                    and x["symbol"][: -len(quote)] not in _STABLE_BASES]
            rows.sort(key=lambda x: -float(x.get("quoteVolume") or 0))
            data = [x["symbol"] for x in rows]
            _SYMBOLS_CACHE.update(ts=now, data=data)
        except Exception:  # noqa: BLE001 - pakai daftar statis sebagai cadangan
            data = list(FALLBACK_SYMBOLS)
    return data[:top]


def symbol_exists(symbol: str) -> bool:
    """Cek apakah pair benar-benar ada di exchange (hasilnya di-cache)."""
    sym, _ = normalize_symbol(symbol)
    if sym in _EXISTS_CACHE:
        return _EXISTS_CACHE[sym]
    try:
        r = requests.get(f"{BINANCE_REST}/api/v3/exchangeInfo",
                         params={"symbol": sym}, timeout=HTTP_TIMEOUT)
        ok = r.status_code == 200 and bool(r.json().get("symbols"))
    except Exception:  # noqa: BLE001 - kalau tidak bisa memvalidasi, anggap valid
        ok = True
    _EXISTS_CACHE[sym] = ok
    return ok


# --------------------------------------------------------------------------- #
# Helper parsing generik
# --------------------------------------------------------------------------- #
def _to_book(levels: Sequence[Sequence[str]]) -> np.ndarray:
    """Ubah list [[price, qty], ...] (string) menjadi array float (N, 2)."""
    if not levels:
        return np.zeros((0, 2), dtype=float)
    return np.asarray(levels, dtype=float).reshape(-1, 2)


# --------------------------------------------------------------------------- #
# Feed: Binance (REST)
# --------------------------------------------------------------------------- #
class BinanceFeed:
    """REST client untuk data publik Binance (tanpa API key)."""

    name = "binance"

    def __init__(self, symbol: str = "BTCUSDT", rest_base: str = BINANCE_REST,
                 session: Optional[requests.Session] = None):
        self.symbol = symbol.upper()
        self.rest_base = rest_base
        self.session = session or requests.Session()

    # ---------------- OHLCV ------------------------------------------------ #
    def fetch_klines(self, interval: str = "1m", limit: int = 500) -> pd.DataFrame:
        """Ambil candle OHLCV. Kolom: open, high, low, close, volume, taker_buy_base."""
        url = f"{self.rest_base}/api/v3/klines"
        params = {"symbol": self.symbol, "interval": interval, "limit": int(limit)}
        r = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        raw = r.json()

        cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
        df = pd.DataFrame(raw, columns=cols[: len(raw[0])])

        # index waktu (UTC) + konversi ke float
        idx = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.drop(columns=["open_time", "close_time", "quote_volume",
                              "taker_buy_quote", "ignore"], errors="ignore")
        for c in df.columns:
            df[c] = pd.to_numeric(df[c])
        df.index = idx
        df.index.name = "time"
        return df

    # ---------------- Order book ------------------------------------------- #
    def fetch_depth(self, limit: int = 1000) -> DepthSnapshot:
        """Ambil order book (limit valid Binance: 5,10,20,50,100,500,1000,5000)."""
        url = f"{self.rest_base}/api/v3/depth"
        params = {"symbol": self.symbol, "limit": int(limit)}
        r = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        return DepthSnapshot(
            ts=int(time.time() * 1000),
            bids=_to_book(d.get("bids", [])),
            asks=_to_book(d.get("asks", [])),
            update_id=int(d.get("lastUpdateId", -1)),
        )


# --------------------------------------------------------------------------- #
# Feed: OKX (fallback)
# --------------------------------------------------------------------------- #
class OkxFeed:
    """Fallback REST client untuk OKX spot (format diseragamkan dengan BinanceFeed)."""

    name = "okx"

    def __init__(self, symbol: str = "BTCUSDT", session: Optional[requests.Session] = None):
        # BTCUSDT -> BTC-USDT
        s = symbol.upper()
        self.symbol = f"{s[:-4]}-{s[-4:]}" if s.endswith("USDT") and "-" not in s else s
        self.session = session or requests.Session()

    def fetch_klines(self, interval: str = "1m", limit: int = 500) -> pd.DataFrame:
        # konversi interval Binance-style -> OKX-style (1m,5m,15m,1H,4H,1D ...)
        unit = interval[-1].upper()
        mult = interval[:-1] or "1"
        okx_bar = {"m": f"{mult}m", "h": f"{mult}H", "d": f"{mult}D", "w": f"{mult}W"}[unit.lower()]

        r = self.session.get(
            f"{OKX_REST}/api/v5/market/candles",
            params={"instId": self.symbol, "bar": okx_bar, "limit": str(int(limit))},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        raw = r.json().get("data", [])
        # OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm] — urutan TERBARU dulu
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close",
                                        "volume", "vol_ccy", "vol_quote", "confirm"])
        df = df.iloc[::-1]  # balik jadi ascending
        df["taker_buy_base"] = np.nan  # OKX publik tidak expose taker buy di endpoint ini
        df.index = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
        df.index.name = "time"
        for c in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
            df[c] = pd.to_numeric(df[c])
        return df[["open", "high", "low", "close", "volume", "taker_buy_base"]]

    def fetch_depth(self, limit: int = 1000) -> DepthSnapshot:
        sz = max(1, min(int(limit), 400))  # OKX publik maksimal 400 level
        r = self.session.get(
            f"{OKX_REST}/api/v5/market/books",
            params={"instId": self.symbol, "sz": str(sz)},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()["data"][0]
        return DepthSnapshot(
            ts=int(d["ts"]),
            bids=_to_book(d.get("bids", [])),
            asks=_to_book(d.get("asks", [])),
        )


# --------------------------------------------------------------------------- #
# Feed agregat: coba beberapa exchange berurutan (auto fallback)
# --------------------------------------------------------------------------- #
class ExchangeFeed:
    """Mencoba beberapa feed berurutan; feed pertama yang sukses dipakai.

    Contoh: ExchangeFeed("BTCUSDT") -> Binance, kalau gagal (451/timeout) -> OKX.
    """

    def __init__(self, symbol: str = "BTCUSDT", providers: Optional[Sequence[str]] = None):
        sym, note = normalize_symbol(symbol)
        self.requested_symbol = (symbol or "").upper()
        self.note = note                      # penjelasan bila simbol dialihkan
        self.symbol = sym
        self.session = requests.Session()
        names = providers or ("binance", "okx")
        registry = {"binance": BinanceFeed, "okx": OkxFeed}
        self.feeds = [registry[n](self.symbol, session=self.session) for n in names]
        self.active = self.feeds[0]

    def fetch_klines(self, interval: str = "1m", limit: int = 500) -> pd.DataFrame:
        last_err = None
        for f in self.feeds:
            try:
                df = f.fetch_klines(interval, limit)
                self.active = f
                return df
            except Exception as e:  # noqa: BLE001 - fallback chain
                last_err = e
        raise RuntimeError(f"Semua provider gagal mengambil klines: {last_err}")

    def fetch_depth(self, limit: int = 1000) -> DepthSnapshot:
        last_err = None
        for f in self.feeds:
            try:
                snap = f.fetch_depth(limit)
                self.active = f
                return snap
            except Exception as e:  # noqa: BLE001 - fallback chain
                last_err = e
        raise RuntimeError(f"Semua provider gagal mengambil depth: {last_err}")

    @property
    def provider_name(self) -> str:
        return getattr(self.active, "name", "unknown")


# --------------------------------------------------------------------------- #
# Kolektor REAL-TIME (REST polling + WebSocket) — dipakai dashboard & mode --live
# --------------------------------------------------------------------------- #
class DesyncError(RuntimeError):
    """Dilempar bila ada gap pada stream diff — order book harus di-snapshot ulang."""


class OrderBookReconstructor:
    """Memelihara order book L2 PENUH dari snapshot REST + diff stream WebSocket.

    Protokol Binance spot:
      1. Ambil snapshot REST (punya `lastUpdateId`).
      2. Buffer event diff dari WS; buang event dengan `u <= lastUpdateId`.
      3. Event pertama yang diproses harus memenuhi  U <= lastUpdateId + 1 <= u.
      4. Setiap event berikutnya harus tersambung (U == lastUpdateId + 1);
         kalau tidak -> DesyncError -> ambil snapshot ulang.

    Hasilnya: heatmap bisa memakai KEDALAMAN PENUH (bukan cuma top-20 level),
    sehingga whale wall yang agak jauh dari harga tetap terlihat.
    """

    def __init__(self, max_levels: int = 5000):
        self.max_levels = max_levels
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id: Optional[int] = None
        self.buffer: List[dict] = []
        self.ready = False
        self.lock = threading.RLock()

    # ---------------- snapshot awal / resync ------------------------------- #
    def set_snapshot(self, snap: DepthSnapshot, last_update_id: int) -> None:
        """Pasang snapshot REST lalu proses event diff yang tertahan (di dalam lock)."""
        with self.lock:
            self.bids = {float(p): float(q) for p, q in snap.bids if float(q) > 0}
            self.asks = {float(p): float(q) for p, q in snap.asks if float(q) > 0}
            self.last_update_id = int(last_update_id)
            buffered, self.buffer = self.buffer, []
            self.ready = True
            for i, ev in enumerate(buffered):
                try:
                    self._apply(ev)
                except DesyncError:
                    # event ini (dan sesudahnya) belum bisa dipakai -> kembalikan ke buffer
                    self.ready = False
                    self.buffer = buffered[i:] + self.buffer
                    break

    def invalidate(self) -> None:
        """Tandai book tidak valid (dipanggil saat koneksi WS putus/reconnect)."""
        with self.lock:
            self.ready = False

    # ---------------- diff stream ----------------------------------------- #
    def add_diff(self, ev: dict) -> bool:
        """Terapkan 1 event diff. True = berhasil diterapkan."""
        with self.lock:
            if not self.ready:
                self.buffer.append(ev)
                if len(self.buffer) > 5000:          # jaga memori saat menunggu resync
                    del self.buffer[: len(self.buffer) - 5000]
                return False
            try:
                return self._apply(ev)
            except DesyncError:
                self.buffer.append(ev)               # simpan untuk diproses setelah resync
                raise

    def _apply(self, ev: dict) -> bool:
        U, u = int(ev["U"]), int(ev["u"])
        if u <= self.last_update_id:          # event kadaluarsa -> abaikan
            return False
        if U > self.last_update_id + 1:       # ada event hilang -> butuh resync
            self.ready = False
            raise DesyncError(f"gap: lastUpdateId={self.last_update_id} U={U} u={u}")
        for book, key in ((self.bids, "b"), (self.asks, "a")):
            for price, qty in ev.get(key, []):
                p, q = float(price), float(qty)
                if q <= 0:
                    book.pop(p, None)          # qty 0 = level dihapus
                else:
                    book[p] = q
        self.last_update_id = u
        return True

    # ---------------- hasil ------------------------------------------------ #
    def snapshot(self, ts: Optional[int] = None) -> DepthSnapshot:
        """Ambil order book saat ini sebagai `DepthSnapshot` terurut."""
        ts = ts or int(time.time() * 1000)
        with self.lock:
            bids = sorted(((p, q) for p, q in self.bids.items() if q > 0),
                          key=lambda x: -x[0])[: self.max_levels]
            asks = sorted(((p, q) for p, q in self.asks.items() if q > 0),
                          key=lambda x: x[0])[: self.max_levels]
        return DepthSnapshot(
            ts=ts,
            bids=np.array(bids, dtype=float).reshape(-1, 2),
            asks=np.array(asks, dtype=float).reshape(-1, 2),
        )


class LiveCollector(threading.Thread):
    """Thread background yang terus mengumpulkan:
        - `df`     : OHLCV terbaru (candle terakhir di-update lewat stream kline)
        - `stream` : buffer rolling snapshot order book (bahan heatmap real-time)
        - `deep`   : order book terdalam terakhir (bahan deteksi whale wall)

    Sumber order book (berurutan sesuai kualitas):
      1. Rekonstruksi L2: snapshot REST(5000 level) + diff stream WS  -> `stream` diisi tiap `sample_every` detik
      2. Fallback: polling REST `depth_limit` tiap `depth_refresh` detik (kalau WS putus)

    Semua state hanya boleh dibaca lewat method `snapshot_*()` (thread-safe).
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1m",
        limit: int = 500,
        depth_limit: int = 5000,     # 5000 level = deteksi whale wall lebih luas
        sample_every: float = 1.0,   # detik, 1 kolom heatmap per sampling
        kline_refresh: float = 20.0, # detik, sinkronisasi ulang OHLCV lewat REST
        depth_refresh: float = 8.0,  # detik, polling REST saat WS tidak aktif
        resync_every: float = 180.0, # detik, snapshot ulang untuk koreksi drift
        max_snapshots: int = 900,    # kapasitas buffer heatmap (±15 menit @1/detik)
        use_ws: bool = True,
    ):
        super().__init__(daemon=True, name="LiveCollector")
        self.feed = ExchangeFeed(symbol)
        self.symbol = symbol.upper()
        self.interval = interval
        self.limit = limit
        self.depth_limit = depth_limit
        self.sample_every = sample_every
        self.kline_refresh = kline_refresh
        self.depth_refresh = depth_refresh
        self.resync_every = resync_every
        self.use_ws = use_ws

        self.lock = threading.RLock()
        self.book = OrderBookReconstructor(max_levels=depth_limit)
        self.df: Optional[pd.DataFrame] = None
        self.deep: Optional[DepthSnapshot] = None
        self.stream: Deque[DepthSnapshot] = deque(maxlen=max_snapshots)
        self.last_price: float = np.nan
        self.status: str = "starting"
        self.error: Optional[str] = None
        self._ws_ok = False            # True setelah pesan WS pertama diterima
        self._need_resync = False
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    # Utilitas baca (thread-safe)
    # ------------------------------------------------------------------ #
    def snapshot_df(self) -> Optional[pd.DataFrame]:
        with self.lock:
            return None if self.df is None else self.df.copy()

    def snapshot_deep(self) -> Optional[DepthSnapshot]:
        with self.lock:
            return self.deep

    def snapshot_stream(self) -> List[DepthSnapshot]:
        with self.lock:
            return list(self.stream)

    @property
    def is_ws_connected(self) -> bool:
        return self._ws_ok

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    # Loop utama
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            self._refresh_klines()
            self._refresh_depth()               # snapshot awal (REST)
            self.status = "live"

            if self.use_ws:
                threading.Thread(target=self._ws_loop, daemon=True,
                                 name="WSStream").start()

            last_k = last_d = last_r = last_s = time.time()
            while not self._stop.is_set():
                now = time.time()

                # 1) simpan 1 kolom heatmap tiap `sample_every` detik
                if self.book.ready and now - last_s >= self.sample_every:
                    snap = self.book.snapshot(int(now * 1000))
                    with self.lock:
                        self.stream.append(snap)
                        self.deep = snap
                        if len(snap.bids) and len(snap.asks):
                            self.last_price = snap.mid
                    last_s = now

                # 2) sinkronisasi OHLCV
                if now - last_k >= self.kline_refresh:
                    self._refresh_klines()
                    last_k = now

                # 3) resync order book berkala / saat desync
                if self._need_resync or now - last_r >= self.resync_every:
                    self._refresh_depth()
                    self._need_resync = False
                    last_r = now

                # 4) fallback: WS mati -> pakai polling REST sebagai bahan heatmap
                if not self._ws_ok and now - last_d >= self.depth_refresh:
                    snap = self.feed.fetch_depth(self.depth_limit)
                    with self.lock:
                        self.stream.append(snap)
                        self.deep = snap
                    last_d = now

                time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            self.status = "error"

    # ---------------- pembaruan REST ------------------------------------ #
    def _refresh_klines(self) -> None:
        df = self.feed.fetch_klines(self.interval, self.limit)
        with self.lock:
            self.df = df
            self.last_price = float(df["close"].iloc[-1])

    def _refresh_depth(self) -> None:
        snap = self.feed.fetch_depth(self.depth_limit)
        with self.lock:
            self.deep = snap
        if snap.update_id and snap.update_id > 0:  # Binance: bisa disinkronkan
            self.book.set_snapshot(snap, snap.update_id)

    # ---------------- WebSocket ----------------------------------------- #
    def _ws_loop(self) -> None:
        """Terima stream gabungan: diff order book (@100ms) + kline."""
        import websocket  # import lokal supaya mode non-live tetap ringan

        sym = self.symbol.lower()
        streams = f"{sym}@depth@100ms/{sym}@kline_{self.interval}"
        url = f"{BINANCE_WS}/stream?streams={streams}"

        while not self._stop.is_set():
            try:
                ws = websocket.WebSocket(timeout=10)
                ws.connect(url)
                # koneksi baru = event selama putus hilang -> wajib snapshot ulang
                self.book.invalidate()
                self._need_resync = True
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    stream = str(payload.get("stream", ""))
                    data = payload.get("data", payload)
                    if "depth" in stream:
                        try:
                            self._on_depth_diff(data)
                        except DesyncError as e:
                            # PENTING: jangan putus koneksi (itu hanya menambah event
                            # yang hilang). Terus terima & buffer; loop utama akan resync.
                            self._need_resync = True
                            self.error = f"resync: {e}"
                    elif "kline" in stream:
                        self._on_kline(data.get("k", data))
                    self._ws_ok = True
            except Exception as e:  # noqa: BLE001 - auto reconnect
                self._ws_ok = False
                self.book.invalidate()
                self.error = f"WS {type(e).__name__}: {e}"
                time.sleep(3)

    def _on_depth_diff(self, d: dict) -> None:
        self.book.add_diff(d)

    def _on_kline(self, k: dict) -> None:
        """Update candle terakhir secara real-time (tanpa menunggu REST polling)."""
        try:
            ts = pd.to_datetime(int(k["t"]), unit="ms", utc=True)
            row = {
                "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
                "close": float(k["c"]), "volume": float(k["v"]),
                "taker_buy_base": float(k.get("V", np.nan)),
            }
        except Exception:  # noqa: BLE001
            return
        with self.lock:
            if self.df is None:
                return
            if ts in self.df.index:
                for c, v in row.items():
                    self.df.loc[ts, c] = v
            else:  # candle baru -> tambahkan & jaga panjang tetap `limit`
                self.df.loc[ts] = row
                self.df = self.df.iloc[-self.limit:]
            self.last_price = row["close"]



# Uji mandiri: python whale_feed.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    feed = ExchangeFeed("BTCUSDT")
    df = feed.fetch_klines("1m", 5)
    print(f"Provider aktif : {feed.provider_name}")
    print(df.tail(3))
    snap = feed.fetch_depth(1000)
    print(f"mid={snap.mid:,.2f} spread={snap.spread_bps:.2f} bps "
          f"imb(±2%)={snap.imbalance(0.02):+.2%} "
          f"bid_liq={snap.side_liquidity('bid'):.3f} ask_liq={snap.side_liquidity('ask'):.3f}")
