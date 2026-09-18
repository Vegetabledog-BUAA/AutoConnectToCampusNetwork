
import os
import time
import threading
import subprocess
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from chromedriver_manager import (
    ensure_chromedriver_ready,
    find_driver_exe,
    resolve_driver_folder,
)

from logger import log, log_with_notification

# 无头浏览器空闲多久后释放（秒）。期间复用同一实例，省掉每次重连重启 Chrome 的开销
DRIVER_IDLE_TTL = 600
# 登录表单出现的最长等待时间（秒）。登录页是 meta refresh 跳转，必须显式等待
LOGIN_FORM_TIMEOUT = 10
# 提交登录后确认在线的轮询上限（秒）
LOGIN_VERIFY_TIMEOUT = 8
# ping 的等待时间（毫秒）与硬超时（秒）
PING_REPLY_TIMEOUT_MS = 1000
PING_HARD_TIMEOUT = 3

# 检查间隔的兜底范围（秒）。配置被写坏成 1 秒时，监控循环会变成死循环猛敲校园网门户。
CHECK_INTERVAL_MIN = 10
CHECK_INTERVAL_MAX = 7200

# 两次"真实登录尝试"之间的最小间隔（秒）。这是防止把校园网账号打爆的硬保险：
# 即使检查间隔被设成 1 秒，也不会真的每秒去提交一次登录。
MIN_LOGIN_INTERVAL = 60
# 连续登录失败时的退避上限（秒）
MAX_BACKOFF = 1800

USERNAME_CANDIDATES = ["username", "userName", "uname", "loginName", "account"]
PASSWORD_CANDIDATES = ["password", "pwd", "pass", "passwd"]
SUBMIT_CANDIDATES = ["login", "submit", "Log In", "登录", "登 录"]
CAPTCHA_CANDIDATES = ["captcha", "vcode", "verifyCode", "verify_code", "code"]


