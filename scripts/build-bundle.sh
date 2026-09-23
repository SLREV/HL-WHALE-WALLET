#!/usr/bin/env bash
# =============================================================
#  Membangun hl-whale-wallet-vercel.zip
#  = bundle minimal siap upload ke Vercel langsung dari CLI
#  Pakai: ./scripts/build-bundle.sh   (dari folder repo manapun)
# =============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

STAGE="$(mktemp -d)/hl-whale-wallet-vercel"
mkdir -p "$STAGE"

# --- file aplikasi inti ---
cp hyperliquid.html index.html vercel.json "$STAGE/"

# --- plotly.min.js: pakai yang ada, atau unduh dari npm ---
if [ ! -f plotly.min.js ]; then
  echo "==> plotly.min.js tidak ditemukan, mengunduh dari npm..."
  npm pack plotly.js-dist-min@2.35.2 >/dev/null
  tar -xzf plotly.js-dist-min-2.35.2.tgz package/plotly.min.js
  mv package/plotly.min.js plotly.min.js
  rm -rf package plotly.js-dist-min-2.35.2.tgz
fi
cp plotly.min.js "$STAGE/"

# --- script & panduan deploy ---
cp deploy/deploy.sh deploy/deploy.bat deploy/README-DEPLOY.txt "$STAGE/"
chmod +x "$STAGE/deploy.sh"

# --- zip (dikeluarkan dari git via .gitignore) ---
rm -f "$ROOT/hl-whale-wallet-vercel.zip"
( cd "$(dirname "$STAGE")" && zip -qr "$ROOT/hl-whale-wallet-vercel.zip" hl-whale-wallet-vercel )

echo "OK -> $ROOT/hl-whale-wallet-vercel.zip"
unzip -l "$ROOT/hl-whale-wallet-vercel.zip"
