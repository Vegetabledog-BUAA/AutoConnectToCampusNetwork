# -*- coding: utf-8 -*-
"""统一的对话框：按钮一律用中文，不出现 Yes / No / OK。

为什么需要这个模块：Qt 内置标准按钮的文案来自 Qt 自带的翻译文件，
没有加载 zh_CN 翻译时（PyInstaller 打包后通常就没加载）会显示成
Yes / No / OK —— 中文界面里混英文按钮很突兀。
这里把标准按钮取出来显式改名，既不依赖翻译文件，也不用自绘对话框。
"""

from PyQt5.QtWidgets import QMessageBox, QPushButton

# 默认按钮文案
OK_TEXT = "确定"
CANCEL_TEXT = "取消"


def _rename(box, button_role, text):
    """把标准按钮的文字改成中文。"""
    button = box.button(button_role)
    if button is not None:
        button.setText(text)
        # 按钮文字变宽后，用小尺寸提示更协调
        button.setStyleSheet("QPushButton { min-width: 68px; padding: 4px 12px; }")
    return button


def _base(parent, icon, title, text):
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    return box


def ask(parent, title, text, ok_text=OK_TEXT, cancel_text=CANCEL_TEXT,
        default_cancel=True):
    """确认对话框。返回 True 表示用户点了「确定」。

    默认把焦点给「取消」：这些都是删除日志、退出程序之类的不可逆操作，
    回车误触时保持不动比直接执行更安全。
    """
    box = _base(parent, QMessageBox.Question, title, text)
    box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    ok_button = _rename(box, QMessageBox.Ok, ok_text)
    cancel_button = _rename(box, QMessageBox.Cancel, cancel_text)
    box.setDefaultButton(cancel_button if default_cancel else ok_button)
    box.setEscapeButton(cancel_button)
    return box.exec_() == QMessageBox.Ok


def info(parent, title, text, ok_text=OK_TEXT):
    """信息提示。只有一个「确定」。"""
    box = _base(parent, QMessageBox.Information, title, text)
    box.setStandardButtons(QMessageBox.Ok)
    button = _rename(box, QMessageBox.Ok, ok_text)
    box.setDefaultButton(button)
    box.setEscapeButton(button)
    box.exec_()
    return True


def warn(parent, title, text, ok_text=OK_TEXT):
    """警告提示。只有一个「确定」。"""
    box = _base(parent, QMessageBox.Warning, title, text)
    box.setStandardButtons(QMessageBox.Ok)
    button = _rename(box, QMessageBox.Ok, ok_text)
    box.setDefaultButton(button)
    box.setEscapeButton(button)
    box.exec_()
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("对话框模块自检")
    print("=" * 60)
    print("确定按钮文案:", OK_TEXT)
    print("取消按钮文案:", CANCEL_TEXT)
    print()

    import sys
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    # 不真正弹窗（会阻塞），只检查按钮文案与角色是否正确
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    ok_button = _rename(box, QMessageBox.Ok, OK_TEXT)
    cancel_button = _rename(box, QMessageBox.Cancel, CANCEL_TEXT)

    failures = 0
    for label, button, want in (("确定", ok_button, OK_TEXT),
                                ("取消", cancel_button, CANCEL_TEXT)):
        got = button.text() if isinstance(button, QPushButton) else "(非按钮)"
        ok = got == want
        failures += 0 if ok else 1
        print("  [%s] %s 按钮文案 = %r" % ("通过" if ok else "失败", label, got))

    ok = box.button(QMessageBox.Ok) is not None
    failures += 0 if ok else 1
    print("  [%s] Ok 角色可正确取回按钮" % ("通过" if ok else "失败"))

    print()
    print("自检%s" % ("通过" if failures == 0 else "未通过"))
    sys.exit(0 if failures == 0 else 1)
