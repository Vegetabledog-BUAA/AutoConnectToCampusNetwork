# -*- coding: utf-8 -*-
"""版本号 —— 全项目唯一来源。

改版本号只需要改这里的 `__version__`，其余地方都从这里取：

- 界面「关于/检查更新」显示的当前版本
- 更新检查时与远端 `latest.json` 里的 version 比较
- `package/AutoConnectInnoSetupScriptFiles.iss` 的 `MyAppVersion` 需要手动同步，
  一致性由 `iss_version()` + `check_consistency()` 校验（开发期自检用）
- `version_info.txt`（写进 exe 的版本资源，资源管理器「属性 → 详细信息」可见）
  由 `write_version_info()` 生成，一致性同样由 `check_consistency()` 校验

版本号一变，先跑 `python version.py --sync` 把两个派生文件对齐，再打包。
"""

import io
import os

__version__ = "2.0.0"

# 写进 exe 版本资源的应用信息。改这里会影响资源管理器里显示的内容。
APP_INTERNAL_NAME = "AutoConnect"
APP_FILE_DESCRIPTION = "校园网自动检查与登录工具"
APP_PRODUCT_NAME = "AutoConnect 校园网自动登录"
APP_COMPANY_NAME = "Spring Equinox (Beihang University)"
APP_COPYRIGHT = "Copyright (C) 2026 Spring Equinox"
APP_ORIGINAL_FILENAME = "AutoConnect.exe"

# 简体中文 + Unicode(1200) 的 LanguageID/Codepage，与 VarFileInfo 的 [2052, 1200] 对应
VERSION_LANG_CODEPAGE = "080404B0"
VERSION_TRANSLATION = "[2052, 1200]"

# 仓库信息（更新清单就放在这个仓库的根目录）
REPO_OWNER = "Vegetabledog-BUAA"
REPO_NAME = "AutoConnectToCampusNetwork"
REPO_BRANCH = "main"

# 更新清单的多个候选地址，按顺序尝试，任意一个成功即可。
#
# 为什么要多个：实测国内不少网络会对 raw.githubusercontent.com 与 cdn.jsdelivr.net
# 做 TLS 中间人，证书链校验直接失败（self-signed certificate in certificate chain），
# 而 api.github.com 通常被放行。三个都用同一个文件，只是通道不同。
MANIFEST_URLS = (
    # 1) GitHub contents API：返回 base64 包装的 JSON，稍重但放行率最高
    "https://api.github.com/repos/%s/%s/contents/latest.json"
    % (REPO_OWNER, REPO_NAME),
    # 2) raw：最轻量，返回纯 JSON
    "https://raw.githubusercontent.com/%s/%s/%s/latest.json"
    % (REPO_OWNER, REPO_NAME, REPO_BRANCH),
    # 3) jsDelivr CDN：国内访问通常较快
    "https://cdn.jsdelivr.net/gh/%s/%s@%s/latest.json"
    % (REPO_OWNER, REPO_NAME, REPO_BRANCH),
)

# 兼容旧调用：默认取第一个源
UPDATE_MANIFEST_URL = MANIFEST_URLS[0]

# 安装包分发页（更新提示里的"去下载"按钮打开这里）
DOWNLOAD_PAGE_URL = (
    "https://bhpan.buaa.edu.cn/link/AAF517447738684467B1D19FE740A288A6"
)

# 仓库的 Release 页面（若改用 GitHub Release 分发安装包，把它填进 latest.json 的
# download_url 即可）
RELEASE_PAGE_URL = "https://github.com/%s/%s/releases" % (REPO_OWNER, REPO_NAME)

_ISS_RELATIVE = os.path.join("package", "AutoConnectInnoSetupScriptFiles.iss")
VERSION_INFO_FILE = "version_info.txt"


def _project_root():
    return os.path.dirname(os.path.abspath(__file__))


def parse_version(text):
    """把版本号字符串解析成可比较的元组。

    容忍 `v1.2.3` / `1.2.3-beta` / `1.2` 这些写法：
    只取数字段，非数字后缀忽略（预发布版本在本项目里不做区分）。
    """
    if not text:
        return ()
    cleaned = str(text).strip().lstrip("vV")
    parts = []
    for chunk in cleaned.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == "":
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(remote, local=__version__):
    """远端版本是否比本地新。

    用元组比较，所以要先把长度补齐 —— 否则 (1, 2) 会被认为小于 (1, 1, 3)。
    """
    r, l = parse_version(remote), parse_version(local)
    if not r or not l:
        return False
    size = max(len(r), len(l))
    r = r + (0,) * (size - len(r))
    l = l + (0,) * (size - len(l))
    return r > l


