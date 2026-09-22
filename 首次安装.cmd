@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0首次安装.ps1"
if errorlevel 1 (
  echo.
  echo 安装未完成，请保留本窗口中的错误信息。
  pause
  exit /b 1
)
pause
