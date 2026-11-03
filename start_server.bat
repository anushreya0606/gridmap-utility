@echo off
setlocal
cd /d "%~dp0"
title GridMap Local Network Server

echo ==============================================================
echo   ⚡ GridMap Local Network Server
echo ==============================================================
echo.

rem Get local IPv4 address
for /f "tokens=4" %%a in ('route print ^| findstr 0.0.0.0.*0.0.0.0') do (
    set LOCAL_IP=%%a
    goto :found_ip
)
:found_ip

echo Server will be accessible on your local network at:
echo   http://%LOCAL_IP%:8000
echo   http://127.0.0.1:8000
echo.
python app.py --host 0.0.0.0 --port 8000
pause
