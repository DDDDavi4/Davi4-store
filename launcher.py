"""
SenseVoice Teleprompter 一键启动器
支持两种模式：
  - 便携版：内嵌 python/ 目录，无需系统 Python
  - 开发版：使用 venv/ 虚拟环境
启动 uvicorn 服务，等待就绪后自动打开浏览器
"""

import subprocess
import sys
import time
import webbrowser
import urllib.request
import urllib.error
import os
from pathlib import Path

# 切换到脚本所在目录
BASE_DIR = Path(__file__).parent.resolve()
os.chdir(BASE_DIR)

PORT = 8765
URL = f"http://localhost:{PORT}"
MAX_WAIT = 120  # 最大等待秒数（便携版首次可能慢）


def find_python():
    """找到可用的 Python 解释器"""
    # 便携版：内嵌 python/python.exe
    portable_python = BASE_DIR / "python" / "python.exe"
    if portable_python.exists():
        return str(portable_python)

    # 开发版：venv/Scripts/python.exe
    venv_python = BASE_DIR / "venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)

    # 系统 Python
    return sys.executable


def is_portable():
    """是否为便携版模式"""
    return (BASE_DIR / "python" / "python.exe").exists()


def check_server_ready():
    """检查服务是否已就绪"""
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/model-status", timeout=2)
        return resp.status == 200
    except Exception:
        return False


def start_server():
    """启动服务"""
    python_exe = find_python()
    portable = is_portable()

    # 检查 Python
    if not os.path.exists(python_exe):
        if portable:
            print("  [Error] python/ directory not found")
        else:
            print("  [Error] venv not found, please run setup.bat first")
        input("  Press Enter to exit...")
        return

    # 开发版：检查并安装缺失依赖
    if not portable:
        print("  Checking dependencies...")
        ret = subprocess.run(
            [python_exe, "-c", "import funasr"],
            capture_output=True,
        )
        if ret.returncode != 0:
            print("  Installing dependencies, please wait...")
            pip_exe = str(BASE_DIR / "venv" / "Scripts" / "pip.exe")
            subprocess.run([
                pip_exe, "install", "fastapi", "uvicorn", "websockets",
                "numpy", "pydantic",
                "funasr", "modelscope", "torch", "torchaudio", "six", "torch_complex",
                "--index-url", "https://download.pytorch.org/whl/cpu",
            ])

    # 启动 uvicorn
    print("  Starting service...")

    local_model = (BASE_DIR / "models" / "SenseVoiceSmall").exists()
    if local_model:
        print("  Using local model, loading ~5s")
    else:
        print("  First run will download SenseVoice model (~893MB), please wait")
    print()

    # 设置环境变量
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BASE_DIR)

    proc = subprocess.Popen(
        [python_exe, "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(PORT),
         "--ws-ping-interval", "30"],
        env=env,
    )

    # 等待服务就绪
    waited = 0
    while waited < MAX_WAIT:
        if check_server_ready():
            break
        time.sleep(2)
        waited += 2
        dots = "." * ((waited // 2) % 4 + 1)
        print(f"\r  Waiting for service{dots} ({waited}s)", end="", flush=True)

    print()

    if check_server_ready():
        print(f"  Service ready! Opening browser...")
        webbrowser.open(URL)
    else:
        print(f"  Timeout. Please open browser manually: {URL}")

    print()
    print("  ==================================================")
    print(f"   Running at: {URL}")
    print("   Press Ctrl+C to stop")
    print("  ==================================================")
    print()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n  Stopping service...")
        proc.terminate()
        proc.wait(timeout=5)
        print("  Service stopped")


if __name__ == "__main__":
    start_server()
