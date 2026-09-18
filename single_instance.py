# -*- coding: utf-8 -*-
"""单实例保护：保证同一时间只有一个 AutoConnect 进程在运行。

问题背景
--------
这个程序此前可以被重复启动，于是出现多个监控线程同时操作网络与配置文件，
表现成"停止监控很慢""配置改了不生效"这类很难归因的问题。
排查时实测到同一时刻存在多份实例（单文件 exe 自身还有一个 bootloader 父进程，
所以进程数看起来更多，但真正跑 Python 逻辑的只有一个）。

实现方式
--------
只用 Windows 命名内核对象，不引入任何第三方依赖：

- **命名互斥体（Mutex）**：权威的"是否已有实例"判定。
  `CreateMutexW` 之后 `GetLastError() == ERROR_ALREADY_EXISTS` 即说明已有一份在跑。
- **命名事件（Event）**：第二个实例不直接甩一个错误弹窗，而是把
  "请把主界面显示出来"这一请求告诉正在运行的实例；收到回应就安静退出，
  收不到才提示用户去托盘查看。

使用约定
--------
- 进程启动时调用 `acquire()`；返回 False 就说明该退出了。
- 启动完托盘/界面后调用 `start_show_window_listener(callback)`，
  callback 由后台线程调用，**必须只做线程安全的事（例如 emit 一个 Qt 信号）**，
  不允许直接操作 Qt 控件。
- 互斥体句柄必须一直握在手里（模块级变量）。句柄一释放，锁就没了。
"""

import os
import sys
import time
import ctypes
import threading

from logger import log

# 内核对象名。不带 Global\ / Local\ 前缀时属于当前会话命名空间，
# 这正是我们要的：同一用户的桌面会话内唯一。
MUTEX_NAME = "AutoConnectToCampusNetwork.SingleInstance"
SHOW_EVENT_NAME = "AutoConnectToCampusNetwork.ShowWindow"
ACK_EVENT_NAME = "AutoConnectToCampusNetwork.ShowWindowAck"

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

# 请求打开主界面时，等待对方回应的默认秒数
SHOW_REQUEST_TIMEOUT = 1.5
# 监听线程等待新请求的时间片（毫秒），用于周期性检查是否需要退出
LISTENER_POLL_MS = 1000

# 互斥体/事件句柄必须常驻，否则会被回收导致锁失效
_mutex_handle = None
_show_event = None
_ack_event = None
_listener_thread = None
_listener_stop = threading.Event()

# ---------------------------------------------------------------- ctypes 绑定

if os.name == "nt":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateEventW.argtypes = [wintypes.LPCVOID, wintypes.BOOL,
                                       wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.SetEvent.restype = wintypes.BOOL
    _kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.ResetEvent.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                    wintypes.LPCWSTR, wintypes.UINT]
    _user32.MessageBoxW.restype = ctypes.c_int

    _MB_OK = 0x00000000
    _MB_ICONINFORMATION = 0x00000040
    _MB_TOPMOST = 0x00040000
    _MB_SETFOREGROUND = 0x00010000
else:  # pragma: no cover - 项目只发布 Windows 版，这里只为源码可移植性留个出口
    _kernel32 = None
    _user32 = None


def _create_event(name):
    """创建或打开一个命名事件（手动重置）。失败返回 None。"""
    handle = _kernel32.CreateEventW(None, True, False, name)
    return handle or None


# ---------------------------------------------------------------- 对外接口

def acquire(name=MUTEX_NAME):
    """尝试取得实例锁。

    :return: (是否取得, 说明文字)。已经取得过则直接返回成功。
    """
    global _mutex_handle

    if os.name != "nt":
        return True, "非 Windows 平台，跳过单实例保护"
    if _mutex_handle:
        return True, "本进程已持有实例锁"

    handle = _kernel32.CreateMutexW(None, False, name)
    # 必须在任何其它调用之前取错误码，后续调用会覆盖它
    error = ctypes.get_last_error()
    if not handle:
        # 创建失败（权限等）时不阻塞用户使用，退化为"没有保护"
        return True, f"创建实例互斥体失败（错误码 {error}），已跳过单实例保护"

    if error == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False, "已有实例在运行"

    _mutex_handle = handle
    return True, "已取得实例锁"


