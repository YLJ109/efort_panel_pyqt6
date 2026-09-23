@echo off
title EFORT Remote Console (PyQt6)
cd /d "%~dp0"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\efort_pyqt6\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
start "" "%PY%" main.py %*
