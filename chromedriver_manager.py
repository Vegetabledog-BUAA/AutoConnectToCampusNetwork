# -*- coding: utf-8 -*-
"""ChromeDriver 的定位、版本检查与自动更新。

设计要点（每一条都对应一次线上踩坑）：

1. **驱动目录唯一化** —— 更新器写入的目录必须与 Selenium 实际加载的目录一致。
   旧实现更新器固定写 `ubelt` 缓存目录，而 `initialize_driver` 读
   `config['chromedriver_path']`，两者不同就会出现「新驱动下载到了用不上的地方，
   Selenium 一直加载旧驱动」。
2. **Chrome 版本探测不依赖 PowerShell / shell** —— 优先读注册表，速度是毫秒级且
   不受路径中的空格、中文、代码页影响。旧实现用 `shell=True` 拼接引号调用
   PowerShell 取 chrome.exe 的 ProductVersion，路径异常时直接抛异常。
3. **驱动版本读取不用 ascii 解码、不用 shell** —— 旧实现 `decode('ascii')`，
   输出含任何非 ASCII 字节就崩。
4. **版本清单查不到该 build 时有回退** —— 旧实现返回 `(None, None)`，
   随后 `requests.get(None)` 直接抛异常，整个更新中断且一次都不下载。
5. **替换失败可旁路安装** —— 旧驱动被残留进程或杀软占用时，`os.remove` 会失败。
   这里改为 `os.replace`，失败则落成 `chromedriver_<版本>.exe` 并记录状态文件，
   由 `find_driver_exe()` 统一解析，保证 Selenium 用上的是新驱动。
6. **所有异常都在本模块内消化**，绝不向调用方抛出。
"""

import io
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

import requests
import ubelt as ub

try:
    import winreg
except ImportError:
    winreg = None

from logger import log, log_with_notification

# Chrome for Testing 版本清单
CT_PATCH_PER_BUILD = ("https://googlechromelabs.github.io/chrome-for-testing/"
                      "latest-patch-versions-per-build-with-downloads.json")
CT_KNOWN_GOOD = ("https://googlechromelabs.github.io/chrome-for-testing/"
                 "known-good-versions-with-downloads.json")
HTTP_TIMEOUT = 60

# 下载失败后的冷却时间（秒），避免断网期间每轮循环都去请求外网
RETRY_COOLDOWN = 300
# 版本查询结果的缓存时间（秒）
VERSION_CACHE_TTL = 600

DRIVER_FILENAME = "chromedriver.exe"
STATE_FILENAME = "driver_state.json"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")
_BYPASS_RE = re.compile(r"^chromedriver_(\d+\.\d+\.\d+\.\d+)\.exe$", re.I)

# Chrome 的 Google Update 客户端 GUID（用于兜底读版本）
CHROME_UPDATE_GUID = "{8A69D345-D564-463c-AFF1-A69D9E530F96}"

_last_download_attempt = 0.0
_version_cache = {"ts": 0.0, "chrome": None, "driver": None,
                  "driver_sig": None, "folder": None}


# ----------------------------------------------------------------- 基础工具

def _version_tuple(text):
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return tuple(int(x) for x in match.group(1).split("."))
    except ValueError:
        return None


def _file_signature(path):
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _registry_value(hive, subkey, value_name=None):
    """读注册表，读不到返回 None（不抛异常）"""
    if winreg is None:
        return None
    for view in (getattr(winreg, "KEY_WOW64_64KEY", 0),
                 getattr(winreg, "KEY_WOW64_32KEY", 0),
                 0):
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as key:
                value, _ = winreg.QueryValueEx(key, value_name or "")
                if value not in (None, ""):
                    return str(value)
        except OSError:
            continue
    return None


# ----------------------------------------------------------------- 目录解析

def default_driver_folder():
    """默认驱动目录（程序数据目录）"""
    return ub.ensure_app_cache_dir('AutoConnect_chromedriver')


def resolve_driver_folder(config=None):
    """决定"驱动到底放在哪个目录"，必须与实际加载驱动的目录一致。

    :return: (目录, 是否修正了 config)
    """
    default = default_driver_folder()

    configured = ""
    if config is not None:
        configured = str(config.get("chromedriver_path") or "").strip().strip('"')

    if not configured:
        if config is not None:
            config["chromedriver_path"] = default
        return default, config is not None

    # 容忍早期版本/用户把这里填成了驱动文件路径
    if configured.lower().endswith(".exe"):
        configured = os.path.dirname(configured)

    if os.path.isdir(configured):
        return configured, False

    log(f"配置里的驱动目录不存在（{configured}），已改用默认目录 {default}", "WARNING")
    if config is not None:
        config["chromedriver_path"] = default
    return default, True


