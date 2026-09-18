# -*- coding: utf-8 -*-
"""版本号 —— 全项目唯一来源。

改版本号只需要改这里的 `__version__`，其余地方都从这里取：

- 界面「关于/检查更新」显示的当前版本
- 更新检查时与远端 `latest.json` 里的 version 比较
- `package/AutoConnectInnoSetupScriptFiles.iss` 的 `MyAppVersion` 需要手动同步，
  一致性由 `iss_version()` + `check_consistency()` 校验（开发期自检用）
"""

import io
import os

__version__ = "1.1.1"

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


def check_consistency(path=None):
    """校验 .iss 里的版本号与 __version__ 一致。

    :return: (是否一致, 说明)。开发期自检用；打包后的 exe 里读不到 .iss，
             此时返回 (True, '读不到 .iss，跳过校验')。
    """
    remote = iss_version(path)
    if remote is None:
        return True, "读不到 .iss（打包环境），跳过版本一致性校验"
    if remote != __version__:
        return False, (f"版本号不一致：version.py 是 {__version__}，"
                       f"安装包脚本是 {remote}")
    return True, f"版本号一致：{__version__}"


if __name__ == "__main__":
    print("当前版本      :", __version__)
    print("仓库          : %s/%s@%s" % (REPO_OWNER, REPO_NAME, REPO_BRANCH))
    print("下载页地址    :", DOWNLOAD_PAGE_URL)
    print("安装包脚本版本:", iss_version())
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
        ("1.1.1", False, "同版本"),
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

    ok, message = check_consistency()
    print("[%s] %s" % ("通过" if ok else "失败", message))
    print()
    print("自检%s" % ("通过" if (all_ok and ok) else "未通过"))
