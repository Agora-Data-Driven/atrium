@echo off
REM ============================================================
REM  Double-click to rebuild the previews from the latest code.
REM  Updates Preview.html and the "Portal Preview" folder.
REM ============================================================
cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

"%PY%" scripts\make_preview.py

echo.
pause
