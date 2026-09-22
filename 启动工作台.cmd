@echo off
setlocal
cd /d "%~dp0"
set "PATH=%~dp0tools\ffmpeg;%PATH%"
set "WORKBENCH_PYTHON=%~dp0.python312\Scripts\pythonw.exe"
if not exist "%WORKBENCH_PYTHON%" set "WORKBENCH_PYTHON=%~dp0.python312\pythonw.exe"
if not exist "%WORKBENCH_PYTHON%" (
  echo [直播切片工作台] 尚未完成安装，请先双击“首次安装.cmd”。
  pause
  exit /b 2
)
start "直播切片工作台" /b "%WORKBENCH_PYTHON%" -X utf8 "%~dp0target-song-cutter\scripts\workflow_app.py"
