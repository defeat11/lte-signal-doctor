@echo off
chcp 65001 >nul
setlocal

set "RULE_NAME=Smart Modem Dashboard 8000"
set "OLD_RULE_NAME=ModemDashboard"
set "PORT=8000"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator permission to update Windows Firewall...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ====================================================
echo Allowing TCP port %PORT% for phones on your network...
echo ====================================================

netsh advfirewall firewall delete rule name="%OLD_RULE_NAME%" >nul 2>&1
netsh advfirewall firewall delete rule name="%RULE_NAME%" >nul 2>&1
netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=TCP localport=%PORT% profile=any enable=yes

echo.
echo Done. Start run_project.bat, then open the printed LAN link from your phone.
echo Keep this enabled only on networks you trust.
echo ====================================================
pause
