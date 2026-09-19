# -*- coding: utf-8 -*-
"""应用图标的统一入口。

集中解决三类"图标显示不对"的问题：

1. **窗口图标从未被设置**。PyQt5 的 QMainWindow 默认不带图标，不调用
   `setWindowIcon` 时标题栏与 Alt+Tab 用的是 Qt 内置默认图标（不是本程序的图标）。
2. **打包后路径不对**。单文件 exe 解压到临时目录，图标资源必须经 `sys._MEIPASS` 定位；
   直接按源码目录拼路径在打包后会读不到文件。
3. **任务栏图标与分组**。Windows 按 AppUserModelID 决定任务栏按钮的图标与分组，
   不显式设置时从 python.exe 启动会显示成 Python 的图标。

所有入口（GUI、托盘、单实例唤起）都从这里取图标，保证三处一致。

`icon.ico` 由 `build_icon.py` 生成，包含 16/20/24/32/40/48/64/128/256 全部常用尺寸 ——
托盘与标题栏实际请求的是 16~24px，只有尺寸齐全才不会由系统缩放导致发虚。
"""

import os
import sys

# 图标文件名（打包时由 AutoConnect.spec 放进包根目录）
ICON_NAME = "icon.ico"

# 任务栏分组用的 AppUserModelID。用反域名风格的稳定字符串，
# 换版本也不要改，否则任务栏上会出现两个图标。
APP_USER_MODEL_ID = "Vegetabledog-BUAA.AutoConnectToCampusNetwork"

# 首次加载后缓存，避免每次取图标都读一遍文件
_cached_icon = None


def icon_path():
    """定位 icon.ico 的绝对路径，找不到返回 None。

    查找顺序（打包 / 源码运行都覆盖）：
    1. `sys._MEIPASS`（PyInstaller 单文件解压目录，打包运行时的首选）
    2. 本文件所在目录（源码运行）
    3. exe 所在目录（允许用户把图标放在 exe 旁边覆盖）
    """
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, ICON_NAME))

    candidates.append(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ICON_NAME))

    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, ICON_NAME))
    except Exception:
        pass

    for path in candidates:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def _fallback_icon():
    """图标文件缺失或损坏时，现画一个。

    宁可给一个自己画的图标，也不要让窗口退回 Qt 默认图标 ——
    后者会让用户以为程序出错。多尺寸一并生成，托盘用小尺寸时不至于糊。
    """
    from PyQt5.QtCore import Qt, QRectF
    from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush

    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            margin = max(1.0, size * 0.06)
            rect = QRectF(margin, margin, size - margin * 2, size - margin * 2)

            # 深蓝圆底 + 浅色环，形似"网络已连通"
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(24, 90, 188)))
            painter.drawEllipse(rect)

            painter.setBrush(Qt.NoBrush)
            pen = QPen(QColor(255, 255, 255))
            pen.setWidthF(max(1.0, size * 0.08))
            painter.setPen(pen)
            inner = rect.adjusted(size * 0.22, size * 0.22,
                                  -size * 0.22, -size * 0.22)
            painter.drawArc(inner, 40 * 16, 280 * 16)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            dot = max(1.5, size * 0.11)
            painter.drawEllipse(QRectF(size * 0.5 - dot / 2,
                                       size * 0.5 - dot / 2, dot, dot))
        finally:
            painter.end()
        icon.addPixmap(pixmap)
    return icon


def load_icon():
    """返回 QIcon。文件缺失时返回内置绘制图标；绝不返回空图标。"""
    global _cached_icon
    if _cached_icon is not None:
        return _cached_icon

    path = icon_path()
    if path:
        try:
            from PyQt5.QtGui import QIcon
            icon = QIcon(path)
            if not icon.isNull() and icon.availableSizes():
                _cached_icon = icon
                return icon
            _log(f"图标文件无法解析（{path}），改用内置图标", "WARNING")
        except Exception as e:
            _log(f"读取图标文件失败（{path}）: {e}，改用内置图标", "WARNING")
    else:
        _log(f"未找到 {ICON_NAME}，改用内置图标", "WARNING")

    _cached_icon = _fallback_icon()
    return _cached_icon


def set_app_user_model_id(app_id=APP_USER_MODEL_ID):
    """设置 Windows 任务栏的 AppUserModelID。

    不设置时，从 python.exe 启动会继承 Python 的图标与分组；设置后任务栏
    会使用本窗口自己的图标。非 Windows 平台直接跳过。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception as e:
        _log(f"设置 AppUserModelID 失败: {e}", "DEBUG")
        return False


def apply_to_application(app=None):
    """把图标挂到 QApplication 上（所有新建窗口的默认图标）。

    必须在窗口创建之前调用；`app` 省略时取当前实例。
    :return: True 表示已应用
    """
    if app is None:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance()
    if app is None:
        return False

    try:
        app.setWindowIcon(load_icon())
    except Exception as e:
        _log(f"设置应用图标失败: {e}", "WARNING")
        return False

    set_app_user_model_id()
    return True


def apply_to_window(window):
    """把图标挂到某个窗口上。

    QApplication 的默认图标对在 QApplication 之前就已构造、或从别处传入的
    窗口不生效，所以每个窗口再显式设一次最稳妥。
    """
    if window is None:
        return False
    try:
        window.setWindowIcon(load_icon())
        return True
    except Exception as e:
        _log(f"设置窗口图标失败: {e}", "WARNING")
        return False


def _log(message, level="INFO"):
    """写日志，但日志模块不可用时不抛异常（图标问题不能影响主流程）。"""
    try:
        from logger import log
        log(message, level)
    except Exception:
        pass


if __name__ == "__main__":
    print("=" * 66)
    print("应用图标模块自检")
    print("=" * 66)
    print("期望文件名  :", ICON_NAME)
    print("AppUserModelID:", APP_USER_MODEL_ID)
    print()

    path = icon_path()
    print("[%s] icon_path() 找到图标" % ("通过" if path else "失败"))
    print("        路径:", path)
    if path:
        print("        大小: %d 字节" % os.path.getsize(path))

    app = None
    try:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)
    except Exception as e:
        print("（无图形环境，跳过 QIcon 部分: %s）" % e)

    if app is not None:
        print()
        print("[%s] apply_to_application() 返回 True"
              % ("通过" if apply_to_application(app) else "失败"))
        icon = app.windowIcon()
        print("[%s] QApplication.windowIcon() 不再为空"
              % ("通过" if not icon.isNull() else "失败"))
        sizes = sorted((s.width(), s.height()) for s in icon.availableSizes())
        print("        可用尺寸:", sizes)

        need = [16, 20, 24, 32, 48, 64, 128, 256]
        missing = [s for s in need if (s, s) not in sizes]
        print("[%s] 常用尺寸齐全" % ("通过" if not missing else "失败"))
        if missing:
            print("        缺失:", missing)

        for size in (16, 24, 32, 48, 256):
            pm = icon.pixmap(size, size)
            status = "通过" if (pm.width(), pm.height()) == (size, size) else "失败"
            print("  [%s] 请求 %3dx%-3d -> 实得 %dx%d"
                  % (status, size, size, pm.width(), pm.height()))

        print()
        print("AppUserModelID 已设置:", set_app_user_model_id())

        # 内置兜底图标也必须可用（图标文件损坏时的最后防线）
        fb = _fallback_icon()
        print("[%s] 兜底图标可用（%d 个尺寸）"
              % ("通过" if (not fb.isNull() and fb.availableSizes()) else "失败",
                 len(fb.availableSizes())))
    print("=" * 66)
