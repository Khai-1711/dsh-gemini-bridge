@echo off
title DSH-Bridge stopper
echo Dang dung tat ca tien trinh DSH-Bridge...
taskkill /FI "WINDOWTITLE eq DSH-Bridge*" /T /F >nul 2>&1
echo Da dung xong cac tien trinh DSH-Bridge.
ping -n 3 127.0.0.1 >nul 2>&1
exit /b 0
