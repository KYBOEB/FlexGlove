@echo off
rem Console launch for debugging: server messages are shown in this window.
rem Normal launch: FlexGlove.pyw or the "FlexGlove" desktop shortcut.
cd /d "%~dp0..\..\host"
if not exist "..\.venv\Scripts\python.exe" (
  echo Run FlexGlove.pyw once first - it prepares the environment.
  pause
  exit /b 1
)
"..\.venv\Scripts\python.exe" -m flexglove %*
pause
