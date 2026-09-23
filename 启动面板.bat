@echo off
rem ============================================================
rem  EFORT Remote Console - one-click launcher (no console window)
rem  Uses pythonw.exe so double-clicking gives ONLY the GUI.
rem  If it does not start, run "start_console.bat" to see errors.
rem ============================================================
cd /d "%~dp0"

set "PYW=C:\Users\Administrator\.workbuddy\binaries\python\envs\efort_pyqt6\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

start "" "%PYW%" main.py %*
exit /b 0
