# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

所有路径都基于 `SPECPATH`（本文件所在目录）计算，不写死绝对路径 ——
这样在任何机器、任何工作目录下执行 `pyinstaller AutoConnect.spec` 结果都一样。
（SPECPATH 由 PyInstaller 注入，等于本文件所在目录。）

版本资源来自 `version_info.txt`，由 `python version.py --sync` 从 `version.py`
的 `__version__` 生成 —— 版本号只有一个来源，不要在别处手改。
"""
import os

from PyInstaller.utils.hooks import collect_all

ROOT = SPECPATH  # noqa: F821  —— PyInstaller 注入的变量
ICON_FILE = os.path.join(ROOT, 'icon.ico')
VERSION_FILE = os.path.join(ROOT, 'version_info.txt')
CONFIG_TEMPLATE = os.path.join(ROOT, 'config.json')

datas = [
    # 图标要在运行时读取（托盘 / 窗口 / 任务栏），必须打进包里。
    # 由 build_icon.py 生成，含 16~256 全部常用尺寸。
    (ICON_FILE, '.'),
    (CONFIG_TEMPLATE, '.'),
]
binaries = []
hiddenimports = ['ubelt']

tmp_ret = collect_all('selenium')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

tmp_ret = collect_all('latest_chromedriver')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AutoConnect',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON_FILE],
    # 写进 exe 的版本资源：资源管理器「属性 → 详细信息」会显示这里的版本与说明。
    # 不指定时该页是空的，用户无法判断手上是哪个版本。
    version=VERSION_FILE,
)
