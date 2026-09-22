@echo off
set "ROOT=%~dp0"
set "OUTDIR=%ROOT%..\target-song-cutter-outputs-20260818"
set "SEC=%~1"
if "%SEC%"=="" set "SEC=5"

start "" /B powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ROOT%run-target-song-cutter-visual-monitor.ps1" -OutputRoot "%OUTDIR%" -RefreshSec %SEC%
