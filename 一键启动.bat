@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title SenseVoice Teleprompter
color 0A

cd /d "%~dp0"

echo.
echo  =====================================================
echo     SenseVoice Teleprompter
echo     Auto Install and Start
echo  =====================================================
echo.

:: ============================================================
:: Step 1: Python
:: ============================================================
if exist "python\python.exe" (
    echo  [1/4] Python ... OK
    goto :step2
)

echo  [1/4] Downloading Python 3.11.9 ...
set PY_VER=3.11.9
set PY_ZIP=python-%PY_VER%-embed-amd64.zip
set PY_URL=https://www.python.org/ftp/python/%PY_VER%/%PY_ZIP%

if not exist "%PY_ZIP%" (
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_ZIP%'"
    if errorlevel 1 (
        echo  [FAIL] Python download failed
        pause
        exit /b 1
    )
)

if not exist "python" (
    powershell -Command "Expand-Archive -Path '%PY_ZIP%' -DestinationPath 'python' -Force"
    del "%PY_ZIP%" 2>nul
)
echo  [1/4] Python ... OK

:: ============================================================
:: Step 2: pip
:: ============================================================
:step2
echo  [2/4] pip ...
python\python.exe -c "import pip" >nul 2>&1
if errorlevel 1 (
    echo        Installing pip ...
    if not exist "get-pip.py" (
        powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py'"
    )
    python\python.exe get-pip.py
    del get-pip.py 2>nul
    powershell -Command "(Get-Content 'python\python311._pth') -replace '#import site','import site' | Set-Content 'python\python311._pth'"
    echo .\Lib\site-packages >> python\python311._pth
)
echo  [2/4] pip ... OK

:: ============================================================
:: Step 3: Dependencies
:: ============================================================
echo  [3/4] Dependencies ...
python\python.exe -c "import funasr" >nul 2>&1
if errorlevel 1 (
    echo        Installing web framework ...
    python\python.exe -m pip install fastapi uvicorn websockets numpy pydantic soundfile six torch_complex
    echo        Installing SenseVoice + PyTorch CPU ...
    python\python.exe -m pip install funasr modelscope --extra-index-url https://download.pytorch.org/whl/cpu torch torchaudio
)
echo  [3/4] Dependencies ... OK

:: ============================================================
:: Step 4: Model
:: ============================================================
echo  [4/4] Model ...
if exist "models\SenseVoiceSmall" (
    echo  [4/4] Model ... OK (local)
) else (
    echo  [4/4] Model ... will download on first run (~893MB)
)

echo.
echo  =====================================================
echo     Starting ...
echo  =====================================================
echo.

set PYTHONPATH=%CD%
python\python.exe launcher.py

pause
endlocal