def _read_state(folder):
    try:
        with open(os.path.join(folder, STATE_FILENAME), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(folder, filename, version):
    try:
        with open(os.path.join(folder, STATE_FILENAME), "w", encoding="utf-8") as f:
            json.dump({"file": filename, "version": version}, f, indent=2)
    except Exception as e:
        log(f"写入驱动状态文件失败: {e}", "WARNING")


def find_driver_exe(folder):
    """返回该目录下实际应该使用的驱动文件路径。

    正常情况下就是 `chromedriver.exe`；如果上次替换时原文件被占用、
    改成了 `chromedriver_<版本>.exe`，则按状态文件返回旁路文件。
    """
    if not folder:
        return ""
    state = _read_state(folder)
    name = state.get("file")
    if name and os.path.isfile(os.path.join(folder, name)):
        return os.path.join(folder, name)
    return os.path.join(folder, DRIVER_FILENAME)


# ----------------------------------------------------------------- 版本读取

def find_chrome_path():
    """定位 chrome.exe。注册表 App Paths 优先，其次常见安装位置。"""
    candidates = []
    if winreg is not None:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            value = _registry_value(
                hive,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
            if value:
                candidates.append(value)

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(os.path.join(
                base, "Google", "Chrome", "Application", "chrome.exe"))

    found = shutil.which("chrome.exe")
    if found:
        candidates.append(found)

    for path in candidates:
        path = str(path).strip().strip('"')
        if path and os.path.isfile(path):
            return path
    return None


def _chrome_version_from_registry():
    """Chrome 自己写的 BLBeacon（最准，且毫秒级）"""
    if winreg is None:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        value = _registry_value(hive, r"SOFTWARE\Google\Chrome\BLBeacon", "version")
        if _version_tuple(value):
            return value
    return None


def _chrome_version_from_install_dir():
    """从安装目录下的版本号子目录名推断（例如 ...\\Application\\153.0.8010.48\\）"""
    chrome_path = find_chrome_path()
    if not chrome_path:
        return None
    folder = os.path.dirname(chrome_path)
    versions = []
    try:
        for name in os.listdir(folder):
            if _version_tuple(name) and os.path.isdir(os.path.join(folder, name)):
                versions.append(name)
    except OSError:
        return None
    if not versions:
        return None
    return max(versions, key=_version_tuple)


def _chrome_version_from_update_client():
    """Google Update 记录的 Chrome 版本号"""
    if winreg is None:
        return None
    for sub in (rf"SOFTWARE\WOW6432Node\Google\Update\Clients\{CHROME_UPDATE_GUID}",
                rf"SOFTWARE\Google\Update\Clients\{CHROME_UPDATE_GUID}"):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            value = _registry_value(hive, sub, "pv")
            if _version_tuple(value):
                return value
    return None


def _chrome_version_from_file():
    """最后兜底：读 chrome.exe 的文件版本信息。

    路径通过环境变量传进 PowerShell，不做字符串拼接，
    避免路径里的空格、中文、单引号破坏命令。
    """
    chrome_path = find_chrome_path()
    if not chrome_path:
        return None
    env = dict(os.environ, AC_CHROME_EXE=chrome_path)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Item -LiteralPath $env:AC_CHROME_EXE).VersionInfo.ProductVersion"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=20, env=env, creationflags=CREATE_NO_WINDOW, check=False)
    except Exception as e:
        log(f"读取 chrome.exe 文件版本失败: {e}", "WARNING")
        return None
    return (proc.stdout or "").strip() or None


def read_chrome_version():
    """返回 (版本号, 来源)。读不到返回 (None, 'none')。"""
    readers = (
        ("注册表 BLBeacon", _chrome_version_from_registry),
        ("安装目录版本子目录", _chrome_version_from_install_dir),
        ("Google Update 记录", _chrome_version_from_update_client),
        ("chrome.exe 文件版本", _chrome_version_from_file),
    )
    for name, reader in readers:
        try:
            value = reader()
        except Exception as e:
            log(f"读取 Chrome 版本（{name}）异常: {e}", "WARNING")
            value = None
        if _version_tuple(value):
            return str(value).strip(), name
    return None, "none"


def read_driver_version(driver_exe):
    """读取驱动版本。不使用 shell，也不做 ascii 解码。"""
    if not driver_exe or not os.path.isfile(driver_exe):
        return None
    try:
        proc = subprocess.run(
            [driver_exe, "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=20, creationflags=CREATE_NO_WINDOW, check=False)
    except Exception as e:
        log(f"执行 {os.path.basename(driver_exe)} --version 失败: {e}", "WARNING")
        return None
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    match = None
    for match in _VERSION_RE.finditer(text):
        pass
    return match.group(1) if match else None


def get_version_snapshot(folder=None, force=False):
    """返回 (Chrome 版本, 驱动版本)，带 TTL 缓存。读不到时为 None。"""
    folder = folder or default_driver_folder()
    driver_exe = find_driver_exe(folder)
    signature = _file_signature(driver_exe)
    now = time.time()
    cache = _version_cache

    if (not force
            and cache["ts"]
            and now - cache["ts"] < VERSION_CACHE_TTL
            and cache["folder"] == folder
            and cache["driver_sig"] == signature):
        return cache["chrome"], cache["driver"]

    chrome_version, source = read_chrome_version()
    if chrome_version:
        log(f"Chrome 版本 {chrome_version}（来源: {source}）", "DEBUG")
    else:
        log("无法获取 Chrome 版本（注册表/安装目录/文件版本信息均不可用）", "WARNING")

    driver_version = read_driver_version(driver_exe)
    if driver_version is None:
        log(f"无法获取 ChromeDriver 版本（{driver_exe}）", "WARNING")

    cache.update(ts=now, chrome=chrome_version, driver=driver_version,
                 driver_sig=signature, folder=folder)
    return chrome_version, driver_version


def is_matched(chrome_version, driver_version):
    """版本是否匹配。

    ChromeDriver 只要求**主版本号**与浏览器一致，补丁版本不同是正常的，
    因此不能用完整版本号比较，否则会因为构建号差异反复触发无意义的下载。
    返回 True / False / None（版本信息不全时无法判断）。
    """
    chrome_parts = _version_tuple(chrome_version)
    driver_parts = _version_tuple(driver_version)
    if not chrome_parts or not driver_parts:
        return None
    return chrome_parts[0] == driver_parts[0]


# ----------------------------------------------------------------- 下载安装

def _pick_driver_url(downloads):
    """挑选本机可用的驱动下载地址。

    注意：版本清单里 chromedriver 的条目顺序是 win32 在 win64 之前，
    不能"取第一个 Windows 条目"，否则 64 位机器会拿到 32 位驱动。
    """
    items = downloads.get("chromedriver", []) or []
    for wanted in _windows_driver_platforms():
        for item in items:
            if item.get("platform") == wanted:
                return item.get("url")
    return None


def _fetch_json(url):
    response = requests.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _windows_driver_platforms():
    """按优先级返回本机应使用的驱动平台标识"""
    machine = (platform.machine() or "").upper()
    if machine in ("ARM64", "AARCH64"):
        return ["win-arm64", "win64", "win32"]
    return ["win64", "win32"]


def find_target_driver_version(chrome_version):
    """找到与 Chrome 匹配的驱动版本与下载地址。

    :return: (版本, 下载地址, 说明)。失败时版本与地址为 None，说明写原因。
    """
    build_key = ".".join(str(chrome_version).split(".")[:3])

    try:
        data = _fetch_json(CT_PATCH_PER_BUILD)
    except Exception as e:
        return None, None, f"无法访问版本清单: {e}"

    item = (data.get("builds") or {}).get(build_key)
    if item:
        url = _pick_driver_url(item.get("downloads") or {})
        if url:
            return item.get("version"), url, ""
        return None, None, f"版本清单里 {build_key} 没有 Windows 版驱动"

    # 回退：清单里还没有这个 build（Chrome 刚升级、Chrome for Testing 尚未跟进），
    # 就在全量清单里挑一个不高于当前 Chrome 的最高版本。
    log(f"版本清单中没有 {build_key}，回退到全量清单挑不高于 Chrome {chrome_version} 的最高版本", "WARNING")
    try:
        all_data = _fetch_json(CT_KNOWN_GOOD)
    except Exception as e:
        return None, None, f"无法访问全量版本清单: {e}"

    target = _version_tuple(chrome_version)
    best = None
    for entry in all_data.get("versions") or []:
        parts = _version_tuple(entry.get("version"))
        if not parts or parts > target:
            continue
        url = _pick_driver_url(entry.get("downloads") or {})
        if url and (best is None or parts > best[0]):
            best = (parts, entry.get("version"), url)

    if best:
        return best[1], best[2], ""
    return None, None, f"清单里找不到不高于 Chrome {chrome_version} 的驱动"


def _extract_driver(zip_bytes, target_dir):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        archive.extractall(target_dir)
    for root, _dirs, files in os.walk(target_dir):
        if DRIVER_FILENAME in files:
            return os.path.join(root, DRIVER_FILENAME)
    return None


def install_driver(url, folder):
    """下载并安装驱动。

    :return: (成功, 说明, 驱动路径)
    """
    os.makedirs(folder, exist_ok=True)

    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        zip_bytes = response.content
    except Exception as e:
        return False, f"下载驱动失败: {e}", None

    if len(zip_bytes) < 1024 * 1024:
        return False, (f"下载内容只有 {len(zip_bytes)} 字节，明显不是驱动包"
                       "（可能被校园网/安全软件劫持）"), None

    with tempfile.TemporaryDirectory(prefix="ac_driver_") as tmp:
        try:
            source = _extract_driver(zip_bytes, tmp)
        except Exception as e:
            return False, f"解压驱动包失败: {e}", None
        if not source:
            return False, "驱动包里没有找到 chromedriver.exe", None

        # 先确认新驱动能跑起来，再替换掉能用的旧驱动
        new_version = read_driver_version(source)
        if not new_version:
            return False, "下载到的驱动无法执行（可能被杀软拦截或下载不完整）", None

        target = os.path.join(folder, DRIVER_FILENAME)
        try:
            os.replace(source, target)
            _write_state(folder, DRIVER_FILENAME, new_version)
            return True, f"已更新为 ChromeDriver {new_version}", target
        except OSError as e:
            # 旧文件被残留进程或安全软件占用：改成旁路文件名安装，
            # 再由 find_driver_exe() 按状态文件解析，保证新驱动真的被用上。
            alt_name = f"chromedriver_{new_version}.exe"
            alt_path = os.path.join(folder, alt_name)
            try:
                shutil.copyfile(source, alt_path)
            except Exception as e2:
                return False, f"写入驱动失败（原文件被占用: {e}；旁路也失败: {e2}）", None
            _write_state(folder, alt_name, new_version)
            log(f"chromedriver.exe 被占用无法覆盖，已旁路安装为 {alt_name}", "WARNING")
            return True, f"已旁路安装为 {alt_name}（原文件被占用: {e}）", alt_path


def ensure_chromedriver_ready(folder=None, allow_download=True, force=False):
    """确保 ChromeDriver 可用且与 Chrome 主版本一致。

    :return: (ready, message)。ready=False 时调用方仍可自行决定是否继续登录。
    """
    global _last_download_attempt

    folder = folder or default_driver_folder()
    chrome_version, driver_version = get_version_snapshot(folder)
    matched = is_matched(chrome_version, driver_version)
    detail = f"Chrome {chrome_version} / ChromeDriver {driver_version}"

    if matched is True:
        if chrome_version != driver_version:
            log(f"ChromeDriver 与 Chrome 主版本一致，补丁版本不同（{detail}），属正常情况", "INFO")
        return True, f"ChromeDriver 可用（{detail}）"

    if not allow_download:
        message = f"ChromeDriver 与 Chrome 版本不匹配（{detail}），当前不允许联网更新"
        log(message, "WARNING")
        return False, message

    now = time.time()
    elapsed = now - _last_download_attempt
    if not force and elapsed < RETRY_COOLDOWN:
        remain = int(RETRY_COOLDOWN - elapsed)
        message = f"ChromeDriver 与 Chrome 版本不匹配（{detail}），{remain} 秒内不重复尝试下载"
        log(message, "WARNING")
        return False, message

    if chrome_version is None:
        message = ("无法获取 Chrome 版本，无法确定该下载哪个驱动。"
                   "请确认 Chrome 已安装，或手动把 chromedriver.exe 放到: " + folder)
        log_with_notification(message, "WARNING", "配置警告")
        return False, message

    _last_download_attempt = now
    log_with_notification(
        f"检测到 {detail} 不匹配，正在尝试自动更新 ChromeDriver…", "WARNING", "配置警告")

    target_version, url, reason = find_target_driver_version(chrome_version)
    if not url:
        message = f"未能找到匹配 Chrome {chrome_version} 的 ChromeDriver：{reason}"
        log_with_notification(message, "WARNING", "配置警告")
        return False, message

    ok, info, path = install_driver(url, folder)
    if not ok:
        log_with_notification(f"ChromeDriver 自动更新失败：{info}", "ERROR", "配置错误")
        return False, info

    chrome_version, driver_version = get_version_snapshot(folder, force=True)
    if is_matched(chrome_version, driver_version) is False:
        message = f"更新后主版本仍不匹配（Chrome {chrome_version} / ChromeDriver {driver_version}）"
        log_with_notification(message, "WARNING", "配置警告")
        return False, message

    log_with_notification(
        f"ChromeDriver 已就绪：{info}（目标版本 {target_version}）", "INFO", "配置通知")
    return True, info


def check_chrome_chromedriver_matched(extra_para=True):
    """兼容旧调用方的包装，已废弃，请改用 ensure_chromedriver_ready()。"""
    ready, _ = ensure_chromedriver_ready(allow_download=bool(extra_para))
    return bool(ready)


# ----------------------------------------------------------------- 诊断

def collect_diagnostics():
    """收集一份可以直接贴出来排查的环境报告"""
    lines = []

    def add(key, value):
        lines.append(f"{key}: {value}")

    add("时间", time.strftime("%Y-%m-%d %H:%M:%S"))
    add("默认驱动目录", default_driver_folder())

    try:
        import config as cfgmod
        cfg = cfgmod.load_config()
        configured = (cfg.get("chromedriver_path") or "(空)")
        add("配置里的驱动目录", configured)
        add("两者是否一致",
            os.path.normcase(str(configured)) == os.path.normcase(default_driver_folder()))
    except Exception as e:
        add("读取配置", f"失败: {type(e).__name__} {e}")

    chrome_path = find_chrome_path()
    add("chrome.exe 路径", chrome_path or "(未找到)")
    for name, reader in (("注册表 BLBeacon", _chrome_version_from_registry),
                         ("安装目录版本子目录", _chrome_version_from_install_dir),
                         ("Google Update 记录", _chrome_version_from_update_client),
                         ("chrome.exe 文件版本", _chrome_version_from_file)):
        try:
            value = reader()
        except Exception as e:
            value = f"异常 {type(e).__name__}: {e}"
        add(f"  Chrome 版本来源 / {name}", value or "(无)")

    chrome_version, source = read_chrome_version()
    add("最终采用 Chrome 版本", f"{chrome_version}（来源: {source}）")

    folder = default_driver_folder()
    driver_exe = find_driver_exe(folder)
    add("实际加载的驱动文件", driver_exe)
    add("驱动文件是否存在", os.path.isfile(driver_exe))
    add("驱动版本", read_driver_version(driver_exe) or "(读不到)")
    add("主版本是否匹配", is_matched(chrome_version, read_driver_version(driver_exe)))

    try:
        add("目录内文件", ", ".join(sorted(os.listdir(folder))))
    except OSError as e:
        add("目录内文件", f"读取失败: {e}")

    if chrome_version:
        target_version, url, reason = find_target_driver_version(chrome_version)
        add("匹配的驱动版本", target_version or f"(未找到: {reason})")
        add("下载地址", url or "(无)")
    return lines


if __name__ == "__main__":
    print("=" * 68)
    print("ChromeDriver 环境诊断")
    print("=" * 68)
    for line in collect_diagnostics():
        print(line)

    print()
    print("=" * 68)
    start = time.time()
    chrome_ver, driver_ver = get_version_snapshot()
    first = time.time() - start
    start = time.time()
    get_version_snapshot()
    cached = time.time() - start
    print("版本快照 :", chrome_ver, "/", driver_ver)
    print("首次查询 : %.3f 秒 | 缓存命中 : %.4f 秒" % (first, cached))

    folder = default_driver_folder()
    ready, message = ensure_chromedriver_ready(folder, allow_download=False)
    print("不联网检查 ->", ready, "|", message)
    ready, message = ensure_chromedriver_ready(folder, allow_download=True)
    print("允许联网   ->", ready, "|", message)
