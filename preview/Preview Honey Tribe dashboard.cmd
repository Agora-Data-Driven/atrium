@echo off
rem ============================================================================
rem  Honey Tribe dashboard -- LOCAL PREVIEW (no password, no cloud)
rem
rem  Double-click this file to review the dashboard on your own laptop:
rem    * serves clients\client_honeytribe\dash\dashboard.html at http://localhost:8137
rem    * reads clients\client_honeytribe\data\honeytribe.json -- the REAL Honey Tribe
rem      numbers, built locally from the context workbook (never the live bucket)
rem    * exactly what the deployed honeytribe-dash service serves, minus the login
rem
rem  If the data file is missing, build it first:
rem    python clients\client_honeytribe\job\build_local.py
rem
rem  A browser tab opens automatically. Edit dashboard.html and refresh to see
rem  changes -- no deploy, no push. Close this window (or Ctrl+C) to stop.
rem ============================================================================
title Honey Tribe dashboard - Local Preview
echo Starting the Honey Tribe dashboard locally...
echo A browser tab will open at http://localhost:8137 in a moment.
echo Close this window or press Ctrl+C to stop.
echo.
python "%~dp0..\clients\client_honeytribe\preview_local.py" 8137
echo.
echo The local preview has stopped.
pause
