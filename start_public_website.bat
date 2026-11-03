@echo off
setlocal
cd /d "%~dp0"
title GridMap Public Web Server

echo ==============================================================
echo   ⚡ GridMap - Free Instant Public HTTPS Website
echo   (Uses your machine's 16 GB RAM - No OOM crashes, No signups)
echo ==============================================================
echo.

rem Public workspace isolation mode (per-browser cookies)
set GRIDMAP_PUBLIC=1
set GRIDMAP_KEEP_DAYS=7

rem Check for cloudflared portable tool
if not exist "cloudflared.exe" (
    echo [1/2] Downloading Cloudflare Quick Tunnel tool (no signup required)...
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe'"
)

echo [2/2] Starting local Python analysis engine on port 5000...
start /min "GridMap Python Server" python app.py --host 127.0.0.1 --port 5000

echo.
echo Connecting public tunnel...
echo Your public HTTPS link will appear below (look for https://...trycloudflare.com):
echo ==============================================================
cloudflared.exe tunnel --url http://127.0.0.1:5000
pause