def iss_version(path=None):
    """读取安装包脚本里的 MyAppVersion（读不到返回 None）。"""
    target = path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), _ISS_RELATIVE)
    try:
        with io.open(target, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("#define MyAppVersion"):
                    return stripped.split('"')[1]
    except Exception:
        return None
    return None


def version_info_text(version=None):
    """生成 PyInstaller 的版本资源文件内容（写进 exe，供资源管理器属性页显示）。

    必须是**单个 Python 表达式**：PyInstaller 直接对它 `eval()`，
    因此不能有前导注释、不能有编码声明。字符串里的引号按 Python 规则转义。
    """
    numbers = parse_version(version or __version__)
    numbers = tuple(numbers) + (0,) * (4 - len(numbers))
    quad = "(%s)" % ", ".join(str(n) for n in numbers[:4])

    def quote(text):
        return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"

    fields = [
        ("CompanyName", APP_COMPANY_NAME),
        ("FileDescription", APP_FILE_DESCRIPTION),
        ("FileVersion", version or __version__),
        ("InternalName", APP_INTERNAL_NAME),
        ("LegalCopyright", APP_COPYRIGHT),
        ("OriginalFilename", APP_ORIGINAL_FILENAME),
        ("ProductName", APP_PRODUCT_NAME),
        ("ProductVersion", version or __version__),
    ]
    rows = ",\n".join("            StringStruct(%s, %s)"
                      % (quote(key), quote(value)) for key, value in fields)

    return """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%s,
    prodvers=%s,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          %s,
          [
%s
          ])
      ]),
    VarFileInfo([VarStruct('Translation', %s)])
  ]
)
""" % (quad, quad, quote(VERSION_LANG_CODEPAGE), rows,
       "[" + VERSION_TRANSLATION.strip("[]") + "]")


def version_info_path(path=None):
    return path or os.path.join(_project_root(), VERSION_INFO_FILE)


def write_version_info(path=None):
    """把版本资源文件写到磁盘。返回写入的路径；失败返回 None。"""
    target = version_info_path(path)
    try:
        with io.open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(version_info_text())
        return target
    except Exception:
        return None


def exe_version_string(path=None):
    """从 version_info.txt 里读回 FileVersion（读不到返回 None）。"""
    target = version_info_path(path)
    try:
        with io.open(target, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    marker = "StringStruct('FileVersion', '"
    start = text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = text.find("'", start)
    return text[start:end] if end > start else None


def check_consistency(path=None, info_path=None):
    """校验 .iss 与 version_info.txt 里的版本号都和 __version__ 一致。

    :return: (是否一致, 说明)。打包后的 exe 里这两个文件都不存在，
             此时跳过校验（这属于正常情况，不是错误）。
    """
    problems = []

    remote = iss_version(path)
    if remote is None:
        problems.append("读不到 .iss（打包环境）")
    elif remote != __version__:
        problems.append("安装包脚本 %s" % remote)

    exe_version = exe_version_string(info_path)
    if exe_version is None:
        problems.append("读不到 version_info.txt（打包环境）")
    elif exe_version != __version__:
        problems.append("版本资源 %s" % exe_version)

    if problems:
        # 两个文件都读不到 => 打包环境，视为通过
        if len(problems) == 2:
            return True, "读不到 .iss 与 version_info.txt（打包环境），跳过版本一致性校验"
        return False, ("版本号不一致：version.py 是 %s，但 %s"
                       % (__version__, "；".join(problems)))
    return True, f"版本号一致（.iss 与 version_info.txt 均为 {__version__}）"


def sync_derived_files(iss_path=None, info_path=None):
    """把 .iss 的 MyAppVersion 与 version_info.txt 同步为 __version__。

    这是"版本号只有一个来源"的落地点：升版本时改完 `__version__`，
    跑一次本函数即可，避免手工漏改某一处。
    :return: (改动的文件列表, 失败信息列表)
    """
    changed, errors = [], []

    # version_info.txt：直接重写
    if write_version_info(info_path):
        changed.append(VERSION_INFO_FILE)
    else:
        errors.append("写入 %s 失败" % VERSION_INFO_FILE)

    # .iss：只替换 MyAppVersion 那一行，其余内容原样保留
    # newline="" 读写：不做换行符转换，避免把 CRLF 的文件整体改成 LF
    target = iss_path or os.path.join(_project_root(), _ISS_RELATIVE)
    try:
        with io.open(target, encoding="utf-8", newline="") as f:
            lines = f.readlines()
        updated = []
        replaced = False
        for line in lines:
            if line.lstrip().startswith("#define MyAppVersion"):
                eol = "\r\n" if line.endswith("\r\n") else "\n"
                updated.append('#define MyAppVersion "%s"%s' % (__version__, eol))
                replaced = True
            else:
                updated.append(line)
        if not replaced:
            errors.append(".iss 里没有找到 MyAppVersion 那一行")
        else:
            with io.open(target, "w", encoding="utf-8", newline="") as f:
                f.writelines(updated)
            changed.append(_ISS_RELATIVE)
    except Exception as e:
        errors.append("更新 .iss 失败: %s" % e)

    return changed, errors


if __name__ == "__main__":
    if "--sync" in __import__("sys").argv:
        files, errs = sync_derived_files()
        print("当前版本:", __version__)
        for name in files:
            print("  已同步:", name)
        for message in errs:
            print("  [失败]", message)
        ok, message = check_consistency()
        print()
        print("[%s] %s" % ("通过" if ok else "失败", message))
        __import__("sys").exit(0 if (ok and not errs) else 1)

    print("当前版本      :", __version__)
    print("仓库          : %s/%s@%s" % (REPO_OWNER, REPO_NAME, REPO_BRANCH))
    print("下载页地址    :", DOWNLOAD_PAGE_URL)
    print("安装包脚本版本:", iss_version())
    print("版本资源版本  :", exe_version_string())
    print()
    print("--- 更新清单候选源（按顺序尝试）---")
    for i, url in enumerate(MANIFEST_URLS, 1):
        print("  %d) %s" % (i, url))
    print()

    print("--- 版本解析 ---")
    for raw in ("1.1.1", "v1.2.0", "2.0", "1.10.3", "1.2.3-beta", "", None):
        print("  %-12r -> %s" % (raw, parse_version(raw)))
    print()

    print("--- 版本比较（本地 %s）---" % __version__)
    cases = [
        ("1.1.1", False, "更旧"),
        ("1.1.0", False, "更旧"),
        ("1.1.2", True, "更新的补丁版"),
        ("v1.2.0", True, "更新的次版本"),
        ("1.1", False, "长度不足需补零后比较"),
        ("2.0", True, "更大的主版本"),
        ("", False, "空值不认为是新版本"),
        ("abc", False, "非数字不认为是新版本"),
    ]
    all_ok = True
    for remote, expected, why in cases:
        got = is_newer(remote)
        ok = got == expected
        all_ok = all_ok and ok
        print("  [%s] is_newer(%-8r) = %-5s （%s）"
              % ("通过" if ok else "失败", remote, got, why))
    print()
    print("--- 版本资源文件 ---")
    print("  路径:", version_info_path())
    info_ok = True
    try:
        import tempfile
        from PyInstaller.utils.win32.versioninfo import (
            load_version_info_from_text_file,
        )
        tmp = os.path.join(tempfile.gettempdir(), "_version_info_check.txt")
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(version_info_text())
        info = load_version_info_from_text_file(tmp)
        dumped = str(info)
        info_ok = ("FileVersion" in dumped
                   and "'%s'" % __version__ in dumped
                   and "ProductVersion" in dumped)
        print("  [%s] PyInstaller 能解析，且 FileVersion = %s"
              % ("通过" if info_ok else "失败", __version__))
        if not info_ok:
            print("        实际内容片段:", dumped[:200].replace("\n", " "))
    except ImportError:
        print("  （当前环境没有 PyInstaller，跳过解析校验）")
    except Exception as e:
        info_ok = False
        print("  [失败] 版本资源文件无法被 PyInstaller 解析:", e)

    print()
    ok, message = check_consistency()
    print("[%s] %s" % ("通过" if ok else "失败", message))
    print()
    print("自检%s" % ("通过" if (all_ok and ok and info_ok) else "未通过"))
