#!/usr/bin/env bash
# ============================================================================
#  run.sh — jalankan dashboard ORDER BOOK ON-CHAIN HYPERLIQUID (hl_app.py)
#
#  Pemakaian:
#      ./run.sh            # port default 8051
#      ./run.sh 8060       # port lain
# ============================================================================
set -e
cd "$(dirname "$0")"

PORT="${1:-8051}"

# Pastikan dependensi terpasang (aman dijalankan ulang)
python -c "import dash, pandas, plotly, requests, websocket" 2>/dev/null \
  || python -m pip install -r requirements.txt

# Hentikan instance lama yang masih memakai port ini (jika ada)
pkill -f "hl_app.py --port ${PORT}" 2>/dev/null || true
sleep 1

echo ""
echo "  Dashboard: http://localhost:${PORT}  (Ctrl+C untuk berhenti)"
echo ""
exec python hl_app.py --port "${PORT}"
