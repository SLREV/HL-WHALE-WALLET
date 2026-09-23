# 📱 Cara Jadi Telegram Mini App

Aplikasi ini bisa dibuka **langsung di dalam Telegram** (di HP & desktop) sebagai
*Mini App* — tinggal dipakai, **tanpa install, tanpa login**. Karena
`hyperliquid.html` murni berjalan di browser, Telegram hanya perlu "membungkus"
URL HTTPS-nya.

```
┌─────────────┐   HTTPS    ┌──────────────────┐   wss/https   ┌─────────────┐
│ Telegram HP │ ────────►  │ Vercel (HTML)    │ ────────────► │ Hyperliquid │
│ (Mini App)  │  ◄──────── │ hyperliquid.html │ ◄──────────── │ (on-chain)  │
└─────────────┘   HTML/JS  └──────────────────┘  buku + fill  └─────────────┘
```

---

## Prasyarat

✅ Sudah deploy ke Vercel (lihat `DEPLOY-VERCEL.md`) dan punya URL, mis:
`https://hl-whale-wallet.vercel.app`
> Wajib HTTPS — URL `vercel.app` sudah otomatis HTTPS.

## Langkah 1 — Buat bot di Telegram (± 1 menit)

1. Buka Telegram → cari **@BotFather** (diblue/badge verified).
2. Kirim: `/newbot`
3. Ketik **nama** bot, mis. `Whale Wall On-Chain`
4. Ketik **username** bot (harus diakhiri `bot`), mis. `whalewall_bot`
5. BotFather memberi **token** — *simpan untuk dirimu sendiri, JANGAN dibagikan /
   tidak perlu dimasukkan ke kode repo ini.*

## Langkah 2 — Pasang Mini App-nya (± 1 menit)

Di @BotFather (pastikan chat dengan bot kamu yang dipilih):

**Cara A — tombol Menu (paling gampang, disarankan):**
1. Kirim: `/setmenubutton` → pilih bot kamu.
2. Ketik teks tombol, mis. `🐋 Buka Whale Wall`
3. Kirim URL: `https://hl-whale-wallet.vercel.app`
4. Selesai → di chat bot kamu muncul tombol **menu** (▢) di kiri kotak ketik yang
   langsung membuka aplikasinya.

**Cara B — link Mini App langsung (bisa disebar ke mana-mana):**
1. Kirim: `/newapp` → pilih bot kamu.
2. Kirim URL yang sama: `https://hl-whale-wallet.vercel.app`
3. Isi nama, deskripsi, dan foto (opsional, bisa di-skip dengan `/empty`).
4. Ketik **short name**, mis. `whalewall` → BotFather memberi link:
   **`https://t.me/whalewall_bot/whalewall`**
5. Link itu bisa dibagikan ke grup, kanal, DM — siapa pun yang klik langsung
   membuka aplikasi di dalam Telegram. 💬

> **Cara A + B boleh dipakai sekaligus.**

## Langkah 3 — Tes

1. Buka `https://t.me/<username_bot>/<short_name>` di Telegram (HP atau Desktop).
2. Aplikasi terbuka **full-screen dengan tema gelap**, ticker harga jalan.
3. Coba ganti coin/timeframe — semuanya berfungsi seperti di browser, karena data
   on-chain diambil dari HP masing-masing pengguna.

---

## Yang sudah diintegrasikan ke kode

`hyperliquid.html` kini memuat **Telegram Web App SDK** dan otomatis:

| Perilaku | Efek di Telegram |
|---|---|
| `tg.ready()` + `tg.expand()` | Aplikasi langsung tampil penuh (full height) |
| `setHeaderColor/setBackgroundColor` | Header Telegram ikut tema gelap aplikasi |
| `disableVerticalSwipes()` | Chart tidak terganggu swipe "tutup aplikasi" |
| Di browser biasa | Blok ini di-skip — tidak ada efek samping |

Tidak ada token/API key di repo — Mini App ini **publik**, tidak butuh auth.

## FAQ

- **Perlu server sendiri?** Tidak. Vercel gratis cukup; Telegram hanya menampilkan URL-nya.
- **Bisa untuk grup/kanal?** Bisa — share link `t.me/...` ke grup/kanal, atau pakai
  `/setmenubutton` agar tombolnya muncul di chat bot.
- **Ganti domain?** Ulangi `/setmenubutton` + `/newapp` dengan URL baru.
- **Update aplikasi?** Cukup `git push` → Vercel auto-deploy → Telegram langsung
  memuat versi baru (tidak perlu apa-apa di BotFather).
