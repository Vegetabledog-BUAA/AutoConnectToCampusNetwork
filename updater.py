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
# 两次自动检查之间的最小间隔（秒），默认 24 小时
CHECK_INTERVAL = 24 * 3600
# 启动后延迟多久才开始检查（秒）：让登录这条主链路先跑起来
STARTUP_DELAY = 20

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


def should_check_now(force=False):
    """自动检查的节流：距上次检查不足 CHECK_INTERVAL 就跳过"""
    if force:
        return True
    return (time.time() - last_check_at()) >= CHECK_INTERVAL


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


def check_for_update(force=False, current=__version__, urls=None):
    """检查更新。**在后台线程里调用。**

    :param force: True 表示忽略节流（用户手动点"检查更新"）
    :return: UpdateInfo（有新版本且未被跳过）或 None。绝不抛异常。
    """
    try:
        if not should_check_now(force):
            log("更新检查：距上次检查不足 %d 小时，本轮跳过"
                % (CHECK_INTERVAL // 3600), "DEBUG")
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
            log(f"更新检查：{info.version} 已被用户跳过，不再提示", "DEBUG")
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
