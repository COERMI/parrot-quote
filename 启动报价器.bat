@echo off
chcp 65001 >nul
cd /d "%~dp0app"

echo ==========================================
echo   羽栖 · 可编辑报价器  启动中...
echo ==========================================
echo.

REM 用托管版 Python 启动后端
"C:\Users\user\.workbuddy\binaries\python\envs\default\Scripts\python.exe" server.py

pause
