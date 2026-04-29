@echo off
chcp 65001 >nul 2>&1
title SenseVoice Teleprompter
color 0A

cd /d "%~dp0"

:: 检查虚拟环境
if not exist "venv\Scripts\python.exe" (
    echo.
    echo  [错误] 虚拟环境不存在，请先运行 setup.bat
    pause
    exit /b 1
)

:: 使用 Python 启动器（自动等待就绪 + 开浏览器）
venv\Scripts\python.exe launcher.py

pause
