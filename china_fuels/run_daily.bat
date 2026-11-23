@echo off
REM China fuels daily report for Windows Task Scheduler (after SunSirs scrape).
REM Suggested: weekdays 09:30 local — scrape at 09:20 in oil_data_ingest.

cd /d "%~dp0"

set "PY="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0..\..\.venv\Scripts\python.exe" set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo [%DATE% %TIME%] Using: %PY%
echo [%DATE% %TIME%] Building China fuels report...
"%PY%" report.py
set ERR=%ERRORLEVEL%
echo [%DATE% %TIME%] Exit code %ERR%
exit /b %ERR%
