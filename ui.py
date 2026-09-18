import sys
import os
import re
import logging
import threading
from html import escape
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QLineEdit, QSpinBox,
    QCheckBox, QPushButton, QTextEdit, QGroupBox,
    QMessageBox, QTabWidget, QFileDialog, QDialog,
    QListWidget, QListWidgetItem, QSplitter, QSizePolicy
)
from PyQt5.QtCore import QTimer, QThread, pyqtSignal, pyqtSlot, QObject, Qt
from PyQt5.QtGui import QTextCursor, QDesktopServices
from PyQt5.QtCore import QUrl

# 添加当前目录到路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import load_config, save_config
from network_checker import NetworkChecker
from logger import (
    setup_logger,
    log,
    log_with_notification,
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
from single_instance import start_show_window_listener
from version import __version__
from updater import (
    check_for_update,
    skip_version,
    STARTUP_DELAY,
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


def config_row_gap(row_height):
    """配置页里每两行控件之间的固定间距（像素）。

    行距（相邻两行控件的垂直中心距）= 2 倍行高：控件自身占 1 倍，
    再额外留 1 倍空隙。

    基准取**控件自身的高度**而不是字体行高 —— 控件还含样式内边距，
    按字体行高折算出来的间距会挤到几乎没有（实测只有 1px）。

    **刻意返回固定像素值**：行距不能交给布局去伸缩，否则窗口一变高，
    行与行就被拉开，看起来是"浮动"的。
    """
    return max(2, int(round(row_height * 1)))


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


class UpdateCheckWorker(QObject):
    """在后台线程里检查更新。

    更新检查要联网，放到 GUI 线程上会卡界面；而它又不是 GUI 线程，
    所以只能 emit 信号，由接收方用 QueuedConnection 接住再碰控件。

    约定：调用方连接 finished 时**必须**带 `type=Qt.QueuedConnection`。
    """

    finished = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._busy = False

    def start(self, force=False):
        """启动一次检查。已有检查在跑时直接忽略，避免重复请求。"""
        if self._busy:
            return False
        self._busy = True
        thread = threading.Thread(target=self._run, args=(bool(force),),
                                  name="UpdateCheck", daemon=True)
        thread.start()
        return True

    def _run(self, force):
        info = None
        try:
            info = check_for_update(force=force)
        except Exception as e:
            # check_for_update 内部已经兜底，这里是最后一道保险
            log(f"更新检查线程异常（已忽略）: {e}", "WARNING")
        finally:
            self._busy = False
        self.finished.emit(info)


class UpdateDialog(QDialog):
    """提示有新版本。

    只做"看更新说明 + 打开下载页"，**不下载、不替换文件** ——
    程序是 PyInstaller 单文件 exe，运行时替换自身不可靠，
    交给用户用安装包覆盖升级最稳。
    """

    ACTION_DOWNLOAD = "download"
    ACTION_LATER = "later"
    ACTION_SKIP = "skip"

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info
        self.action = self.ACTION_LATER
        self._build_ui()

    def _build_ui(self):
        self.setWindowTitle("发现新版本")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        head = QLabel(
            f"<b>发现新版本 {escape(self.info.version)}</b>"
            f"<span style='color:gray'>（当前 {escape(__version__)}）</span>")
        layout.addWidget(head)

        if self.info.published_at:
            date_label = QLabel(f"发布日期：{escape(self.info.published_at)}")
            date_label.setStyleSheet("color: gray; font-size: 11px;")
            layout.addWidget(date_label)

        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(self.info.notes or "（本次更新没有提供说明）")
        notes.setMinimumHeight(120)
        notes.setToolTip("可选中复制")
        layout.addWidget(notes)

        if self.info.mandatory:
            tip = QLabel("此版本为必需更新，建议尽快升级。")
            tip.setStyleSheet("color: #b36b00;")
            layout.addWidget(tip)

        buttons = QHBoxLayout()
        self.download_btn = QPushButton("去下载")
        self.download_btn.setDefault(True)
        self.download_btn.setToolTip(self.info.download_url)
        self.download_btn.clicked.connect(self._on_download)
        buttons.addWidget(self.download_btn)

        buttons.addStretch()

        self.later_btn = QPushButton("稍后再说")
        self.later_btn.clicked.connect(self._on_later)
        buttons.addWidget(self.later_btn)

        self.skip_btn = QPushButton("跳过此版本")
        self.skip_btn.setToolTip("在下次版本发布前不再提示该版本")
        self.skip_btn.clicked.connect(self._on_skip)
        buttons.addWidget(self.skip_btn)

        layout.addLayout(buttons)

    def _on_download(self):
        self.action = self.ACTION_DOWNLOAD
        url = self.info.download_url
        try:
            QDesktopServices.openUrl(QUrl(url))
            log(f"已打开下载页: {url}", "INFO")
        except Exception as e:
            log(f"打开下载页失败: {e}", "WARNING")
            QMessageBox.information(self, "下载地址", f"请在浏览器中打开：\n{url}")
        self.accept()

    def _on_later(self):
        self.action = self.ACTION_LATER
        self.accept()

    def _on_skip(self):
        self.action = self.ACTION_SKIP
        skip_version(self.info.version)
        self.accept()


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
    # 右侧是主要信息展示区，所以尽量少给左侧；25% 是"日期 + 文件大小"还能完整显示的下限。
    LOG_SPLIT_RATIO = (25, 75)

    # 单实例保护：新实例请求打开主界面时，由监听线程 emit，本信号负责把它
    # 排队回 GUI 线程再真正操作窗口。
    show_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.check_thread = None
        self.config = load_config()
        self.ui_handler = None
        self.auto_scroll = True
        self._log_splitter_sized = False
        self._compact_applied = False

        self.show_requested.connect(self._bring_to_front, type=Qt.QueuedConnection)

        self.init_ui()
        self.load_config_values()
        self.setup_ui_logging()
        self.sync_monitoring_status()

        self.status_sync_timer = QTimer(self)
        self.status_sync_timer.timeout.connect(self.sync_monitoring_status)
        self.status_sync_timer.start(1000)  # 每1秒同步一次

        self._setup_update_check()

    def _setup_update_check(self):
        """准备更新检查：后台线程 + 启动后延迟自动检查一次。

        结果只能经信号回到 GUI 线程，所以连接时必须带 QueuedConnection。
        """
        self.update_worker = UpdateCheckWorker()
        self.update_worker.finished.connect(
            self._on_update_checked, type=Qt.QueuedConnection)
        # 延迟一会儿再查，且不占用启动的关键路径
        QTimer.singleShot(STARTUP_DELAY * 1000,
                          lambda: self._start_update_check(force=False))

    def _start_update_check(self, force):
        if not self.update_worker.start(force=force):
            log("更新检查正在进行中，本次请求已忽略", "DEBUG")
            return
        if force:
            self.version_label.setText(f"v{__version__} · 正在检查…")
            self.check_update_btn.setEnabled(False)
        log("开始检查更新", "INFO")

    def check_update_manually(self):
        """「检查更新」按钮：忽略节流，并且无论结果如何都给用户一个反馈。"""
        self._start_update_check(force=True)

    @pyqtSlot(object)
    def _on_update_checked(self, info):
        """更新检查结果回来（运行在 GUI 线程）"""
        self.check_update_btn.setEnabled(True)
        if info is None:
            self.version_label.setText(f"v{__version__} · 已是最新")
            self.version_label.setToolTip("上次检查：刚刚")
            return

        self.version_label.setText(f"v{__version__} · 发现新版本 {info.version}")
        self.version_label.setToolTip("点击「检查更新」查看详情")
        try:
            dialog = UpdateDialog(info, self)
            dialog.exec_()
        except Exception as e:
            # 弹窗出问题也不能影响主功能，退回系统托盘通知
            log(f"显示更新提示失败: {e}", "WARNING")
            log_with_notification(
                f"发现新版本 {info.version}，请打开主界面查看", "INFO", "有可用更新")

    def request_show(self):
        """响应"新实例请求打开主界面"（由单实例监听线程调用，只投递信号）"""
        self.show_requested.emit()

    @pyqtSlot()
    def _bring_to_front(self):
        """把窗口带到前台（运行在 GUI 线程）"""
        try:
            if self.isMinimized():
                self.showNormal()
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception as e:
            log(f"显示主界面失败: {e}", "WARNING")

    def init_ui(self):
        """初始化用户界面"""
        self.setWindowTitle(f"网络自动检查与登录系统  v{__version__}")
        # 配置页只有 7 行控件，窗口给太高只会在下面留一大片空白。
        # 这里先给一个默认尺寸，真正的"紧凑高度"在首次显示后由
        # _apply_compact_window_size() 按配置页实际所需算出来并固定。
        self.setGeometry(100, 100, 780, 420)
        # 宽度可调（便于看日志），高度不可调 —— 否则行距会随窗口浮动
        self.setMinimumWidth(720)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # 创建标签页
        tabs = QTabWidget()
        self.tabs = tabs
        layout.addWidget(tabs)

        # 配置标签页
        config_tab = QWidget()
        self.config_tab = config_tab
        config_layout = QVBoxLayout(config_tab)
        config_layout.setContentsMargins(8, 8, 8, 8)
        tabs.addTab(config_tab, "配置")

        # 用户配置组：只放账号
        user_group = QGroupBox("用户配置")
        user_group_layout = QVBoxLayout(user_group)
        user_group_layout.setContentsMargins(8, 8, 8, 8)

        # 用户名
        username_row = QHBoxLayout()
        username_row.addWidget(QLabel("用户名:"))
        self.username_input = QLineEdit()
        username_row.addWidget(self.username_input)
        user_group_layout.addLayout(username_row)

        # 密码
        pwd_row = QHBoxLayout()
        pwd_row.addWidget(QLabel("密码:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        pwd_row.addWidget(self.password_input)
        user_group_layout.addLayout(pwd_row)

        config_layout.addWidget(user_group)

        # 系统配置组：登录网址 -> 测试网址 -> 检查间隔 -> 日志保留天数 -> 开机自启动
        system_group = QGroupBox("系统配置")
        system_layout = QVBoxLayout(system_group)
        system_layout.setContentsMargins(8, 8, 8, 8)

        # 登录网址
        login_url_layout = QHBoxLayout()
        login_url_layout.addWidget(QLabel("登录网址:"))
        self.login_url_input = QLineEdit()
        login_url_layout.addWidget(self.login_url_input)
        system_layout.addLayout(login_url_layout)

        # 测试网址
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("测试网址:"))
        self.test_url_input = QLineEdit()
        url_layout.addWidget(self.test_url_input)
        system_layout.addLayout(url_layout)

        # 检查间隔
        interval_layout = QHBoxLayout()
        interval_layout.addWidget(QLabel("检查间隔(秒):"))
        self.interval_input = QSpinBox()
        self.interval_input.setRange(10, 3600)
        self.interval_input.setSuffix(" 秒")
        interval_layout.addWidget(self.interval_input)
        system_layout.addLayout(interval_layout)

        # 日志保留天数
        retention_layout = QHBoxLayout()
        retention_layout.addWidget(QLabel("日志保留天数:"))
        self.retention_input = QSpinBox()
        self.retention_input.setRange(MIN_RETENTION_DAYS, MAX_RETENTION_DAYS)
        self.retention_input.setSuffix(" 天")
        self.retention_input.setToolTip("启动时按此天数清理过期日志文件")
        retention_layout.addWidget(self.retention_input)
        system_layout.addLayout(retention_layout)

        # 开机自启动（放在系统配置组最后）
        self.autostart_checkbox = QCheckBox("开机自动启动")
        self.autostart_checkbox.stateChanged.connect(self.on_autostart_changed)
        system_layout.addWidget(self.autostart_checkbox)

        config_layout.addWidget(system_group)

        # 两个组框都只按内容高度占位，多出来的空间一律留到最下面。
        # 不做这一步的话 QVBoxLayout 会把剩余空间平分给它们：只有两行的
        # 「用户配置」被撑得和五行的「系统配置」一样高，行与行之间出现大片空白，
        # 看起来就是"用户配置太占地方、系统配置太挤"。
        for box in (user_group, system_group):
            box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

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

        button_layout.addStretch()

        self.check_update_btn = QPushButton("检查更新")
        self.check_update_btn.setToolTip(f"当前版本 {__version__}，点击立即检查是否有新版本")
        self.check_update_btn.clicked.connect(self.check_update_manually)
        button_layout.addWidget(self.check_update_btn)

        config_layout.addLayout(button_layout)

        # 统一行距：相邻两行控件的中心距 = 1.5 倍行高。
        # 放在这里设置是因为此时所有输入控件都已建好，可以直接量到真实行高。
        row_height = self.username_input.sizeHint().height()
        row_gap = config_row_gap(row_height)
        for lay in (config_layout, user_group_layout, system_layout):
            lay.setSpacing(row_gap)
        self._config_row_height = row_height

        # 剩余空间全部推到底部：让"表单 + 按钮"作为一整块紧贴在顶部，
        # 不随窗口高度变化而散开
        config_layout.addStretch(1)

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
        # 版本与更新状态用**常驻标签**显示，不能只用 showMessage ——
        # 状态栏的临时消息会被每秒一次的 sync_monitoring_status 覆盖掉，
        # 用户根本看不到"已是最新版本"这类反馈。
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setToolTip("当前版本；点「检查更新」可手动检查新版本")
        self.statusBar().addPermanentWidget(self.version_label)

        # 日志页是隐藏标签页，它的布局要到真正切换到该页时才计算。
        # 所以除了首次显示窗口，切到这个页时也要再校正一次分隔比例。
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.log_splitter.splitterMoved.connect(self._on_log_splitter_moved)

    def showEvent(self, event):
        """首次显示后：算出紧凑高度并固定；再校正日志页分隔比例。

        这两件事都必须等窗口真正有了尺寸才能做：
        - setSizes / setFixedHeight 在 show() 之前设置会被 Qt 的首次布局覆盖；
        - 日志页是隐藏标签页，它在此之前没有布局。
        """
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_compact_window_size)

    def _apply_compact_window_size(self):
        """把窗口高度收缩到"刚好装下配置页"，并固定住。

        配置页布局的 sizeHint 就是它紧凑所需的高度（末尾的 stretch 贡献 0），
        再加上标签栏 / 状态栏 / 窗口边框这些固定开销，就是窗口应有的高度。
        多出来的空间不再被布局分掉，行距也就不会浮动。
        """
        if self._compact_applied:
            return
        try:
            self._compact_applied = True
            overhead = self.height() - self.config_tab.height()
            content = self.config_tab.layout().sizeHint().height()
            target = max(content + overhead, self.minimumSizeHint().height())

            self.setFixedHeight(target)
            log(f"窗口已收缩为紧凑尺寸：{self.width()} x {target}"
                f"（配置页需 {content}px + 固定开销 {overhead}px）", "INFO")
        except Exception as e:
            log(f"计算紧凑窗口尺寸失败: {e}", "WARNING")

        # 高度定下来之后再校正日志页的左右比例
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
        self.test_url_input.setText(self.config.get("test_url", "https://www.taobao.com/"))
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

    # 单实例保护：接收"新实例请求打开主界面"的事件。
    # 此时没有托盘进程，窗口就由本进程自己负责显示。
    start_show_window_listener(window.request_show)

    try:
        return app.exec_()
    except Exception as e:
        print(f"UI运行错误: {e}")
        return 1