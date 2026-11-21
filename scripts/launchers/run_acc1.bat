@echo off
title DSH-Bridge [acc1 - Lane 1 - Port 10001]
cd /d "%~dp0..\.."
set BRIDGE_INSTANCE=acc1
python main.py
pause
