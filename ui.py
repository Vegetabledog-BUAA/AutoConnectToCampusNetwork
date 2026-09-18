import sys
import os
import re
import logging
from html import escape
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QLineEdit, QSpinBox,
    QCheckBox, QPushButton, QTextEdit, QGroupBox,
    QMessageBox, QTabWidget, QFileDialog,
    QListWidget, QListWidgetItem, QSplitter, QSizePolicy
)
from PyQt5.QtCore import QTimer, QThread, pyqtSignal, QObject, Qt
from PyQt5.QtGui import QTextCursor

# 添加当前目录到路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import load_config, save_config
from network_checker import NetworkChecker
from logger import (
    setup_logger,
    log,
    set_ui_handler,
    current_log_file,
    list_log_files,
    read_log_tail,
    read_recent_logs,
    delete_log_file,
    apply_log_retention,
    LOG_DIR,
    RECENT_LOG_DAYS,
    PER_FILE_TAIL_LINES,
    DEFAULT_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
)
from auto_start import (
    setup_autostart,
    check_autostart_status,
    can_enable_autostart,
)

# 日志级别与显示颜色的映射（加载历史日志与实时追加共用）
LOG_COLORS = {
    "ERROR": "red",
    "WARNING": "orange",
    "INFO": "black",
    "DEBUG": "gray",
    "CRITICAL": "darkred",
}
# 单个日志文件最多回显多少行（行数上限统一由 logger 提供，避免两边不一致）。
# 旧实现会把整个日志文件逐行 append 进 QTextEdit，日志长到几百 KB 时
# 界面要卡好几秒（每行一次带 HTML 的控件追加）。
LOG_TAIL_LINES = PER_FILE_TAIL_LINES

# 日志页顶部那行"当前显示文件"小字的可用像素宽度（超出部分做省略）
LOG_PATH_LABEL_WIDTH = 300

# 日志页左侧三个按钮的紧凑样式。
# 默认样式下 QPushButton 的最小宽度约 75px，三个按钮就把左列顶到 264px 下不去，
# 挤掉了右侧日志展示区的宽度 —— 而这些按钮只是辅助操作，不需要那么宽。
COMPACT_BUTTON_STYLE = "QPushButton { min-width: 0px; padding: 2px 8px; }"


