@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
if exist "C:\Anaconda3\python.exe" set "PY=C:\Anaconda3\python.exe"
if not defined PY if exist "D:\Python\python.exe" set "PY=D:\Python\python.exe"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (where py >nul 2>nul && set "PY=py")

if not defined PY (
  echo.
  echo   [ERROR] Python not found.
  echo   Install Python 3, or edit start.bat and set PY to the full path.
  echo.
  pause
  exit /b 1
)

netstat -ano | findstr ":8001" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo   [ERROR] Port 8001 is already taken by another process:
  netstat -ano ^| findstr ":8001" ^| findstr "LISTENING"
  echo.
  echo   Kill it with:  taskkill /F /PID ^<pid^>
  echo   Then run this file again.
  echo.
  pause
  exit /b 1
)

set "LAN="
if /i "%~1"=="lan" set "LAN=--lan"
if /i "%~2"=="lan" set "LAN=--lan"

echo.
echo   TongXiao - NTU Campus Policy QA
echo   Python: %PY%
if defined LAN (echo   Server: http://127.0.0.1:8001  [LAN mode: 0.0.0.0]) else (echo   Server: http://127.0.0.1:8001)
echo   Keep this window open while using the site.
echo.

if /i "%~1"=="noopen" goto run
if /i "%~2"=="noopen" goto run
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8001"

:run
"%PY%" server.py 8001 %LAN%
echo.
echo   Server stopped. Press any key to close this window.
pause >nul
