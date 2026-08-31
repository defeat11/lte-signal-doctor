@echo off
chcp 65001 >nul
echo Watchdog started. Monitoring http://127.0.0.1:8000/api/health every 30s...

:loop
powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 5; if ($r.StatusCode -ne 200) { exit 1 } } catch { exit 1 }" >nul 2>nul
if %errorlevel% neq 0 (
    echo [%date% %time%] Health check failed. Restarting run_project.bat... >> watchdog.log
    echo [%date% %time%] Health check failed. Restarting run_project.bat...
    start "" run_project.bat
)
timeout /t 30 /nobreak >nul
goto loop
