@echo off
rem FlexGlove launcher for Windows. First run creates .venv and installs packages
rem from the local "wheels" folder (offline) or from the internet.
setlocal
cd /d "%~dp0"
title FlexGlove
set "VENV=%~dp0.venv"
set "MARK=%VENV%\flexglove_ready.txt"
if exist "%MARK%" goto run

echo.
echo  [FlexGlove] First run: preparing Python environment (1-2 min)...
echo.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || goto nopython

if exist "%VENV%" rmdir /s /q "%VENV%"
%PY% -m venv "%VENV%" || goto fail
if not exist "%~dp0wheels" goto online
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check --no-index --find-links "%~dp0wheels" -r "%~dp0requirements.txt" && goto installed
echo  [FlexGlove] Offline install failed, trying the internet...
:online
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt" || goto fail
:installed
echo ok> "%MARK%"

:run
cd /d "%~dp0host"
"%VENV%\Scripts\python.exe" -m flexglove %*
if errorlevel 1 (
  echo.
  echo  [FlexGlove] The app stopped with an error. See the messages above.
  pause
)
exit /b

:nopython
echo  [FlexGlove] Python 3.10 or newer was not found.
echo  Install it with one of:
echo    1) winget install Python.Python.3.12
echo    2) https://www.python.org/downloads/  (tick "Add python.exe to PATH")
echo  Then run FlexGlove.bat again.
pause
exit /b 1

:fail
echo  [FlexGlove] Setup failed. Delete the .venv folder and try again, or send the messages above to the team.
pause
exit /b 1