class NetworkChecker:
    def __init__(self, config):
        self.config = config
        self.driver = None
        self.is_running = False
        self.attempt_count = 0
        # 可中断等待用的信号：停止监控时立即唤醒监控循环，不再等满检查间隔
        self._stop_event = threading.Event()
        self._driver_last_used = 0.0
        # 登录限流与失败退避
        self._last_login_at = 0.0
        self._backoff = 0

    # ---------------- 浏览器管理 ----------------

    def _driver_folder(self):
        """解析驱动目录，与自动更新共用同一套解析逻辑。

        这里**绝不写配置文件**。解析是确定性的（路径不存在就回落到默认目录），
        每轮重新算一遍即可。曾经在这里顺手 `save_config(self.config)`，
        而 Python 会先求值实参，于是任何"拿着临时 dict 创建 NetworkChecker"的调用方
        （包括回归测试脚本）都会把那份临时配置整体覆盖到用户真实 config.json 上。
        """
        return resolve_driver_folder(self.config)[0]

    def initialize_driver(self):
        """初始化 ChromeDriver - 完全隐藏所有窗口"""
        if self.driver:
            return True
        try:
            driver_path = find_driver_exe(self._driver_folder())
            if not os.path.isfile(driver_path):
                log_with_notification(
                    f"ChromeDriver 不存在: {driver_path}。请确认驱动目录配置正确，"
                    "或等待程序在联网状态下自动下载",
                    "ERROR", "配置错误")
                return False

            options = Options()

            # 无头模式
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1280,800")

            # 减少启动开销与无关后台活动
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-sync")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-component-update")
            # DOM 就绪即返回，不等图片等资源加载完（元素可用性由显式等待保证）
            options.page_load_strategy = "eager"

            # 减少日志输出
            options.add_argument("--log-level=3")
            options.add_argument("--silent")
            options.add_experimental_option('excludeSwitches', ['enable-logging'])

            # 禁用自动化特征
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)

            # 关键：配置 Service 来隐藏命令行窗口
            # 注意：Selenium 4 读取的属性名是 creation_flags（下划线），
            # 写成 creationflags 不会生效，控制台窗口依然会闪现。
            service = Service(driver_path)
            service.creation_flags = subprocess.CREATE_NO_WINDOW

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
            self._driver_last_used = time.time()
            log("无头 ChromeDriver 初始化成功（隐藏模式）", "INFO")
            return True

        except Exception as e:
            log_with_notification(f"初始化 ChromeDriver 时出错: {e}", "ERROR", "配置错误")
            self.driver = None
            return False

    def _release_driver(self):
        """关闭浏览器并清空引用"""
        driver, self.driver = self.driver, None
        if driver is None:
            return
        try:
            driver.quit()
            log("已关闭登录浏览器", "INFO")
        except Exception as e:
            log(f"关闭浏览器失败: {e}", "WARNING")

    def _release_driver_if_idle(self):
        """长时间未使用时释放浏览器，回收内存"""
        if self.driver is None:
            return
        if time.time() - self._driver_last_used >= DRIVER_IDLE_TTL:
            log(f"浏览器空闲已超过 {DRIVER_IDLE_TTL} 秒，释放以回收内存", "INFO")
            self._release_driver()

    # ---------------- 网络检查 ----------------

    def _extract_host(self, test_url: str) -> str:
        if not test_url:
            return "kimi.moonshot.cn"
        if "://" in test_url:
            host = urlparse(test_url).hostname
            return host or test_url
        return test_url.split("/")[0]

    def check_network(self, quiet=False):
        """使用 ping 检查网络连通性。

        去掉了原来的 DNS 预解析（它只为打日志，但 DNS 被劫持时可能拖慢好几秒），
        并给 ping 加上硬超时，保证本步骤不会卡住整个监控循环。
        """
        test_url = self.config.get('test_url', 'https://kimi.moonshot.cn')
        host = self._extract_host(test_url)
        try:
            proc = subprocess.run(
                ["ping", "-n", "1", "-w", str(PING_REPLY_TIMEOUT_MS), host],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=PING_HARD_TIMEOUT,
                creationflags=subprocess.CREATE_NO_WINDOW  # 隐藏命令行窗口
            )
            ok = proc.returncode == 0
            if quiet:
                return ok
            if ok:
                log(f"网络正常: {host}", "INFO")
            else:
                log_with_notification(f"网络异常，ping 失败: {host}", "WARNING", "网络警告")
            return ok
        except subprocess.TimeoutExpired:
            if not quiet:
                log_with_notification(f"网络检查超时: {host}", "WARNING", "网络警告")
            return False
        except Exception as e:
            if not quiet:
                log_with_notification(f"执行网络检查失败: {e}", "ERROR", "网络错误")
            return False

    # ---------------- 登录 ----------------

    def _find_element(self, candidates, by_list=(By.ID, By.NAME)):
        """按候选 id/name 查找元素，返回第一个可见元素"""
        for name in candidates:
            for by in by_list:
                try:
                    element = self.driver.find_element(by, name)
                except Exception:
                    continue
                try:
                    if element.is_displayed():
                        return element
                except Exception:
                    continue
        return None

    def _wait_for_login_form(self, timeout=LOGIN_FORM_TIMEOUT):
        """等待登录表单出现。登录页存在 meta refresh 跳转，不能取完页面就找元素"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_running:
                return False
            if self._find_element(USERNAME_CANDIDATES) is not None:
                return True
            time.sleep(0.2)
        return False

    def _captcha_required(self):
        """判断登录页是否要求输入验证码。

        门户默认隐藏验证码面板，只有服务端要求时才会显示；
        一旦要求，自动登录不可能成功，必须让用户手动处理。
        """
        return self._find_element(CAPTCHA_CANDIDATES) is not None

    def _fill(self, candidates, value):
        element = self._find_element(candidates)
        if element is None:
            return False
        try:
            element.clear()
            element.send_keys(value)
            return True
        except Exception:
            return False

    def _click_submit(self):
        """点击登录按钮，优先按 id/name，再按按钮文字"""
        for text in SUBMIT_CANDIDATES:
            for by in (By.ID, By.NAME):
                try:
                    self.driver.find_element(by, text).click()
                    return True
                except Exception:
                    continue
        try:
            for button in self.driver.find_elements(By.TAG_NAME, "button"):
                if button.text.strip() in SUBMIT_CANDIDATES:
                    button.click()
                    return True
        except Exception:
            pass
        return False

    def _wait_until_online(self, timeout=LOGIN_VERIFY_TIMEOUT):
        """轮询确认登录是否真的生效，成功立即返回（不必等满超时）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.check_network(quiet=True):
                return True
            time.sleep(1)
        return False

    def _do_login(self, user_name, pwd, login_url):
        driver = self.driver
        log(f"尝试登录: {login_url}", "INFO")
        driver.get(login_url)

        if not self._wait_for_login_form():
            log_with_notification("登录页表单未加载出来", "ERROR", "浏览器错误")
            return False

        if self._captcha_required():
            log_with_notification(
                "登录页要求输入验证码（连续登录失败后会被触发），自动登录已暂停。"
                f"请手动打开 {login_url} 完成一次登录，之后验证码会自动解除",
                "ERROR",
                "需要人工处理",
            )
            return False

        if not self._fill(USERNAME_CANDIDATES, user_name):
            log_with_notification("填写用户名失败", "ERROR", "配置错误")
            return False
        if not self._fill(PASSWORD_CANDIDATES, pwd):
            log_with_notification("填写密码失败", "ERROR", "配置错误")
            return False

        if not self._click_submit():
            log_with_notification("未找到登录提交按钮", "ERROR", "浏览器错误")
            return False

        self._driver_last_used = time.time()
        log("登录提交已点击", "INFO")

        # 确认是否真的登录成功，不再无条件报"登录流程完成"
        if self._wait_until_online():
            log_with_notification("登录成功，网络已恢复", "INFO", "网络重连")
            return True
        log_with_notification(
            "已提交登录但未检测到网络恢复，请检查账号密码是否正确", "WARNING", "网络警告")
        return False

    def login(self):
        """登录尝试：复用无头浏览器 + 显式等待 + 结果校验"""
        if not self.is_running:
            return False

        user_name = (self.config.get('username') or "").strip()
        pwd = self.config.get('password') or ""
        login_url = self.config.get('login_url', 'https://gw.buaa.edu.cn/')

        if not user_name or not pwd:
            log_with_notification("用户名或密码缺失，跳过登录", "WARNING", "配置警告")
            return False

        # 浏览器会话失效时重建一次再试，避免一次崩溃就整轮放弃
        for attempt in (1, 2):
            if not self.is_running:
                log("监控已停止，取消本次登录", "INFO")
                return False
            if self.driver is None and not self.initialize_driver():
                log_with_notification("无法初始化浏览器，跳过登录", "ERROR", "配置错误")
                return False
            try:
                return self._do_login(user_name, pwd, login_url)
            except WebDriverException as e:
                log(f"浏览器会话异常: {e}", "WARNING")
                self._release_driver()
                if attempt == 2:
                    log_with_notification(f"登录时发生错误: {e}", "ERROR", "浏览器错误")
                    return False
            except Exception as e:
                log_with_notification(f"登录时发生错误: {e}", "ERROR", "网络错误")
                self._release_driver()
                return False
        return False

    # ---------------- 监控循环 ----------------

    def _run_once(self):
        """跑一轮检查：网络 -> 必要时登录 -> 顺手对齐驱动

        驱动检查特意放在登录之后：冷启动时查询 Chrome 版本要向 PowerShell 问一次、
        并遍历 Chrome 安装目录，约 3~4 秒。放在登录之前会拖慢"尽快恢复上网"这条关键路径，
        也会拉长"停止监控"时等待当前轮结束的时间。
        """
        ok = self.check_network()

        if not ok and self.is_running:
            self._try_relogin()
            if self.check_network(quiet=True):
                ok = True  # 已恢复上网，后面允许联网对齐驱动

        # ChromeDriver 检查/更新：best-effort，失败只记日志。
        # 不再用它的返回值去决定"要不要登录"——旧实现把两者与在一起，
        # 导致断网（extra_para=False）时既不更新驱动、也不尝试登录，形成死锁。
        # 这里再单独套一层兜底，确保驱动检查的任何异常都不会妨碍登录。
        try:
            ready, driver_message = ensure_chromedriver_ready(
                self._driver_folder(), allow_download=ok)
            if not ready:
                log(f"ChromeDriver 未就绪：{driver_message}（仍会继续尝试登录）", "WARNING")
        except Exception as e:
            log(f"ChromeDriver 检查发生异常，已忽略并继续: {e}", "ERROR")

        self._release_driver_if_idle()

    def _try_relogin(self):
        """尝试重连，带登录限流与失败退避。

        限流是硬保险：无论检查间隔被设成多少，两次真实登录之间至少间隔
        MIN_LOGIN_INTERVAL 秒。否则一旦配置被写坏（例如间隔变成 1 秒），
        程序会在几小时内对校园网门户提交几千次登录，既可能触发验证码，
        也可能把账号打到风控里。
        """
        now = time.time()
        waited = now - self._last_login_at
        if self._last_login_at and waited < MIN_LOGIN_INTERVAL:
            remain = int(MIN_LOGIN_INTERVAL - waited)
            log(f"距上次登录尝试仅 {int(waited)} 秒，本轮跳过（至少间隔 {MIN_LOGIN_INTERVAL} 秒，"
                f"剩余 {remain} 秒）", "WARNING")
            return

        self.attempt_count += 1
        self._last_login_at = now
        log_with_notification(f"尝试重连 (第 {self.attempt_count} 次)", "WARNING", "网络警告")

        if self.login():
            self.attempt_count = 0
            self._backoff = 0
            return

        # 连续失败则指数退避，避免一直高频重试
        self._backoff = min(max(self._backoff * 2, MIN_LOGIN_INTERVAL), MAX_BACKOFF)
        log_with_notification(
            f"已连续失败 {self.attempt_count} 次，下次重试间隔延长到 {self._backoff} 秒；"
            "若持续失败请检查账号密码与测试网址是否正确",
            "WARNING", "网络警告")

    def start_checking(self):
        """监控循环（等待可被 stop_checking 立即打断）"""
        self.is_running = True
        self.attempt_count = 0
        self._stop_event.clear()

        try:
            interval = int(self.config.get('check_interval', 300))
        except (TypeError, ValueError):
            interval = 300
        if not (CHECK_INTERVAL_MIN <= interval <= CHECK_INTERVAL_MAX):
            log(f"检查间隔 {interval} 秒超出允许范围 "
                f"[{CHECK_INTERVAL_MIN}, {CHECK_INTERVAL_MAX}]，已按 {CHECK_INTERVAL_MIN} 秒执行",
                "WARNING")
            interval = max(CHECK_INTERVAL_MIN, min(interval, CHECK_INTERVAL_MAX))

        log("开始网络监控", "INFO")
        log(f"检查间隔: {interval} 秒", "INFO")

        while self.is_running:
            # 整轮兜底：任何未预期的异常都只记日志，绝不让监控线程静默退出
            try:
                self._run_once()
            except Exception as e:
                log_with_notification(
                    f"监控循环本轮发生异常，已跳过本次检查: {e}", "ERROR", "监控错误")

            # 可中断等待：停止监控时立刻返回，不再等满整个检查间隔。
            # 连续失败时按退避时间等待，避免持续打扰校园网门户。
            wait_seconds = max(interval, self._backoff)
            if self._stop_event.wait(wait_seconds):
                break

        self._stop_event.clear()
        self._release_driver()
        log("网络监控已停止", "INFO")

    def stop_checking(self):
        """停止监控。

        只做通知，不在这里关浏览器 —— 关闭浏览器要 0.5 秒以上，
        而这个方法是从 GUI 主线程调用的，同步关闭会造成界面卡顿。
        资源释放交给监控线程在循环退出时完成（通常几十毫秒内）。
        """
        self.is_running = False
        self._stop_event.set()
        log("正在停止网络监控...", "INFO")


if __name__ == "__main__":
    cfg = {
        "test_url": "https://www.taobao.com/",
        "login_url": "https://gw.buaa.edu.cn/",
        "check_interval": 300,
        "username": "",
        "password": "",
    }
    checker = NetworkChecker(cfg)
    print("检查间隔配置  :", checker.config["check_interval"])
    print("探测主机      :", checker._extract_host(cfg["test_url"]))

    start = time.time()
    online = checker.check_network()
    print("网络检查结果  :", online, "| 耗时 %.2f 秒" % (time.time() - start))

    start = time.time()
    checker.stop_checking()
    print("停止请求耗时  : %.4f 秒（应为毫秒级）" % (time.time() - start))

    print("\n--- 可中断等待验证（模拟间隔 300 秒时点停止）---")
    checker.is_running = True
    thread = threading.Thread(target=checker.start_checking, daemon=True)
    start = time.time()
    thread.start()
    time.sleep(1.0)
    checker.stop_checking()
    thread.join(timeout=10)
    print("从停止请求到线程退出: %.2f 秒" % (time.time() - start))
    print("线程已退出    :", not thread.is_alive())
