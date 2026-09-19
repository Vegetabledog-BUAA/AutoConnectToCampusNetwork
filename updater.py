# -*- coding: utf-8 -*-
"""检查是否有新版本。

设计原则（都是踩过的坑换来的）
------------------------------
1. **只做"检查 + 提示 + 打开下载页"，不做自动替换。**
   程序是 PyInstaller 单文件 exe，正在运行时无法替换自身；再加上下载几百 MB、
   校验完整性、UAC 提权，失败路径太多。安装包本身就是覆盖升级，交给用户点两下最稳。

2. **本模块不依赖 PyQt。** 它只负责"取数据 + 判断"，全部在后台线程里跑；
   线程安全的结果投递（信号 + QueuedConnection）由界面层负责，
   避免重演"后台线程直接操作 Qt 对象"那类问题。

3. **任何失败都静默返回 None。** 校园网掉线时本来就断网，
   更新检查绝不能因此弹错、拖慢启动，更不能影响登录这条主链路。

4. **结果带缓存。** 默认 24 小时内只真正请求一次，避免每次启动都打扰
   raw.githubusercontent.com；用户手动点"检查更新"时用 force=True 绕过。

清单格式（放在公开的分发仓库里，见 version.UPDATE_MANIFEST_URL）::

    {
      "version": "1.1.2",
      "download_url": "https://...",   # 可选，缺省用 version.DOWNLOAD_PAGE_URL
      "notes": "本次更新内容…",          # 可选
      "mandatory": false,               # 可选，true 表示强制更新
      "published_at": "2026-09-19"      # 可选
    }
"""

import os
import json
import time
import base64

import requests
import ubelt as ub

from logger import log
from version import (
    __version__,
    MANIFEST_URLS,
    DOWNLOAD_PAGE_URL,
    is_newer,
    parse_version,
)

# 单次请求的超时（秒）。宁可放弃检查，也不要卡住启动。
FETCH_TIMEOUT = 6
# 自动检查的默认间隔（小时）。-1 = 关闭，0 = 每次启动都检查，正数 = 至少间隔这么多小时。
# 实际取值来自配置项 update_check_hours（界面「系统配置 → 自动检查更新」）。
DEFAULT_INTERVAL_HOURS = 24
# 启动后延迟多久才开始检查（秒）：让登录这条主链路先跑起来
STARTUP_DELAY = 20

# 同一进程里只允许安排一次自动检查：托盘与主界面都会尝试安排，
# 各自安排一次会让启动时出现两条"开始检查更新"日志（实际只有第一条会联网）。
_process_auto_check_taken = False

_dpath = ub.ensure_app_cache_dir("AutoConnect_chromedriver")
STATE_FILE = os.path.join(_dpath, "update_state.json")


class UpdateInfo(object):
    """一次更新检查的结果"""

    def __init__(self, version, download_url="", notes="", mandatory=False,
                 published_at=""):
        self.version = version
        self.download_url = download_url or DOWNLOAD_PAGE_URL
        self.notes = notes or ""
        self.mandatory = bool(mandatory)
        self.published_at = published_at or ""

    def __repr__(self):
        return ("UpdateInfo(version=%r, mandatory=%s, url=%r)"
                % (self.version, self.mandatory, self.download_url))


# ------------------------------------------------------------------ 缓存状态

def _read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(**updates):
    """更新缓存文件。失败只记日志，不影响功能。"""
    try:
        data = _read_state()
        data.update(updates)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"写入更新状态失败: {e}", "WARNING")


