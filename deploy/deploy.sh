#!/usr/bin/env bash
# =============================================================
#  Deploy HL-WHALE-WALLET langsung ke Vercel PRODUCTION
#  Pakai: ./deploy.sh      (butuh Node.js ter-install)
# =============================================================
set -e
cd "$(dirname "$0")"

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js belum ter-install. Unduh di https://nodejs.org"
  exit 1
fi

if ! command -v vercel >/dev/null 2>&1; then
  echo "==> Meng-install Vercel CLI (sekali saja)..."
  npm install -g vercel
fi

if ! vercel whoami >/dev/null 2>&1; then
  echo "==> Login ke Vercel (pilih 'Continue with GitHub/Email' di browser)..."
  vercel login
fi

echo "==> Deploy ke PRODUCTION..."
vercel --prod --yes

echo ""
echo "✅ Selesai! URL publikmu tercetak di atas (https://<nama>.vercel.app)"
echo "   📱 Jadi Telegram Mini App: kirim URL itu ke @BotFather -> /setmenubutton atau /newapp"
