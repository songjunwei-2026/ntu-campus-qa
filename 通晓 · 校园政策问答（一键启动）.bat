@echo off
rem TongXiao - NTU campus policy QA, one-click launcher
rem 1) port 8001 already listening -> just open the browser
rem 2) not running                  -> hand over to start.bat in this folder
rem No absolute paths anywhere: everything resolves from this script's own
rem folder, so the project can be moved or cloned anywhere and still works.
setlocal
set "PORT=8001"
set "URL=http://127.0.0.1:%PORT%"
pushd "%~dp0"

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  start "" "%URL%"
  popd
  exit /b 0
)

if not exist "%~dp0start.bat" (
  echo.
  echo   [ERROR] start.bat not found next to this launcher:
  echo   %~dp0start.bat
  echo.
  pause
  popd
  exit /b 1
)

call "%~dp0start.bat"
popd
