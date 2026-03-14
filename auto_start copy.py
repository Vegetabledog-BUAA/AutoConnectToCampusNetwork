import os
import sys
try:
    import winshell
    from win32com.client import Dispatch
except Exception:
    winshell = None
    Dispatch = None

def setup_autostart(enable=True):
    """设置开机自启动（仅 Windows 有效）"""
    if winshell is None or Dispatch is None or os.name != "nt":
        return False, "当前环境不支持自启动"
    try:
        startup_folder = winshell.startup()
        shortcut_name = "NetworkChecker.lnk"
        shortcut_path = os.path.join(startup_folder, shortcut_name)

        if enable:
            if getattr(sys, 'frozen', False):
                application_path = sys.executable
                arguments = "--tray"
                working_dir = os.path.dirname(application_path)
            else:
                application_path = sys.executable
                script_path = os.path.abspath(sys.argv[0])
                arguments = f'"{script_path}" --tray'
                working_dir = os.path.dirname(script_path)

            shell = Dispatch('WScript.Shell')
            shortcut = shell.CreateShortCut(shortcut_path)
            shortcut.Targetpath = application_path
            shortcut.Arguments = arguments
            shortcut.WorkingDirectory = working_dir
            shortcut.IconLocation = application_path
            shortcut.save()
            return True, "开机自启动设置成功（托盘模式）"
        else:
            if os.path.exists(shortcut_path):
                os.remove(shortcut_path)
            return True, "开机自启动已禁用"
    except Exception as e:
        return False, f"设置开机自启动失败: {e}"

def check_autostart_status():
    if winshell is None or os.name != "nt":
        return False
    startup_folder = winshell.startup()
    shortcut_path = os.path.join(startup_folder, "NetworkChecker.lnk")
    return os.path.exists(shortcut_path)