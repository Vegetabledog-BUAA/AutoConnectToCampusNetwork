#!/usr/bin/env python3
import sys
import os
import argparse
from logger import setup_logger, log, apply_log_retention

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def run_doctor():
    """收集环境诊断报告：写入 doctor.txt + 日志，弹窗告知文件位置。

    用于在别的机器上定位"驱动下不下来 / 版本对不上"这类问题。
    """
    import ubelt as ub
    from chromedriver_manager import collect_diagnostics
    from logger import LOG_DIR, list_log_files, read_recent_logs, RECENT_LOG_DAYS

    dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
    report_path = os.path.join(dpath, 'doctor.txt')

    lines = collect_diagnostics()
    lines.append("")
    lines.append(f"日志目录: {LOG_DIR}")

    # 日志已改为按天分文件存放，这里同样按天读取，不再拼旧的单文件路径
    try:
        files = list_log_files()
        lines.append(f"日志文件: {len(files)} 个（"
                     + (", ".join(f"{os.path.basename(i['path'])}({i['size']}B)"
                                  for i in files) if files else "无")
                     + "）")
    except Exception as e:
        lines.append(f"(列出日志文件失败: {e})")

    lines.append("")
    lines.append(f"=== 最近 {RECENT_LOG_DAYS} 天日志（每个文件尾 40 行）===")
    try:
        bundles = read_recent_logs(RECENT_LOG_DAYS, 40)
        if not bundles:
            lines.append("(暂无日志)")
        for path, tail in bundles:
            lines.append(f"----- {os.path.basename(path)} -----")
            lines.extend(line.rstrip() for line in tail)
    except Exception as e:
        lines.append(f"(读取日志失败: {e})")

    text = "\n".join(lines)
    try:
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(text + "\n")
        log(f"诊断报告已写入: {report_path}", "INFO")
    except Exception as e:
        log(f"写入诊断报告失败: {e}", "ERROR")

    for line in lines:
        log(f"[诊断] {line}", "INFO")

    try:
        from PyQt5.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information(
            None, "环境诊断完成",
            f"诊断报告已保存到：\n{report_path}\n\n同样的内容也写进了日志文件。")
    except Exception:
        pass
    return 0


def main():

    parser = argparse.ArgumentParser(description='网络自动检查与登录系统')
    parser.add_argument('--gui', action='store_true', help='启动GUI界面')
    parser.add_argument('--auto', action='store_true', help='命令行自动监控')
    parser.add_argument('--tray', action='store_true', help='仅托盘模式')
    parser.add_argument('--doctor', action='store_true',
                        help='收集环境诊断报告（排查 Chrome / ChromeDriver 问题）')
    args = parser.parse_args()

    setup_logger()
    log("程序启动", "INFO")

    # 启动时应用日志保留策略：归档旧版单文件日志 + 清理超过保留天数的日志
    try:
        log(apply_log_retention(), "INFO")
    except Exception as e:
        log(f"应用日志保留策略失败: {e}", "WARNING")

    if args.doctor:
        sys.exit(run_doctor())

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