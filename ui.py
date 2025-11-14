import sys
import os
import re
import logging
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QSpinBox, 
                             QCheckBox, QPushButton, QTextEdit, QGroupBox,
                             QMessageBox, QTabWidget, QFileDialog)
from PyQt5.QtCore import QTimer, QThread, pyqtSignal, QObject
from PyQt5.QtGui import QTextCursor

# 添加当前目录到路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import load_config, save_config
from network_checker import NetworkChecker
from logger import setup_logger, log, set_ui_handler
from auto_start import setup_autostart, check_autostart_status

class UIHandler(QObject, logging.Handler):
    """自定义日志处理器，用于将日志发送到UI"""
    log_signal = pyqtSignal(str, str)
    
    def __init__(self):
        # 先调用QObject的__init__，再调用logging.Handler的__init__
        QObject.__init__(self)
        logging.Handler.__init__(self)
        self.setLevel(logging.INFO)
    
    def emit(self, record):
        """发送日志记录"""
        try:
            msg = self.format(record)
            level = record.levelname
            self.log_signal.emit(msg, level)
        except Exception:
            pass

class CheckThread(QThread):
    """网络检查线程"""
    status_signal = pyqtSignal(str, str)
    
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.checker = None
        self.is_running = False
    
    def run(self):
        self.is_running = True
        self.checker = NetworkChecker(self.config)
        
        try:
            self.checker.start_checking()
        except Exception as e:
            self.status_signal.emit(f"监控线程错误: {e}", "ERROR")
        finally:
            self.is_running = False
    
    def stop(self):
        self.is_running = False
        if self.checker:
            self.checker.stop_checking()
        self.quit()
        self.wait(5000)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.check_thread = None
        self.config = load_config()
        self.ui_handler = None
        self.init_ui()
        self.load_config_values()
        self.setup_ui_logging()
        self.sync_monitoring_status()
        self.status_sync_timer = QTimer(self)
        self.status_sync_timer.timeout.connect(self.sync_monitoring_status)
        self.status_sync_timer.start(1000)  # 每1秒同步一次
        
    def init_ui(self):
        """初始化用户界面"""
        self.setWindowTitle("网络自动检查与登录系统")
        self.setGeometry(100, 100, 800, 600)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        # 创建标签页
        tabs = QTabWidget()
        layout.addWidget(tabs)
        
        # 配置标签页
        config_tab = QWidget()
        config_layout = QVBoxLayout(config_tab)
        tabs.addTab(config_tab, "配置")
        
        # 登录配置组
        login_group = QGroupBox("登录配置")
        login_layout = QVBoxLayout(login_group)
        
        # 用户名
        user_layout = QHBoxLayout()
        user_layout.addWidget(QLabel("用户名:"))
        self.username_input = QLineEdit()
        user_layout.addWidget(self.username_input)
        login_layout.addLayout(user_layout)
        
        # 密码
        pwd_layout = QHBoxLayout()
        pwd_layout.addWidget(QLabel("密码:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        pwd_layout.addWidget(self.password_input)
        login_layout.addLayout(pwd_layout)
        
        # 登录网址
        login_url_layout = QHBoxLayout()
        login_url_layout.addWidget(QLabel("登录网址:"))
        self.login_url_input = QLineEdit()
        login_url_layout.addWidget(self.login_url_input)
        login_layout.addLayout(login_url_layout)
        
        # 检查间隔
        interval_layout = QHBoxLayout()
        interval_layout.addWidget(QLabel("检查间隔(秒):"))
        self.interval_input = QSpinBox()
        self.interval_input.setRange(10, 3600)
        self.interval_input.setSuffix(" 秒")
        interval_layout.addWidget(self.interval_input)
        login_layout.addLayout(interval_layout)
        
        # 测试网址
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("测试网址:"))
        self.test_url_input = QLineEdit()
        url_layout.addWidget(self.test_url_input)
        login_layout.addLayout(url_layout)
        
        config_layout.addWidget(login_group)
        
        # 系统配置组
        system_group = QGroupBox("系统配置")
        system_layout = QVBoxLayout(system_group)
        
        # 开机自启动
        self.autostart_checkbox = QCheckBox("开机自动启动")
        system_layout.addWidget(self.autostart_checkbox)
        
        config_layout.addWidget(system_group)
        
        # 按钮组
        button_layout = QHBoxLayout()
        
        self.save_btn = QPushButton("保存配置")
        self.save_btn.clicked.connect(self.save_config)
        button_layout.addWidget(self.save_btn)
        
        self.start_btn = QPushButton("开始监控")
        self.start_btn.clicked.connect(self.start_monitoring)
        button_layout.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("停止监控")
        self.stop_btn.clicked.connect(self.stop_monitoring)
        self.stop_btn.setEnabled(False)
        button_layout.addWidget(self.stop_btn)
        
        config_layout.addLayout(button_layout)
        
        # 日志标签页
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        tabs.addTab(log_tab, "日志")
        
        # 日志控制按钮
        log_control_layout = QHBoxLayout()
        self.clear_log_btn = QPushButton("清空日志")
        self.clear_log_btn.clicked.connect(self.clear_log_display)
        log_control_layout.addWidget(self.clear_log_btn)
        
        self.pause_log_btn = QPushButton("暂停滚动")
        self.pause_log_btn.setCheckable(True)
        self.pause_log_btn.clicked.connect(self.toggle_log_scroll)
        log_control_layout.addWidget(self.pause_log_btn)
        
        log_control_layout.addStretch()
        log_layout.addLayout(log_control_layout)
        
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        log_layout.addWidget(self.log_display)
        
        # 状态栏
        self.statusBar().showMessage("就绪")
        
        # 自动滚动标志
        self.auto_scroll = True
    

    
    def setup_ui_logging(self):
        """设置UI日志处理"""
        self.ui_handler = UIHandler()
        self.ui_handler.log_signal.connect(self.append_log)
        set_ui_handler(self.ui_handler)
        self.load_history_logs()
        log("GUI界面初始化完成", "INFO")
    
    def load_config_values(self):
        """加载配置值到界面"""
        self.username_input.setText(self.config.get('username', ''))
        self.password_input.setText(self.config.get('password', ''))
        self.login_url_input.setText(self.config.get('login_url', 'https://gw.buaa.edu.cn/'))
        self.interval_input.setValue(self.config.get('check_interval', 300))
        self.test_url_input.setText(self.config.get('test_url', 'https://kimi.moonshot.cn'))

        # 检查自启动状态
        self.autostart_checkbox.setChecked(check_autostart_status())
    
    def save_config(self, is_start_monitoring=False):
        """保存配置"""
        self.config['username'] = self.username_input.text()
        self.config['password'] = self.password_input.text()
        self.config['login_url'] = self.login_url_input.text()
        self.config['check_interval'] = self.interval_input.value()
        self.config['test_url'] = self.test_url_input.text()


        save_config(self.config)

        # 设置开机自启动
        success, message = setup_autostart(self.autostart_checkbox.isChecked())

        # 将新配置应用到正在运行的托盘监控
        try:
            from tray_icon import tray_manager
            if tray_manager:
                tray_manager.reload_config(self.config)
        except Exception:
            pass

        if success:
            log(f"保存配置并设置自启动成功: {message}", "INFO")
            if is_start_monitoring == False:
                QMessageBox.information(self, "成功", f"配置已保存。{message}")
        else:
            log(f"保存配置完成，但自启动设置失败: {message}", "WARNING")
            QMessageBox.warning(self, "提示", f"配置已保存，但自启动设置失败：{message}")
    
    def _validate_required_before_start(self):
        """校验启动必需项：用户名、密码、chromedriver_path"""
        username = (self.username_input.text() or "").strip()
        password = (self.password_input.text() or "").strip()
        if not username or not password:
            QMessageBox.warning(self, "缺少配置", "请先填写用户名与密码。")
            return False
        return True
    
    def start_monitoring(self):
        """开始监控"""
        # 先保存配置
        # self.save_config()
        
        # # 检查必要配置
        # if not self.config['username'] or not self.config['password']:
        #     QMessageBox.warning(self, "警告", "请先配置用户名和密码！")
        #     return
        
        # # 创建并启动检查线程
        # self.check_thread = CheckThread(self.config)
        # self.check_thread.status_signal.connect(self.append_log)
        # self.check_thread.start()
        
        # self.start_btn.setEnabled(False)
        # self.stop_btn.setEnabled(True)
        # self.statusBar().showMessage("监控运行中...")
        # log("GUI监控启动", "INFO")
        # 启动前校验
        if not self._validate_required_before_start():
            log("必填项缺失，阻止启动监控", "WARNING")
            return

        # 启动前保存配置，确保托盘读取到最新配置
        self.save_config(is_start_monitoring = True)
        
        from tray_icon import tray_manager
        if tray_manager:
            tray_manager.start_monitoring()
    
    def stop_monitoring(self):
        """停止监控"""
        # if self.check_thread:
        #     self.check_thread.stop()
        
        # self.start_btn.setEnabled(True)
        # self.stop_btn.setEnabled(False)
        # self.statusBar().showMessage("监控已停止")
        # log("GUI监控停止", "INFO")
        from tray_icon import tray_manager
        if tray_manager:
            tray_manager.stop_monitoring()
            
    def setup_ui_logging(self):
        self.ui_handler = UIHandler()
        self.ui_handler.log_signal.connect(self.append_log)
        set_ui_handler(self.ui_handler)

        # ✅ 加载历史日志
        self.load_history_logs()
        log("GUI界面初始化完成", "INFO")
    
    def load_history_logs(self):
        log_file = self.config['log_file_path'] if 'log_file_path' in self.config else f'network_checker.log'
        if os.path.exists(log_file):
            color_map = {
                "ERROR": "red",
                "WARNING": "orange", 
                "INFO": "black",
                "DEBUG": "gray",
                "CRITICAL": "darkred"
            }
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    level = re.search(r'\] (\w+):', line)
                    if level:
                        level_name = level.group(1)
                        color = color_map.get(level_name, "black")
                    else:
                        color = "black"
                    html_message = f'<font color="{color}">{line.strip()}</font>'
                    self.log_display.append(html_message)

    def append_log(self, message, level):
        """追加日志到显示框"""
        try:
            color_map = {
                "ERROR": "red",
                "WARNING": "orange", 
                "INFO": "black",
                "DEBUG": "gray",
                "CRITICAL": "darkred"
            }
            
            color = color_map.get(level, "black")
            
            # 使用HTML格式显示带颜色的日志
            html_message = f'<font color="{color}">{message}</font>'
            
            # 保存当前滚动位置
            scrollbar = self.log_display.verticalScrollBar()
            at_bottom = scrollbar.value() == scrollbar.maximum()
            
            # 追加日志
            self.log_display.append(html_message)
            
            # 如果之前就在底部或者启用了自动滚动，则滚动到底部
            if self.auto_scroll and at_bottom:
                self.log_display.moveCursor(QTextCursor.End)
                
        except Exception as e:
            print(f"日志显示错误: {e}")
    
    def clear_log_display(self):
        reply = QMessageBox.question(
            self, '确认清空', '确定要清空所有日志显示和日志文件吗？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        self.log_display.clear()
        log_file = self.config['log_file_path'] if 'log_file_path' in self.config else f'network_checker.log'
        try:
            if os.path.exists(log_file):
                open(log_file, 'w', encoding='utf-8').close()
                log("日志文件已清空", "INFO")
        except Exception as e:
            log(f"清空日志文件失败: {e}", "ERROR")
    
    def toggle_log_scroll(self, checked):
        """切换日志自动滚动"""
        self.auto_scroll = not checked
        if checked:
            self.pause_log_btn.setText("继续滚动")
            log("日志自动滚动已暂停", "INFO")
        else:
            self.pause_log_btn.setText("暂停滚动")
            log("日志自动滚动已启用", "INFO")
    
    def closeEvent(self, event):
        # """关闭事件处理"""
        # if self.check_thread and self.check_thread.isRunning():
        #     reply = QMessageBox.question(
        #         self, '确认退出', 
        #         '监控正在运行，确定要退出吗？',
        #         QMessageBox.Yes | QMessageBox.No,
        #         QMessageBox.No
        #     )
            
        #     if reply == QMessageBox.Yes:
        #         self.stop_monitoring()
        #         # 移除UI日志处理器
        #         set_ui_handler(None)
        #         event.accept()
        #     else:
        #         event.ignore()
        # else:
        #     # 移除UI日志处理器
        #     set_ui_handler(None)
        #     event.accept()
        """关闭事件：仅隐藏窗口，不停止监控"""
        event.ignore()
        self.hide()
        log("GUI隐藏到托盘", "INFO")
    def sync_monitoring_status(self):
        """同步托盘监控状态到GUI"""
        from tray_icon import tray_manager
        if tray_manager and tray_manager.is_monitoring:
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.statusBar().showMessage("监控运行中...")
        else:
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.statusBar().showMessage("监控已停止")

def start_ui():
    """启动UI界面 - 独立运行"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    
    window = MainWindow()
    window.show()
    
    try:
        return app.exec_()
    except Exception as e:
        print(f"UI运行错误: {e}")
        return 1