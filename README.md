# ⛓ Hyperliquid — Order Book ON-CHAIN & Whale Wall

Versi **khusus on-chain Hyperliquid**. Tidak ada data CEX (Binance/OKX) di paket ini:
semua level buku order diambil dari state **HyperCore** yang dikomit validator, dan
setiap **FILL** dibawa lengkap dengan **alamat wallet taker + hash transaksi**, jadi
bisa diverifikasi — **bukan simulasi**.

---

## Branding

Merek (nama + logo) diatur di bagian atas `hyperliquid.html`:

```js
const BRAND = "ByAnjarS";        // header aplikasi
const WATERMARK = "AnjarS";      // teks samar di tengah chart
const LOGO_HEADER = "data:image/png;base64,...";
const LOGO_WATERMARK = "data:image/png;base64,...";
```

Untuk Python (dashboard & chart CLI) mereknya ada di `branding.py`.

## 1. Instalasi

```bash
pip install -r requirements.txt
#   plotly>=6.1.0  pandas>=2.0.0  numpy>=1.24.0
#   requests>=2.31.0  websocket-client>=1.6.0  dash>=3.0.0
```

Tidak perlu API key. Semua endpoint publik.

---

## 2. Cara pakai

### 2.1 Dashboard live (paling real-time)

```bash
python hl_app.py --port 8051
# buka http://localhost:8051
```

Isi dashboard:
* **Coin** — semua perp di Hyperliquid (BTC, ETH, SOL, **PAXG** = emas, HYPE, …)
* **Timeframe** — 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 1d
* **Min wall (USDT)** — ambang "$besar" untuk sebuah level/wall
* **Grid harga (bin)** — lebar 1 sel heatmap; kosong = auto, isi `5` → kelipatan 5
* **Refresh** — 1–10 detik
* **Tampilkan** — ☑ Whale walls · ☑ Rencana entry/SL/TP
* **Ticker harga besar** (update **0,7 detik**) — harga dari **FILL terakhir** (presisi penuh,
  bukan kelipatan $100 seperti buku), arah BUY/SELL, dan umur data
* **Baris status** — harga (fill) & mid buku, grid, jumlah wall, jumlah snapshot buku tersimpan,
  **jumlah fill on-chain + jumlah wallet taker berbeda**, status WS, dan rencana order
* Panel **🔍 Bukti on-chain** — 10 fill terakhir beserta wallet taker & hash tx

### 2.2 Sekali jalan → file HTML

```bash
python hl_app.py --once --coin BTC --interval 1m --live 120 \
                 --min-notional 500000 --bin-size 5 --plan \
                 --out output/hyperliquid_BTC_onchain.html
```

`--live 120` = merekam 120 detik data on-chain (buku + fill) sebelum chart dibuat.
Makin lama merekam, makin tebal heatmap order book aslinya.

### 2.3 Lihat daftar coin

```bash
python hl_app.py --list-coins
```

> **Emas on-chain = `PAXG`** (1 PAXG = 1 troy ounce emas fisik yang disimpan di brankas).
> `XAUUSDT` tidak ada di Hyperliquid — pakai `PAXG`.

---

## 3. Bukti bahwa ini data nyata

Contoh hasil `--once` (40 detik, BTC):

```
[ok] output/hyperliquid_BTC_onchain.html
     harga mid      : 86,050.0000
     grid harga     : 5  (221 baris heatmap)
     wall terdeteksi: 2 bid / 2 ask
     snapshot buku  : 39
     FILL on-chain  : 205 (81 wallet taker berbeda)

===== BUKTI ON-CHAIN: 8 FILL TERAKHIR (wallet taker + hash) =====
WAKTU      SISI         HARGA         SIZE  WALLET TAKER   TX
------------------------------------------------------------------------------
14:51:14   BUY    86,069.0000       0.0002  0xf5d81a13     0xd87570629d6f7438
14:51:13   SELL   86,068.0000       0.0002  0x6f58f37e     0x7ddcd5afe659063f
14:51:13   BUY    86,069.0000       0.0024  0x3a140f84     0x1835e879b3368148
```

Cara memverifikasi sendiri:
1. **`l2Book` memuat field `n`** = jumlah order individu yang diagregasi di level itu.
2. Channel **`trades` mengirim `users[]`** = alamat wallet taker (kolom WALLET TAKER di atas)
   plus `hash` transaksi → bisa dicek di explorer Hyperliquid.
3. Total ukuran level **berubah antar snapshot** (bukan angka statis).
4. Heatmap tersusun dari snapshot buku yang benar-benar diterima (lihat "snapshot buku").

---

## 3b. Kenapa dulu harga terasa "delay" (dan apa yang sudah diperbaiki)

| Sumber | Presisi | Kecepatan update | Dipakai untuk |
|---|---|---|---|
| Buku `l2Book` (nSigFigs 3) | BTC kelipatan **$100** | tiap **~5 detik** (batas push exchange) | peta wall jarak jauh (heatmap) |
| **FILL / `trades`** | **$1** (presisi penuh) | **sub-detik**, tiap transaksi | **harga live + candle terakhir** |

Dulu harga diambil dari `l2Book`, jadi tampak membeku di angka bulat (mis. 86.250)
dan hanya berubah tiap ~5 detik. Sekarang:

1. **Harga live diambil dari FILL** — tampil besar di ticker, update tiap 0,7 detik,
   lengkap dengan label umur data (`update 0.8s lalu`).
