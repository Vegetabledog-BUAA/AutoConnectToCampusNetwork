import latest_chromedriver
import ubelt as ub
from logger import log 

def check_chrome_chromedriver_matched(extra_para = True):
    chrome_version = latest_chromedriver.chrome_info.get_version()
    dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
    chromedriver_version  = latest_chromedriver.download_driver.get_version(dpath)
    if chrome_version and chromedriver_version:
        major_chrome_version = chrome_version.split('.')
        major_chromedriver_version = chromedriver_version.split('.')
        if major_chrome_version[0] != major_chromedriver_version[0] and major_chrome_version[1] != major_chromedriver_version[1] and major_chrome_version[2] != major_chromedriver_version[2]:
            if extra_para:
                latest_chromedriver.download_only_if_needed(chromedriver_folder=dpath)
                log("检测到 ChromeDriver 版本与 Chrome 浏览器不匹配，已自动更新 ChromeDriver", "INFO")
            else:
                log("检测到 ChromeDriver 版本与 Chrome 浏览器不匹配，但无网络连接，无法自动更新 ChromeDriver，若无法自动连接网络请重新手动连接网络", "INFO")
    elif not chrome_version:
        log("无法获取 Chrome 浏览器版本信息，无法检查 ChromeDriver 版本匹配情况", "WARNING")
    else:
        if extra_para:
            log("无法获取 ChromeDriver 版本信息，将尝试重新下载 ChromeDriver", "WARNING")
            latest_chromedriver.download_only_if_needed(chromedriver_folder=dpath)
            log("下载 ChromeDriver成功", "INFO")
        else:
            log("无法获取 ChromeDriver 版本信息，且无网络连接，无法重新下载 ChromeDriver", "WARNING")

if __name__ == "__main__":
    check_chrome_chromedriver_matched()