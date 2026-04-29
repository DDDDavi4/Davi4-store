@echo off
chcp 65001 >nul
title SenseVoice Teleprompter - 安装与启动

echo.
echo  =====================================================
echo     SenseVoice Teleprompter
echo     一键安装并启动（首次需要几分钟）
echo  =====================================================
echo.

:: 检查是否已安装
if exist "python\python.exe" (
    echo  [√] Python 已安装
    echo.
    goto :check_deps
)

:: ---- 第一步：下载 Python Embedded ----
echo  [1/4] 下载 Python Embedded ...
set PYTHON_VERSION=3.11.9
set PYTHON_ZIP=python-%PYTHON_VERSION%-embed-amd64.zip
set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/%PYTHON_ZIP%

if not exist "%PYTHON_ZIP%" (
    echo  正在下载 %PYTHON_URL% ...
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_ZIP%'"
    if errorlevel 1 (
        echo  [×] 下载 Python 失败！请检查网络连接。
        pause
        exit /b 1
    )
)

:: 解压
if not exist "python" (
    echo  解压中...
    powershell -Command "Expand-Archive -Path '%PYTHON_ZIP%' -DestinationPath 'python' -Force"
    del "%PYTHON_ZIP%"
)
echo  [√] Python 安装完成
echo.

:: ---- 第二步：配置 pip ----
:check_deps
echo  [2/4] 检查 pip ...
python\python.exe -c "import pip" >nul 2>&1
if errorlevel 1 (
    echo  正在安装 pip ...
    if not exist "get-pip.py" (
        powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py'"
    )
    python\python.exe get-pip.py
    del get-pip.py 2>nul

    :: 修改 ._pth 文件以启用 site-packages
    powershell -Command "(Get-Content 'python\python311._pth') -replace '#import site','import site' | Set-Content 'python\python311._pth'"
    :: 添加 site-packages 路径
    echo .\Lib\site-packages >> python\python311._pth
)
echo  [√] pip 就绪
echo.

:: ---- 第三步：安装 Python 依赖 ----
echo  [3/4] 安装依赖（首次需要5-10分钟）...
python\python.exe -c "import funasr" >nul 2>&1
if errorlevel 1 (
    echo  正在安装核心依赖...
    python\python.exe -m pip install --no-cache-dir ^
        fastapi uvicorn websockets ^
        numpy pydantic ^
        soundfile ^
        funasr modelscope torch torchaudio --index-url https://download.pytorch.org/whl/cpu ^
        six torch_complex
    if errorlevel 1 (
        echo  [×] 依赖安装失败！尝试修复...
        python\python.exe -m pip install --no-cache-dir six torch_complex
    )
)
echo  [√] 依赖安装完成
echo.

:: ---- 第四步：检查模型 ----
echo  [4/4] 检查模型...
if exist "models\SenseVoiceSmall" (
    echo  [√] 模型已存在
) else (
    echo  首次启动时会自动下载模型（约893MB），请耐心等待。
)
echo.

:: ---- 启动服务 ----
echo  =====================================================
echo     正在启动服务...
echo     启动后会自动打开浏览器
echo     按 Ctrl+C 停止服务
echo  =====================================================
echo.

:: 设置环境变量
set PYTHONPATH=%CD%

:: 启动
python\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8765 --ws-ping-interval 30

pause
