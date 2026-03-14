import os
import sys

try:
    import winshell
    from win32com.client import Dispatch
except Exception:
    winshell = None
    Dispatch = None


SHORTCUT_NAME = "AutoConnect.lnk"


def _get_shortcut_path():
    """获取启动文件夹中的快捷方式路径"""
    startup_folder = winshell.startup()
    return os.path.join(startup_folder, SHORTCUT_NAME)


def _is_frozen_exe():
    """是否为打包后的 exe 运行环境"""
    return getattr(sys, "frozen", False)


def setup_autostart(enable=True):
    """
    设置开机自启动
    仅允许打包后的 exe 创建自启动，避免 .py + python.exe 导致黑框
    """
    if winshell is None or Dispatch is None or os.name != "nt":
        return False, "当前环境不支持自启动"

    try:
        shortcut_path = _get_shortcut_path()

        if enable:
            # 只允许 exe 创建启动项
            if not _is_frozen_exe():
                return False, "请运行打包后的 exe 后再启用开机自启动"

            exe_path = os.path.abspath(sys.executable)
            working_dir = os.path.dirname(exe_path)

            shell = Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(shortcut_path)
            shortcut.Targetpath = exe_path
            shortcut.Arguments = "--tray"
            shortcut.WorkingDirectory = working_dir
            shortcut.IconLocation = exe_path
            shortcut.WindowStyle = 7  # 最小化/后台方式启动
            shortcut.save()

            return True, "开机自启动设置成功（已指向打包后的 exe）"

        else:
            if os.path.exists(shortcut_path):
                os.remove(shortcut_path)
            return True, "开机自启动已禁用"

    except Exception as e:
        return False, f"设置开机自启动失败: {e}"


def check_autostart_status():
    """检查开机自启动是否已启用"""
    if winshell is None or os.name != "nt":
        return False
    try:
        shortcut_path = _get_shortcut_path()
        return os.path.exists(shortcut_path)
    except Exception:
        return False


def can_enable_autostart():
    """当前环境是否允许启用自启动"""
    return (
        winshell is not None
        and Dispatch is not None
        and os.name == "nt"
        and _is_frozen_exe()
    )