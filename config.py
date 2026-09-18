# -*- coding: utf-8 -*-
import json
import os

import ubelt as ub

from crypto_utils import (
    encrypt_data,
    decrypt_data,
    get_key_fingerprint,
    get_unique_id_info,
)
from logger import (
    log,
    log_with_notification,
    DEFAULT_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
)

dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
CONFIG_FILE = os.path.join(dpath, "config.json")

# 日志路径不再写进配置：日志按天分文件放在 logger.LOG_DIR 下，
# 需要时用 logger.current_log_file() 动态获取（跨天会变）。

# 磁盘上配置的密钥指纹字段名
KEY_FP_FIELD = "key_fp"

# 检查间隔的合法范围（秒）与默认值
CHECK_INTERVAL_MIN = 10
CHECK_INTERVAL_MAX = 7200
DEFAULT_CHECK_INTERVAL = 300
DEFAULT_TEST_URL = "https://kimi.moonshot.cn"

# 写入前必须齐全的字段。缺任何一个都说明调用方传了不完整的 dict，
# 这种情况必须拒绝写入，而不是把用户的完整配置覆盖掉。
REQUIRED_FIELDS = ("check_interval", "test_url", "login_url")

# 明显不是真实站点的保留域名（RFC 2606/6761）。
# 用于兜住"配置被写进了测试桩数据"这类事故：真的出现会立刻纠正并告警。
RESERVED_HOST_SUFFIXES = (".test", ".invalid", ".example", ".localhost")


def _default_config():
    return {
        "username": "",
        "password": "",
        "check_interval": DEFAULT_CHECK_INTERVAL,
        "test_url": DEFAULT_TEST_URL,
        "login_url": "https://gw.buaa.edu.cn/",
        "log_retention_days": DEFAULT_RETENTION_DAYS,
        "chromedriver_path": "",
    }


def _extract_host(url):
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0].split("@")[-1].split(":")[0].lower()


def _is_reserved_host(url):
    host = _extract_host(url)
    if not host:
        return True
    return any(host == suffix.lstrip(".") or host.endswith(suffix)
               for suffix in RESERVED_HOST_SUFFIXES)


def sanitize_config(config):
    """校验并纠正明显非法的配置项，返回修正说明列表。

    这是"配置被外部写坏"的最后一道防线：即使磁盘上出现了不合理的值，
    也不会被原样拿去跑监控循环。
    """
    notes = []

    raw_interval = config.get("check_interval", DEFAULT_CHECK_INTERVAL)
    try:
        interval = int(raw_interval)
    except (TypeError, ValueError):
        interval = DEFAULT_CHECK_INTERVAL
    if not (CHECK_INTERVAL_MIN <= interval <= CHECK_INTERVAL_MAX):
        notes.append(
            f"检查间隔 {raw_interval!r} 超出允许范围 [{CHECK_INTERVAL_MIN}, "
            f"{CHECK_INTERVAL_MAX}]，已纠正为 {DEFAULT_CHECK_INTERVAL}")
        interval = DEFAULT_CHECK_INTERVAL
    config["check_interval"] = interval

    test_url = str(config.get("test_url") or "").strip()
    if not test_url:
        notes.append(f"测试网址为空，已纠正为 {DEFAULT_TEST_URL}")
        config["test_url"] = DEFAULT_TEST_URL
    elif _is_reserved_host(test_url):
        notes.append(
            f"测试网址 {test_url!r} 是保留域名，不可能是真实站点"
            f"（疑似被写入了测试数据），已纠正为 {DEFAULT_TEST_URL}")
        config["test_url"] = DEFAULT_TEST_URL

    if not str(config.get("login_url") or "").strip():
        notes.append("登录网址为空，已纠正为默认网关地址")
        config["login_url"] = _default_config()["login_url"]

    # 日志保留天数：缺失时补默认值（属于新字段，不算异常）；给了非法值才纠正
    if "log_retention_days" not in config:
        config["log_retention_days"] = DEFAULT_RETENTION_DAYS
    else:
        raw_days = config.get("log_retention_days")
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
        if not (MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS):
            notes.append(
                f"日志保留天数 {raw_days!r} 超出允许范围 "
                f"[{MIN_RETENTION_DAYS}, {MAX_RETENTION_DAYS}]，已纠正为 {DEFAULT_RETENTION_DAYS}")
            days = DEFAULT_RETENTION_DAYS
        config["log_retention_days"] = days

    return notes


