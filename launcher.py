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


def main():
    portable = is_portable()
    mode_label = "便携版" if portable else "开发版"

    print()
    print("  ==================================================")
    print("     SenseVoice Teleprompter")
    print(f"     ({mode_label})")
    print("  ==================================================")
    print()
    print("    [1] 检查环境")
    print("    [2] 启动工具")
    print("    [0] 退出")
    print()

    choice = input("  请输入选项 [0-2]: ").strip()
    print()

    if choice == "0":
        return

    if choice == "1":
        check_env()
        return

    if choice == "2":
        start_server()
        return

    print("  无效选项，请输入 0、1 或 2")


def check_env():
    """检查运行环境"""
    python_exe = find_python()
    portable = is_portable()

    print("  ------------------------------------------------")
    print("  检查运行环境")
    print("  ------------------------------------------------")
    print()

    # 检查 Python
    if not os.path.exists(python_exe):
        print("  [×] Python: 未找到可用的 Python 解释器")
        if portable:
            print("      便携版缺少 python/ 目录")
        else:
            print("      请先运行 setup.bat 安装环境")
        return

    ret = subprocess.run([python_exe, "--version"], capture_output=True, text=True)
    print(f"  [√] Python: {ret.stdout.strip()}  ({python_exe})")
    print()

    # 检查关键依赖
    deps = [
        ("funasr", "SenseVoice 引擎"),
        ("torch", "PyTorch (CPU)"),
        ("fastapi", "Web 框架"),
        ("uvicorn", "ASGI 服务器"),
        ("sounddevice", "音频采集"),
        ("numpy", "数值计算"),
    ]
    print("  检查依赖:")
    all_ok = True
    for pkg, desc in deps:
        ret = subprocess.run([python_exe, "-c", f"import {pkg}"], capture_output=True)
        status = "[√]" if ret.returncode == 0 else "[×]"
        if ret.returncode != 0:
            all_ok = False
        print(f"    {status} {pkg:20s} {desc}")

    if not all_ok and not portable:
        print()
        print("  [提示] 部分依赖缺失，请运行 setup.bat 安装")
    print()

    # 检查 SenseVoice 模型
    local_model = BASE_DIR / "models" / "SenseVoiceSmall"
    if local_model.exists():
        print(f"  [√] SenseVoice 模型: 本地 models/ 目录已存在")
    else:
        # 检查 ModelScope 缓存
        ret = subprocess.run(
            [python_exe, "-c",
             "from pathlib import Path; p=Path.home()/'.cache/modelscope/hub'; "
             "dirs=[d for d in p.iterdir() if d.is_dir() and 'sense' in d.name.lower()] if p.exists() else []; "
             "print(len(dirs))"],
            capture_output=True, text=True,
        )
        model_count = int(ret.stdout.strip()) if ret.returncode == 0 else 0
        if model_count > 0:
            print(f"  [√] SenseVoice 模型: ModelScope 缓存 ({model_count})")
        else:
            print("  [ ] SenseVoice 模型: 首次启动时自动下载 (~893MB)")
    print()


def start_server():
    """启动服务"""
    python_exe = find_python()
    portable = is_portable()

    # 检查 Python
    if not os.path.exists(python_exe):
        if portable:
            print("  [错误] 便携版 python/ 目录不完整")
        else:
            print("  [错误] 虚拟环境不存在，请先运行 setup.bat")
        input("  按回车键退出...")
        return

    # 开发版：检查并安装缺失依赖
    if not portable:
        print("  检查依赖...")
        ret = subprocess.run(
            [python_exe, "-c", "import funasr"],
            capture_output=True,
        )
        if ret.returncode != 0:
            print("  [提示] 正在安装依赖，请稍候...")
            pip_exe = str(BASE_DIR / "venv" / "Scripts" / "pip.exe")
            subprocess.run([
                pip_exe, "install", "fastapi", "uvicorn", "websockets",
                "sounddevice", "numpy", "pydantic",
                "funasr", "modelscope", "torch", "torchaudio", "six", "torch_complex",
                "--index-url", "https://download.pytorch.org/whl/cpu",
            ])

    # 启动 uvicorn
    print("  正在启动服务...")

    local_model = (BASE_DIR / "models" / "SenseVoiceSmall").exists()
    if local_model:
        print("  使用本地模型，加载约需5秒")
    else:
        print("  首次启动需下载 SenseVoice 模型（~893MB），请耐心等待")
    print()

    # 设置环境变量：确保模型缓存路径正确
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
        print(f"\r  等待服务就绪{dots} ({waited}s)", end="", flush=True)

    print()

    if check_server_ready():
        print(f"  ✓ 服务已就绪！正在打开浏览器...")
        webbrowser.open(URL)
    else:
        print(f"  ⚠ 等待超时，请手动打开浏览器访问 {URL}")

    print()
    print("  ==================================================")
    print(f"   服务运行中，访问地址: {URL}")
    print("   按 Ctrl+C 停止服务")
    print("  ==================================================")
    print()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n  正在停止服务...")
        proc.terminate()
        proc.wait(timeout=5)
        print("  服务已停止")


if __name__ == "__main__":
    main()
