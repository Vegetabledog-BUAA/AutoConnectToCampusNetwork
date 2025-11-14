import os
import logging
import ubelt as ub

dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
CONFIG_FILE = os.path.join(dpath, "config.json")
LOG_FILE = os.path.join(dpath, 'auto_connect.log')

# 全局日志记录器
_logger = None
_ui_log_handler = None

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

def get_logger():
    """获取日志记录器实例"""
    if _logger is None:
        setup_logger()
    return _logger