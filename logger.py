import os
import logging
import ubelt as ub

dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
CONFIG_FILE = os.path.join(dpath, "config.json")
LOG_FILE = os.path.join(dpath, 'auto_connect.log')

# 全局日志记录器
_logger = None
_ui_log_handler = None

# 全局通知回调函数
_notification_callback = None

def setup_logger():
    """设置日志系统"""
    global _logger
    if _logger is not None:
        return _logger
    

    _logger = logging.getLogger('AutoConnectLogger')
    _logger.setLevel(logging.INFO)

    if not _logger.handlers:
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        _logger.addHandler(file_handler)
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
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        _ui_log_handler.setFormatter(formatter)
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