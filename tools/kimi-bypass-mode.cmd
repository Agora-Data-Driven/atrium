@echo off
REM Double-click to launch Claude Code on Moonshot Kimi in a fresh window,
REM in the folder this .cmd lives in. Any args pass through to claude.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kimi-bypass-mode.ps1" %*
pause
