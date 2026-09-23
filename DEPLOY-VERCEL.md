# 🚀 Cara Deploy ke Vercel (biar bisa diakses banyak orang)

## Kenapa ini cocok untuk Vercel?

`hyperliquid.html` **murni berjalan di browser pengunjung** (client-side):

- WebSocket `wss://api.hyperliquid.xyz/ws` dan REST `/info` dipanggil **langsung dari browser** setiap pengunjung (CORS Hyperliquid terbuka).
- Server (Vercel) hanya menyajikan 1 file HTML → **gratis (paket Hobby)** dan tidak ada beban server berapa pun banyaknya pengunjung.
- Tidak ada API key, tidak ada build step, tidak ada database.

---

## Opsi A — Lewat GitHub (disarankan: auto-deploy setiap push)

1. **Merge perubahan ini ke `main`** (lewat Pull Request yang sudah disiapkan, atau merge manual).
2. Buka [vercel.com/new](https://vercel.com/new) → login (bisa pakai akun GitHub).
3. Klik **Import** pada repo `SLREV/HL-WHALE-WALLET`.
4. Biarkan semua default:
   - **Framework Preset:** `Other`
   - **Build Command:** kosong
   - **Output Directory:** kosong (root repo)
   - **Install Command:** kosong
5. Klik **Deploy**. ± 10 detik selesai → dapat URL seperti
   `https://hl-whale-wallet.vercel.app` → **langsung bisa dibagikan ke siapa saja.**

Setiap kali kamu push ke `main`, Vercel otomatis meng-update situsnya.

### Domain sendiri (opsional)
Di dashboard Vercel → project → **Settings → Domains** → tambahkan domain kamu
(arahkan `CNAME` ke `cname.vercel-dns.com`). SSL otomatis.

---

## Opsi B — Lewat Vercel CLI (tanpa lewat dashboard)

```bash
npm install -g vercel
vercel login            # sekali saja
cd HL-WHALE-WALLET
vercel --prod           # deploy langsung ke production
```

File `vercel.json` di repo ini sudah mengatur agar `/` menyajikan `hyperliquid.html`.

---

## ⚠️ Catatan penting: dashboard Python (`hl_app.py`) TIDAK bisa di Vercel

`hl_app.py` adalah aplikasi **Dash** yang butuh proses Python hidup terus-menerus +
koneksi WebSocket server-side. Vercel itu *serverless* (fungsi mati dalam hitungan
detik), jadi tidak mendukung. Pilihan:

| Kebutuhan | Solusi |
|---|---|
| Web live untuk banyak orang | ✅ `hyperliquid.html` di Vercel (cara di atas) — fungsinya sama, datanya tetap on-chain real-time |
| Tetap ingin dashboard Dash/Python | Deploy ke **Railway** / **Render** / **Fly.io** dengan start command: `pip install -r requirements.txt && python hl_app.py --host 0.0.0.0 --port $PORT` |
| Chart statis (snapshot) | `python hl_app.py --once ... --out output/x.html` lalu upload file HTML hasilnya ke folder repo → ikut ke-deploy sebagai halaman statis |

---

## Verifikasi setelah deploy

1. Buka URL Vercel → harus muncul dashboard "Hyperliquid · Whale Wall ON-CHAIN".
2. Ticker harga jalan (update ±0,7 detik) dan status WebSocket **ON**.
3. Kalau chart kosong: tunggu 2–5 detik (mengambil candle + snapshot buku pertama).

---

## Ringkasan file yang ditambahkan untuk Vercel

| File | Fungsi |
|---|---|
| `vercel.json` | Menyajikan `hyperliquid.html` di path `/` |
| `.vercelignore` | Tidak meng-upload file Python ke deployment (tidak dipakai) |
| `hyperliquid.html` (edit) | Plotly dimuat dari CDN (sebelumnya butuh file lokal yang tidak ada) |
