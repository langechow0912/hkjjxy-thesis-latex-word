@echo off
REM ---------------------------------------------------------------
REM Thesis format pipeline - start the local web service
REM Edit the password below before exposing the service to public.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."

set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

set THESIS_AUTH_USER=thesis
set THESIS_AUTH_PASS=change-me-please
set THESIS_MAX_JOBS=2
set THESIS_MAX_UPLOAD_MB=20

echo Starting thesis format service on http://127.0.0.1:8000
echo User: %THESIS_AUTH_USER%
"%PY%" web\app.py --host 127.0.0.1 --port 8000
if /i not "%~1"=="--silent" pause
