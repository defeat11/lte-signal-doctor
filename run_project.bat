@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

title Smart Modem Dashboard - LAN AI
REM optional: run watchdog.bat in another window for auto-restart.
set "DASHBOARD_HOST=0.0.0.0"
if not defined DASHBOARD_PORT set "DASHBOARD_PORT=8000"

echo ====================================================
echo Starting Smart Modem Dashboard in LAN mode...
echo.
echo Local PC:
echo   http://127.0.0.1:%DASHBOARD_PORT%/dashboard.html
echo.
echo Phones and other devices:
echo   The exact LAN link will be printed by the Python app.
echo ====================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    echo Install Python or run this from a terminal where Python is available.
    pause
    exit /b 1
)

python modem_analyzer.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] The app stopped with an error.
    echo If phones cannot open the dashboard, run allow_firewall.bat once as Administrator.
    pause
)
