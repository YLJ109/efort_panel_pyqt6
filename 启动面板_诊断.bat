@echo off
rem ============================================================
rem  EFORT Remote Console - launcher WITH console (for troubleshooting).
rem  Use this when the silent launcher shows nothing: errors stay on screen.
rem ============================================================
title EFORT Remote Console - console mode
cd /d "%~dp0"

set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\efort_pyqt6\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting EFORT Remote Console (python=%PY%) ...
"%PY%" -u main.py %*
echo.
echo Exit code = %ERRORLEVEL%
pause