def release():
    """释放实例锁与相关句柄（进程退出时系统也会自动释放）。"""
    global _mutex_handle, _show_event, _ack_event

    _listener_stop.set()
    for attr in ("_mutex_handle", "_show_event", "_ack_event"):
        handle = globals().get(attr)
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass
            globals()[attr] = None


def request_show_window(timeout=SHOW_REQUEST_TIMEOUT):
    """（由第二个实例调用）请求正在运行的实例打开主界面。

    :return: True 表示请求已被正在运行的实例处理；False 表示没有实例响应。
    """
    if os.name != "nt":
        return False

    request_event = _create_event(SHOW_EVENT_NAME)
    ack_event = _create_event(ACK_EVENT_NAME)
    if not request_event or not ack_event:
        return False

    try:
        # 先清掉可能残留的旧回应，再发请求，避免把上一次的回应当成这一次的
        _kernel32.ResetEvent(ack_event)
        _kernel32.SetEvent(request_event)
        result = _kernel32.WaitForSingleObject(ack_event, int(timeout * 1000))
        return result == WAIT_OBJECT_0
    except Exception as e:
        log(f"请求显示主界面失败: {e}", "WARNING")
        return False
    finally:
        _kernel32.CloseHandle(request_event)
        _kernel32.CloseHandle(ack_event)


def start_show_window_listener(callback):
    """（由正在运行的实例调用）开始监听"新实例请求打开主界面"。

    :param callback: 由监听线程调用。**只允许做线程安全的事**，
                     例如 emit 一个 Qt 信号（跨线程 emit + QueuedConnection 是安全的），
                     绝不能在回调里直接操作 Qt 控件。
    :return: True 表示监听已启动（或已在运行）
    """
    global _show_event, _ack_event, _listener_thread

    if os.name != "nt":
        return False
    if _listener_thread is not None:
        return True

    _show_event = _create_event(SHOW_EVENT_NAME)
    _ack_event = _create_event(ACK_EVENT_NAME)
    if not _show_event or not _ack_event:
        log("创建单实例事件对象失败，新实例将无法自动唤起主界面", "WARNING")
        return False

    _listener_stop.clear()

    def _loop():
        while not _listener_stop.is_set():
            try:
                result = _kernel32.WaitForSingleObject(_show_event, LISTENER_POLL_MS)
            except Exception:
                return
            if result != WAIT_OBJECT_0:
                continue

            _kernel32.ResetEvent(_show_event)
            try:
                callback()
                log("收到新实例的请求，已打开主界面", "INFO")
            except Exception as e:
                log(f"响应显示主界面请求失败: {e}", "ERROR")
            finally:
                # 无论成功与否都要回应，否则新实例会以为没人处理而弹窗
                _kernel32.SetEvent(_ack_event)

    _listener_thread = threading.Thread(
        target=_loop, name="SingleInstanceListener", daemon=True)
    _listener_thread.start()
    return True


def _show_message_box(text, title):
    """弹一个系统消息框。

    :return: (是否成功显示并被确认, 错误码)。
             **不要忽略失败** —— 调用方必须留痕，
             否则用户会觉得"双击了完全没反应"。
             错误码必须在这里就地取，`GetLastError` 会被后续任何一次调用覆盖。
    """
    if os.name != "nt":
        return False, 0
    try:
        result = int(_user32.MessageBoxW(
            None, text, title,
            _MB_OK | _MB_ICONINFORMATION | _MB_TOPMOST | _MB_SETFOREGROUND))
        if result:
            return True, 0
        return False, ctypes.get_last_error()
    except Exception:
        return False, ctypes.get_last_error()


def notify_already_running(message=None):
    """提示用户程序已经在运行。用系统弹窗，不依赖 Qt（此时可能还没建 QApplication）。

    :return: True 表示用户看到了弹窗；False 表示弹窗没能显示（信息已落到日志与控制台）
    """
    text = message or (
        "AutoConnectToCampusNetwork 已经在运行中。\n\n"
        "如果没看到界面，请在系统托盘（屏幕右下角）查看程序图标。")

    shown, error = _show_message_box(text, "程序已在运行")
    if shown:
        return True

    # 弹窗没能显示时不能就这么算了 —— 至少把原因和内容写进日志、打到控制台
    log(f"提示弹窗未能显示（错误码 {error}）：{text}", "WARNING")
    print(text)
    return False


