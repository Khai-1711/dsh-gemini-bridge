@echo off
title DSH-Bridge launcher
cd /d %~dp0
rem Khởi động CẢ HAI luồng, mỗi luồng một cửa sổ riêng (thu nhỏ). Luồng nào đang chạy thì bỏ qua.
call :launch acc1
call :launch acc2
echo Da khoi dong xong. Mo http://127.0.0.1:10000/dashboard de bat/tat tung luong.
ping -n 4 127.0.0.1 >nul 2>&1
exit /b 0

:launch
set LANE=%1
set LPORT=
rem Đọc PORT từ file .env.<ID> để không bao giờ lệch với cấu hình (dòng PORT="10000" -> 10000)
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
start "DSH-Bridge [%LANE%]" /min cmd /c "set BRIDGE_INSTANCE=%LANE%&& python main.py || pause"
exit /b 0
