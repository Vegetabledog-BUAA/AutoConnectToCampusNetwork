# -*- coding: utf-8 -*-
"""日志系统：按日期分文件写入 + 按保留时长清理。

日志位置：`<程序数据目录>/logs/auto_connect_YYYY-MM-DD.log`

- 跨天自动切换到新文件（不必重启程序）；
- 启动时按配置的「日志保留天数」删除过期文件；
- 提供日志文件的列举 / 读取 / 删除接口，供界面的日志管理器使用。
"""

import os
import time
import datetime
import logging

import ubelt as ub

dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
CONFIG_FILE = os.path.join(dpath, "config.json")

# 日志目录与文件命名
LOG_DIR = os.path.join(dpath, "logs")
LOG_FILE_PREFIX = "auto_connect_"
LOG_FILE_EXT = ".log"
DATE_FORMAT = "%Y-%m-%d"

# 旧版单文件日志的路径（用于一次性迁移）
LEGACY_LOG_FILE = os.path.join(dpath, "auto_connect.log")

DEFAULT_RETENTION_DAYS = 7
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365

# 打开界面时默认回显最近几天的日志
RECENT_LOG_DAYS = 3
# 单个文件回显的最大行数 / 全部回显的最大行数
PER_FILE_TAIL_LINES = 300
TOTAL_TAIL_LINES = 1200

LOG_FORMAT = '[%(asctime)s] %(levelname)s: %(message)s'
LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'

# 全局日志记录器
_logger = None
_ui_log_handler = None
_daily_handler = None

# 全局通知回调函数
_notification_callback = None


def _make_formatter():
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)


def current_log_file(date_str=None):
    """指定日期（默认今天）的日志文件完整路径"""
    date_str = date_str or time.strftime(DATE_FORMAT)
    return os.path.join(LOG_DIR, f"{LOG_FILE_PREFIX}{date_str}{LOG_FILE_EXT}")


# 兼容旧引用：含义是"今天的日志文件"。
# 注意跨天后它会过期，需要动态取值的地方请调用 current_log_file()。
LOG_FILE = current_log_file()


def _parse_file_date(name):
    """从文件名解析日期（形如 auto_connect_2026-09-18.log）。

    只取前缀 10 个字符，因此 auto_connect_2026-09-18_legacy.log 也能正确归到 09-18。
    """
    if not (name.startswith(LOG_FILE_PREFIX) and name.endswith(LOG_FILE_EXT)):
        return None
    raw = name[len(LOG_FILE_PREFIX):-len(LOG_FILE_EXT)]
    date_part = raw[:10]
    try:
        datetime.datetime.strptime(date_part, DATE_FORMAT)
    except ValueError:
        return None
    return date_part


