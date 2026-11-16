@echo off
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% flash_gui.py
if errorlevel 1 pause
