import os
import time
import subprocess
import socket
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from chromedriver_manager import check_chrome_chromedriver_matched

from logger import log

class NetworkChecker:
    def __init__(self, config):
        self.config = config
        self.driver = None
        self.is_running = False
        self.attempt_count = 0

    def initialize_driver(self):
        """初始化 ChromeDriver - 完全隐藏所有窗口"""
        if self.driver:
            return True
        try:
            driver_path = (self.config.get('chromedriver_path') or "").strip()
            driver_path = driver_path + "\\chromedriver.exe"
            if not driver_path or not os.path.isfile(driver_path):
                log("chromedriver_path 未配置或文件不存在", "ERROR")
                return False

            options = webdriver.ChromeOptions()
            
            # 无头模式
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1280,800")
            
            # 减少日志输出
            options.add_argument("--log-level=3")
            options.add_argument("--silent")
            options.add_experimental_option('excludeSwitches', ['enable-logging'])
            
            # 禁用自动化特征
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)

            # 关键：配置 Service 来隐藏命令行窗口
            service = Service(driver_path)
            
            # 设置服务参数来隐藏窗口
            service.creationflags = subprocess.CREATE_NO_WINDOW  # 这行是关键！
            
            self.driver = webdriver.Chrome(service=service, options=options)

            # 移除自动化特征
            try:
                self.driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {
                        "source": """
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined
                        })
                        """
                    }
                )
            except Exception:
                pass

            self.driver.set_page_load_timeout(20)
            log(f"无头 ChromeDriver 初始化成功（隐藏模式）", "INFO")
            return True
            
        except Exception as e:
            log(f"初始化 ChromeDriver 时出错: {e}", "ERROR")
            self.driver = None
            return False

    def _extract_host(self, test_url: str) -> str:
        if not test_url:
            return "kimi.moonshot.cn"
        if "://" in test_url:
            host = urlparse(test_url).hostname
            return host or test_url
        return test_url.split("/")[0]

    def check_network(self):
        """使用 ping 检查网络连通性"""
        test_url = self.config.get('test_url', 'https://kimi.moonshot.cn')
        host = self._extract_host(test_url)
        try:
            try:
                socket.gethostbyname(host)
            except Exception as e:
                log(f"DNS 解析失败: {host} {e}", "WARNING")

            proc = subprocess.run(
                ["ping", "-n", "1", "-w", "1500", host],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=subprocess.CREATE_NO_WINDOW  # 隐藏命令行窗口
            )
            ok = proc.returncode == 0
            if ok:
                log(f"网络正常: {host}", "INFO")
            else:
                log(f"网络异常，ping 失败: {host}", "WARNING")
            return ok
        except Exception as e:
            log(f"执行网络检查失败: {e}", "ERROR")
            return False

    def login(self):
        """简单登录尝试"""
        try:
            print("执行登录流程...")
            if self.driver is None and not self.initialize_driver():
                log("无法初始化浏览器，跳过登录", "ERROR")
                return False

            user_name = self.config.get('username', '')
            pwd = self.config.get('password', '')
            login_url = self.config.get('login_url', 'https://gw.buaa.edu.cn/')

            if not user_name or not pwd:
                log("用户名或密码缺失，跳过登录", "WARNING")
                return False

            log(f"尝试登录: {login_url}", "INFO")
            self.driver.get(login_url)
            # time.sleep(2)

            username_candidates = ["username", "userName", "uname", "loginName", "account"]
            password_candidates = ["password", "pwd", "pass", "passwd"]
            submit_candidates = ["login", "submit", "Log In", "登录", "登 录"]

            def try_fill(name_list, value):
                for name in name_list:
                    # by name
                    try:
                        el = self.driver.find_element(By.NAME, name)
                        el.clear()
                        el.send_keys(value)
                        return True
                    except Exception:
                        pass
                    # by id
                    try:
                        el = self.driver.find_element(By.ID, name)
                        el.clear()
                        el.send_keys(value)
                        return True
                    except Exception:
                        pass
                return False

            def try_click(candidates):
                # id/name
                for text in candidates:
                    # name
                    try:
                        el = self.driver.find_element(By.NAME, text)
                        el.click()
                        return True
                    except Exception:
                        pass
                    # id
                    try:
                        el = self.driver.find_element(By.ID, text)
                        el.click()
                        return True
                    except Exception:
                        pass
                # 按钮文字
                try:
                    buttons = self.driver.find_elements(By.TAG_NAME, "button")
                    for b in buttons:
                        if b.text.strip() in candidates:
                            b.click()
                            return True
                except Exception:
                    pass
                return False

            filled_user = try_fill(username_candidates, user_name)
            filled_pwd = try_fill(password_candidates, pwd)

            if not filled_user or not filled_pwd:
                log("填写登录表单失败", "WARNING")
                return False

            clicked = try_click(submit_candidates)
            if clicked:
                log("登录提交已点击", "INFO")
            else:
                log("未找到登录提交按钮", "WARNING")

            time.sleep(2)
            log("登录流程完成", "INFO")
            
            try:
                self.driver.quit()
                log("已自动关闭登录浏览器窗口", "INFO")
            except Exception as e:
                log(f"关闭登录浏览器窗口失败: {e}", "WARNING")
            finally:
                self.driver = None

            return True
        except Exception as e:
            log(f"登录时发生错误: {e}", "ERROR")
            # 异常时也尽量清理浏览器
            try:
                if self.driver:
                    self.driver.quit()
                    log("异常后已关闭浏览器", "INFO")
            except Exception:
                pass
            finally:
                self.driver = None
            return False

    def start_checking(self):
        """监控循环"""
        self.is_running = True
        self.attempt_count = 0
        interval = int(self.config.get('check_interval', 300))
        log("开始网络监控", "INFO")
        log(f"检查间隔: {interval} 秒", "INFO")

        while self.is_running:
            ok = self.check_network()
            check_chrome_chromedriver_matched(extra_para = ok)
            if not ok:
                self.attempt_count += 1
                log(f"尝试重连 (第 {self.attempt_count} 次)", "WARNING")
                self.login()
            time.sleep(interval)

        log("网络监控已停止", "INFO")

    def stop_checking(self):
        """停止监控与释放资源"""
        self.is_running = False
        log("正在停止网络监控...", "INFO")
        try:
            if self.driver:
                self.driver.quit()
        except Exception as e:
            log(f"关闭浏览器时错误: {e}", "WARNING")
        finally:
            self.driver = None