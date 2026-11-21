@echo off
title DSH-Bridge [acc2 - Lane 2 - Port 10002]
cd /d "%~dp0..\.."
set BRIDGE_INSTANCE=acc2
python main.py
pause
