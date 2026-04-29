@echo off
chcp 65001 >nul 2>&1
title 环境初始化 - SenseVoice Teleprompter

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║     SenseVoice Teleprompter - 环境初始化          ║
echo  ║     识别引擎: SenseVoiceSmall (阿里 FunASR)       ║
echo  ╚══════════════════════════════════════════════════╝
echo.

cd /d "%~dp0"

::: 检查 Python
where python >nul 2>&1
if errorlevel 1 (
    echo  [错误] 未检测到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

python --version

::: 创建虚拟环境
if not exist "venv" (
    echo.
    echo  [1/4] 创建虚拟环境...
    python -m venv venv
) else (
    echo  [1/4] 虚拟环境已存在，跳过
)

::: 安装 CPU 版 PyTorch
echo.
echo  [2/4] 安装 PyTorch (CPU 版)...
venv\Scripts\pip.exe install torch torchaudio --index-url https://download.pytorch.org/whl/cpu

::: 安装核心依赖
echo.
echo  [3/4] 安装 Python 依赖 (可能需要几分钟)...
venv\Scripts\pip.exe install --upgrade pip
venv\Scripts\pip.exe install fastapi uvicorn websockets sounddevice numpy pydantic
venv\Scripts\pip.exe install funasr modelscope hydra-core sentencepiece jieba omegaconf transformers safetensors librosa soundfile six torch_complex

::: 预下载模型
echo.
echo  [4/4] 预下载模型 (SenseVoiceSmall)...
venv\Scripts\python.exe -c "from funasr.models.sense_voice.model import SenseVoiceSmall; from funasr import AutoModel; m = AutoModel(model='iic/SenseVoiceSmall', vad_model='fsmn-vad', device='cpu', disable_update=True); print('SenseVoice OK!')"

echo.
echo  ══════════════════════════════════════════════════
echo  ✅ 环境初始化完成!
echo  引擎: SenseVoiceSmall (CPU, 中文高准确率)
echo  现在可以运行 "启动.bat" 启动服务
echo  ══════════════════════════════════════════════════
echo.
pause
