"""
SenseVoice Teleprompter - 打包脚本
将项目打包成自包含文件夹，可在任意 Windows 电脑上运行。
双击"一键启动.bat"即可自动安装 Python + 依赖 + 模型。

用法：python build_portable_v2.py
输出：../SenseVoiceTeleprompter_standalone/
"""

import subprocess
import sys
import os
import shutil
from pathlib import Path

# ============ 配置 ============
BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR.parent / "SenseVoiceTeleprompter_standalone"

# 需要拷贝的文件（不含 venv、python、models 等大目录）
PROJECT_FILES = [
    "server.py",
    "frontend/index.html",
    "一键启动.bat",
    "导播文稿模板.xlsx",
    "示例段落.json",
    "示例台词.txt",
]

# 前端资源
FRONTEND_DIR = BASE_DIR / "frontend"


def run(cmd, cwd=None):
    """执行命令"""
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd)
    return result.returncode


def main():
    print()
    print("  ======================================================")
    print("     SenseVoice Teleprompter - 打包为独立部署版")
    print(f"     输出目录: {OUTPUT_DIR}")
    print("  ======================================================")
    print()

    # 清理旧输出
    if OUTPUT_DIR.exists():
        print("  清理旧输出...")
        shutil.rmtree(OUTPUT_DIR)

    OUTPUT_DIR.mkdir(parents=True)

    # ---- 第一步：拷贝项目文件 ----
    print("\n  [1/2] 拷贝项目文件...")
    for rel_path in PROJECT_FILES:
        src = BASE_DIR / rel_path
        if not src.exists():
            print(f"    [!] 跳过不存在的文件: {rel_path}")
            continue
        dst = OUTPUT_DIR / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        size_mb = src.stat().st_size / (1024 * 1024)
        print(f"    [√] {rel_path} ({size_mb:.1f} MB)")

    # 拷贝前端目录
    dst_frontend = OUTPUT_DIR / "frontend"
    dst_frontend.mkdir(exist_ok=True)
    for f in FRONTEND_DIR.iterdir():
        if f.is_file() and f.suffix in ('.html', '.css', '.js'):
            shutil.copy2(f, dst_frontend / f.name)
            print(f"    [√] frontend/{f.name}")

    # ---- 第二步：创建 README ----
    readme = OUTPUT_DIR / "使用说明.txt"
    readme.write_text(
        "SenseVoice Teleprompter - 语音识别/提词器/导播\n"
        "=============================================\n\n"
        "使用方法：\n"
        "  1. 双击「一键启动.bat」\n"
        "  2. 首次运行会自动下载 Python 和依赖（约10分钟）\n"
        "  3. 首次识别会自动下载模型（约893MB）\n"
        "  4. 之后每次双击即可直接使用\n\n"
        "注意事项：\n"
        "  - 需要联网（首次下载依赖和模型）\n"
        "  - 需要 Windows 64 位系统\n"
        "  - 使用浏览器录音，不需要安装额外驱动\n"
        "  - 启动后会自动打开浏览器访问 http://localhost:8765\n\n"
        "文件夹说明：\n"
        "  python/    - 自动下载的 Python 环境（首次启动后生成）\n"
        "  models/    - 自动下载的模型文件（首次识别后生成）\n"
        "  frontend/  - 前端页面\n"
        "  server.py  - 后端服务\n\n"
        "如需完全卸载，删除整个文件夹即可。\n",
        encoding="utf-8",
    )
    print(f"    [√] 使用说明.txt")

    # ---- 完成 ----
    # 计算总大小
    total_size = sum(f.stat().st_size for f in OUTPUT_DIR.rglob("*") if f.is_file())
    total_mb = total_size / (1024 * 1024)

    print(f"\n  ======================================================")
    print(f"  [√] 打包完成!")
    print(f"  输出目录: {OUTPUT_DIR}")
    print(f"  总大小:   {total_mb:.1f} MB（不含 Python 和模型）")
    print(f"")
    print(f"  使用方法：")
    print(f"    1. 将整个文件夹拷贝到目标电脑")
    print(f"    2. 双击「一键启动.bat」")
    print(f"    3. 等待自动安装完成，浏览器会自动打开")
    print(f"  ======================================================\n")


if __name__ == "__main__":
    main()
