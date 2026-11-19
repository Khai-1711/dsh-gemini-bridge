@echo off
title DSH-Bridge launcher
cd /d %~dp0
rem 1. Khoi dong Cong Frontend (Port 10000)
call :launch_frontend
rem 2. Khoi dong cac luong worker (Port 10001, 10002)
call :launch acc1
call :launch acc2
echo Da khoi dong xong. Mo http://127.0.0.1:10000/dashboard de dieu khien dashboard va cac luong.
ping -n 4 127.0.0.1 >nul 2>&1
exit /b 0

:launch_frontend
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 10000 -State Listen -ErrorAction SilentlyContinue) { exit 1 }"
if errorlevel 1 (
  echo [Frontend] cong 10000 da co tien trinh chay - bo qua.
  exit /b 0
)
start "DSH-Bridge [Frontend - Port 10000]" /min cmd /c "python main.py || pause"
exit /b 0

:launch
set LANE=%1
set LPORT=
for /f "tokens=2 delims==" %%a in ('findstr /b "PORT=" .env.%LANE% 2^>nul') do set LPORT=%%~a
if "%LPORT%"=="" (
  echo [%LANE%] khong tim thay file .env.%LANE% hoac PORT - bo qua.
  exit /b 0
)
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %LPORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 }"
if errorlevel 1 (
  echo [%LANE%] cong %LPORT% da co tien trinh chay - bo qua.
  exit /b 0
)
start "DSH-Bridge [%LANE% - Port %LPORT%]" /min cmd /c "set "BRIDGE_INSTANCE=%LANE%" && python main.py || pause"
exit /b 0
