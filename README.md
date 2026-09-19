# AutoConnectToCampusNetwork

> 北航校园网自动登录 / 断线自动重连工具 · Windows

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Selenium](https://img.shields.io/badge/Selenium-4.38-43B02A?logo=selenium&logoColor=white)
![PyQt5](https://img.shields.io/badge/PyQt5-5.15-41CD52?logo=qt&logoColor=white)
![License](https://img.shields.io/badge/License-Apache%202.0-blue)

程序常驻系统托盘，按设定间隔检查网络连通性；检测到掉线时自动打开校园网认证页完成登录，
并轮询确认网络**真实恢复**后才判定成功。同时自动维护与当前 Chrome 版本匹配的 ChromeDriver，
避免 Chrome 升级后驱动失效导致工具不可用。

---

## 目录

- [Features](#features)
- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Usage](#usage)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)
- [Development](#development)
- [Known Limitations](#known-limitations)
- [Contributing](#contributing)
- [License](#license)

---

## Features

| 能力 | 说明 |
|---|---|
| 断线自动重连 | 网络不通时自动提交登录，并轮询校验真实恢复，失败则进入退避重试 |
| 登录限流与退避 | 两次真实登录至少间隔 60 秒；连续失败按 60 → 120 → … → 1800 秒指数退避 |
| ChromeDriver 自维护 | 四路探测 Chrome 版本（注册表优先，毫秒级），自动下载匹配驱动 |
| 驱动旁路安装 | 旧驱动被残留进程或杀软占用时，落为 `chromedriver_<版本>.exe` 并记录状态，保证加载的确实是新驱动 |
| 浏览器实例复用 | 空闲 10 分钟内复用同一 Chrome 实例，省去每次重连重启浏览器的开销 |
| 按天日志与保留策略 | `logs/auto_connect_YYYY-MM-DD.log`，跨天自动切文件，超期自动清理 |
| 内置日志管理器 | 图形界面内浏览 / 查看 / 打开 / 删除任意一天的日志 |
| 环境诊断 | `AutoConnect.exe --doctor` 生成可直接粘贴的环境报告 |
| 凭据加密存储 | 账号密码 AES-256-CBC 加密，密钥由本机硬件标识派生，配置内保存密钥指纹用于一致性校验 |
| 可中断监控循环 | 停止监控毫秒级响应，不必等满一个检查周期 |
| 单实例保护 | 命名互斥体保证同一时间只有一个实例；重复启动会把已有实例的主界面唤起，不会产生第二个监控线程 |
| 检查更新 | 启动后自动检查（间隔可选：每次启动 / 每天 / 每 3 天 / 每周 / 关闭，默认每天），也可在配置页手动检查；发现新版给出更新说明与下载入口 |
| 图标与版本信息 | 窗口 / 任务栏 / 托盘 / 快捷方式共用同一份多尺寸图标（16~256），exe 带版本资源，可在「属性 → 详细信息」查看 |

---

## How It Works

```
                    ┌──────────────────────────┐
                    │   托盘 / 图形界面 (PyQt5)  │
                    └────────────┬─────────────┘
                                 │ 控制信号 + 日志信号
                    ┌────────────▼─────────────┐
        循环检查 ──▶ │  NetworkChecker 监控线程   │
                    └───────┬──────────┬───────┘
                            │          │
              网络不通时登录 │          │ 顺手对齐驱动
                            ▼          ▼
                ┌────────────────┐  ┌──────────────────────┐
                │  Selenium 无头  │  │ chromedriver_manager │
                │  Chrome 登录    │  │ 版本探测 + 下载安装    │
                └───────┬────────┘  └──────────────────────┘
                        │
                        ▼
                ┌────────────────┐
                │ 校园网认证门户   │
                └────────────────┘
```

关键设计取舍：

- **驱动检查不阻塞登录。** 驱动对齐是 best-effort，任何异常只记日志；是否登录只取决于网络是否连通。
- **读配置的解析函数无写副作用。** 所有配置写入集中在 `config.save_config()`，并带字段完整性校验与原子写。
- **后台线程不直接操作 Qt 对象。** 通知经信号投递回 GUI 线程，并对同内容做节流。

---

## Quick Start

### Installer

从 [安装包](https://bhpan.buaa.edu.cn/link/AAF517447738684467B1D19FE740A288A6) 下载安装后：

1. 首次启动为托盘模式，右键托盘图标 → 打开主界面。
2. 填写用户名、密码，点击「保存配置」。
3. **在网络正常的情况下**点击「开始监控」，程序会自动下载匹配的 ChromeDriver（首次可能较慢）。
4. 如需开机自启，勾选「开机自动启动」（仅打包版可用）。

### From Source

```bash
git clone https://github.com/Vegetabledog-BUAA/AutoConnectToCampusNetwork.git
cd AutoConnectToCampusNetwork
pip install -r requirement.txt

python main.py --gui      # 打开图形界面
python main.py --tray     # 托盘模式（默认）
python main.py --doctor   # 环境诊断
```

> `crypto_utils.py` 未纳入版本控制，源码运行前请先按 [For Forkers](#for-forkers) 说明准备该模块。

---

## Usage

### 配置项

| 项目 | 默认值 | 说明 |
|---|---|---|
| 用户名 / 密码 | — | 校园网账号，保存时加密写入本地配置 |
| 登录网址 | `https://gw.buaa.edu.cn/` | 认证门户地址 |
| 检查间隔 | 300 秒 | 界面可调范围 10~3600 秒；程序内部另有 60 秒登录硬下限 |
| 测试网址 | `https://kimi.moonshot.cn` | 用于判断外网连通性，建议填稳定且响应快的站点 |
| 日志保留天数 | 7 天 | 含今天在内共保留 N 天 |
| 自动检查更新 | 每天 | 可选「每次启动 / 每天 / 每 3 天 / 每周 / 关闭」。选「关闭」只是不做自动检查，「检查更新」按钮始终可用 |
| 开机自动启动 | 关闭 | 仅打包版可用 |

> 「自动检查更新」的实际规则：启动后约 20 秒检查一次，距上次检查未满所选间隔就跳过，
> 跳过时会在日志里写明原因（例如「距上次检查不足 1 天（上次 09-18 21:30），本轮自动跳过」），
> 不会出现"日志里有一条检查记录却说不出结果"的情况。

### 日志

日志按天分文件写入数据目录下的 `logs/`，启动时按保留天数清理过期文件。

图形界面的日志页为左右分栏：左侧列出各日志文件（含大小），右侧展示内容。

- 单击左侧条目 → 右侧显示该文件；双击 → 用系统默认程序打开
- 「刷新显示」重新载入最近若干天日志；「暂停滚动」冻结实时日志滚动
- 「删除」不保留地删除选中文件，包含正在写入的当天日志
- 打开界面时默认回显最近 3 天，每个文件显示尾部 300 行

### CLI Reference

| 参数 | 作用 |
|---|---|
| *(无)* | 启动托盘模式 |
| `--gui` | 打开图形界面 |
| `--tray` | 仅托盘模式 |
| `--auto` | 前台运行监控循环，无界面 |
| `--doctor` | 生成环境诊断报告至 `doctor.txt` 并提示文件位置（诊断工具，不受单实例限制） |

### 数据目录

默认位于 `%LOCALAPPDATA%\AutoConnect_chromedriver\`：

| 路径 | 内容 |
|---|---|
| `config.json` | 配置；账号密码为密文，另有 `key_fp` 密钥指纹 |
| `logs\auto_connect_YYYY-MM-DD.log` | 按天日志 |
| `chromedriver.exe` | 当前使用的 ChromeDriver |
| `driver_state.json` | 驱动旁路安装时的记录 |
| `doctor.txt` | `--doctor` 生成的诊断报告 |

> `config.json` 中仅账号密码加密，网址与间隔等参数为明文。

---

## Troubleshooting

| 现象 | 处理方式 |
|---|---|
| `session not created: This version of ChromeDriver only supports Chrome version XXX` | 联网状态下「停止监控」再「开始监控」，程序会重新对齐驱动 |
| 日志提示「登录页要求输入验证码，自动登录已暂停」 | 门户在连续失败后会要求验证码，此时自动登录无法完成；手动登录一次门户即可解除，再重新开始监控 |
| 重连持续失败 | 确认是否开启了 VPN（会导致无法访问校园网门户）；确认「测试网址」为可访问的真实站点；查看日志页报错 |
| 日志占用磁盘 | 调小「日志保留天数」，下次启动自动清理；或在日志页左侧删除指定文件 |
| 换机 / 重装系统后账号读不出来 | 密钥由本机硬件标识派生，旧密文无法解密。程序会明确报错并拒绝加载，重新填写账号密码保存一次即可 |
| 界面账号密码为空，保存会覆盖原凭据吗 | 不会。检测到为空时会沿用磁盘上已有的密文，仅提示不覆盖 |
| 双击图标后界面没出来 | 程序已在运行时会直接唤起已有实例的主界面；若仍未出现，请查看系统托盘图标。同一时间只允许一个实例 |

排查驱动与版本问题时，请先执行 `--doctor` 并在反馈中附上生成的 `doctor.txt`。报告包含：
两个驱动目录是否一致、Chrome 四个来源分别读到的版本、实际加载的驱动与版本、主版本是否匹配、
下载地址，以及最近 3 天日志。

---

## Project Structure

```
AutoConnectToCampusNetwork/
├── main.py                      程序入口、命令行参数、--doctor
├── config.py                    配置读写：加解密、字段校验、原子写、密钥指纹
├── crypto_utils.py              加密实现（未纳入版本控制）
├── crypto_utils_without_key.py  公开模板，隐去密钥结构
├── logger.py                    日志系统：按天分文件、保留策略、读取与删除接口
├── network_checker.py           监控循环与登录逻辑：限流、退避、驱动对齐、浏览器复用
├── chromedriver_manager.py      Chrome / ChromeDriver 版本探测与驱动下载安装
├── tray_icon.py                 系统托盘
├── ui.py                        图形界面：配置页与日志页
├── dialogs.py                   统一的对话框（中文按钮：确定 / 取消）
├── app_icon.py                  应用图标：统一解析并挂到窗口、任务栏、托盘
├── auto_start.py                开机自启（注册表 / 启动目录）
├── single_instance.py           单实例保护（命名互斥体 + 唤起主界面）
├── version.py                   版本号单一来源 + 更新清单地址 + 版本资源生成
├── updater.py                   更新检查（多源回退、节流、结果缓存）
├── latest.json                  更新清单（发布新版本时改这里）
├── version_info.txt             写进 exe 的版本资源（由 version.py --sync 生成）
├── icon.ico                     应用图标（含 16~256 全部尺寸）
├── icon_original.ico            图标原图备份
├── build_icon.py                由原图重新生成 icon.ico（补齐各尺寸）
├── AutoConnect.spec             PyInstaller 打包配置
├── build.py                     打包脚本
└── package/
    └── AutoConnectInnoSetupScriptFiles.iss   Inno Setup 安装包脚本
```

---

## Development

### Self-check Entry Points

下列模块自带 `if __name__ == "__main__":` 入口，可单独运行查看输出：

```bash
python config.py                # 配置路径、密钥指纹与各项取值（凭据打码）
python logger.py                # 日志目录、按天文件列表、保留策略
python chromedriver_manager.py  # 完整驱动诊断，并尝试对齐驱动
python network_checker.py       # 实测网络探测耗时，验证停止监控的响应速度
python app_icon.py              # 图标能否被加载、尺寸是否齐全、兜底图标是否可用
python build_icon.py            # 重新生成 icon.ico，并校验原图两张逐字节未变
python dialogs.py               # 对话框按钮文案（确定 / 取消）
python version.py               # 版本号、派生文件一致性、更新清单候选源
python version.py --sync        # 把 .iss 与 version_info.txt 对齐到 __version__
```

### Build and Package

```bash
# 0) 若有改动图标原图：重新生成各尺寸的 icon.ico（会校验原图两张逐字节未变）
python build_icon.py

# 1) 生成单文件 exe（使用全新目录，避免旧产物被占用）
python -m PyInstaller AutoConnect.spec --noconfirm --workpath build_new --distpath dist_new

# 2) 以产物覆盖 dist/AutoConnect.exe

# 3) 编译安装包（相对路径以 .iss 所在目录为基准）
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" package\AutoConnectInnoSetupScriptFiles.iss
```

打包前需先退出正在运行的托盘程序，否则 `dist/AutoConnect.exe` 被占用会导致替换失败。

`AutoConnect.spec` 的路径全部基于 `SPECPATH` 计算，不写死绝对路径，换机器无需修改。

### Publishing a New Version

版本号只有一处来源：`version.py` 的 `__version__`。
`.iss` 的 `MyAppVersion` 与 `version_info.txt`（写进 exe 的版本资源）都由它派生：

```bash
# 改完 version.py 的 __version__ 后，一条命令对齐两个派生文件
python version.py --sync    # 同时校验一致性；不一致会以非 0 退出码结束
```

1. 改 `version.py` 的 `__version__`，执行 `python version.py --sync`；
2. 打包 exe 与安装包（见上）—— exe 的版本资源会让资源管理器
   「属性 → 详细信息」显示新版本号；
3. 上传安装包到分发渠道（北航网盘），拿到分享链接；
4. 修改仓库根目录的 `latest.json`：`version` 填新版本号，`download_url` 填下载链接，
   `notes` 填更新说明（用 `\n` 换行）；
5. 推送 `latest.json` —— 之后所有旧版本客户端会在下次启动时收到提示。

客户端读取清单时有三个候选通道（`raw.githubusercontent.com` /
`cdn.jsdelivr.net` / `api.github.com`），按顺序尝试，任意一个可用即可 ——
实测部分校园网会对前两者做 TLS 中间人导致证书校验失败，此时会自动走 API 通道。

### For Forkers

为降低风险，仓库中的 `crypto_utils.py` 被 `.gitignore` 忽略，仅保留
`crypto_utils_without_key.py` 这一隐去了 Key 结构的模板（模板中 `unique_data` 为空，
直接使用等同于未加密）。二次开发时请自行设计密钥派生方式；该模块的改动仅存在于本地，
换机器重建会丢失，注意备份。

本项目针对北航深澜（SRUN）认证门户开发，但登录地址可配置，理论上适用于同类型门户。

---

## Known Limitations

- **依赖浏览器自动化登录。** 门户一旦强制启用验证码，自动登录会暂停并提示手动处理。
- **仅支持 Windows。** 驱动探测、单实例保护与开机自启均使用 Windows 专有接口。

---

## Contributing

欢迎通过 [Issues](https://github.com/Vegetabledog-BUAA/AutoConnectToCampusNetwork/issues) 反馈问题或提出建议。

提交前请注意：

1. 描述复现步骤，并附上 `--doctor` 生成的诊断报告与相关日志片段。
2. 改动后请运行各模块自检入口确认无回归。
3. 提交日志中不要包含账号、密码或数据目录中的其他敏感内容。

---

## License

本项目采用 [Apache License 2.0](LICENSE) 授权。
