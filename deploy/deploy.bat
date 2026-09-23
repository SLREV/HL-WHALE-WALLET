@echo off
rem =============================================================
rem  Deploy HL-WHALE-WALLET langsung ke Vercel PRODUCTION (Windows)
rem  Butuh Node.js ter-install: https://nodejs.org
rem =============================================================
cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo ERROR: Node.js belum ter-install. Unduh di https://nodejs.org
  pause
  exit /b 1
)

where vercel >nul 2>nul
if errorlevel 1 (
  echo ==^> Meng-install Vercel CLI, tunggu sebentar...
  call npm install -g vercel
)

vercel whoami >nul 2>nul
if errorlevel 1 (
  echo ==^> Login ke Vercel, browser akan terbuka...
  call vercel login
)

echo ==^> Deploy ke PRODUCTION...
call vercel --prod --yes

echo.
echo ✅ Selesai! URL publik tercetak di atas ^(https://^<nama^>.vercel.app^)
echo    Untuk Telegram Mini App: kirim URL itu ke @BotFather
pause