class UIHandler(QObject, logging.Handler):
    """自定义日志处理器，用于将日志发送到UI"""
    log_signal = pyqtSignal(str, str)

    def __init__(self):
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
    # 日志页左右宽度比例（左侧文件管理 : 右侧日志内容）。
    # 右侧是主要信息展示区，所以尽量少给左侧；22% 是"日期 + 文件大小"还能完整显示的下限。
    LOG_SPLIT_RATIO = (22, 78)

    def __init__(self):
        super().__init__()
        self.check_thread = None
        self.config = load_config()
        self.ui_handler = None
        self.auto_scroll = True
        self._log_splitter_sized = False

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
        # 日志是主要查看对象，窗口默认给得宽一些，日志区才有足够高度
        self.setGeometry(100, 100, 920, 700)
        self.setMinimumSize(720, 520)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # 创建标签页
        tabs = QTabWidget()
        self.tabs = tabs
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
        self.autostart_checkbox.stateChanged.connect(self.on_autostart_changed)
        system_layout.addWidget(self.autostart_checkbox)

        config_layout.addWidget(system_group)

        # 日志配置组
        log_group = QGroupBox("日志")
        log_group_layout = QVBoxLayout(log_group)

        retention_layout = QHBoxLayout()
        retention_layout.addWidget(QLabel("日志保留天数:"))
        self.retention_input = QSpinBox()
        self.retention_input.setRange(MIN_RETENTION_DAYS, MAX_RETENTION_DAYS)
        self.retention_input.setSuffix(" 天")
        self.retention_input.setToolTip("启动时按此天数清理过期日志文件")
        retention_layout.addWidget(self.retention_input)
        log_group_layout.addLayout(retention_layout)

        log_hint = QLabel(
            f"按天生成日志文件，启动时删除超过保留天数的旧文件；"
            f"日志页默认回显最近 {RECENT_LOG_DAYS} 天")
        log_hint.setStyleSheet("color: gray;")
        log_hint.setWordWrap(True)
        log_group_layout.addWidget(log_hint)

        config_layout.addWidget(log_group)

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

        # 日志标签页：左侧日志文件管理，右侧日志内容
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        # 日志页以内容展示为主：边距收紧，把这部分空间全部让给右侧日志区
        log_layout.setContentsMargins(6, 6, 6, 6)
        log_layout.setSpacing(4)
        tabs.addTab(log_tab, "日志")

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)
        self.log_splitter = splitter

        # 左侧：日志文件管理框
        manager_box = QGroupBox("日志文件")
        manager_layout = QVBoxLayout(manager_box)
        manager_layout.setContentsMargins(6, 6, 6, 6)
        manager_layout.setSpacing(4)

        self.log_list = QListWidget()
        self.log_list.setMinimumWidth(150)
        self.log_list.setToolTip("单击选择一个日志文件，右侧显示其内容；双击用系统程序打开")
        self.log_list.currentItemChanged.connect(self.on_log_item_changed)
        self.log_list.itemDoubleClicked.connect(lambda _item: self.open_selected_log())
        manager_layout.addWidget(self.log_list, 1)

        manager_buttons = QHBoxLayout()
        manager_buttons.setSpacing(4)
        self.refresh_list_btn = QPushButton("刷新")
        self.refresh_list_btn.setStyleSheet(COMPACT_BUTTON_STYLE)
        self.refresh_list_btn.setToolTip("重新扫描日志目录")
        self.refresh_list_btn.clicked.connect(self.refresh_log_list)
        manager_buttons.addWidget(self.refresh_list_btn)

        self.open_selected_btn = QPushButton("打开")
        self.open_selected_btn.setStyleSheet(COMPACT_BUTTON_STYLE)
        self.open_selected_btn.setToolTip("用系统默认程序打开选中的日志文件")
        self.open_selected_btn.clicked.connect(self.open_selected_log)
        manager_buttons.addWidget(self.open_selected_btn)

        self.delete_selected_btn = QPushButton("删除")
        self.delete_selected_btn.setStyleSheet(COMPACT_BUTTON_STYLE)
        self.delete_selected_btn.setToolTip("删除选中的日志文件（不保留）")
        self.delete_selected_btn.clicked.connect(self.delete_selected_log)
        manager_buttons.addWidget(self.delete_selected_btn)
        manager_layout.addLayout(manager_buttons)

        # 目录只显示末两级（完整路径在 tooltip 里）。
        # 这里千万不能显示完整路径：这个 QLabel 开了自动换行，而路径没有空格无法折行，
        # 它的最小宽度会等于整行文本宽度，直接把左侧整列顶到 260px+ 下不来。
        short_dir = os.path.join(
            os.path.basename(os.path.dirname(LOG_DIR)), os.path.basename(LOG_DIR))
        self.log_dir_label = QLabel(f"目录：{short_dir}")
        self.log_dir_label.setWordWrap(False)
        self.log_dir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_dir_label.setStyleSheet("color: gray; font-size: 10px;")
        self.log_dir_label.setToolTip(f"日志目录：{LOG_DIR}")
        # Ignored：宽度不参与最小尺寸计算，列宽再窄也压得下去（文字本身就短）
        self.log_dir_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        manager_layout.addWidget(self.log_dir_label)

        splitter.addWidget(manager_box)

        # 右侧：日志内容
        viewer_box = QGroupBox("日志内容")
        viewer_layout = QVBoxLayout(viewer_box)
        viewer_layout.setContentsMargins(6, 6, 6, 6)
        viewer_layout.setSpacing(4)

        viewer_buttons = QHBoxLayout()
        viewer_buttons.setSpacing(6)
        self.refresh_log_btn = QPushButton("刷新显示")
        self.refresh_log_btn.setToolTip(f"重新载入最近 {RECENT_LOG_DAYS} 天的日志")
        self.refresh_log_btn.clicked.connect(self.refresh_log_display)
        viewer_buttons.addWidget(self.refresh_log_btn)

        self.pause_log_btn = QPushButton("暂停滚动")
        self.pause_log_btn.setCheckable(True)
        self.pause_log_btn.clicked.connect(self.toggle_log_scroll)
        viewer_buttons.addWidget(self.pause_log_btn)

        viewer_buttons.addStretch()

        # 当前显示的日志文件：做成按钮行末尾的一小行灰字。
        # 旧实现是一条横贯整个窗口的路径栏，长路径会换行、白白吃掉日志区高度。
        # 完整路径放进 tooltip，左侧日志列表与目录标签也都能看到。
        self.log_path_label = QLabel()
        self.log_path_label.setStyleSheet("color: gray; font-size: 11px;")
        self.log_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_path_label.setToolTip("当前显示的日志文件")
        # Maximum：可以比 sizeHint 更小，避免长文件名把按钮行撑宽
        self.log_path_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        viewer_buttons.addWidget(self.log_path_label)
        viewer_layout.addLayout(viewer_buttons)

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setToolTip("日志内容（主要信息展示区），可选中后按 Ctrl+C 复制")
        # 拉伸因子 1：剩余高度全部给日志展示区
        viewer_layout.addWidget(self.log_display, 1)

        splitter.addWidget(viewer_box)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        log_layout.addWidget(splitter)

        # 状态栏
        self.statusBar().showMessage("就绪")

        # 日志页是隐藏标签页，它的布局要到真正切换到该页时才计算。
        # 所以除了首次显示窗口，切到这个页时也要再校正一次分隔比例。
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.log_splitter.splitterMoved.connect(self._on_log_splitter_moved)

    def showEvent(self, event):
        """首次显示后再应用日志页的分隔比例。

        在 show() 之前调 setSizes 会被 Qt 的首次布局重排覆盖掉（实测退回 50:50），
        所以放到窗口真正有了尺寸之后再设一次。
        """
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_log_splitter_sizes)

    def _on_tab_changed(self, index):
        """切到日志页时校正一次左右宽度（日志页在此之前是隐藏的、没有布局）"""
        if index == 1:
            QTimer.singleShot(0, self._apply_log_splitter_sizes)

    def _on_log_splitter_moved(self, *_args):
        """用户手动拖过分隔条后，就不再自动改动比例"""
        self._log_splitter_sized = True

    def _apply_log_splitter_sizes(self):
        """按 LOG_SPLIT_RATIO 设置日志页左右宽度，把面积让给日志内容"""
        try:
            # 用户已经手动调整过，或者日志页还没真正显示（宽度为 0），就跳过
            if self._log_splitter_sized:
                return
            if not self.log_splitter.isVisible() or self.log_splitter.width() <= 0:
                return

            total = self.log_splitter.width()
            left, right = self.LOG_SPLIT_RATIO
            left_px = int(total * left / float(left + right))
            self.log_splitter.setSizes([left_px, total - left_px])
        except Exception as e:
            log(f"设置日志页分隔比例失败: {e}", "WARNING")

    def setup_ui_logging(self):
        """设置UI日志处理"""
        self.ui_handler = UIHandler()
        self.ui_handler.log_signal.connect(self.append_log)
        set_ui_handler(self.ui_handler)

        # 先挂日志处理器，再回显历史日志，避免回显过程本身产生的新日志被漏掉
        self.refresh_log_list()
        self.show_recent_logs()
        log("GUI界面初始化完成", "INFO")

    def load_config_values(self):
        """加载配置值到界面"""
        self.username_input.setText(self.config.get("username", ""))
        self.password_input.setText(self.config.get("password", ""))
        self.login_url_input.setText(self.config.get("login_url", "https://gw.buaa.edu.cn/"))
        self.interval_input.setValue(self.config.get("check_interval", 300))
        self.test_url_input.setText(self.config.get("test_url", "https://kimi.moonshot.cn"))
        self.retention_input.setValue(
            self.config.get("log_retention_days", DEFAULT_RETENTION_DAYS))

        # 回显当前启动项状态
        self.autostart_checkbox.blockSignals(True)
        self.autostart_checkbox.setChecked(check_autostart_status())
        self.autostart_checkbox.blockSignals(False)

        # 仅打包后的 exe 允许启用自启动
        if can_enable_autostart():
            self.autostart_checkbox.setEnabled(True)
            self.autostart_checkbox.setToolTip("勾选后将以托盘模式随 Windows 启动")
        else:
            self.autostart_checkbox.setEnabled(False)
            self.autostart_checkbox.setToolTip("请运行打包后的 exe 后再启用开机自启动")

    def on_autostart_changed(self, state):
        """用户切换开机自启动"""
        enabled = bool(state)

        # 未打包时不允许开启
        if enabled and not can_enable_autostart():
            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(False)
            self.autostart_checkbox.blockSignals(False)
            QMessageBox.information(self, "提示", "请运行打包后的 exe 后再启用开机自启动。")
            return

        success, message = setup_autostart(enabled)

        # 同步真实状态，防止界面状态与实际不一致
        actual_status = check_autostart_status()
        self.autostart_checkbox.blockSignals(True)
        self.autostart_checkbox.setChecked(actual_status)
        self.autostart_checkbox.blockSignals(False)

        if success:
            log(message, "INFO")
            self.statusBar().showMessage(message, 5000)
        else:
            log(message, "WARNING")
            QMessageBox.warning(self, "提示", message)

    def save_config(self, is_start_monitoring=False):
        """保存配置"""
        self.config["username"] = self.username_input.text()
        self.config["password"] = self.password_input.text()
        self.config["login_url"] = self.login_url_input.text()
        self.config["check_interval"] = self.interval_input.value()
        self.config["test_url"] = self.test_url_input.text()
        self.config["log_retention_days"] = self.retention_input.value()

        save_config(self.config)

        # 立刻按新的保留天数清理一次过期日志，让设置马上生效
        try:
            log(f"日志保留策略已应用：{apply_log_retention(self.retention_input.value())}",
                "INFO")
        except Exception as e:
            log(f"应用日志保留策略失败: {e}", "WARNING")

        # 设置开机自启动
        if self.autostart_checkbox.isChecked():
            success, message = setup_autostart(True)
        else:
            success, message = setup_autostart(False)

        # 将新配置应用到正在运行的托盘监控
        try:
            from tray_icon import tray_manager
            if tray_manager:
                tray_manager.reload_config(self.config)
        except Exception:
            pass

        # 再同步一次真实状态，避免 UI 勾选状态与实际不一致
        self.autostart_checkbox.blockSignals(True)
        self.autostart_checkbox.setChecked(check_autostart_status())
        self.autostart_checkbox.blockSignals(False)

        if success:
            log(f"保存配置并设置自启动成功: {message}", "INFO")
            if not is_start_monitoring:
                QMessageBox.information(self, "成功", f"配置已保存。\n{message}")
        else:
            log(f"保存配置完成，但自启动设置失败: {message}", "WARNING")
            if not is_start_monitoring:
                QMessageBox.warning(self, "提示", f"配置已保存，但自启动设置失败：\n{message}")

    def _validate_required_before_start(self):
        """校验启动必需项：用户名、密码"""
        username = (self.username_input.text() or "").strip()
        password = (self.password_input.text() or "").strip()
        if not username or not password:
            QMessageBox.warning(self, "缺少配置", "请先填写用户名与密码。")
            return False
        return True

    def start_monitoring(self):
        """开始监控"""
        # 启动前校验
        if not self._validate_required_before_start():
            log("必填项缺失，阻止启动监控", "WARNING")
            return

        # 启动前保存配置，确保托盘读取到最新配置
        self.save_config(is_start_monitoring=True)

        from tray_icon import tray_manager
        if tray_manager:
            tray_manager.start_monitoring()

    def stop_monitoring(self):
        """停止监控"""
        from tray_icon import tray_manager
        if tray_manager:
            tray_manager.stop_monitoring()

    @staticmethod
    def _tail_lines(path, limit):
        """从文件尾部读取最后 limit 行（委托给 logger，便于统一维护）"""
        return read_log_tail(path, limit)

    # ---------------- 日志管理器（左侧） ----------------

    def refresh_log_list(self):
        """重新扫描日志目录，刷新左侧列表"""
        selected = self.selected_log_path()

        self.log_list.blockSignals(True)
        self.log_list.clear()
        for item in list_log_files():
            entry = QListWidgetItem(f"{item['date']}    {item['size'] / 1024:8.1f} KB")
            entry.setData(Qt.UserRole, item["path"])
            entry.setToolTip(item["path"])
            self.log_list.addItem(entry)
        self.log_list.blockSignals(False)

        # 尽量保持原来的选中项
        if selected:
            self.select_log_in_list(selected)
        self.statusBar().showMessage(f"日志目录下共 {self.log_list.count()} 个文件", 3000)

    def select_log_in_list(self, path):
        for row in range(self.log_list.count()):
            entry = self.log_list.item(row)
            if entry and os.path.normcase(entry.data(Qt.UserRole)) == os.path.normcase(path):
                self.log_list.setCurrentItem(entry)
                return True
        return False

    def selected_log_path(self):
        """左侧当前选中的日志文件路径；没有选中时返回 None"""
        entry = self.log_list.currentItem()
        return entry.data(Qt.UserRole) if entry else None

    def on_log_item_changed(self, current, _previous):
        """单击左侧列表项 → 右侧显示该文件内容"""
        if current is None:
            return
        self.show_log_file(current.data(Qt.UserRole))

    def open_selected_log(self):
        """用系统默认程序打开选中的日志文件"""
        self.open_log_file(self.selected_log_path() or current_log_file())

    def delete_selected_log(self):
        """删除选中的日志文件"""
        path = self.selected_log_path()
        if not path:
            QMessageBox.information(self, "提示", "请先在左侧选择一个日志文件。")
            return

        size_kb = os.path.getsize(path) / 1024 if os.path.exists(path) else 0
        reply = QMessageBox.question(
            self,
            "确认删除日志",
            f"将删除该日志文件，内容不会保留：\n{os.path.basename(path)}"
            f"（{size_kb:.1f} KB）\n\n确定继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        ok, message = delete_log_file(path)
        if ok:
            log(f"已删除日志文件: {path}", "INFO")
            self.statusBar().showMessage(message, 5000)
            if os.path.normcase(path) == os.path.normcase(current_log_file()):
                # 删掉的正是今天正在写入的文件，写入句柄会在下一条日志时自动重开
                self.log_display.clear()
                log("日志文件已删除，重新开始记录", "INFO")
                self.show_recent_logs()
            else:
                self.refresh_log_list()
        else:
            log(message, "ERROR")
            QMessageBox.warning(self, "提示", f"{message}\n\n路径：{path}")

    # ---------------- 日志展示（右侧） ----------------

    def _set_log_path_text(self, text, full_path=None):
        """更新展示区顶部那一小行路径小字。

        只显示短文本（文件名/文件个数），完整路径进 tooltip；
        过长时按像素宽度做中间省略，避免把按钮行撑宽。
        """
        if not text:
            self.log_path_label.setText("")
            self.log_path_label.setToolTip("")
            return
        metrics = self.log_path_label.fontMetrics()
        elided = metrics.elidedText(text, Qt.ElideMiddle, LOG_PATH_LABEL_WIDTH)
        self.log_path_label.setText(elided)
        self.log_path_label.setToolTip(full_path or text)

    def _render_log_bundles(self, bundles):
        """把 [(路径, [行])] 渲染进展示框（一次性 setHtml）"""
        html_parts = []
        for path, lines in bundles:
            html_parts.append(f'<font color="#185FA5">===== {escape(os.path.basename(path))} =====</font>')
            if not lines:
                html_parts.append('<font color="gray">（无可显示内容）</font>')
                continue
            for line in lines:
                matched = re.search(r"\] (\w+):", line)
                color = LOG_COLORS.get(matched.group(1), "black") if matched else "black"
                html_parts.append(f'<font color="{color}">{escape(line.strip())}</font>')

        if html_parts:
            self.log_display.setHtml("<br>".join(html_parts))
        else:
            self.log_display.setHtml('<font color="gray">（暂无日志）</font>')
        self.log_display.moveCursor(QTextCursor.End)

    def show_log_file(self, path, limit=PER_FILE_TAIL_LINES):
        """在右侧显示单个日志文件（默认只取尾部若干行）"""
        if not path:
            return
        if not os.path.exists(path):
            self._set_log_path_text(f"文件不存在：{os.path.basename(path)}", path)
            self.log_display.setHtml('<font color="gray">（文件不存在或已被删除）</font>')
            return

        self._set_log_path_text(os.path.basename(path), path)
        self._render_log_bundles([(path, read_log_tail(path, limit))])

    def show_recent_logs(self):
        """启动/刷新时显示最近若干天的日志"""
        bundles = read_recent_logs(RECENT_LOG_DAYS, PER_FILE_TAIL_LINES)
        if bundles:
            if len(bundles) == 1:
                summary = os.path.basename(bundles[0][0])
            else:
                summary = f"最近 {RECENT_LOG_DAYS} 天共 {len(bundles)} 个文件"
            self._set_log_path_text(summary, LOG_DIR)
        else:
            today = current_log_file()
            self._set_log_path_text(f"{os.path.basename(today)}（暂无内容）", today)
        self._render_log_bundles(bundles)

    def refresh_log_display(self):
        """刷新：重新扫描目录并载入最近几天的日志"""
        self.log_list.blockSignals(True)
        self.log_list.setCurrentItem(None)
        self.log_list.blockSignals(False)
        self.refresh_log_list()
        self.show_recent_logs()
        self.statusBar().showMessage("日志已刷新", 3000)
        log("日志显示已刷新", "INFO")

    def open_log_file(self, path=None):
        """用系统默认程序打开日志文件（未关联则打开所在文件夹）"""
        log_file = path or current_log_file()
        try:
            if not os.path.exists(log_file):
                # 文件还没生成时先建一个空文件，保证"打开"这个动作有结果
                os.makedirs(os.path.dirname(log_file), exist_ok=True)
                open(log_file, "a", encoding="utf-8").close()
            os.startfile(log_file)
            log(f"已打开日志文件: {log_file}", "INFO")
            self.statusBar().showMessage("已打开日志文件", 3000)
        except Exception as e:
            # 打不开就退而打开所在目录，并把路径告诉用户
            folder = os.path.dirname(log_file)
            log(f"打开日志文件失败({e})，改为打开所在文件夹: {folder}", "WARNING")
            try:
                os.startfile(folder)
                self.statusBar().showMessage("已打开日志所在文件夹", 3000)
            except Exception as e2:
                log(f"打开日志文件夹也失败: {e2}", "ERROR")
                QMessageBox.warning(self, "提示", f"无法打开日志文件：{e}\n\n路径：{log_file}")

    def append_log(self, message, level):
        """追加日志到显示框"""
        try:
            color = LOG_COLORS.get(level, "black")

            # 使用HTML格式显示带颜色的日志
            html_message = f'<font color="{color}">{escape(message)}</font>'

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