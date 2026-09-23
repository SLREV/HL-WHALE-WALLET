════════════════════════════════════════════════════════════
  HL-WHALE-WALLET · Bundle Deploy Vercel (upload langsung CLI)
════════════════════════════════════════════════════════════

ISI BUNDLE
  hyperliquid.html   Aplikasi utama (Whale Wall on-chain, real-time)
  index.html         Redirect otomatis ke aplikasi
  vercel.json        Konfigurasi Vercel (path / -> aplikasi)
  plotly.min.js      Library chart — sudah lokal, TIDAK butuh internet CDN
  deploy.sh          Script deploy otomatis (macOS / Linux)
  deploy.bat         Script deploy otomatis (Windows)

────────────────────────────────────────────────────────────
CARA PALING CEPAT (± 1 menit, butuh Node.js dari nodejs.org)
────────────────────────────────────────────────────────────
  macOS / Linux :  ./deploy.sh
  Windows       :  klik dua kali deploy.bat

  Script otomatis: install Vercel CLI -> buka browser login ->
  deploy ke production -> mencetak URL publik.

────────────────────────────────────────────────────────────
CARA MANUAL (3 perintah)
────────────────────────────────────────────────────────────
  1)  npm install -g vercel
  2)  vercel login
  3)  vercel --prod --yes

────────────────────────────────────────────────────────────
SETALAH DEPLOY
────────────────────────────────────────────────────────────
  • Kamu dapat URL publik:  https://<nama-project>.vercel.app
    → langsung bisa dibagikan ke siapa saja (web).
  • 📱 Jadi Telegram Mini App:
      kirim URL itu ke @BotFather
      → /setmenubutton  (tombol menu di chat bot)
      → /newapp         (link t.me/<bot>/<short-name> untuk disebar)
  • Update aplikasi: edit file di folder ini, jalankan deploy lagi.
  • Custom domain: dashboard Vercel → Settings → Domains.

Tidak perlu API key / token / env var. Semua data on-chain
diambil langsung dari browser masing-masing pengunjung.
