import latest_chromedriver
import ubelt as ub
from logger import log, log_with_notification 

def check_chrome_chromedriver_matched(extra_para = True):
    chrome_version = latest_chromedriver.chrome_info.get_version()
    dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
    chromedriver_version  = latest_chromedriver.download_driver.get_version(dpath)
    if chrome_version and chromedriver_version:
        major_chrome_version = chrome_version.split('.')
        major_chromedriver_version = chromedriver_version.split('.')
        if major_chrome_version[0] != major_chromedriver_version[0] or major_chrome_version[1] != major_chromedriver_version[1] or major_chrome_version[2] != major_chromedriver_version[2]:
            if extra_para:
                latest_chromedriver.download_only_if_needed(chromedriver_folder=dpath)
                log_with_notification("检测到 ChromeDriver 版本与 Chrome 浏览器不匹配，已自动更新 ChromeDriver", "WARNING", "配置警告")
                return True
            else:
                log_with_notification("检测到 ChromeDriver 版本与 Chrome 浏览器不匹配，但无网络连接，无法自动更新 ChromeDriver，若无法自动连接网络请重新手动连接网络", "WARNING", "配置警告")
                return False
        return True
    elif not chrome_version:
        if extra_para:
            log_with_notification("无法获取 Chrome 浏览器版本信息，无法检查 ChromeDriver 版本匹配情况，将自动下载最新版本 ChromeDriver ", "WARNING", "配置警告")
            latest_chromedriver.download_only_if_needed(chromedriver_folder=dpath)
            log_with_notification("下载 ChromeDriver成功", "INFO", "配置通知")
            return True
        else:
            log_with_notification("无法获取 Chrome 浏览器版本信息，且无网络连接，无法重新下载 ChromeDriver", "WARNING", "配置警告")
            return False
    else:
        if extra_para:
            log_with_notification("无法获取 ChromeDriver 版本信息，将尝试重新下载 ChromeDriver", "WARNING", "配置警告")
            latest_chromedriver.download_only_if_needed(chromedriver_folder=dpath)
            log_with_notification("下载 ChromeDriver成功", "INFO", "配置通知")
            return True
        else:
            log_with_notification("无法获取 ChromeDriver 版本信息，且无网络连接，无法重新下载 ChromeDriver", "WARNING", "配置警告")
            return False

if __name__ == "__main__":
    check_chrome_chromedriver_matched()