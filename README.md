# Android ADB 鼠标侧键控制器

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white)](#环境要求)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

用电脑鼠标侧键控制 USB 连接的 Android 手机上正在播放的视频：

- 侧键 1：后退 5 秒
- 侧键 2：播放 / 暂停

无需安装手机 App、投屏或 root。电脑需要安装 Python 和 Android Platform-Tools（ADB）；手机需开启 USB 调试。

## 功能

- 全局监听鼠标侧键，也支持可选的键盘快捷键。
- 使用 ADB 控制媒体播放；播放 / 暂停会在媒体键无效时尝试点击播放器按钮。
- 通过滑动实现后退，并在应用提供播放位置时自动校准滑动距离。
- 提供设备诊断、演练、输入监视和自动标定命令。

## 环境要求

- Windows 10 / 11
- Python 3.9 或更高版本
- Android Platform-Tools 中的 `adb`
- Android 手机已开启 USB 调试，并通过 USB 连接电脑

程序会尝试自动查找 adb；如果没有找到，请将 platform-tools 添加到 PATH，或在 `config.json` 中设置 `adb_path`。

## 安装与启动

```bat
git clone https://github.com/LEI-HAI/Phone-Tutorial-Remote.git
cd Phone-Tutorial-Remote
pip install -r requirements.txt
python main.py --doctor
python main.py
```

首次连接手机时，请在手机上确认 USB 调试授权。运行 `python main.py --doctor` 确认设备状态正常后，再启动监听。

> 仓库原名 `Android_ADB`，2026-11 改名为 `Phone-Tutorial-Remote`（名字更直白：给“看教程视频”用的远程控制）。旧地址 `github.com/LEI-HAI/Android_ADB` 会由 GitHub 自动重定向，clone / issue 链接都仍然可用。

按 `Ctrl+Alt+Q` 或 `Ctrl+C` 退出。也可以在 Windows 上双击 `start.bat` 启动。

### 首次使用新手机或新播放器

将手机停留在视频全屏播放页面，运行：

```bat
python main.py --calibrate
```

程序会尝试定位播放 / 暂停按钮并测量滑动距离，结果保存在本地 `profiles.json`。更换设备、应用、屏幕分辨率或方向后，建议重新标定。

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `python main.py` | 开始监听鼠标侧键 |
| `python main.py --doctor` | 检查 Python 依赖、ADB 和设备连接 |
| `python main.py --calibrate` | 标定播放按钮和后退滑动比例 |
| `python main.py --dry-run` | 演练侧键映射，不控制手机 |
| `python main.py --monitor` | 监视鼠标和键盘输入，不连接手机 |
| `python main.py --test play` | 执行一次播放 / 暂停操作 |
| `python main.py --test rewind` | 执行一次后退操作 |
| `python main.py --list-devices` | 列出已连接设备 |
| `python main.py --device <serial>` | 多台设备时指定目标设备 |

`--test play` 和 `--test rewind` 会实际操作手机上的媒体。使用前请确认当前播放内容可以被暂停或 seek。

## 配置

默认配置位于 `config.json`。常用选项包括：

| 选项 | 说明 |
| --- | --- |
| `play_pause_mode` | `auto` 自动选择媒体键或点击；`media_key` 仅发媒体键；`tap` 仅点击按钮 |
| `play_pause_x_ratio` / `play_pause_y_ratio` | 播放按钮在屏幕可用区域中的比例坐标；通常优先使用 `--calibrate` |
| `rewind_mode` | `swipe` 使用滑动（默认）；`media_key` 使用媒体键 |
| `swipe_seconds` | 每次后退的目标秒数 |
| `swipe_ms_per_px` | 滑动距离换算参数；可由标定自动调整 |
| `debounce_ms` | 侧键防抖时间，默认 350 毫秒 |
| `exit_key` | 全局退出快捷键，默认 `ctrl+alt+q` |
| `device_serial` | 多设备时指定设备序列号 |

完整配置项及默认值见 `config.json`。

## 常见问题

### 找不到设备或显示 unauthorized

确认 USB 线支持数据传输、手机已开启 USB 调试，并在手机弹窗中允许这台电脑。运行 `python main.py --doctor` 查看诊断结果。多台设备连接时，使用 `--device <serial>` 指定目标设备。

### 侧键没有反应

先运行 `python main.py --dry-run` 确认电脑能检测到侧键，再运行 `python main.py --doctor` 检查设备连接。部分游戏或安全软件可能拦截全局鼠标钩子；可在 `config.json` 中设置键盘备用快捷键。

### 播放 / 暂停或按钮点击无效

让目标视频播放器处于前台，再运行 `python main.py --calibrate`。也可用 `python main.py --test play --verbose` 查看操作日志。不同应用的播放器界面可能需要单独标定。

### 后退距离不准确

不同应用和视频的滑动响应可能不同。重新运行 `python main.py --calibrate`，并在实际播放内容上试用几次，让程序根据可读取的播放位置校准。

## 已知限制

- 已在 Windows 和部分 Android 设备上验证；macOS / Linux 尚未验证。
- 自动校准依赖目标应用提供可读取的媒体播放状态；读取不到时，后退只能使用固定滑动距离。
- 部分视频播放器使用自绘控件，按钮位置可能需要通过标定适配。
- ADB 滑动可能与应用自身的手势冲突。
- Scrcpy 后端目前尚未实现，请使用默认的 ADB 后端。

## 许可

本项目以 [MIT License](LICENSE) 发布。