def last_check_at():
    try:
        return float(_read_state().get("last_check", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def skipped_version():
    """被用户选择"跳过此版本"的版本号"""
    return str(_read_state().get("skipped_version") or "")


def skip_version(version):
    _write_state(skipped_version=str(version or ""))
    log(f"已跳过版本 {version} 的更新提示", "INFO")


def should_check_now(force=False, interval_hours=None):
    """自动检查的节流：距上次检查不足间隔就跳过。

    保留这个函数名是为了向后兼容；新代码建议直接用 `auto_check_due()`，
    它会同时给出"为什么跳过"的说明，便于写进日志。
    """
    if force:
        return True
    return auto_check_due(interval_hours)[0]


def resolve_interval_hours(hours=None):
    """把配置值规整成合法的间隔小时数。非法值一律回落到默认值。"""
    if hours is None:
        return DEFAULT_INTERVAL_HOURS
    try:
        value = int(hours)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS
    return -1 if value < -1 else value


def format_interval(hours):
    """把小时数说成人话（用于界面与提示）。"""
    if hours < 0:
        return "已关闭"
    if hours == 0:
        return "每次启动"
    if hours == 168:
        return "每周"
    if hours % 24 == 0:
        days = hours // 24
        return "每天" if days == 1 else "每 %d 天" % days
    return "每 %d 小时" % hours


def _duration_text(hours):
    """把间隔说成时长，用于「距上次检查不足 X」这类句子。"""
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return "1 天" if days == 1 else "%d 天" % days
    return "%d 小时" % hours


def auto_check_due(interval_hours=None):
    """现在该不该做一次自动检查。

    :return: (是否到期, 说明)。说明可以直接写进日志 ——
             上一版的"跳过"日志是 DEBUG 级别，用户看不到，
             于是只看到一条"开始检查更新"却没有任何结果，像是凭空多查了一次。
    """
    hours = resolve_interval_hours(interval_hours)
    if hours < 0:
        return False, "自动检查更新已关闭"

    last = last_check_at()
    if not last:
        return True, "尚无检查记录，执行首次自动检查"
    if hours == 0:
        return True, "已设为每次启动都检查"

    elapsed = time.time() - last
    if elapsed < hours * 3600:
        return False, ("距上次检查不足 %s（上次 %s %s），本轮自动跳过"
                       % (_duration_text(hours),
                          time.strftime("%m-%d", time.localtime(last)),
                          time.strftime("%H:%M", time.localtime(last))))
    return True, "距上次检查已到 %s" % format_interval(hours)


def take_auto_check_slot():
    """同一进程内只允许安排一次自动检查。返回 True 表示本次由你安排。

    托盘与主界面都会安排自动检查，两边都排的话启动时会打出两条
    "开始检查更新"，其中一条必然被节流跳过 —— 纯噪音。
    """
    global _process_auto_check_taken
    if _process_auto_check_taken:
        return False
    _process_auto_check_taken = True
    return True


def reset_auto_check_slot():
    """仅供测试：恢复"还未安排过自动检查"的状态。"""
    global _process_auto_check_taken
    _process_auto_check_taken = False


# ------------------------------------------------------------------ 抓取判断

def _unwrap_manifest(payload):
    """把各来源的响应统一成清单 dict。

    - raw / jsDelivr 直接返回清单 JSON；
    - GitHub contents API 返回的是
      `{"content": "<base64>", "encoding": "base64", ...}`，需要解一层。
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("encoding") == "base64" and payload.get("content"):
        try:
            raw = base64.b64decode(payload["content"]).decode("utf-8")
            decoded = json.loads(raw)
        except Exception as e:
            log(f"更新检查：清单 base64 解码失败: {e}", "DEBUG")
            return None
        return decoded if isinstance(decoded, dict) else None

    return payload


def fetch_manifest(urls=None, timeout=FETCH_TIMEOUT):
    """依次尝试各个候选源，返回第一个成功的清单；全都失败返回 None。

    任何异常都被吞掉（断网、DNS 失败、证书被中间人拦掉、限流 403…），
    调用方不需要 try。
    """
    for url in (urls or MANIFEST_URLS):
        try:
            response = requests.get(url, timeout=timeout, headers={
                # 无 UA 的请求容易被 CDN / API 拒绝，给一个明确的标识
                "User-Agent": f"AutoConnectToCampusNetwork/{__version__}",
                "Accept": "application/vnd.github.raw+json, application/json",
                "Cache-Control": "no-cache",
            })
            if response.status_code != 200:
                log(f"更新检查：[{_short(url)}] HTTP {response.status_code}",
                    "DEBUG")
                continue

            manifest = _unwrap_manifest(response.json())
            if not isinstance(manifest, dict) or not manifest.get("version"):
                log(f"更新检查：[{_short(url)}] 清单格式不正确（缺少 version）",
                    "DEBUG")
                continue

            log(f"更新检查：清单取自 {_short(url)}", "DEBUG")
            return manifest
        except Exception as e:
            # 断网、DNS、证书校验失败都会走到这里 —— 换下一个源
            log(f"更新检查：[{_short(url)}] {type(e).__name__}: "
                f"{str(e)[:120]}", "DEBUG")
            continue

    log("更新检查：所有候选源都不可用（已忽略）", "DEBUG")
    return None


def _short(url):
    """日志里只留域名 + 末段，避免整条 URL 刷屏"""
    try:
        host = url.split("://", 1)[1].split("/", 1)[0]
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        return f"{host}/…/{tail}"
    except Exception:
        return url


def build_update_info(manifest, current=__version__):
    """把清单转成 UpdateInfo；不需要更新时返回 None。纯函数，便于测试。"""
    if not manifest:
        return None
    remote = str(manifest.get("version") or "").strip()
    if not is_newer(remote, current):
        return None
    return UpdateInfo(
        version=remote,
        download_url=str(manifest.get("download_url") or "").strip(),
        notes=str(manifest.get("notes") or "").strip(),
        mandatory=bool(manifest.get("mandatory")),
        published_at=str(manifest.get("published_at") or "").strip(),
    )


def check_for_update(force=False, current=__version__, urls=None,
                     interval_hours=None):
    """检查更新。**在后台线程里调用。**

    :param force: True 表示忽略节流（用户手动点"检查更新"）
    :param interval_hours: 自动检查的最小间隔（小时），来自配置项
    :return: UpdateInfo（有新版本且未被跳过）或 None。绝不抛异常。
    """
    try:
        if not force:
            due, reason = auto_check_due(interval_hours)
            if not due:
                # INFO 级别：让用户看得见"为什么没有检查"，
                # 否则日志里只剩一条"开始检查更新"，像是凭空多查了一次
                log("更新检查：%s" % reason, "INFO")
                return None

        manifest = fetch_manifest(urls)
        # 无论成功与否都记一次时间，避免网络异常时每次启动都重试
        _write_state(last_check=time.time())
        if manifest is None:
            return None

        info = build_update_info(manifest, current)
        if info is None:
            log(f"更新检查：当前已是最新版本（{current}）", "INFO")
            return None

        if not force and info.version == skipped_version():
            log(f"更新检查：{info.version} 已被用户跳过，不再提示", "INFO")
            return None

        log(f"更新检查：发现新版本 {info.version}（当前 {current}）", "INFO")
        return info
    except Exception as e:
        # 这个地方绝不能把异常抛给调用它的线程
        log(f"更新检查发生异常（已忽略）: {e}", "WARNING")
        return None


if __name__ == "__main__":
    print("=" * 66)
    print("更新检查自检")
    print("=" * 66)
    print("当前版本    :", __version__)
    print("候选清单源  :")
    for i, url in enumerate(MANIFEST_URLS, 1):
        print("   %d) %s" % (i, url))
    print("缓存文件    :", STATE_FILE)
    print("上次检查    :", time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(last_check_at()))
          if last_check_at() else "(从未)")
    print()

    print("--- 1. 清单转 UpdateInfo（纯函数）---")
    cases = [
        ({"version": "1.1.2"}, True, "更新版本"),
        ({"version": "1.1.1"}, False, "同版本"),
        ({"version": "1.0.0"}, False, "旧版本"),
        ({"version": "v2.0.0", "notes": "说明", "mandatory": True}, True, "带 v 前缀 + 强制"),
        ({"version": ""}, False, "版本为空"),
        ({"notes": "没有 version"}, False, "缺字段"),
        (None, False, "清单为空"),
    ]
    for manifest, expect, why in cases:
        info = build_update_info(manifest)
        ok = (info is not None) == expect
        print("  [%s] %-46s （%s）"
              % ("通过" if ok else "失败", repr(manifest)[:46], why))
        if info:
            print("          -> %r" % (info,))
    print()

    print("--- 2. 版本号非法/长尾情况不会抛异常 ---")
    for bad in ({"version": "abc"}, {"version": None}, {"version": 123},
                {"version": [1, 2]}):
        try:
            r = build_update_info(bad)
            print("  [通过] %-30r -> %r" % (bad, r))
        except Exception as e:
            print("  [失败] %-30r 抛异常: %r" % (bad, e))
    print()

    print("--- 3. 节流与跳过 ---")
    _write_state(last_check=time.time())
    print("  刚检查过 -> should_check_now() =", should_check_now(), "（期望 False）")
    print("  强制检查 -> should_check_now(True) =", should_check_now(True), "（期望 True）")
    _write_state(last_check=0)
    print("  清空时间 -> should_check_now() =", should_check_now(), "（期望 True）")
    skip_version("9.9.9")
    print("  标记跳过后 skipped_version() =", skipped_version())
    skip_version("")
    print()

    print("--- 3b. 间隔设置，以及「为什么跳过」的说明 ---")
    for hours, label in ((24, "每天"), (72, "每 3 天"), (168, "每周"),
                         (0, "每次启动"), (-1, "关闭")):
        print("  %-8s（%3d 小时）-> 说明: %s" % (label, hours, format_interval(hours)))
    print("  非法值 99 小时 -> 规整为", resolve_interval_hours(99))
    print("  非法值 'x'     -> 规整为", resolve_interval_hours("x"))
    _write_state(last_check=time.time())
    for hours in (-1, 0, 24, 168):
        due, why = auto_check_due(hours)
        print("  刚查过 + 间隔 %-4d 小时 -> 到期=%-5s（%s）" % (hours, due, why))
    _write_state(last_check=time.time() - 25 * 3600)
    due, why = auto_check_due(24)
    print("  25 小时前查过 + 间隔 24 小时 -> 到期=%-5s（%s）" % (due, why))
    _write_state(last_check=0)
    print("  从没查过 -> 到期=%s（%s）" % auto_check_due(24))
    reset_auto_check_slot()
    print("  同一进程只安排一次自动检查:", take_auto_check_slot(), take_auto_check_slot(),
          "（期望 True False）")
    reset_auto_check_slot()
    print()

    print("--- 4. 多源回退：三种通道的响应都能解析 ---")
    fake = {"version": "9.9.9", "notes": "测试"}
    wrapped = {
        "content": base64.b64encode(
            json.dumps(fake).encode("utf-8")).decode("ascii"),
        "encoding": "base64",
    }
    print("  raw / jsDelivr 形态 ->", _unwrap_manifest(fake))
    print("  contents API 形态   ->", _unwrap_manifest(wrapped))
    print("  垃圾数据           ->", _unwrap_manifest({"foo": "bar"}))
    print("  None               ->", _unwrap_manifest(None))
    print()

    print("--- 5. 真实抓取（网络不通会返回 None，属正常）---")
    manifest = fetch_manifest()
    print("  fetch_manifest() ->", manifest)
    info = check_for_update(force=True)
    print("  check_for_update(force=True) ->", info)
    print()
    print("自检结束（第 5 步依赖网络，失败不代表代码有问题）")
