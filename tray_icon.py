import sys
import os
import time
import threading
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import QTimer, QObject, pyqtSignal, pyqtSlot, Qt

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from logger import log_with_notification, set_notification_callback, log
from config import load_config
from network_checker import NetworkChecker
from single_instance import start_show_window_listener
from updater import check_for_update, STARTUP_DELAY

tray_manager = None

def _resource_path(name: str) -> str:
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

class UIStarter(QObject):
    start_ui_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.start_ui_signal.connect(self.start_ui, type=Qt.QueuedConnection)
        self.window = None

    @pyqtSlot()
    def start_ui(self):
        try:
            if self.window is None:
                from ui import MainWindow
                self.window = MainWindow()
                self.window.destroyed.connect(self._on_window_destroyed)
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
        except Exception as e:
            log_with_notification(f"显示 GUI 失败: {e}", "ERROR", "GUI错误")

    def _on_window_destroyed(self, _obj=None):
        self.window = None

class TrayIconManager(QObject):
    exit_app_signal = pyqtSignal()
    # 通知必须回到 GUI 线程再弹：showMessage 是 Qt 对象操作，
    # 从监控线程直接调用会造成跨线程访问（实测表现就是双击托盘卡死打不开界面）。
    notification_signal = pyqtSignal(str, str, int)
    # 更新检查的结果同样只能投递信号，不能在工作线程里碰托盘对象
    update_available_signal = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.tray_icon: QSystemTrayIcon = None
        self.status_action: QAction = None
        self.monitor_action: QAction = None
        self.is_monitoring = False
        self.network_checker = None
        self.check_thread: threading.Thread = None
        self.ui_starter = UIStarter()
        self.config = load_config()
        # 相同通知的最小间隔（秒），避免异常时刷屏弹气泡
        self._last_notification = {}
        self._notification_min_gap = 30

        self.notification_signal.connect(self._show_notification, type=Qt.QueuedConnection)
        self.update_available_signal.connect(
            self._on_update_available, type=Qt.QueuedConnection)

        # 设置全局通知回调
        set_notification_callback(self.show_notification)

        self.setup_tray_icon()

    def setup_tray_icon(self):
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                log_with_notification("系统托盘不可用", "ERROR", "托盘错误")
                return
            self.tray_icon = QSystemTrayIcon()
            icon_file = _resource_path('icon.ico')
            if os.path.exists(icon_file):
                self.tray_icon.setIcon(QIcon(icon_file))
            self.create_context_menu()
            self.tray_icon.activated.connect(self.on_tray_icon_activated)
            self.tray_icon.show()
            log("托盘图标已创建", "INFO")
        except Exception as e:
            log_with_notification(f"创建托盘图标失败: {e}", "ERROR", "托盘错误")

    def create_context_menu(self):
        menu = QMenu()
        status_action = QAction("状态: 已停止", menu)
        status_action.setEnabled(False)
        menu.addAction(status_action)
        self.status_action = status_action
        menu.addSeparator()
        show_gui_action = QAction("打开主界面", menu)
        show_gui_action.triggered.connect(self.show_gui)
        menu.addAction(show_gui_action)
        self.monitor_action = QAction("开始监控", menu)
        self.monitor_action.triggered.connect(self.toggle_monitoring)
        menu.addAction(self.monitor_action)
        menu.addSeparator()
        exit_action = QAction("退出", menu)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(exit_action)
        self.tray_icon.setContextMenu(menu)

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_gui()

    def show_gui(self):
        log_with_notification("从托盘打开 GUI", "INFO", "GUI操作")
        self.ui_starter.start_ui_signal.emit()

    def request_show_gui(self):
        """响应"新实例请求打开主界面"（由单实例监听线程调用）。

        监听线程不是 GUI 线程，所以这里只投递信号，真正的窗口操作在槽里执行。
        """
        try:
            self.ui_starter.start_ui_signal.emit()
        except Exception as e:
            log(f"投递显示主界面请求失败: {e}", "WARNING")

    # ---------------- 更新检查 ----------------

    def check_update_in_background(self):
        """启动后自动检查更新（节流由 updater 内部负责，24 小时最多一次）。

        只有托盘没开界面时才会走到这里，所以用气泡提示而不是弹对话框；
        详细说明留给主界面的「检查更新」按钮。
        """
        def _run():
            info = None
            try:
                info = check_for_update(force=False)
            except Exception as e:
                # updater 内部已兜底，这里是最后一道保险
                log(f"更新检查异常（已忽略）: {e}", "WARNING")
            if info is not None:
                # 工作线程里只投递信号，真正的托盘调用回 GUI 线程
                self.update_available_signal.emit(info.version, info.download_url)

        threading.Thread(target=_run, name="UpdateCheck", daemon=True).start()

    @pyqtSlot(str, str)
    def _on_update_available(self, version, _url):
        """收到"有新版本"（运行在 GUI 线程）"""
        log(f"发现新版本 {version}，打开主界面可查看详情", "INFO")
        self.show_notification(
            "有可用更新",
            f"发现新版本 {version}，右键托盘图标打开主界面可查看详情", 6000)

    def toggle_monitoring(self):
        if self.is_monitoring:
            self.stop_monitoring()
        else:
            self.start_monitoring()
    
    def has_required_config(self):
        """检查必需配置：用户名、密码、chromedriver_path"""
        try:
            config_update_flag = False
            username = (self.config.get('username') or "").strip()
            password = (self.config.get('password') or "").strip()
            chromedriver_path = (self.config.get('chromedriver_path') or "").strip()
            if not username or not password:
                return False, "用户名或密码未配置"
            if not chromedriver_path:
                import ubelt as ub
                dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
                self.config['chromedriver_path'] = dpath
                config_update_flag = True
            if config_update_flag:
                from config import save_config
                save_config(self.config)
            return True, ""
        except Exception as e:
            return False, f"检查配置失败: {e}"

    def start_monitoring(self):
        if self.is_monitoring:
            return
        ok, msg = self.has_required_config()
        if not ok:
            log_with_notification(f"启动监控被阻止：{msg}", "ERROR", "配置错误")
            return
        try:
            # 不再传递通知回调，因为现在使用全局回调
            self.network_checker = NetworkChecker(self.config)
            self.is_monitoring = True
            self.check_thread = threading.Thread(target=self.network_checker.start_checking, daemon=True)
            self.check_thread.start()
            self.update_status("运行中")
            self.monitor_action.setText("停止监控")
            log("托盘监控启动", "INFO")
        except Exception as e:
            log_with_notification(f"启动监控失败: {e}", "ERROR", "监控错误")

    def stop_monitoring(self):
        if not self.is_monitoring:
            return
        try:
            self.is_monitoring = False
            if self.network_checker:
                # 只发停止信号：监控循环用的是可中断等待，正常几十毫秒内就退出，
                # 不会再像以前那样必须等满整个检查间隔（最长 300 秒）。
                self.network_checker.stop_checking()
            if self.check_thread and self.check_thread.is_alive():
                # 本方法运行在 Qt 主线程上，join 超时会让界面出现"点了没反应"的
                # 卡顿感（旧实现固定等 5 秒），因此这里只做一次很短的确认。
                self.check_thread.join(timeout=1.5)
                if self.check_thread.is_alive():
                    log("监控线程正在收尾（可能卡在一次登录尝试中），完成后自动退出", "INFO")
            self.update_status("已停止")
            self.monitor_action.setText("开始监控")
            log_with_notification("托盘监控停止", "INFO", "监控停止")
        except Exception as e:
            log_with_notification(f"停止监控失败: {e}", "ERROR", "监控错误")

    def update_status(self, status):
        if self.status_action:
            self.status_action.setText(f"状态: {status}")
        if self.tray_icon:
            self.tray_icon.setToolTip(f"网络自动检查与登录系统 ({status})")
    
    def reload_config(self, new_config=None):
        """从外部（GUI）热更新配置"""
        try:
            if new_config is None:
                from config import load_config
                self.config = load_config()
            else:
                self.config = dict(new_config)
            if self.network_checker:
                # 直接替换配置对象，下一轮循环生效
                self.network_checker.config = self.config
            log_with_notification("配置已热更新", "INFO", "配置通知")
        except Exception as e:
            log_with_notification(f"配置热更新失败: {e}", "ERROR", "配置错误")

    def show_notification(self, title, message, duration=3000):
        """线程安全的通知入口。

        监控线程会调用这里（log_with_notification 的全局回调），
        因此只投递信号，真正的 Qt 调用放到 GUI 线程的 _show_notification 里执行。
        """
        try:
            self.notification_signal.emit(str(title), str(message), int(duration))
        except Exception as e:
            log(f"通知投递失败: {e}", "WARNING")

    @pyqtSlot(str, str, int)
    def _show_notification(self, title, message, duration):
        """在 GUI 线程里真正弹出托盘气泡（带同内容节流）"""
        try:
            if not self.tray_icon:
                return
            now = time.time()
            key = (title, message)
            last = self._last_notification.get(key)
            if last is not None and now - last < self._notification_min_gap:
                return
            self._last_notification[key] = now
            # 只保留最近的若干条，避免字典无限增长
            if len(self._last_notification) > 50:
                for old_key in sorted(self._last_notification,
                                      key=self._last_notification.get)[:25]:
                    self._last_notification.pop(old_key, None)
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information, duration)
        except Exception as e:
            log(f"通知显示失败: {e}", "WARNING")

    def exit_app(self):
        reply = QMessageBox.question(
            None, '确认退出', '确定要退出系统吗？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.tray_icon.hide()
            if self.is_monitoring:
                self.stop_monitoring()
            self.exit_app_signal.emit()

def start_tray_only():
    global tray_manager
    try:
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        tray_manager = TrayIconManager()
        tray_manager.exit_app_signal.connect(app.quit)

        # 单实例保护：接收"新实例请求打开主界面"的事件。
        # 回调由监听线程触发，request_show_gui 内只 emit 信号，是线程安全的。
        start_show_window_listener(tray_manager.request_show_gui)

        # 启动后延迟检查更新：有新版只发一条气泡，不打扰正在做的事
        QTimer.singleShot(int(STARTUP_DELAY * 1000),
                          tray_manager.check_update_in_background)

        # 仅在配置完整时才自动启动监控
        ok, msg = tray_manager.has_required_config()
        if ok:
            QTimer.singleShot(1000, tray_manager.start_monitoring)
        else:
            tray_manager.update_status("已停止")
            log_with_notification(f"未自动启动监控：{msg}", "WARNING", "启动警告")

        log("托盘模式启动完成", "INFO")
        return app, tray_manager
    except Exception as e:
        log_with_notification(f"启动托盘模式失败: {e}", "ERROR", "启动错误")
        return None, None

__all__ = ["TrayIconManager", "start_tray_only", "tray_manager"]