def list_log_files():
    """列出日志目录下的日志文件（按日期从新到旧）"""
    result = []
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return result

    for name in names:
        date_part = _parse_file_date(name)
        if not date_part:
            continue
        path = os.path.join(LOG_DIR, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        result.append({
            "path": path,
            "name": name,
            "date": date_part,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        })

    result.sort(key=lambda item: (item["date"], item["name"]), reverse=True)
    return result


def read_log_tail(path, limit):
    """从文件尾部读取最后 limit 行（大文件也不会整份读进来）"""
    if not path or limit <= 0:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            position = f.tell()
            block = 8192
            data = b""
            while position > 0 and data.count(b"\n") <= limit:
                step = min(block, position)
                position -= step
                f.seek(position)
                data = f.read(step) + data
    except OSError:
        return []
    return data.decode("utf-8", errors="ignore").splitlines()[-limit:]


def read_recent_logs(days=RECENT_LOG_DAYS, per_file_limit=PER_FILE_TAIL_LINES,
                     total_limit=TOTAL_TAIL_LINES):
    """读取最近若干天的日志，按时间从旧到新返回 [(路径, [行]), ...]"""
    selected = list_log_files()[:max(1, int(days))]
    selected.reverse()

    bundles = []
    remaining = int(total_limit)
    for item in selected:
        if remaining <= 0:
            break
        lines = read_log_tail(item["path"], min(int(per_file_limit), remaining))
        remaining -= len(lines)
        bundles.append((item["path"], lines))
    return bundles


def delete_log_file(path):
    """删除指定的日志文件。

    若它正好是当前正在写入的文件，必须先把写入句柄释放掉，
    否则 Windows 会以"文件被占用"拒绝删除；下次写日志时会自动重新打开。

    :return: (是否成功, 说明文字)
    """
    if not path:
        return False, "未指定日志文件"

    if _daily_handler is not None:
        try:
            _daily_handler.release_file(path)
        except Exception:
            pass

    try:
        if os.path.exists(path):
            os.remove(path)
        return True, "日志文件已删除"
    except Exception as e:
        return False, f"删除日志文件失败: {e}"


def clear_log_file():
    """兼容旧调用：删除今天的日志文件"""
    return delete_log_file(current_log_file())


def cleanup_old_logs(retention_days):
    """删除超过保留天数的日志文件。

    :return: (被删除的文件路径列表, 出错说明)
    """
    try:
        days = int(retention_days)
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    days = max(MIN_RETENTION_DAYS, min(days, MAX_RETENTION_DAYS))

    # 保留 days 天（含今天）：早于 cutoff 的才删除
    cutoff = datetime.date.today() - datetime.timedelta(days=days - 1)

    deleted = []
    errors = []
    for item in list_log_files():
        try:
            file_date = datetime.datetime.strptime(item["date"], DATE_FORMAT).date()
        except ValueError:
            continue
        if file_date >= cutoff:
            continue
        ok, message = delete_log_file(item["path"])
        if ok:
            deleted.append(item["path"])
        else:
            errors.append(message)
    return deleted, "；".join(errors)


def migrate_legacy_log():
    """把旧版单文件 auto_connect.log 搬进 logs/ 目录（只做一次）。

    只做"改名"这一种干净操作：改名失败（例如旧版程序还在运行、文件被占用）时
    只记录告警，下次启动会再试。刻意不做"复制一份"的兜底 —— 那会留下
    「归档副本 + 原文件」两份重复内容，反而更难清理。

    :return: 迁移后的路径；无需迁移或未能完成时返回 None
    """
    if not os.path.isfile(LEGACY_LOG_FILE):
        return None
    try:
        if os.path.getsize(LEGACY_LOG_FILE) == 0:
            os.remove(LEGACY_LOG_FILE)
            return None
    except OSError:
        pass

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        date_str = time.strftime(
            DATE_FORMAT, time.localtime(os.path.getmtime(LEGACY_LOG_FILE)))

        # 选一个不冲突的归档名，避免覆盖已有日志
        target = current_log_file(date_str)
        if os.path.exists(target):
            target = None
            for index in range(1, 100):
                suffix = "_legacy" if index == 1 else f"_legacy{index}"
                candidate = os.path.join(
                    LOG_DIR, f"{LOG_FILE_PREFIX}{date_str}{suffix}{LOG_FILE_EXT}")
                if not os.path.exists(candidate):
                    target = candidate
                    break
            if target is None:
                log("旧日志归档失败：同名归档文件过多，请先清理 logs 目录", "WARNING")
                return None

        os.replace(LEGACY_LOG_FILE, target)
        return target
    except OSError as e:
        log(f"旧日志文件无法归档（{e}）。若旧版程序仍在运行请先退出，下次启动会自动重试",
            "WARNING")
        return None
    except Exception as e:
        log(f"归档旧日志失败: {e}", "WARNING")
        return None


def apply_log_retention(retention_days=None):
    """启动时调用：迁移旧日志 → 应用保留天数 → 清理过期文件。

    :return: 说明文字
    """
    if retention_days is None:
        retention_days = DEFAULT_RETENTION_DAYS
        try:
            from config import load_config
            retention_days = int(
                load_config().get("log_retention_days", DEFAULT_RETENTION_DAYS))
        except Exception:
            pass
    retention_days = max(MIN_RETENTION_DAYS, min(int(retention_days), MAX_RETENTION_DAYS))

    if _daily_handler is not None:
        _daily_handler.retention_days = retention_days

    migrated = migrate_legacy_log()
    deleted, error = cleanup_old_logs(retention_days)

    parts = []
    if migrated:
        parts.append(f"旧日志已归档为 {os.path.basename(migrated)}")
    parts.append(f"日志保留 {retention_days} 天，已清理 {len(deleted)} 个过期文件")
    if error:
        parts.append(f"部分文件清理失败：{error}")
    return "；".join(parts)


class DailyFileHandler(logging.Handler):
    """按日期分文件的日志处理器。

    每次写入都检查日期，跨天自动切到新文件，不需要重启程序。
    """

    def __init__(self, retention_days=DEFAULT_RETENTION_DAYS):
        super().__init__()
        self.retention_days = retention_days
        self._date = None
        self._stream = None

    @property
    def path(self):
        return current_log_file(self._date) if self._date else None

    def _open_for(self, date_str):
        self._close_stream()
        os.makedirs(LOG_DIR, exist_ok=True)
        self._stream = open(current_log_file(date_str), "a", encoding="utf-8")
        self._date = date_str

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def release_file(self, path=None):
        """释放日志文件句柄。

        删除正在写入的日志文件前必须调用；下一次写入会自动重新打开。
        传入 path 时，只在该文件正是当前写入文件时才释放。

        注意：**不能叫 release**。logging.Handler 自己有 release()，那是用来
        释放 handler 锁的；一旦覆盖它，锁就只加不解，
        持有锁的线程一退出，其它线程写日志会永久阻塞。
        """
        if path and self.path and os.path.normcase(path) != os.path.normcase(self.path):
            return
        self._close_stream()

    def emit(self, record):
        try:
            date_str = time.strftime(DATE_FORMAT)
            if self._stream is None or self._date != date_str:
                self._open_for(date_str)
                # 跨天时顺手按保留天数清理一次
                try:
                    cleanup_old_logs(self.retention_days)
                except Exception:
                    pass
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            # 日志写失败不能再抛出去（窗口模式没有 stderr，抛出去只会更糟）
            pass

    def close(self):
        self._close_stream()
        super().close()


def setup_logger():
    """设置日志系统"""
    global _logger, _daily_handler
    if _logger is not None:
        return _logger

    _logger = logging.getLogger('AutoConnectLogger')
    _logger.setLevel(logging.INFO)

    if not _logger.handlers:
        formatter = _make_formatter()

        _daily_handler = DailyFileHandler(DEFAULT_RETENTION_DAYS)
        _daily_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        _logger.addHandler(_daily_handler)
        _logger.addHandler(console_handler)

    return _logger


def set_ui_handler(ui_handler):
    """设置UI日志处理器"""
    global _ui_log_handler, _logger
    if _logger is None:
        setup_logger()

    if _ui_log_handler:
        try:
            _logger.removeHandler(_ui_log_handler)
        except Exception:
            pass

    _ui_log_handler = ui_handler
    if _ui_log_handler:
        _ui_log_handler.setFormatter(_make_formatter())
        _ui_log_handler.setLevel(logging.INFO)
        _logger.addHandler(_ui_log_handler)


def log(message, level="INFO"):
    """记录日志"""
    if _logger is None:
        setup_logger()

    level = level.upper()
    if level == "INFO":
        _logger.info(message)
    elif level == "WARNING":
        _logger.warning(message)
    elif level == "ERROR":
        _logger.error(message)
    elif level == "DEBUG":
        _logger.debug(message)
    else:
        _logger.info(message)


def log_with_notification(message, level="INFO", notification_title="系统通知"):
    """
    记录日志并发送通知
    :param message: 日志消息
    :param level: 日志级别
    :param notification_title: 通知标题
    """
    # 记录日志
    log(message, level)

    # 发送通知（仅在严重级别时发送）
    if _notification_callback:
        try:
            _notification_callback(notification_title, message)
        except Exception as e:
            log(f"发送通知失败: {e}", "WARNING")


def set_notification_callback(callback_func):
    """设置通知回调函数"""
    global _notification_callback
    _notification_callback = callback_func


def get_logger():
    """获取日志记录器实例"""
    if _logger is None:
        setup_logger()
    return _logger


if __name__ == "__main__":
    setup_logger()
    print("日志目录:", LOG_DIR)
    print("今天的日志文件:", current_log_file())
    log("日志模块自检", "INFO")
    print("启动时清理:", apply_log_retention(DEFAULT_RETENTION_DAYS))
    print()
    print("--- 日志文件列表 ---")
    for item in list_log_files():
        print("  %s  %8d 字节  %s" % (item["date"], item["size"], item["name"]))
    print()
    print("--- 最近 3 天回显（每文件末尾 5 行）---")
    for path, lines in read_recent_logs(3, 5, 30):
        print(f"[{os.path.basename(path)}]")
        for line in lines:
            print("   ", line)
