@echo off
REM ---------------------------------------------------------------
REM Launch the web service automatically at Windows logon.
REM The Cloudflare tunnel is installed as a service by install-tunnel.bat
REM and already starts with Windows, so both sides survive a reboot.
REM ---------------------------------------------------------------
setlocal
schtasks /create /tn "ThesisFormatWeb" /tr "\"%~dp0start.bat\" --silent" /sc onlogon /f
if errorlevel 1 (
    echo Failed to create the scheduled task. Run this as your own account.
) else (
    echo Created task "ThesisFormatWeb": start.bat runs at every logon.
    echo Remove later with:  schtasks /delete /tn ThesisFormatWeb /f
)
pause
