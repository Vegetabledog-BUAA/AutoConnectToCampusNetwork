import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent
ICON = ROOT / "icon.ico"
MAIN = ROOT / "main.py"

def run(cmd):
    # 去除多余控制台输出，保持静默
    subprocess.check_call(cmd)

def ensure_requirements():
    pkgs = [
        "pyinstaller",
        "PyQt5",
        "selenium",
        "latest_chromedriver",
        "ubelt",        # chromedriver_manager 直接依赖
        "certifi",      # SSL 证书校验
        "pywin32",
        "winshell",
        "cryptography",
    ]
    run([sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir"] + pkgs)

def build():
    os.chdir(ROOT)
    datas = []
    if ICON.exists():
        datas += ["--icon", str(ICON)]
        datas += ["--add-data", f"{ICON};."]  # 确保 icon.ico 被打包
    cfg = ROOT / "config.json"
    if cfg.exists():
        datas += ["--add-data", f"{cfg};."]

    # 隐式导入（防止打包后某些动态加载失败）
    hidden_imports = [
        "--hidden-import", "ubelt",
        "--hidden-import", "selenium",
        "--hidden-import", "latest_chromedriver",
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "-F",            # 单文件
        "-w",            # 无控制台窗口
        "--name", "AutoConnect",
        str(MAIN),
    ] + datas + hidden_imports

    run(cmd)
    # 不使用 print 避免在非打包环境出现控制台
    # 构建结果路径：dist/NetworkChecker.exe

if __name__ == "__main__":
    ensure_requirements()
    build()