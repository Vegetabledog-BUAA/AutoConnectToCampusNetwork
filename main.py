#!/usr/bin/env python3
import sys
import os
import argparse
from logger import setup_logger, log, apply_log_retention, install_excepthook
from single_instance import (
    acquire as acquire_instance_lock,
    request_show_window,
    notify_already_running,
)

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 已有实例在运行时的退出码，便于脚本区分"启动失败"与"已在运行"
EXIT_ALREADY_RUNNING = 2


def guard_single_instance():
    """单实例保护。返回 True 表示可以继续启动，False 表示应立刻退出。

    有多份实例同时跑时，多个监控线程会争抢网络与配置，历史上表现为
    "停止监控很慢""配置改了不生效"这类难以归因的问题，因此必须拦住。

    拦下来之后不是直接甩个错误弹窗：先请求正在运行的实例把主界面显示出来，
    有回应就安静退出；没有回应（例如对方是 --auto 无界面模式）才提示用户。
    """
    ok, message = acquire_instance_lock()
    if ok:
        return True

    log(f"检测到已有实例在运行，本进程退出（{message}）", "WARNING")
    if request_show_window():
        log("已请求正在运行的实例打开主界面", "INFO")
    else:
        notify_already_running()
    return False

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
        from PyQt5.QtWidgets import QApplication
        from app_icon import apply_to_application
        import dialogs
        app = QApplication.instance() or QApplication(sys.argv)
        # 诊断弹窗也要用本程序的图标，否则看着像野窗口
        apply_to_application(app)
        dialogs.info(
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
    # 必须在建 QApplication 之前装：否则槽函数里的异常会让 PyQt5 直接 abort，
    # 进程静默消失且日志里没有任何线索
    install_excepthook()

    # 单实例保护：--doctor 只是诊断工具，允许在程序运行期间执行，因此不参与限制
    if not args.doctor and not guard_single_instance():
        sys.exit(EXIT_ALREADY_RUNNING)

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