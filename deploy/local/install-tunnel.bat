@echo off
REM ---------------------------------------------------------------
REM Cloudflare Tunnel setup: login -> create tunnel -> write config
REM -> route DNS -> install as Windows service (auto start).
REM Requires: your domain is already managed by Cloudflare.
REM ---------------------------------------------------------------
setlocal EnableDelayedExpansion
cd /d "%~dp0..\.."

set "TUNNEL_NAME=thesis-format"
set "CFDIR=%USERPROFILE%\.cloudflared"

where cloudflared >nul 2>nul
if errorlevel 1 (
    echo cloudflared not found, installing with winget ...
    winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo Install failed. Download manually from:
        echo   https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi
        goto :eof
    )
    set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
)

echo.
echo === Step 1/4: login to Cloudflare ===
echo A browser window will open. Pick your domain's Cloudflare site and authorize.
pause
cloudflared tunnel login
if errorlevel 1 goto :eof

echo.
echo === Step 2/4: create tunnel ===
cloudflared tunnel create %TUNNEL_NAME%
if errorlevel 1 goto :eof

for /f "delims=" %%f in ('dir /b /o-d "%CFDIR%\*.json" 2^>nul') do (
    if not defined TUNNEL_JSON set "TUNNEL_JSON=%%f"
)
if not defined TUNNEL_JSON (
    echo Cannot find tunnel credentials json under %CFDIR%
    goto :eof
)
set "TUNNEL_ID=%TUNNEL_JSON:~0,-5%"
set "CRED=%CFDIR%\%TUNNEL_JSON%"
set "CRED=!CRED:\=/!"
echo Tunnel id: %TUNNEL_ID%

echo.
echo === Step 3/4: write config.yml ===
set /p DOMAIN=Enter the public domain for this service (example: thesis.yourdomain.com):
if "%DOMAIN%"=="" (
    echo No domain entered, abort.
    goto :eof
)
(
echo url: http://127.0.0.1:8000
echo tunnel: %TUNNEL_ID%
echo credentials-file: %CRED%
echo ingress:
echo   - hostname: %DOMAIN%
echo     service: http://127.0.0.1:8000
echo   - service: http_status:404
) > "%CFDIR%\config.yml"
echo Written to %CFDIR%\config.yml

echo.
echo === Step 4/4: route DNS and install service ===
cloudflared tunnel route dns %TUNNEL_NAME% %DOMAIN%
if errorlevel 1 goto :eof
cloudflared service install
if errorlevel 1 goto :eof

echo.
echo Done. The tunnel starts with Windows and serves https://%DOMAIN%
echo Start the web service first: deploy\local\start.bat
echo Check anytime:  https://%DOMAIN%/api/health
pause
