@echo off
chcp 65001 >nul
echo 正在停止报价器服务...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo 关闭进程 %%a
    taskkill /f /pid %%a >nul 2>&1
)
echo 已停止。
pause
