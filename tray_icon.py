import sys
import os
import threading
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import QTimer, QObject, pyqtSignal, pyqtSlot, Qt

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from logger import log
from config import load_config
from network_checker import NetworkChecker

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
            log(f"显示 GUI 失败: {e}", "ERROR")

    def _on_window_destroyed(self, _obj=None):
        self.window = None

class TrayIconManager(QObject):
    exit_app_signal = pyqtSignal()

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
        self.setup_tray_icon()

    def setup_tray_icon(self):
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                log("系统托盘不可用", "ERROR")
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
            log(f"创建托盘图标失败: {e}", "ERROR")

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
        log("从托盘打开 GUI", "INFO")
        self.ui_starter.start_ui_signal.emit()

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
            chrome_version = (self.config.get('chrome_version') or "").strip()
            chromedriver_path = (self.config.get('chromedriver_path') or "").strip()
            chromedriver_version = (self.config.get('chromedriver_version') or "").strip()
            if not username or not password:
                return False, "用户名或密码未配置"
            if not chrome_version:
                import latest_chromedriver
                self.config['chrome_version'] = latest_chromedriver.chrome_info.get_version()
                config_update_flag = True
            if not chromedriver_path:
                import latest_chromedriver
                import ubelt as ub
                dpath = ub.ensure_app_cache_dir('latest_chromedriver')
                self.config['chromedriver_path'] = dpath
                config_update_flag = True
            if not chromedriver_version:
                self.config['chromedriver_version'] = latest_chromedriver.download_driver.get_version(self.config['chromedriver_path'])
                if self.config['chromedriver_version'] is None:
                    latest_chromedriver.safely_set_chromedriver_path()
                    self.config['chromedriver_version'] =  latest_chromedriver.download_driver.get_version(self.config['chromedriver_path'])
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
            log(f"启动监控被阻止：{msg}", "WARNING")
            self.show_notification("无法启动监控", msg, 4000)
            return
        try:
            self.network_checker = NetworkChecker(self.config)
            self.is_monitoring = True
            self.check_thread = threading.Thread(target=self.network_checker.start_checking, daemon=True)
            self.check_thread.start()
            self.update_status("运行中")
            self.monitor_action.setText("停止监控")
            log("托盘监控启动", "INFO")
            self.show_notification("网络监控", "监控已启动")
        except Exception as e:
            log(f"启动监控失败: {e}", "ERROR")

    def stop_monitoring(self):
        if not self.is_monitoring:
            return
        try:
            self.is_monitoring = False
            if self.network_checker:
                self.network_checker.stop_checking()
            if self.check_thread and self.check_thread.is_alive():
                self.check_thread.join(timeout=5)
            self.update_status("已停止")
            self.monitor_action.setText("开始监控")
            log("托盘监控停止", "INFO")
            self.show_notification("网络监控", "监控已停止")
        except Exception as e:
            log(f"停止监控失败: {e}", "ERROR")

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
            log("配置已热更新", "INFO")
            self.show_notification("配置更新", "新配置已应用", 2500)
        except Exception as e:
            log(f"配置热更新失败: {e}", "ERROR")

    def show_notification(self, title, message, duration=3000):
        try:
            if self.tray_icon:
                self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information, duration)
        except Exception as e:
            log(f"通知显示失败: {e}", "WARNING")

    def exit_app(self):
        log("托盘退出请求", "INFO")
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

        # 仅在配置完整时才自动启动监控
        ok, msg = tray_manager.has_required_config()
        if ok:
            QTimer.singleShot(1000, tray_manager.start_monitoring)
        else:
            tray_manager.update_status("已停止")
            log(f"未自动启动监控：{msg}", "WARNING")
            tray_manager.show_notification("提示", f"未自动启动监控：{msg}。请打开主界面完成配置。", 5000)

        tray_manager.show_notification("网络检查系统", "程序已在后台运行（托盘）", 4000)
        log("托盘模式启动完成", "INFO")
        return app, tray_manager
    except Exception as e:
        log(f"启动托盘模式失败: {e}", "ERROR")
        return None, None

__all__ = ["TrayIconManager", "start_tray_only", "tray_manager"]