def _read_raw_config():
    """读取磁盘上的原始配置（账号密码仍为密文）。失败返回 None。"""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_config():
    """加载配置文件，如果不存在则创建默认配置"""
    default_config = _default_config()

    if not os.path.exists(CONFIG_FILE):
        save_config(default_config)
        return default_config

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # 密钥指纹校验：密钥变化后不允许用错误密钥去解密，避免拿到乱码当账号密码
        disk_fp = config.get(KEY_FP_FIELD)
        current_fp = get_key_fingerprint()
        if disk_fp and disk_fp != current_fp:
            _, source = get_unique_id_info()
            log_with_notification(
                "配置文件中的密钥指纹与本机不一致，已停止加载账号密码（当前密钥来源:%s）。"
                "可能是硬件或系统标识发生变化，请重新填写账号密码后再保存" % source,
                "ERROR",
                "配置错误",
            )
            return default_config

        # 解密用户名和密码
        if config.get("username"):
            config["username"] = decrypt_data(config["username"])
        if config.get("password"):
            config["password"] = decrypt_data(config["password"])

        # 校验并纠正非法值（并把纠正结果明确报出来）
        for note in sanitize_config(config):
            log_with_notification(f"配置项异常：{note}", "WARNING", "配置警告")

        return config
    except Exception as e:
        log(f"加载配置失败: {e}", "ERROR")
        return default_config


def save_config(config):
    """保存配置到文件。

    返回 True 表示写入成功。

    两道保护：
    1. 校验字段完整性 —— 拒绝把"残缺的 dict"写进去（曾经有调用方拿着临时 dict
       把自己的测试数据整体覆盖到用户真实配置上）；
    2. 写临时文件再 os.replace —— 避免写到一半被打断留下半个 JSON。
    另外本函数不会修改传入的 config 对象（加密只作用于内部副本）。
    """
    try:
        data = dict(config)  # 浅拷贝即可：字段均为不可变值

        # 字段完整性校验
        missing = [field for field in REQUIRED_FIELDS if not data.get(field)]
        if missing:
            log_with_notification(
                f"拒绝写入配置：缺少必需字段 {missing}。"
                "这通常说明调用方传入了不完整的配置对象",
                "ERROR",
                "配置错误",
            )
            return False

        # 非法值纠正（同样作用于写入前的副本，不改变调用方的对象）
        for note in sanitize_config(data):
            log_with_notification(f"写入前已纠正配置项：{note}", "WARNING", "配置警告")

        # 密钥变化保护：磁盘上已有密文且指纹不匹配时，拒绝写入，避免覆盖原有凭据
        raw = _read_raw_config()
        current_fp = get_key_fingerprint()
        if raw is not None:
            disk_fp = raw.get(KEY_FP_FIELD)
            if disk_fp and disk_fp != current_fp:
                log_with_notification(
                    "检测到本机加密密钥已变化，为避免覆盖配置文件中原有的账号密码，已拒绝写入。"
                    "请重新填写账号密码后再保存",
                    "ERROR",
                    "配置错误",
                )
                return False

        # 空值保护：配置加载失败时界面上的账号密码是空的，
        # 此时点「保存配置」会把原有凭据直接抹掉。这里沿用磁盘上已有的密文，
        # 只提示、不覆盖（确实想清空请直接删除配置文件）。
        preserved = set()
        if raw is not None:
            for field in ("username", "password"):
                if not data.get(field) and raw.get(field):
                    data[field] = raw[field]
                    preserved.add(field)
                    log(f"传入的 {field} 为空，已保留配置文件中原有的值（未覆盖）", "WARNING")

        # 加密用户名和密码（已从磁盘沿用的密文不再二次加密）
        for field in ("username", "password"):
            if data.get(field) and field not in preserved:
                data[field] = encrypt_data(data[field])
        data[KEY_FP_FIELD] = current_fp

        # 原子写：先写临时文件，再整体替换，避免中途被打断留下损坏的配置
        tmp_path = CONFIG_FILE + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_FILE)
        return True
    except Exception as e:
        log(f"保存配置失败: {e}", "ERROR")
        return False


if __name__ == "__main__":
    cfg = load_config()
    print("配置文件路径:", CONFIG_FILE)
    print("当前密钥指纹:", get_key_fingerprint())
    print("密钥来源    :", get_unique_id_info()[1])
    for key, value in cfg.items():
        if key in ("username", "password"):
            text = value or ""
            shown = (text[:2] + "***" + text[-2:]) if len(text) > 4 else ("(空)" if not text else "***")
            print(f"  {key} = {shown} (长度 {len(text)})")
        else:
            print(f"  {key} = {value}")
    print("账号密码是否可读:", bool(cfg.get("username") and cfg.get("password")))
    print()
    print("--- sanitize_config 自检 ---")
    demo = {"check_interval": 1, "test_url": "https://x.test/"}
    for note in sanitize_config(demo):
        print("  纠正:", note)
    print("  结果:", demo)