2. **Candle terakhir digerakkan oleh FILL** — `close`, `high`, `low` dan `volume`
   candle yang sedang berjalan langsung mengikuti transaksi, tidak menunggu push
   candle dari server (yang datangnya tiap ~1 menit).
3. **Candle terakhir tidak lagi menempel di tepi** — sumbu X diberi **celah 10 lebar
   candle** di kanan (bisa diubah: `--right-pad 20`), dan ada garis putus-putus putih
   + label harga terakhir (labelnya di kiri, karena angka sumbu harga ada di kanan).
4. **Snapshot buku tidak disimpan dobel** — tersimpan hanya saat timestamp buku
   berubah (sebelumnya tersimpan tiap detik walaupun isinya sama).

> Catatan jujur: push `l2Book` Hyperliquid dibatasi ~5 detik oleh exchange itu
> sendiri. Itu batas server, bukan kode kita — karena itu harga live diambil dari
> FILL yang tidak dibatasi.

## 4. Mengatur kehalusan grid harga

| Cara | Efek |
|---|---|
| Kosong (auto) | `0.5 bps` dari harga → BTC 1m ≈ **5**, ETH ≈ 0.2, SOL ≈ 0.01, PAXG ≈ 0.25 |
| Isi `5` di **Grid harga** | Grid persis kelipatan 5: 86.000, 86.005, 86.010, … |

Semakin kecil grid, level makin presisi tapi baris heatmap makin banyak. Kalau grid
yang diminta menghasilkan lebih dari 1000 baris, nilainya otomatis dikalikan 2
(tetap kelipatan rapi) — angka akhirnya selalu tampil di baris status (`grid 5`).

---

## 5. Cara membaca chart

* **Heatmap** (di belakang candle) — tebal likuiditas per level harga per waktu.
  Hijau = bid (limit buy), merah = ask (limit sell), makin terang = makin tebal.
* **Garis putus-putus horizontal + label** — whale wall (`BID $xM` / `ASK $xM`).
* **Garis cyan** = level entry · **merah putus** = stop loss · **hijau putus** = take profit.
  Zona merah transparan = area risiko, hijau transparan = area target.
* **Panel bawah** (scatter) — imbalance bid/ask.

Aturan pakai (ringkas):
* Limit **BUY** diletakkan sedikit **di atas** puncak wall BID (ikut antrean whale).
* **SL** di bawah dasar wall BID · **TP** di bawah wall ASK terdekat.
* Short dibalik: entry di bawah wall ASK, SL di atasnya, TP di atas wall BID.
* R:R < `min_rr` (default 1.5) ditolak; skor keyakinan < 50 = lebih baik menunggu.

---

## 6. Semua opsi CLI

| Opsi | Default | Keterangan |
|---|---|---|
| `--coin` | `BTC` | Coin Hyperliquid |
| `--interval` | `1m` | 1m/3m/5m/15m/30m/1h/2h/4h/1d |
| `--min-notional` | `500000` | Ambang notional sebuah wall (USDT) |
| `--top-n` | `6` | Maksimal wall yang digambar |
| `--bin-size` | — | Lebar grid harga absolut (mis. `5`) |
| `--right-pad` | `10` | Celah kosong di tepi kanan chart, dalam satuan lebar candle |
| `--sigfigs` | `3` | Agregasi level buku: 3 = ±220 bps, ~11 bps/bucket |
| `--sample-every` | `1.0` | Simpan snapshot buku tiap N detik (bahan heatmap) |
| `--host` / `--port` | `0.0.0.0` / `8051` | Alamat dashboard |
| `--once` | off | Mode rekam → simpan HTML (tanpa dashboard) |
| `--live` | `60` | Durasi rekam detik (mode `--once`) |
| `--out` | `output/hl_<COIN>_<TF>.html` | File HTML hasil |
| `--plan` | off | Sertakan rencana entry/SL/TP |
| `--balance` / `--risk-pct` | `1000` / `1.0` | Modal & % risiko untuk ukuran posisi |
| `--list-coins` | off | Tampilkan daftar coin |

---

## 7. Troubleshooting

| Gejala | Solusi |
|---|---|
| `○ reconnect` di status | WS putus sebentar, otomatis tersambung lagi (tanpa kehilangan buffer) |
| Heatmap kosong | Tunggu 10–30 detik: butuh minimal beberapa snapshot buku |
| Tidak ada wall | Kecilkan **Min wall** (Hyperliquid bukunya lebih tipis dari Binance) |
| `grid` terlalu kasar | Isi angka lebih kecil di **Grid harga** (mis. `5`) |
| Chart berat / lambat | Perbesar **Grid harga**, atau kecilkan `--live` |
| Coin tidak ada | `python hl_app.py --list-coins` |

---

## 8. Struktur berkas

```
hl_app.py        <- aplikasi utama (dashboard + mode --once)
onchain_feed.py  <- klien Hyperliquid: REST (meta/l2Book/candle) + WebSocket
whale_analytics.py  deteksi whale wall & matriks heatmap
whale_chart.py      renderer Plotly (heatmap + candle + garis wall/rencana)
trade_plan.py       mesin rencana entry/SL/TP & ukuran posisi
whale_feed.py       tipe data bersama (DepthSnapshot)
output/             contoh chart HTML
```

---

## 9. Risiko

Data 100% nyata, tapi **limit order di Hyperliquid tetap bisa dibatalkan** kapan saja
(seperti bursa mana pun) — wall bisa menghilang sebelum tersentuh. State-nya
terkomit di chain, namun itu **bukan** jaminan order akan dieksekusi.
Ini alat bantu analisa, bukan saran finansial.
