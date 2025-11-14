#!/usr/bin/env python3
import sys
import os
import argparse
from logger import setup_logger, log

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def main():

    parser = argparse.ArgumentParser(description='网络自动检查与登录系统')
    parser.add_argument('--gui', action='store_true', help='启动GUI界面')
    parser.add_argument('--auto', action='store_true', help='命令行自动监控')
    parser.add_argument('--tray', action='store_true', help='仅托盘模式')
    args = parser.parse_args()

    setup_logger()
    log("程序启动", "INFO")

    if args.gui:
        # ...existing code...
        from ui import start_ui
        sys.exit(start_ui())

    if args.auto:
        # ...existing code...
        from config import load_config
        from network_checker import NetworkChecker
        cfg = load_config()
        nc = NetworkChecker(cfg)
        nc.start_checking()
        return

    # 托盘模式（显式或默认）
    log("启动托盘模式", "INFO")
    from tray_icon import start_tray_only
    app, tray_manager = start_tray_only()
    if app and tray_manager:
        sys.exit(app.exec_())
    else:
        log("托盘模式启动失败，回退 GUI", "WARNING")
        from ui import start_ui
        sys.exit(start_ui())

if __name__ == "__main__":
    main()