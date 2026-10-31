@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  py -3 start.py %*
) else (
  python start.py %*
)

set code=%errorlevel%
if not "%code%"=="0" pause
exit /b %code%
