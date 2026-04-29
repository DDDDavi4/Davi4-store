"""
SenseVoice Teleprompter - 便携版构建脚本
在当前机器上执行一次，生成可分发的便携版文件夹。

用法：python build_portable.py
输出：../SenseVoiceTeleprompter_portable/ （可直接拷贝到其他电脑使用）
"""

import subprocess
import sys
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

# ============ 配置 ============
BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR.parent / "SenseVoiceTeleprompter_portable"

# Python Embedded 版本
PYTHON_VERSION = "3.11.9"
PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"

# 需要安装的 pip 包
PIP_CORE = [
    "fastapi", "uvicorn", "websockets",
    "sounddevice", "numpy", "pydantic",
]

PIP_FUNASR = [
    "funasr", "modelscope", "hydra-core", "sentencepiece",
    "jieba", "omegaconf", "transformers", "safetensors",
    "librosa", "soundfile", "six", "torch_complex",
]

# 需要拷贝到便携版的文件
PROJECT_FILES = [
    "server.py",
    "launcher.py",
    "frontend/index.html",
    "导播文稿模板.xlsx",
    "示例段落.json",
    "示例台词.txt",
]


def run(cmd, cwd=None, check=True):
    """执行命令并实时输出"""
    print(f"  $ {cmd}")
    proc = subprocess.Popen(
        cmd, shell=True, cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    for line in proc.stdout:
        try:
            decoded = line.decode("utf-8", errors="replace").rstrip()
        except Exception:
            decoded = line.decode("gbk", errors="replace").rstrip()
        # 替换无法在 gbk 控制台输出的字符
        try:
            decoded.encode("gbk")
        except UnicodeEncodeError:
            decoded = decoded.encode("gbk", errors="replace").decode("gbk")
        print(f"    {decoded}")
    proc.wait()
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed (exit code {proc.returncode})")
    return proc.returncode


def download_file(url, dest):
    """下载文件，显示进度"""
    print(f"  下载: {url}")
    def report(block, blocksize, totalsize):
        done = block * blocksize
        if totalsize > 0:
            pct = done / totalsize * 100
            mb = done / 1048576
            total_mb = totalsize / 1048576
            print(f"\r  进度: {mb:.1f}/{total_mb:.1f} MB ({pct:.0f}%)", end="", flush=True)
    urllib.request.urlretrieve(url, dest, reporthook=report)
    print()
    size_mb = os.path.getsize(dest) / 1048576
    print(f"  完成: {size_mb:.1f} MB")


def step_download_python(output_dir):
    """步骤1：下载 Python Embedded"""
    python_dir = output_dir / "python"
    if (python_dir / "python.exe").exists():
        print(f"  [跳过] python/ 已存在")
        return

    print(f"\n{'='*50}")
    print(f"  步骤 1/5: 下载 Python {PYTHON_VERSION} Embedded")
    print(f"{'='*50}\n")

    zip_path = output_dir / "_python_embed.zip"
    download_file(PYTHON_EMBED_URL, zip_path)

    print("  解压中...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(python_dir)
    zip_path.unlink()

    # 修改 ._pth 文件启用 site-packages
    pth_files = list(python_dir.glob("*._pth"))
    for pth in pth_files:
        content = pth.read_text(encoding="utf-8")
        if "#import site" in content:
            content = content.replace("#import site", "import site")
            # 添加 site-packages 路径
            content = content.replace(".\n", ".\n.\nLib\\site-packages\n", 1)
            pth.write_text(content, encoding="utf-8")
            print(f"  已修改 {pth.name} 启用 site-packages")
        elif "import site" not in content:
            content += "\nimport site\n"
            pth.write_text(content, encoding="utf-8")
            print(f"  已修改 {pth.name} 启用 site-packages")

    # 下载 get-pip.py 并安装 pip
    print("  安装 pip...")
    get_pip = output_dir / "_get_pip.py"
    download_file("https://bootstrap.pypa.io/get-pip.py", get_pip)
    run(f'"{python_dir / "python.exe"}" "{get_pip}"')
    get_pip.unlink()

    print("  [OK] Python Embedded 安装完成")


def step_install_packages(output_dir):
    """步骤2：安装所有 pip 包"""
    pip_exe = output_dir / "python" / "Scripts" / "pip.exe"

    if not pip_exe.exists():
        print("  [错误] pip 未安装，请先执行步骤1")
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"  步骤 2/5: 安装 Python 依赖")
    print(f"{'='*50}\n")

    print("  安装核心依赖...")
    run(f'"{pip_exe}" install {" ".join(PIP_CORE)}')

    print("  安装 PyTorch (CPU)...")
    run(f'"{pip_exe}" install torch torchaudio --index-url https://download.pytorch.org/whl/cpu')

    print("  安装 FunASR 引擎...")
    run(f'"{pip_exe}" install {" ".join(PIP_FUNASR)}')

    print("  [OK] 所有依赖安装完成")


def step_download_models(output_dir):
    """步骤3：下载模型到本地"""
    models_dir = output_dir / "models"
    sensevoice_dir = models_dir / "SenseVoiceSmall"
    vad_dir = models_dir / "fsmn-vad"

    if sensevoice_dir.exists() and vad_dir.exists():
        print(f"\n  [跳过] 模型已存在于 models/ 目录")
        return

    print(f"\n{'='*50}")
    print(f"  步骤 3/5: 下载 SenseVoice 模型到本地")
    print(f"{'='*50}\n")

    models_dir.mkdir(parents=True, exist_ok=True)

    # 生成临时下载脚本，避免复杂字符串转义问题
    script_path = output_dir / "_download_models.py"
    script_content = '''\
import sys, os, shutil
from pathlib import Path

models_dir = Path(r"%s")
sensevoice_dir = Path(r"%s")
vad_dir = Path(r"%s")

def download_model(model_id, dest_dir):
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    from modelscope import snapshot_download
    print(f"  下载模型: {model_id} ...")
    path = snapshot_download(model_id, cache_dir=models_dir)
    print(f"  下载完成: {path}")
    if os.path.isdir(path) and os.path.abspath(path) != os.path.abspath(dest_dir):
        shutil.copytree(path, dest_dir, dirs_exist_ok=True)
        print(f"  已复制到: {dest_dir}")

print("  预注册模型类...")
from funasr.models.sense_voice.model import SenseVoiceSmall
print("  注册成功")

print("  下载 SenseVoiceSmall 模型 (~893MB)...")
download_model("iic/SenseVoiceSmall", sensevoice_dir)

print("  下载 fsmn-vad 模型...")
download_model("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", vad_dir)

print("  所有模型下载完成!")
''' % (str(models_dir), str(sensevoice_dir), str(vad_dir))
    script_path.write_text(script_content, encoding="utf-8")

    python_exe = output_dir / "python" / "python.exe"
    run(f'"{python_exe}" "{script_path}"', check=False)

    if script_path.exists():
        script_path.unlink()

    print("  [OK] 模型下载完成")


def step_copy_files(output_dir):
    """步骤4：拷贝项目文件"""
    print(f"\n{'='*50}")
    print(f"  步骤 4/5: 拷贝项目文件")
    print(f"{'='*50}\n")

    for rel_path in PROJECT_FILES:
        src = BASE_DIR / rel_path
        dst = output_dir / rel_path
        if not src.exists():
            print(f"  [警告] 源文件不存在: {rel_path}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  [OK] {rel_path}")

    print("  [OK] 项目文件拷贝完成")


def step_create_launcher(output_dir):
    """步骤5：创建启动脚本"""
    print(f"\n{'='*50}")
    print(f"  步骤 5/5: 创建启动脚本")
    print(f"{'='*50}\n")

    bat_content = (
        "@echo off\n"
        "chcp 65001 >nul 2>&1\n"
        "title SenseVoice Teleprompter\n"
        "color 0A\n"
        "cd /d \"%~dp0\"\n"
        "\n"
        "python\\python.exe launcher.py\n"
        "pause\n"
    )
    (output_dir / "启动.bat").write_text(bat_content, encoding="gbk")
    print("  [OK] 启动.bat")

    install_bat = (
        "@echo off\n"
        "chcp 65001 >nul 2>&1\n"
        "title 修复依赖 - SenseVoice Teleprompter\n"
        "cd /d \"%~dp0\"\n"
        "\n"
        "echo.\n"
        "echo  正在修复缺失的依赖...\n"
        "echo.\n"
        "\n"
        "python\\Scripts\\pip.exe install fastapi uvicorn websockets sounddevice numpy pydantic\n"
        "python\\Scripts\\pip.exe install torch torchaudio --index-url https://download.pytorch.org/whl/cpu\n"
        "python\\Scripts\\pip.exe install funasr modelscope hydra-core sentencepiece jieba omegaconf transformers safetensors librosa soundfile six torch_complex\n"
        "\n"
        "echo.\n"
        "echo  修复完成！请重新运行 启动.bat\n"
        "echo.\n"
        "pause\n"
    )
    (output_dir / "修复依赖.bat").write_text(install_bat, encoding="gbk")
    print("  [OK] 修复依赖.bat")

    readme = (
        "# SenseVoice Teleprompter - 便携版\n"
        "\n"
        "## 使用方法\n"
        "\n"
        "1. 双击 启动.bat 即可运行\n"
        "2. 首次启动会自动加载模型（约5秒）\n"
        "3. 浏览器会自动打开 http://localhost:8765\n"
        "\n"
        "## 功能\n"
        "\n"
        "- 语音识别模式：实时语音转文字\n"
        "- 提词器模式：朗读跟踪 + 高亮当前行\n"
        "- 导播模式：段落定位 + 镜头切换提示\n"
        "\n"
        "## 文件说明\n"
        "\n"
        "- 启动.bat - 主启动脚本\n"
        "- 修复依赖.bat - 如果启动失败，运行此脚本修复依赖\n"
        "- python/ - 嵌入式 Python 环境（不要删除）\n"
        "- models/ - 本地 AI 模型文件（不要删除）\n"
        "- frontend/ - 前端页面\n"
        "- server.py - 后端服务\n"
        "- launcher.py - 启动器\n"
        "\n"
        "## 系统要求\n"
        "\n"
        "- Windows 10/11 64位\n"
        "- 约 2.5GB 磁盘空间\n"
        "- 麦克风\n"
        "\n"
        "## 分发\n"
        "\n"
        "整个文件夹可以拷贝到其他 Windows 电脑直接使用，无需安装任何软件。\n"
    )
    (output_dir / "README.txt").write_text(readme, encoding="utf-8")
    print("  [OK] README.txt")

    print("  [OK] 启动脚本创建完成")


def main():
    print()
    print("  ==================================================")
    print("     SenseVoice Teleprompter - 便携版构建工具")
    print("  ==================================================")
    print()
    print(f"  输出目录: {OUTPUT_DIR}")
    print(f"  Python: {PYTHON_VERSION} Embedded")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        step_download_python(OUTPUT_DIR)
        step_install_packages(OUTPUT_DIR)
        step_download_models(OUTPUT_DIR)
        step_copy_files(OUTPUT_DIR)
        step_create_launcher(OUTPUT_DIR)

        print()
        print("  ==================================================")
        print("  [OK] 构建完成！")
        print(f"  输出: {OUTPUT_DIR}")
        print("  将整个文件夹拷贝到其他电脑即可使用")
        print("  ==================================================")
        print()

    except Exception as e:
        print(f"\n  [错误] 构建失败: {e}")
        print("  可以重新运行此脚本，已完成的步骤会自动跳过")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
