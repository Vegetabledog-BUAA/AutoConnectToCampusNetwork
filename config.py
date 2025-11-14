import json
import os
import ubelt as ub
from crypto_utils import encrypt_data, decrypt_data
from logger import log

dpath = ub.ensure_app_cache_dir('AutoConnect_chromedriver')
CONFIG_FILE = os.path.join(dpath, "config.json")
LOG_FILE = os.path.join(dpath, 'auto_connect.log')

def load_config():
    """加载配置文件，如果不存在则创建默认配置"""
    default_config = {
        "username": "",
        "password": "",
        "check_interval": 300,
        "test_url": "https://kimi.moonshot.cn",
        "login_url": "https://gw.buaa.edu.cn/",
        "log_file_path": LOG_FILE,
        "chrome_version": "",
        "chromedriver_path": "",
        "chromedriver_version": ""
    }
    
    if not os.path.exists(CONFIG_FILE):
        save_config(default_config)
        return default_config
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # 解密用户名和密码
            if config.get("username"):
                config["username"] = decrypt_data(config["username"])
            if config.get("password"):
                config["password"] = decrypt_data(config["password"])
            return config
    except Exception as e:
        log(f"加载配置失败: {e}", "ERROR")
        return default_config
    
def save_config(config):
    """保存配置到文件"""
    try:
        # 加密用户名和密码
        if config.get("username"):
            config["username"] = encrypt_data(config["username"])
        if config.get("password"):
            config["password"] = encrypt_data(config["password"])

        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        log(f"保存配置失败: {e}", "ERROR")
        
if __name__ == "__main__":
    cfg = load_config()
    print("当前配置:", cfg)