# Android ADB 鼠标侧键控制器

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white)](#环境要求)
[![ADB](https://img.shields.io/badge/Control-ADB%20%2B%20pynput-3DDC84?logo=android&logoColor=white)](#工作原理)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

用电脑鼠标的侧键，控制手机（USB 连着电脑）上正在播放的视频：

- **侧键1（后退键）-> 后退 5 秒**
- **侧键2（前进键）-> 播放 / 暂停**

一根 USB 线 + ADB 就够：**不装手机 App、不投屏、不需要 root**。

实现上只用 ADB：播放 / 暂停优先走媒体按键 `keyevent 85`（不行再自动试 79 / 126 / 127），
一旦系统当前没人接收媒体键（App 退到后台或被别的播放器抢走），自动改成点击画面上的播放 / 暂停按钮；
后退走「横滑手势 + 读取播放位置闭环校准」，因为 B 站等客户端的 MediaSession 根本不响应 `keyevent 89`
（实测无效，见下文「为什么不用 keyevent 89」）。

**换手机、换视频 App** 不用改代码：跑一次 `python main.py --calibrate`，程序会自己找到点得动的播放按钮、
测出滑动的换算比例，按「包名 + 分辨率 + 方向」记进 `profiles.json`，以后直接用。

## 目录

- [功能一览](#功能一览)
- [工作原理](#工作原理)
- [目录结构](#目录结构)
- [环境要求](#环境要求)
- [安装](#安装)
- [三步跑起来](#三步跑起来)
- [命令行参数](#命令行参数)
- [配置说明（config.json）](#配置说明configjson)
- [后退 5 秒是怎么做到的](#后退-5-秒是怎么做到的)
- [播放 / 暂停是怎么做到的](#播放--暂停是怎么做到的)
- [常见问题](#常见问题)
- [预留：接入 Scrcpy（备选方案）](#预留接入-scrcpy备选方案)
- [开发者](#开发者)
- [已知限制](#已知限制)
- [许可协议](#许可协议)

## 30 秒上手

**准备**：Windows 10/11、Python 3.9+、一根能传数据的 USB 线、手机已开「USB 调试」。
手机不用装任何东西，电脑上也不用把 adb 装进 PATH（程序自己找）。

```bat
git clone https://github.com/LEI-HAI/Android_ADB.git
cd Android_ADB
pip install -r requirements.txt

python main.py --doctor   :: 自检：找 adb、找手机、看媒体会话
python main.py            :: 开始监听，然后按鼠标侧键
```

- 只想先确认键位对不对、不碰手机 -> python main.py --dry-run
- 换了手机或换了视频 App -> 先跑一次 python main.py --calibrate（自动找播放按钮、测滑动比例）
- 退出 -> Ctrl+Alt+Q 或 Ctrl+C

## 功能一览

- 全局监听鼠标侧键：焦点在任何窗口（浏览器、IDE、全屏游戏）都能触发
- 侧键1 -> 后退 5 秒：滑动屏幕左侧，读播放位置算实际退了几秒，误差大自动补一刀
- 侧键2 -> 播放 / 暂停：先发 `keyevent 85`，不行再依次试 79 / 126 / 127，全都无效才点画面按钮（读了状态再决定，不盲点）
- 换手机 / 换 App 免改代码：`--calibrate` 自动找按钮、自动测滑动比例，结果存 `profiles.json`（按包名 + 分辨率 + 方向隔离）
- 按钮位置多来源兜底：标定档案 -> config.json 比例 -> 无障碍节点（uiautomator）探测
- 350ms 防抖：手抖、长按不会连续误发
- 指令串行执行：adb 有几十一百毫秒延迟，用队列排队，不卡监听、不丢动作
- 启动自动查找 adb、自动检测设备，未连接 / 未授权 / 多设备都有明确提示
- 启动时打印手机屏幕尺寸、当前媒体会话与播放位置，方便确认连的是对的设备
- 中文控制台日志，一眼看懂发生了什么
- Ctrl+C 或组合键 `Ctrl+Alt+Q` 安全退出（**不用 Esc**：游戏里按 Esc 开菜单会把程序一起关掉）
- 键盘备用绑定（可选）：游戏拦鼠标钩子时，用 `ctrl+alt+left` 之类的组合键代替
- 自带演练模式、环境自检、单键调试、输入监视器、单元测试

## 工作原理

```mermaid
flowchart LR
    A["鼠标侧键 x1 / x2"] --> B["pynput 全局钩子<br/>焦点在任何窗口都生效"]
    B --> C["防抖 350ms<br/>x1 / x2 独立计时"]
    C --> D["动作队列<br/>单线程串行，不丢不卡"]
    D -->|x1| E["Rewinder<br/>后退 5 秒"]
    D -->|x2| F["PlayPauser<br/>播放 / 暂停"]
    E --> G["读 PlaybackState<br/>input swipe<br/>再读校验 / 补刀"]
    F --> H["媒体键 85 / 79 / 126 / 127<br/>读状态校验<br/>不行就点画面按钮"]
    G --> I["AdbClient<br/>subprocess + adb shell input"]
    H --> I
    I --> J[("USB 连接的手机")]
```

一句话：**钩子只负责把动作塞进队列，真正的 adb 调用在单独线程里串行执行**，
所以 adb 慢（单次 50~200ms）也不会卡住鼠标监听，更不会丢动作。

## 目录结构

~~~text
Android_ADB/
├─ main.py                      # 入口：命令行、设备连接、生命周期
├─ config.json                  # 配置（键位、后退方式、秒数、快捷键）
├─ profiles.json                # 标定档案（--calibrate 生成，与具体手机绑定，已 gitignore）
├─ requirements.txt
├─ start.bat                    # Windows 一键启动（自动找 Python、缺依赖自动装）
├─ README.md
├─ android_adb/
│  ├─ config.py                 # 配置加载 + 校验
│  ├─ logger.py                 # 日志格式（控制台 + 滚动日志文件）
│  ├─ adb_client.py             # 找 adb、检测设备、keyevent / swipe / dumpsys
│  ├─ media_state.py            # 解析 dumpsys media_session（播放状态、位置、媒体按钮会话）
│  ├─ play_pause.py             # 播放/暂停策略：媒体键阶梯 / 点击按钮 / 自动兜底（默认）
│  ├─ rewind.py                 # 后退策略：滑动（默认）/ 媒体键（备选），比例可存档复用
│  ├─ calibrate.py              # --calibrate：自动找播放按钮 + 实测滑动换算比例
│  ├─ profiles.py               # 标定档案 profiles.json 的读写（包名 + 分辨率 + 方向）
│  ├─ ui_probe.py               # uiautomator dump 解析，找播放按钮 / 进度条节点
│  ├─ controller.py             # 动作编排（鼠标动作 -> 后端调用）
│  ├─ mouse_listener.py         # 鼠标监听、键盘备用绑定、防抖、异步队列、输入监视、钩子看门狗
│  ├─ hotkeys.py                # 退出快捷键、组合键解析
│  └─ backends/
│     ├─ base.py                # 后端抽象接口
│     ├─ adb_backend.py         # 默认后端：ADB（播放/暂停 + 滑动后退）
│     ├─ dry_run.py             # 演练后端：只打印命令
│     └─ scrcpy_backend.py      # 预留：Scrcpy 按键注入
├─ logs/
│  └─ controller.log            # 运行日志（自动滚动，出错时先看这里）
└─ tests/
   └─ test_core.py              # 离线单元测试（不用手机）
~~~

各层通过接口解耦：换一种控制方式（Scrcpy、蓝牙 HID、宏键盘）只要实现
`android_adb/backends/base.py` 里的 `play_pause()` / `rewind()`，其它代码不用动；
只想换「后退」的实现，改 `android_adb/rewind.py` 里的 `Rewinder` 子类；只想换「播放/暂停」的实现，
改 `android_adb/play_pause.py` 里的 `PlayPauser` 子类即可。

## 环境要求

- Windows 10 / 11（代码本身跨平台，adb 探测逻辑以 Windows 为主）
- Python 3.9+（本机实测 3.13.5）
- pynput（全局监听）
- platform-tools 里的 adb，手机已开启「USB 调试」
- 手机不需要 root：`input keyevent` / `input swipe` / `dumpsys` 都是普通权限就能用的调试命令

## 安装

不想用 git 的话：在仓库页点 **Code -> Download ZIP**，解压到任意目录（路径里没有空格和中文最省事）。

~~~bat
cd /d D:\path\to\Android_ADB
pip install -r requirements.txt
~~~

运行依赖只有 pynput 一个；adb 不必装进 PATH，程序会自己找（找不到时见「常见问题 2」）。

## 三步跑起来

### 1）确认手机连上了

~~~bat
python main.py --doctor
~~~

正常输出类似：

~~~text
=== 环境自检 ===
Python：3.13.5（C:\...\python.exe）
后退方式：swipe（目标 5 秒，31 ms/px）
播放/暂停方式：auto（点击位置比例 0.039 / 0.904）
标定档案：<仓库>\profiles.json（1 条：tv.danmaku.bili|2800x1272|r1）
[OK] pynput 已安装
[OK] adb：C:\Users\...\platform-tools\adb.EXE
[OK] adb 版本：Android Debug Bridge version 1.0.41
[OK] 设备 <serial> 状态=device 型号=PLR110
[OK] 屏幕：2800x1272（rotation=1）
[OK] 媒体按钮会话：tv.danmaku.bili/HeadsetControlPlayAdapter.Session/446 (userId=0)
[OK] 媒体会话：tv.danmaku.bili（播放中，position=79.0s）

自检结果：通过，可以启动主程序
~~~

### 2）先演练一遍

不碰手机，只在控制台打印「将要执行什么」：

~~~bat
python main.py --dry-run
~~~

按一下两个侧键，能看到 `检测到鼠标侧键 X1 -> 后退 5 秒` 之类的日志，说明钩子正常。

### 3）正式运行

~~~bat
python main.py
~~~

也可以直接**双击 start.bat**（它会自动找 Python、缺 pynput 会先装好）。

运行时日志示例：

~~~text
21:46:54 | 信息 | 使用 adb：C:\Users\...\platform-tools\adb.exe
21:46:54 | 信息 | 已连接设备：<serial> (V1936AL)
21:46:54 | 信息 | 手机屏幕 2340x1080，媒体会话：tv.danmaku.bili（播放中，102.3s）
21:46:54 | 信息 | 鼠标监听已启动：X1 -> 后退 5 秒，X2 -> 播放/暂停
21:46:54 | 信息 | 后端：ADB（播放/暂停 keyevent 85；后退 滑动后退（目标 5 秒，约 30.3 ms/px，自动校准））
21:46:54 | 信息 | 监听中…… 按 CTRL+ALT+Q 或 Ctrl+C 退出
21:47:10 | 信息 | 检测到鼠标侧键 X1 -> 后退 5 秒
21:47:10 | 信息 | 发送后退指令
21:47:11 | 信息 | 已后退 4.8 秒（滑动 161 像素）
~~~

退出：按 **Ctrl+Alt+Q** 或 **Ctrl+C**，程序会停掉监听、结束 adb 调用后退出。

> 为什么不是 Esc：退出键是**全局**监听的，按到就退出，等于抢走了所有程序的 Esc。
> 你在游戏里按 Esc 开菜单、在浏览器里按 Esc 取消——控制器都会跟着关掉。
> 组合键不会误伤；真要单键也行（config.json 里写 `"exit_key": "f8"`），
> 启动时会打印一条警告提醒你风险。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| 无参数 | 启动全局监听 |
| --doctor | 环境自检（Python / pynput / adb / 设备 / 屏幕 / 媒体会话 / 媒体按钮会话） |
| --list-devices | 列出设备后退出 |
| --send-key 224 | 发送单个按键后退出（调试用，224 是唤醒屏幕） |
| --test play / rewind / all | 测试一次动作后退出（**调后退手感就用它**） |
| --calibrate | 自动标定：找播放按钮位置 + 实测滑动换算比例，写入 profiles.json（换手机 / 换 App 后跑一次） |
| --monitor | 只打印鼠标 / 键盘事件，不连手机（排查「侧键没反应」） |
| --dry-run | 演练模式：只打印命令，不连接手机 |
| --device <serial> | 指定设备（多设备时用，--list-devices 可查） |
| --backend adb / scrcpy | 选择后端，默认 adb |
| --duration 10 | 监听 10 秒后自动退出（调试用） |
| --config 路径 | 使用其它配置文件 |
| --verbose | 输出调试日志（含每次滑动的像素数与校准值） |

## 配置说明（config.json）

### ADB / 媒体键

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| adb_path | null | adb.exe 路径；null 表示自动查找（PATH、常见安装位置、WindowsApps 等） |
| device_serial | null | 多设备时指定序列号 |
| adb_connect | null | 无线调试地址，例如 192.168.1.10:5555 |
| adb_timeout | 8.0 | 单条 adb 命令超时（秒） |
| play_pause_keycode | 85 | 播放 / 暂停用的按键码 |
| play_pause_mode | auto | 播放 / 暂停怎么发：`auto` = 先媒体键、无效再点按钮；`media_key` = 只发媒体键；`tap` = 只点按钮 |
| play_pause_x_ratio | 0.039 | 按钮位置 x（占屏幕宽度比例）；`auto`/`tap` 用 |
| play_pause_y_ratio | 0.904 | 按钮位置 y（占屏幕高度比例）；`auto`/`tap` 用 |
| play_pause_verify_ms | 400 | 发完媒体键等多久再读状态，确认真的生效（毫秒） |
| play_pause_tap_settle_ms | 350 | 点完按钮等多久再读状态（毫秒） |
| play_pause_extra_keycodes | null | 主键失效后依次尝试的键码；null = [79, 126, 127]，空列表 = 不试 |
| ui_probe | true | 是否用 uiautomator 节点找按钮（自绘控件拿不到时自动跳过） |
| profiles_file | profiles.json | 标定档案路径；相对路径以项目根目录为准，null = 不存档 |

### 后退方式（默认滑动）

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| rewind_mode | swipe | `swipe` = 横滑手势（推荐）；`media_key` = 老方案，发媒体键 |
| swipe_seconds | 5.0 | 每次后退的目标秒数 |
| swipe_ms_per_px | 31.0 | 初始换算：滑动 1 像素约等于多少毫秒视频（本机实测 B 站全屏约 30~31） |
| swipe_start_ratio | 0.80 | 滑动起点 x 占屏幕宽度比例（从右侧往左滑） |
| swipe_y_ratio | 0.55 | 滑动所在高度占屏幕高度比例（避开顶部标题和底部按钮） |
| swipe_duration_ms | 200 | 手势持续时长（毫秒），太短容易被识别成点击 |
| swipe_settle_ms | 320 | 松手后等多久再读播放位置（等播放器把 seek 落盘） |
| auto_calibrate | true | 按实际后退秒数自动修正 swipe_ms_per_px |

### 媒体键后备（rewind_mode=media_key 时生效）

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| rewind_keycode | 89 | 后退按键码（KEYCODE_MEDIA_REWIND） |
| rewind_repeat | 1 | 一次「后退」连发几次 |
| rewind_interval_ms | 120 | 连发间隔（毫秒） |
| rewind_fallback_keycode | 21 | 媒体键发送失败时改用的键（21 = 方向键左）；null 表示不启用 |
| rewind_fallback_repeat | 1 | 备用键连发次数 |

### 鼠标 / 键盘 / 其它

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| rewind_button | x1 | 鼠标后退键的 pynput 名称 |
| play_pause_button | x2 | 鼠标前进键的 pynput 名称 |
| debounce_ms | 350 | 防抖窗口，窗口内的重复按下丢弃 |
| rewind_key | null | 键盘备用绑定，例如 `ctrl+alt+left`（null = 关闭） |
| play_pause_key | null | 键盘备用绑定，例如 `ctrl+alt+space`（null = 关闭） |
| exit_key | ctrl+alt+q | 退出快捷键，支持组合键；写单键（如 `esc`、`f8`）也认，但全局生效、容易误触 |
| enable_exit_hotkey | true | 是否启用退出快捷键 |
| backend | adb | adb 或 scrcpy |
| log_level | INFO | 控制台日志级别：DEBUG / INFO / WARNING / ERROR |
| log_file | logs/controller.log | 日志文件（相对项目根目录），null = 不写文件 |
| log_file_level | DEBUG | 日志文件里记多细（DEBUG 会记每次滑动、每次重启） |
| hook_restart | true | 鼠标/键盘钩子被系统摘掉时自动重启 |
| hook_restart_limit | 30 | 连续重启上限（0 = 不限），超过后退出并写日志 |
| heartbeat_seconds | 300 | 每 5 分钟写一条「还在跑」的心跳日志（0 = 关闭） |
| dry_run | false | true 时只打印命令，不连手机 |

改完保存即可，下次启动生效；运行中改配置不会热更新。

## 后退 5 秒是怎么做到的

### 为什么不用 keyevent 89

在本机（vivo V1936AL / Android 11 + B 站客户端）实测：`keyevent 89` 对 B 站**完全无效** ——
连发 2 次、长按、`cmd media_session dispatch rewind` 都不改变播放位置；
`keyevent 21`（方向键左）同样无效。原因看 `dumpsys media_session` 就明白：

~~~text
state=PlaybackState {state=3, position=102310, speed=1.0, actions=823, ...}
~~~

actions=823 的位里没有 ACTION_REWIND，也就是这个 App 根本没声明支持「后退」，
媒体键发过去自然没人处理。所以默认改用**手势**：

### 横滑手势 + 闭环校准

1. `dumpsys media_session` 读出当前播放位置（例如 333.8s）
2. 按 `swipe_ms_per_px` 算出要滑多少像素（5 秒 ≈ 161 像素）
3. `input swipe` 从屏幕右侧往左滑
4. 等 `swipe_settle_ms` 后再读一次位置（例如 328.5s）
5. 算误差：目标 5 秒、实际 5.3 秒 -> 误差 0.3 秒，可以接受；误差 ≥0.4 秒就补一刀
6. 用实际测量值修正 `swipe_ms_per_px`（指数滑动平均），下次更准

日志会直接写「已后退 4.8 秒（滑动 161 像素）」，不用猜。

注意：不同视频的「1 像素等于多少毫秒」并不一样（B 站全屏下约等于总时长的 21%，
换一个 20 分钟的视频，同样的像素对应的秒数会变）。所以**每换一个视频，滑一两次就会自动校准过来**；
如果发现总是差很多，把 `auto_calibrate` 保持 true，或者手动调 `swipe_ms_per_px`。

### 三种方案怎么选

| 方案 | 配置 | 适用 |
| --- | --- | --- |
| 横滑手势（默认） | `rewind_mode: "swipe"` | B 站、抖音这类支持左右滑动快进的 App |
| 媒体键 | `rewind_mode: "media_key"` | 播放器确实声明了 ACTION_REWIND（部分本地播放器） |
| 占位 / 调试 | `--dry-run` | 没连手机时验证键位映射 |

调手感的方法：放一个视频，执行

~~~bat
python main.py --test rewind --verbose
~~~

看日志里「校准：本次 161px -> 4.80 秒」和「已后退 4.8 秒」，不满意就改 `swipe_seconds`。

## 播放 / 暂停是怎么做到的

### 为什么暂停久了媒体键会失效

`keyevent 85`（KEYCODE_MEDIA_PLAY_PAUSE）不是直接发给 App 的，而是发给「当前媒体按钮会话」（media button session）。
系统只在某个 App 是活跃媒体会话时才转发过去。实测（vivo V1936AL / Android 11 + B 站）遇到的两种情况：

~~~text
# 1) 正常：系统的媒体按钮会话指向 B 站，keyevent 85 立刻生效
Media button session is tv.danmaku.bili/HeadsetControlPlayAdapter.Session (userId=0)
state=PlaybackState {state=2, position=1038633, speed=1.0, actions=823, ...}

# 2) 失效：会话变成 null，keyevent 85 和 cmd media_session dispatch 都毫无反应
Media button session is null
Last MediaButtonReceiver: Restored MBR {component=...com.tencent.qqmusic...}

# 3) 被抢占（新手机 Android 16 上实测）：会话指向抖音，85 发给抖音，B 站毫无反应
Media button session is com.ss.android.ugc.aweme/FeedPlayerSession/352 (userId=0)
~~~

第 2、3 种情况下，B 站的 logcat 里连一行按键日志都没有——它压根没收到。特点分别是
「暂停一会儿 / 切了音乐 App 之后」和「刷过短视频 / 别的播放器刚响过」，点一下视频画面、让 B 站重新活跃，
会话就回来了，媒体键也随之恢复。

所以光发键盘码不可靠，程序改成**先看状态再决定**（`play_pause_mode: auto`，默认）：

| 当前状态 | 做法 |
| --- | --- |
| 播放中，且媒体按钮会话存在 | 直接发媒体键（最快，不校验） |
| 读不到媒体会话（前台不是播放器） | 只发媒体键，**绝不盲点屏幕**（避免误触） |
| 媒体按钮会话是 null | 直接点击画面左下角的播放 / 暂停按钮 |
| 媒体按钮会话属于**别的 App**（抖音、QQ 音乐…） | **一个键都不发**（发了只会打断别人），直接点按钮 |
| 暂停中但会话还在 | 发媒体键 -> 等 `play_pause_verify_ms` -> 重读状态，没变才点按钮 |

点击兜底每轮最多点 2 次（控件栏 3 秒无操作会自动隐藏，第一下只负责把控件栏唤出来，第二下才真正切换），
每次都重读 `PlaybackState` 确认成功；两次都没成功就打印警告，提示运行 `--calibrate`。

「发媒体键」不是只发一个码，而是一个**按键阶梯**：上次成功的键 -> `play_pause_keycode`（默认 85）
-> `play_pause_extra_keycodes`（默认 79 / 126 / 127）。每发一个都读状态校验，第一个真正让状态翻转的键
会被记住（同一进程内优先用它）。这样换成只认 `KEYCODE_HEADSETHOOK` 或 `MEDIA_PLAY` / `MEDIA_PAUSE` 的播放器也能用。

### 按钮坐标怎么来的

config.json 里的默认值（0.039 / 0.904）是旧手机（vivo V1936AL，横屏 2340x1080）量的：按钮中心 (92, 976)。
新手机（vivo PLR110，横屏 2800x1272）上按钮跑到了 (84, 1107) = 0.030 / 0.870 —— 由 `--calibrate` 量出来写进档案。
**换设备别信默认值，先跑一次 `--calibrate`。** 程序按**当前屏幕尺寸 × 比例**换算，转屏不用改配置。

但这是「一台手机 + 一个 App」量出来的值，换设备就不准了，所以点击候选点有三层：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `profiles.json` 档案 | 按「包名 + 分辨率 + 方向」记住上次点准的位置（运行中自动学习，`--calibrate` 也会写） |
| 2 | `config.json` 比例 | 默认的 0.039 / 0.904，兜底用 |
| 3 | uiautomator 节点 | 标准控件的播放器能直接拿到按钮真实中心；B 站是自绘 SurfaceView，拿不到就自动跳过 |

点了确实生效的位置会被写回档案，下次直接用；点不动就会提示跑 `--calibrate`。

### 换手机 / 换 App：跑一次 `--calibrate`

~~~bat
python main.py --calibrate
~~~

程序会（手机请停在视频全屏播放页面）：

1. 先试 uiautomator 节点，拿不到就在「底部左下角」一小片区域逐点点一下，用播放状态是否翻转判断哪个位置是按钮；
2. 横滑一段固定距离，读实际后退了多少秒，算出 `ms/px`；
3. 把结果按「包名 + 分辨率 + 方向」写进 `profiles.json`，并把视频恢复成播放状态；
4. 打印绝对坐标、来源（ui_node / grid）和换算比例。

新手机上的实测输出（网格从 3% 扫到 16%、从 85% 扫到 93%，最多 32 个点）：

~~~text
界面节点里没有播放/暂停按钮（自绘控件），改用网格搜索
开始网格搜索按钮位置（最多 32 个点）
点击 (84, 1081) 没有改变状态（state=2）
命中按钮：(84, 1107) = (0.030, 0.870)
滑动 700 像素测量后退比例……
实测：700 像素 -> 后退 19.5 秒（27.89 ms/px），后退 5 秒约需 179 像素
[OK] 播放/暂停按钮：绝对坐标 (84, 1107)，来源=grid
[OK] 后退换算：27.89 ms/px（后退 5 秒约滑 179 像素）
~~~

网格不再往下扫（`y > 0.93`）：那一片是进度条和章节条，点上去会触发 seek
——实测在 B 站的章节条上点两下会直接跳章节。

换台手机、换视频平台，跑这一条命令就行，不用手改比例坐标。

### 三种模式怎么选

| 配置 | 行为 | 适用 |
| --- | --- | --- |
| `play_pause_mode: "auto"`（默认） | 媒体键优先，失败 / 无人接收时点按钮 | 绝大多数情况 |
| `play_pause_mode: "media_key"` | 只发媒体键，不碰屏幕 | 不想让程序点屏幕（例如在看别的东西） |
| `play_pause_mode: "tap"` | 只点按钮 | 媒体键完全无效的 App |

调的时候用：

~~~bat
python main.py --test play --verbose
~~~

日志会写清楚走的是哪条路：「发送播放/暂停指令」->（必要时）「媒体键没有生效，改为点击画面上的播放/暂停按钮」
->「点击播放/暂停按钮 (91, 976) 第 2 次」。

## 常见问题

**1. 双击 start.bat 窗口一闪而过**

已修：start.bat 现在会先 `cd` 到自己所在目录，按 `py -3` -> `python` -> `python3` ->
`%LOCALAPPDATA%\Programs\Python\Python3xx\python.exe` 依次探测，找不到 Python 会说明原因并 `pause`。

如果还是闪退，在 cmd 里手动执行一次看报错：

~~~bat
cd /d D:\path\to\Android_ADB
start.bat
~~~

> 小知识：start.bat 是 **GBK 编码**，开头有 `chcp 936`。用记事本另存为 UTF-8 会让 cmd 解析中文出错，
> 表现为一堆「'xxx' 不是内部或外部命令」。要改这个文件，请保持 ANSI/GBK 编码。

**2. 提示「未找到 adb 可执行文件」**

任选一种：

- 把 platform-tools 整个文件夹放到本项目下，即 `Android_ADB\platform-tools\adb.exe`
- 把 adb 所在目录加进系统 PATH，重开命令行
- 在 config.json 里填 adb_path，或设置环境变量 ADB_PATH

**3. 提示「未检测到任何设备」/「尚未授权」/「offline」**

- 换一根支持数据传输的线（很多充电线没有数据线芯）
- 手机：设置 -> 关于手机 -> 连点版本号进入开发者选项 -> 打开 USB 调试
- 插上后手机会弹「允许 USB 调试吗」，勾选「一律允许」并确定
- 仍然不行：`adb kill-server` 后重新 `python main.py --doctor`

**4. 播放 / 暂停正常，但后退没反应**

- 先确认 `rewind_mode` 是 `swipe`（默认）。`media_key` 对 B 站无效。
- 执行 `python main.py --test rewind --verbose`，看有没有 `input swipe ...` 和「已后退 x.x 秒」。
- 如果日志说「未检测到正在播放的媒体会话」，说明读不到位置：确认视频 App 在前台、正在播放或暂停中（不是首页）。
  这时程序仍会滑动，只是没法测量，会打印「已向左滑动 161 像素（约后退 5 秒，未能读取播放位置）」。
- 如果滑动方向反了（进度反而前进），把 `swipe_start_ratio` 调大一点（例如 0.85），让起点更靠右，
  确保滑动区域落在视频画面上而不是弹幕栏 / 侧边推荐栏。

**5. 侧键完全没反应（游戏 / 全屏程序里最常见）**

按顺序排查：

1. `python main.py --monitor`，然后在目标窗口里按侧键：
   - **有 `[鼠标] x1` 输出** -> 钩子正常，问题在手机端或映射配置；
   - **没有任何输出** -> 被目标程序拦了。Windows 的 UIPI 规定：低权限进程收不到高权限窗口的输入，
     游戏 / 模拟器 / 带反作弊的程序常以管理员身份运行。
     解决：**右键 start.bat -> 以管理员身份运行**（或右键 cmd -> 以管理员身份运行后执行 `python main.py`）。
2. 有些反作弊会主动卸载全局鼠标钩子（防宏），管理员权限也无效。这时用键盘备用绑定：
   把 `config.json` 里的 `rewind_key` / `play_pause_key` 设成组合键，例如

   ~~~json
   "rewind_key": "ctrl+alt+left",
   "play_pause_key": "ctrl+alt+space"
   ~~~

   再在鼠标驱动（罗技 G HUB、雷蛇 Synapse、雷柏等）里把侧键映射成同样的组合键。
   这样侧键不再是「鼠标侧键」，而是普通键盘事件，绕开反作弊对鼠标钩子的限制。
3. 日志显示的是别的键名（例如 `middle`）：说明你的鼠标侧键不是 x1/x2，
   把 `rewind_button` / `play_pause_button` 改成日志里出现的名字。
4. 有「检测到侧键」但没有「发送」：看是否有防抖提示（连按太快），或把 `debounce_ms` 调小。

**6. 按键延迟明显**

每次 adb 调用 50~200ms（滑动 + 两次 dumpsys 大约 0.6~1 秒），属于正常现象。
因为用了队列，连按不会丢，只会依次执行。想更快可以把 `swipe_settle_ms` 调小（默认 320ms），
或在 config.json 里把 `adb_path` 指向更快的 adb。

**7. 中文乱码**

程序启动时会把控制台代码页切到 UTF-8 并以 UTF-8 输出；start.bat 自身用 GBK + `chcp 936`，
中文提示显示正常。若仍然乱码，换 Windows Terminal 或把控制台字体改成支持中文的字体。

**8. 跑一段时间后自己停了，cmd 窗口自动消失**

先说结论：这是老版本的行为，现在已经改成「钩子掉了就自动重新挂上」，并把过程写进日志。

原因是 Windows 会主动摘掉全局鼠标钩子，常见触发场景：

- 锁屏、切换用户、UAC 安全桌面（装软件 / 改设置的弹窗）
- 休眠唤醒、显示器休眠唤醒、远程桌面连接
- 鼠标驱动更新、某些反作弊或安全软件临时卸载钩子
- 老版本还有个坑：cmd 开着「快速编辑」，在窗口里点一下进入选择状态，写日志就会被阻塞，
  回调卡顿超过 300ms 系统也会摘钩

现在的处理：

1. 每次回调都包了异常处理，出错只记日志，不再连带把监听线程弄死；
2. 钩子线程没了会被看门狗发现并**自动重启**（`hook_restart`，默认最多连续 30 次），
   控制台能看到「MouseHook 意外停止，第 1 次自动重启……」→「已恢复监听」；
3. 启动时关掉了控制台的「快速编辑」，避免点一下窗口就把钩子拖死；
4. 所有信息（包括崩溃堆栈、线程异常、心跳）写进 `logs\controller.log`，窗口关了也能查；
5. 真的救不回来时程序会以 **退出码 4** 结束，并在窗口里打印原因和日志路径，
   start.bat 会 `pause` 把窗口留住。

如果以后又出现「侧键没反应」，先看两处：

~~~bat
rem 1) 日志最后 30 行
powershell -NoProfile -Command "Get-Content logs\controller.log -Tail 30"
rem 2) 日志里有没有重启/心跳记录
findstr /C:"自动重启" /C:"心跳" logs\controller.log
~~~

退出码含义（`echo %ERRORLEVEL%` 可查，窗口里也会打印）：

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常退出（退出快捷键 / Ctrl+C / 定时结束） |
| 1 | 配置或启动参数有问题 |
| 2 | 没检测到手机 / 未授权 / offline |
| 3 | adb 或控制后端出错 |
| 4 | 钩子反复重启仍失败（多半被游戏或安全软件拦了） |
| 5 | 程序内部未预期错误（日志里有完整堆栈） |

> 如果还是自己停了：把 `logs\controller.log` 最后 50 行发我，里面会写清楚是哪个钩子、
> 第几次重启、最后的异常是什么。

**9. 在游戏里按 Esc 开菜单，控制器自己退了**

老版本把退出键设成了单个 Esc，而退出键是全局监听的——所以游戏菜单的 Esc、
浏览器取消、对话框关闭，全都会把程序关掉。

现在的处理：

1. 默认退出键改成 **Ctrl+Alt+Q**，组合键基本不会和别的软件撞车；
2. 退出键也支持任意组合键，写在 `config.json` 的 `exit_key`，例如 `"ctrl+alt+q"`、`"ctrl+shift+f12"`；
3. 如果你非要单键（`"exit_key": "f8"`），程序启动时会打印警告，说明它会全局生效；
4. 不想用快捷键：`"enable_exit_hotkey": false`，只用 Ctrl+C 退出；
5. 改完 `exit_key` 记得重启程序生效；按对组合键时日志会写「收到退出快捷键 CTRL+ALT+Q，正在退出……」。

**10. 按了侧键，视频没动（播放 / 暂停不生效）**

按顺序看：

1. `python main.py --doctor`，注意这一行：
   - `[OK] 媒体按钮会话：tv.danmaku.bili/...` -> 媒体键通路正常，问题多半在映射或防抖；
   - `[--] 媒体按钮会话：null` -> 此刻按媒体键没人接收。程序会自动改用点击按钮（`play_pause_mode: auto`），
     也可以用 `--test play --verbose` 直接验证点击是否生效；
   - `媒体按钮会话：com.ss.android.ugc.aweme/...`（别的 App）-> 85 会发给抖音，程序检测到这一点会直接点按钮，
     日志里写「媒体按钮会话被 ... 占着，改为点击画面按钮」。想让媒体键恢复，点一下 B 站播放器即可。
2. `python main.py --test play --verbose`，看日志：
   - 「点击播放/暂停按钮 (91, 976) 第 1 次」后状态没变 -> 控件栏当时是隐藏的，程序会再点一次，属正常；
   - 两次都没成功 -> 按钮坐标不对，按视频画面微调 `play_pause_x_ratio` / `play_pause_y_ratio`：
     用截图（`adb exec-out screencap -p > shot.png`）量出按钮中心的像素坐标，再除以屏幕宽高。
3. 手机前台根本不是播放器（在首页 / 通知栏 / 别的 App）：发媒体键只会控制那个 App。
4. 想彻底避开媒体键：`"play_pause_mode": "tap"`，全程只点按钮。
5. 竖屏短视频（抖音这类）按钮位置和横屏完全不同，先把 App 转成横屏全屏，或者单独调两个比例。

如果你把 `play_pause_mode` 改成 `media_key`，程序只发媒体键、既不校验也不点屏幕（这是刻意的：
`media_key` 的语义就是「只发键，不碰屏幕」）。所以媒体键恰好失效时表现就是「没反应」，而不会自动兜底。

## 预留：接入 Scrcpy（备选方案）

如果确认目标 App 对 ADB 手势 / 媒体键都不买账，可以改用 Scrcpy 的按键注入：

- 代码位置：`android_adb/backends/scrcpy_backend.py`
- 现在选择 backend=scrcpy 会直接报「尚未实现」，不会假装启动成功
- 文件顶部写了接入要点：scrcpy 2.x 的 control socket（默认 27183 端口）协议里，
  type=0 表示 INJECT_KEYCODE，负载为 action(u8) + keycode(u32) + repeat(u32) + metaState(u32)，发 DOWN / UP 两帧即可
- 实现后只需把 config.json 的 backend 改成 scrcpy，其余代码不用动

## 开发者

接手 / 二次开发前先看 [AGENTS.md](AGENTS.md)：环境事实、分层、设计不变量、踩过的坑（媒体键失联、
keyevent 89 无效、日志轮转失败等）、扩展指南、收尾检查清单都在里面。这份文档是写给 AI 编码助手和贡献者的，
普通用户只看本 README 就够。

踩坑欢迎开 [Issue](https://github.com/LEI-HAI/Android_ADB/issues)（附上手机型号、Android 版本、视频 App、
logs/controller.log 的最后几十行），修好了直接提 PR。

运行测试：

~~~bat
python -m unittest discover -s tests -v
~~~

83 个用例，覆盖：设备列表解析、配置校验（含滑动参数、播放/暂停模式与比例、组合键）、防抖、动作分发、
媒体会话解析（老格式 `state=2` 与 Android 14+ 的 `state=PAUSED(2)`）与「两次快照间的 seek 计算」（暂停 / 播放两种情形）、
媒体按钮会话解析（含「会话属于别的 App 时只点击、不发键」）、
播放/暂停的分支（播放中只按键、会话 null 时直接点、暂停有会话时按键后校验再兜底、按键阶梯逐个试、控件栏隐藏时点两次）、
点击失败时的报错、三种模式的选择、标定档案读写与「档案点优先于 config 比例」、无障碍节点解析与评分、
滑动后退的目标秒数与自动校准、标定流程（网格命中 / 节点命中 / 写档案）、补刀方向、转屏后宽高交换、
键盘备用绑定、看门狗重启、日志配置、退出键组合判定（单按 Esc 不再退出）、Scrcpy 占位。

## 已知限制

- 后退精度依赖「读播放位置」：App 没有注册 MediaSession（或不是当前活跃会话）时只能按固定像素滑，
  这时会明确提示「未能读取播放位置」，不会假装成功
- `input swipe` 是模拟手指，若 App 在滑动区域有自己的手势（例如弹幕手势、双击点赞），可能被误判；
  可通过 `swipe_y_ratio` / `swipe_start_ratio` 挪开
- 不同时长视频的「像素 -> 秒」比例不同，靠自动校准收敛，第一个视频可能需要滑一两次
- 点击兜底的默认比例坐标是按「横屏全屏 + B 站」量的；换 App / 竖屏播放请先跑 `--calibrate`（它会自动写
  `profiles.json`），实在不行再手调 `play_pause_x_ratio` / `play_pause_y_ratio`
- 标定按「包名 + 分辨率 + 方向」隔离，同一 App 换视频不用重标；但手机改分辨率 / 缩放后需要重跑一次
- 自绘控件（如 B 站全屏播放器）拿不到无障碍节点，标定只能靠网格点击，理论上有极小概率点到别的控件（范围限制在左下角）
- 媒体按钮会话可能被别的 App（抖音、QQ 音乐）占着，程序会自动改成点按钮，代价是慢一点（多一次读状态 + 点击）；
  想让媒体键一直好用，先点一下播放器让 B 站成为当前媒体会话
- 已在 Android 11（旧机）与 Android 16（新机）上验证；Android 14+ 的 `PlaybackState` 新格式
  （`state=PLAYING(3)`）与 Android 16 去掉 `SurfaceOrientation` 都已适配，更老的系统未测
- 只在 Windows 上验证过；macOS / Linux 需要自行配置 adb_path，pynput 权限另需授权

## 许可协议

[MIT](LICENSE) © 2026 LEI-HAI

可以随便用来控制自己的手机、二次开发、塞进自己的工具集；出问题我不背锅（条款见 LICENSE）。
如果能帮到你，欢迎点个 Star；也欢迎在 Issue 里说说你用的是哪台手机、哪个 App、哪个视频客户端，
方便把兼容性情况写进这份文档。

## English (short version)

A desktop-to-Android media remote over ADB. Your mouse side buttons become **play/pause** and
**back 5 seconds** for whatever video is playing on a USB-connected phone.

- **Side button 1** -> seek back 5 s: reads the real PlaybackState, performs a horizontal swipe,
  re-reads the position and corrects itself (closed loop). keyevent 89 is ignored by most Chinese
  video apps, so it is deliberately not the default.
- **Side button 2** -> play/pause: tries media keys 85 / 79 / 126 / 127, verifies the state actually
  changed, and falls back to tapping the on-screen button when no app owns the system
  media-button session (common after another player such as Douyin or QQ Music grabbed it).
- Global pynput hook, 350 ms debounce (per button), single worker queue so adb latency never
  blocks the hook and actions never run out of order.
- python main.py --calibrate auto-locates the play button and measures swipe-to-seconds for a new
  phone or app, stored in profiles.json keyed by package + resolution + rotation.
- Diagnostics: --doctor, --monitor, --dry-run, --test play|rewind, --list-devices, --send-key.

Quick start (Windows 10/11, USB debugging enabled, no root needed):

```bat
pip install -r requirements.txt
python main.py --doctor
python main.py
```

Exit with Ctrl+Alt+Q or Ctrl+C. Requires Python 3.9+ and platform-tools adb (auto-detected, does not
need to be on PATH). Console output is Chinese; the architecture and the pitfalls are documented in
[AGENTS.md](AGENTS.md). Verified on Android 11 and Android 16 devices with the Bilibili app.