def is_running(name=MUTEX_NAME):
    """只查询是否已有实例，不占用锁。用于诊断与自检。"""
    if os.name != "nt":
        return False
    handle = _kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return error == ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    import subprocess

    print("=" * 66)
    print("单实例保护自检")
    print("=" * 66)
    print("平台          :", sys.platform)
    print("互斥体名      :", MUTEX_NAME)

    print("\n--- 1. 首次取得实例锁 ---")
    ok, message = acquire()
    print("  acquire() ->", ok, "|", message)
    assert ok, "首次取得实例锁应当成功"

    print("\n--- 2. 同进程重复取得（应直接返回成功，不算冲突）---")
    ok2, message2 = acquire()
    print("  acquire() ->", ok2, "|", message2)
    assert ok2

    print("\n--- 3. 另起一个进程尝试取得同一把锁（应被拒绝）---")
    child_code = (
        "import sys; sys.path.insert(0, %r);"
        "import single_instance as si;"
        "ok, msg = si.acquire();"
        "print('CHILD_RESULT', ok, msg);"
        "sys.exit(0 if not ok else 1)" % os.path.dirname(os.path.abspath(__file__))
    )
    proc = subprocess.run([sys.executable, "-c", child_code],
                          capture_output=True, text=True, timeout=60)
    print("  子进程输出:", (proc.stdout or "").strip() or (proc.stderr or "").strip()[-200:])
    print("  子进程退出码:", proc.returncode, "（期望 0 = 被正确拒绝）")
    assert proc.returncode == 0, "第二个进程本应被拒绝取得锁"

    print("\n--- 4. 无监听者时请求显示主界面（应超时返回 False）---")
    start = time.time()
    handled = request_show_window(timeout=0.5)
    print("  request_show_window() ->", handled, "| 耗时 %.2f 秒" % (time.time() - start))
    assert handled is False, "没有监听者时不该报告已被处理"

    print("\n--- 5. 启动监听后，再请求显示主界面（应被回应）---")
    hits = []
    start_show_window_listener(lambda: hits.append(time.time()))
    time.sleep(0.3)
    start = time.time()
    handled = request_show_window(timeout=2.0)
    cost = time.time() - start
    print("  request_show_window() ->", handled, "| 耗时 %.2f 秒" % cost)
    time.sleep(0.3)
    print("  监听回调被调用次数:", len(hits))
    assert handled is True, "有监听者时应当收到回应"
    assert len(hits) == 1, "回调应当被调用一次"

    print("\n--- 6. is_running() 查询 ---")
    print("  is_running() ->", is_running(), "（本进程持锁，期望 True）")
    assert is_running() is True

    print("\n--- 7. 释放后，新进程应能取得锁 ---")
    release()
    print("  release() 完成，is_running() ->", is_running())
    proc2 = subprocess.run([sys.executable, "-c", child_code],
                           capture_output=True, text=True, timeout=60)
    print("  子进程输出:", (proc2.stdout or "").strip() or (proc2.stderr or "").strip()[-200:])
    print("  子进程退出码:", proc2.returncode, "（期望 1 = 成功取得锁）")
    assert proc2.returncode == 1, "释放后新进程应当能取得锁"

    print("\n--- 8. 弹窗无法显示时必须留痕（否则用户会觉得双击没反应）---")
    original_show = _show_message_box
    try:
        globals()["_show_message_box"] = lambda text, title: (False, 1400)
        shown = notify_already_running("自检文案：弹窗不可用")
        print("  弹窗失败时 notify_already_running() ->", shown, "（期望 False）")
        assert shown is False

        globals()["_show_message_box"] = lambda text, title: (True, 0)
        shown = notify_already_running("自检文案：弹窗可用")
        print("  弹窗成功时 notify_already_running() ->", shown, "（期望 True）")
        assert shown is True
    finally:
        globals()["_show_message_box"] = original_show

    print("\n全部自检通过")
