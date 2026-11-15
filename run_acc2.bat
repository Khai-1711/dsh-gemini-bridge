@echo off
title DSH-Bridge [acc2]
cd /d %~dp0
set BRIDGE_INSTANCE=acc2
python main.py
pause
