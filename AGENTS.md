# AGENTS.md

给接手这个项目的 Agent 的速查手册。**先读这一页，再动代码。**

> 这份文档面向 AI 编码助手和贡献者，写得比较「内部」；普通用户请直接看 [README.md](README.md)。
> 本机专属的路径、序列号等已做脱敏（写成 `<仓库根目录>`、`<serial>`），换台机器照样能照着做。

- 项目：用电脑鼠标侧键（经 ADB）控制手机上正在播放的视频
- 路径：`<仓库根目录>`（仓库根目录 = 此目录）
- 版本：`android_adb.__version__ = "1.0.0"`
- 状态：可用。83 个单元测试通过，已在两台真机（新：vivo PLR110 / Android 16；旧：vivo V1936AL / Android 11）上验证播放/暂停与后退；
  换设备/换 App 用 `--calibrate` 自动适配

---

## 1. 一句话背景

侧键1 -> 后退 5 秒，侧键2 -> 播放/暂停。
手机用 USB 连着电脑、开了 USB 调试，程序在电脑后台全局监听鼠标，通过 `adb shell input` 操作手机。

**没有手机端 App，不投屏，不需要 root。**

---

## 2. 本机环境事实（照抄，别猜）

| 项 | 值 |
| --- | --- |
| 系统 | Windows 11 |
| Python | 3.13.5，`%LOCALAPPDATA%\Programs\Python\Python313\python.exe` |
| pynput | 已安装（没有 `__version__` 属性，别用它判断版本） |
| adbutils | **未安装**，项目用标准库 `subprocess` 调 adb，不引入第三方依赖 |
| adb | 不在 PATH。自动探测选中：`%APPDATA%\AndroidAdCleaner\platform-tools\adb.exe`（1.0.41） |
| 手机（现用） | vivo PLR110，serial `<serial>`，Android 16（SDK 36），物理 1272x2800，density 560 |
| 手机（旧，仅参考） | vivo V1936AL，serial `<serial>`，Android 11（SDK 30），物理 1080x2340 |
| 目标 App | B 站 `tv.danmaku.bili`，全屏播放器；新机横屏可用区域 2800x1272，旧机 2340x1080 |
| 截图 | **必须** `cmd /c ""adb" -s <serial> exec-out screencap -p > shot.png"`，PowerShell 的 `>` 重定向和管道会把 PNG 弄坏 |

代码里的 `find_adb()` 会依次找：`ADB_PATH` 环境变量 -> `config.adb_path` -> `shutil.which` -> 常见安装目录 ->
扫描 `%APPDATA%\*\platform-tools\adb.exe` 与 `C:\Program Files\*\adb.exe`。新机器上先跑 `--doctor` 确认。

---

## 3. 分层与数据流

```text
pynput 全局钩子
   └─ MouseHook.on_click        (x1 / x2 -> Action)
        └─ Debouncer            (按 key 独立计时 350ms)
             └─ ActionDispatcher (Queue(maxsize=16) + 单个 worker 线程，串行执行)
                  └─ MediaController.handle(Action)      <- 所有异常在这里吞掉
                       └─ AdbMediaBackend
                            ├─ PlayPauser  (play_pause.py)  -> 按键阶梯(85→79/126/127) / 点击按钮 / 自动兜底
                            ├─ Rewinder    (rewind.py)      -> 横滑手势 / 媒体键，ms/px 可存档
                            └─ ProfileStore (profiles.py)  -> profiles.json：按「包名|WxH|rN」存按钮点与 ms/px
                                 └─ AdbClient (subprocess -> adb shell input ...)
```

| 文件 | 职责 |
| --- | --- |
| `main.py` | CLI 参数、设备连接、后端装配、监听生命周期、退出码、崩溃处理 |
| `android_adb/config.py` | `@dataclass Config`：默认值 / 加载 merge / 环境变量 / validate 夹取（**配置唯一真源**） |
| `android_adb/logger.py` | 中文日志格式、`_SafeRotatingFileHandler`（轮转失败原地截断） |
| `android_adb/adb_client.py` | 找 adb、`devices()`/`pick_device()`、`shell/keyevent/tap/swipe/screen_size/rotation/media_session_dump`、异常族 |
| `android_adb/media_state.py` | 解析 `dumpsys media_session`：`PlaybackState`、`parse_media_session`、`pick_state`、`media_button_session` |
| `android_adb/play_pause.py` | `PlayPauser` / `KeyeventPlayPauser` / `SmartPlayPauser`（按键阶梯 + 三层点击候选）/ `make_play_pauser` |
| `android_adb/rewind.py` | `Rewinder` / `KeyeventRewinder` / `SwipeRewinder`（闭环校准 + 档案优先）/ `make_rewinder` |
| `android_adb/calibrate.py` | `Calibrator`：`--calibrate` 用（先试无障碍节点，再网格点击找按钮；实测 ms/px） |
| `android_adb/profiles.py` | `profile_key()`、`ProfileStore`：`profiles.json` 读写（按钮比例、ms/px，原子替换） |
| `android_adb/ui_probe.py` | `parse_dump` / `find_play_pause_node` / `find_seek_bar_node` / `UiProber`（uiautomator dump 解析） |
| `android_adb/controller.py` | `Action` 枚举、`ACTION_LABELS`、`MediaController.handle()` |
| `android_adb/mouse_listener.py` | `Debouncer`、`ActionDispatcher`、`MouseHook`、`KeyboardHook`、`HookSupervisor`、`InputMonitor` |
| `android_adb/hotkeys.py` | `parse_combo`/`resolve_key`、`ExitHotkey` |
| `android_adb/backends/` | `base.MediaBackend` 抽象；`adb_backend`（默认）、`dry_run`（演练）、`scrcpy_backend`（**未实现，构造即抛 BackendUnavailable**） |
| `tests/test_core.py` | 83 个离线用例，不需要手机 |
| `start.bat` | Windows 一键启动（**GBK 编码**，开头 `chcp 936`） |
| `README.md` | 面向用户的说明；改行为时同步更新 |

---

## 4. 不变量（改代码时别破坏）

1. **监听线程永不因为 adb 出错而退出**：`MediaController.handle()` 必须吞掉 `BackendError` 和任何异常，只记日志。
2. **adb 调用全部串行**：鼠标回调只做 `queue.put`，真正的 adb 在 worker 线程里跑（adb 单次 50~200ms，并发没意义还会乱序）。
3. **防抖按 key 独立计时**（x1 和 x2 互不影响），默认 350ms。
4. **读不到播放状态时绝不盲点屏幕**（`SmartPlayPauser`：`state is None` -> 只发媒体键）。
5. **模块内只用 `logging`**，不要 `print`（只有 `main.py` 的命令行输出用 print）。
6. **所有 .py 文件 UTF-8**；`start.bat` 必须保持 GBK/ANSI，改成 UTF-8 会让 cmd 解析中文报错。
7. 新增配置项时，四处同时改：`config.py` 字段 + `_INT_FIELDS`/`_FLOAT_FIELDS`/`validate()` + `config.json` + `README.md` 表格。

---

## 5. 血泪坑（都踩过，别再踩）

### 5.1 `keyevent 89` 对 B 站无效 —— 后退必须用滑动
B 站 MediaSession 的 `actions=823`，没有 ACTION_REWIND / FAST_FORWARD。发 89、连发、`cmd media_session dispatch rewind`、
`keyevent 21` 全部无效。所以 `rewind_mode` 默认 `swipe`：读位置 -> `input swipe` -> 再读位置 -> 误差 ≥0.4s 补一刀 ->
用实时测量值指数滑动平均修正 `swipe_ms_per_px`。别想着"换个键码就行"。

### 5.2 媒体键会"失联" —— 播放/暂停需要点击兜底
`keyevent 85` 由系统转给「媒体按钮会话」（`dumpsys media_session` 里的 `Media button session is ...`）。
视频暂停一会儿、或切过 QQ 音乐之后，这一行会变成 `null`，此时按 85 **没有任何 App 收到**（logcat 里连一行都没有）。
点一下画面让 App 重新活跃，会话就回来了。

所以默认 `play_pause_mode: auto`：播放中直接发键；会话为 null 直接点按钮；暂停但有会话则发键后读状态校验，没变才点。
点击兜底要**点两次**（控件栏 3 秒不动会自动隐藏，第一下只是唤出控件），每次点完都重读 `PlaybackState`。

**会话被别的 App 抢占**（新机上实测踩到）：`Media button session is com.ss.android.ugc.aweme/FeedPlayerSession/352 (userId=0)`，
而 B 站的会话只是普通会话。此时发 85 是发给**抖音**的，B 站毫无反应（前后两者 state 都不变，logcat 里 B 站一行都没收到）。
`SmartPlayPauser` 现在先解析会话属主（`_session_owner`，取 `/` 前的包名），属主不是当前视频的包名时**一个媒体键都不发**，
直接走点击兜底。点活 B 站后会话会回到 `tv.danmaku.bili`，85 又能用。

### 5.3 B 站播放按钮无法用无障碍节点定位 —— 所以做成了「三层候选 + 自动标定」
`uiautomator dump` 里只有几十个节点，底部控制条是自绘 SurfaceView，没有任何可点节点。
config 里的默认比例坐标是旧机横屏 2340x1080 下量的 `(92, 976)` -> `x_ratio=0.039, y_ratio=0.904`；
新机 2800x1272 上实测按钮在 `(84, 1107)` = `0.030 / 0.870`（由 `--calibrate` 写进 profiles.json）。
**新设备上别信 config 默认值，先跑 `--calibrate`。**

点击候选顺序（`SmartPlayPauser._tap_candidates`）：`profiles.json` 档案点 -> config 比例点 -> uiautomator 节点点，
按 8px 去重；点中并让状态翻转就写回档案（下次直接用）。换 App / 换手机跑 `python main.py --calibrate`（见 §7）。

**评分坑**：`ui_probe.score_play_pause_node` 里，「包含关键词」必须先满足 `looks_like_control`
（clickable 或 class 含 button/image），否则 B 站文案「播放列表里没有更多视频了哦哦哦」这种 TextView 会被误判成按钮。

### 5.4 faulthandler 长期占用日志文件，导致轮转失败刷屏
旧代码 `_install_crash_handlers` 用 `open(log_path, "a")` 把 faulthandler 挂在 `controller.log` 上，
`RotatingFileHandler` 改名必然 `WinError 32`，每写一条日志就往控制台喷一段 `--- Logging error ---`。
现在：崩溃写独立的 `logs/crash.log`，`_SafeRotatingFileHandler` 轮转失败时原地截断并只提示一次。
**不要把 faulthandler 重新指回 `controller.log`。**

### 5.5 `Path.home()` 可能直接抛 RuntimeError
在服务/精简环境下会抛 `RuntimeError: Could not determine home directory.`。
`adb_client._home_dir()` 已兜底（`USERPROFILE` / `HOME`），新代码要取家目录必须用它。

### 5.6 `AdbClient.run_text()` 的签名
`run()` 有 `timeout`，`run_text()` 曾经没有，导致 `shell()` 传 timeout 时
`TypeError: AdbClient.run_text() got an unexpected keyword argument 'timeout'`。现在已补上，别把参数又丢了。

### 5.7 本机 DSH 沙箱曾经坏过（环境问题，不是项目问题）
早期所有文件/shell 工具启动 worker 都报 `SetNamedSecurityInfoW failed (Win32 5): grantWrite(<仓库根目录>)`，
只能改用 `run_code` 里 Node 的 `fs` + `child_process.spawnSync` 干活。后来策略改成 danger-full-access 才恢复。
如果又遇到同样的报错，先用 `pwsh` 试一条只读命令确认，再决定是否走 Node 绕行。

### 5.8 `spawnSync` 的 env 缺少 `SystemDrive` 会在当前目录生成 `%SystemDrive%\...`
我用精简 env 跑 python/adb 时，Windows 把 `%SystemDrive%\ProgramData\Microsoft\Windows\Caches\*.db`
原样创建在了项目目录下。跑外部命令时 env 至少带上 `SystemRoot`、`SystemDrive`、`USERPROFILE`、`APPDATA`、`TEMP`、`PATH`。
（遇到就删掉那个目录，它是垃圾缓存。）

### 5.9 `pynput` 在部分环境下读不到版本
`import pynput; pynput.__version__` 会 AttributeError，`--doctor` 只判断"能否 import"。

### 5.10 Android 14+ 的 `PlaybackState` 改成了「状态名(数字)」格式
Android 11 写 `state=PlaybackState {state=2, position=...}`，Android 14/16 写
`state=PlaybackState {state=PAUSED(2), position=127883, ..., custom actions=[...]}`。
旧正则 `\bstate=(-?\d+)` 匹配不到，`PlaybackState.state` 恒为 0 -> 被当成"未知状态" ->
永远只发媒体键、不点按钮、后退也测不出位移（`advance_between` 拿不到 position 就跳过校准）。
现在 `media_state._parse_state_value()` 先试 `\{state=([A-Z_]+)\((-?\d+)\)`，再退回纯数字。
`state` 的正则**只在 `_STATE_VALUE_RES` 里**，别再往 `_FIELD_RES` 里加回去（否则会抢匹配）。

### 5.11 Android 16 的 `dumpsys input` 没有 `SurfaceOrientation`
旧 `rotation()` 只匹配 `SurfaceOrientation:\s*(\d)`，在 Android 16 上恒返回 0 ->
`display_size()` 把 2800x1272 当成 1272x2800，点击坐标全错（表现为"点了没反应"或点到别处）。
现在依次试：`SurfaceOrientation` -> `Viewport ... orientation=(\d)`（Android 14+ 有
`Viewport INTERNAL: ..., orientation=1, logicalFrame=[0, 0, 2800, 1272]`）-> `dumpsys display` 的
`mCurrentOrientation=(\d)`。新代码判断横竖屏请复用 `rotation()`/`display_size()`，别自己再解析一遍。

---

## 6. 常用命令（全部实测可用）

```bat
cd /d <仓库根目录>

python -m compileall -q .                       :: 语法检查
python -m unittest discover -s tests -v         :: 83 个用例，不需要手机
python main.py --doctor                         :: 环境自检（设备/屏幕/媒体会话/媒体按钮会话）
python main.py --test play --verbose            :: 真机验证一次播放/暂停
python main.py --test rewind --verbose          :: 真机验证一次后退 5 秒
python main.py --dry-run --duration 3           :: 不连手机，演练键位映射
python main.py --duration 4                     :: 真机监听冒烟（4 秒后退出）
python main.py --monitor                        :: 只打印鼠标/键盘事件，排查"侧键没反应"
python main.py --list-devices                   :: 列设备
python main.py --send-key 85                    :: 直发一个按键
python main.py --calibrate                      :: 自动标定按钮位置 + 滑动比例（换手机/换 App 后跑）
```

- 退出码：`0` 正常 / `1` 配置或参数 / `2` 设备问题 / `3` adb 或后端 / `4` 钩子反复重启失败 / `5` 未预期异常
- 日志：`logs/controller.log`（滚动，DEBUG 级）、`logs/crash.log`（崩溃栈）

---

## 7. 标定坐标（换 App / 竖屏时）

### 7.1 首选：一条命令自动标定

```bat
python main.py --calibrate
```

手机停在视频全屏播放页面。它会：先试 uiautomator 节点，拿不到就在左下角网格（`GRID_X` x `GRID_Y`，8x4 = 最多 32 点）逐点点一下，
用 `PlaybackState.state` 是否翻转判断按钮位置；成功后 `_ensure_playing()` 恢复播放；再横滑 `width*0.25` 像素、
读实际后退秒数算出 `ms/px`；结果写 `profiles.json`（key = `包名|宽x高|r方向`）。

网格是 `GRID_X = (0.030, 0.040, 0.050, 0.060, 0.075, 0.095, 0.120, 0.160)`、
`GRID_Y = (0.850, 0.870, 0.890, 0.910)`；**y 不要再往下扩**，下面就是进度条/章节条，
实测在 B 站章节条上点两下会直接跳章节。新机实测输出：

```text
界面节点里没有播放/暂停按钮（自绘控件），改用网格搜索
开始网格搜索按钮位置（最多 32 个点）
点击 (84, 1081) 没有改变状态（state=2）
命中按钮：(84, 1107) = (0.030, 0.870)
滑动 700 像素测量后退比例……
实测：700 像素 -> 后退 19.5 秒（27.89 ms/px），后退 5 秒约需 179 像素
[OK] 播放/暂停按钮：绝对坐标 (84, 1107)，来源=grid
[OK] 后退换算：27.89 ms/px（后退 5 秒约滑 179 像素）
```

### 7.2 兜底：手动量

```bat
adb exec-out screencap -p > shot.png
```

量出播放/暂停按钮中心的像素 `(px, py)`，除以当前可用宽高：

```
play_pause_x_ratio = px / width
play_pause_y_ratio = py / height
```

横屏（`rotation` 为奇数）时可用宽高会自动交换，所以比例是"相对可用区域"的，转屏不用改。
验证：`python main.py --test play --verbose`，日志里会打印实际点击的绝对坐标。

---

## 8. 测试约定（改代码必读）

- 测试全部**不连手机**。用 `FakeClient` / `FakeSwipeClient` / `FakePlayPauseClient` / `FakeBackend` / `FakeWatchHook`。
- `AdbBackendTest` 里的 `FakeClient` **只有 `keyevent()`**，没有 `media_session_dump()`/`tap()`。
  因此 `SmartPlayPauser` 必须把"读状态失败"降级为"只发媒体键"，否则这个测试会挂（这条约束是有意的）。
- `FakePlayPauseClient` 的 `session` 参数：默认是"有会话"的字符串，**显式传 `None` 才是"媒体按钮会话为 null"**。
  曾经用 None 当默认值，导致"有会话"的用例全走成 null 分支。
- 播/停状态字符串两种格式都要有用例：`state=2`（老）与 `state=PAUSED(2)`（Android 14+）。
  会话属主是别的包名（如 `com.ss.android.ugc.aweme/...`）时必须"不发键、只点击"。
- 真实设备会动！`--test play` 会真的暂停/播放你的视频，`--test rewind` 会真的 seek。测试完把视频恢复成播放状态。
- 禁止把真机序列号、绝对路径写进断言。

---

## 9. 扩展指南

| 想做什么 | 改哪里 |
| --- | --- |
| 加一个新动作（如"音量+"） | `controller.Action` + `ACTION_LABELS` + `handle()` 分支 + 后端方法 + `mouse_listener` 的默认键映射 + 配置字段 |
| 换"后退"的实现 | 写 `rewind.Rewinder` 子类，在 `make_rewinder()` 里按 `rewind_mode` 返回；同时在 `config.validate()` 的白名单里加新值 |
| 换"播放/暂停"的实现 | 写 `play_pause.PlayPauser` 子类，在 `make_play_pauser()` 里按 `play_pause_mode` 返回 + 白名单加值 |
| 接 Scrcpy | 实现 `backends/scrcpy_backend.py`（顶部注释写了控制协议要点：27183 端口，type=0 为 INJECT_KEYCODE，负载 action(u8)+keycode(u32)+repeat(u32)+metaState(u32)，发 DOWN/UP 两帧），然后在 `main.build_backend()` 里已有分支；`backend: "scrcpy"` 即可切换 |
| 换控制通道（蓝牙 HID / 宏键盘） | 实现 `backends/base.MediaBackend` 的 `play_pause()`/`rewind()`/`describe()`/`close()`，在 `build_backend()` 加分支，其余代码不动 |

---

## 10. 配置字段速查

完整字段与注释见 `config.json` 与 `android_adb/config.py`（那边是唯一真源）。容易搞混的几个：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `play_pause_mode` | `auto` | `auto` 媒体键 + 点击兜底 / `media_key` 只发键不点屏幕 / `tap` 只点按钮 |
| `play_pause_x_ratio`, `play_pause_y_ratio` | 0.039 / 0.904 | 播放按钮比例坐标 |
| `play_pause_verify_ms` | 400 | 发键后等多久再读状态（实测 200~400ms 足够翻转） |
| `play_pause_tap_settle_ms` | 350 | 点完等多久再读状态 |
| `play_pause_extra_keycodes` | null | 主键无效后依次试的键码；null = [79, 126, 127]，空 = 不试 |
| `ui_probe` | true | 是否用 uiautomator 节点找按钮（自绘控件拿不到就跳过） |
| `profiles_file` | `profiles.json` | 标定档案路径；相对 BASE_DIR，null = 不存档（已 gitignore） |
| `rewind_mode` | `swipe` | `swipe` 手势 / `media_key` 老方案（对 B 站无效） |
| `swipe_seconds` | 5.0 | 每次后退目标秒数 |
| `swipe_ms_per_px` | 31.0 | 初始换算，会被自动校准覆盖 |
| `auto_calibrate` | true | 按实测秒数修正换算 |
| `debounce_ms` | 350 | 防抖窗口 |
| `exit_key` | `ctrl+alt+q` | 组合键；**故意不用 Esc**（游戏里 Esc 开菜单会误退出） |
| `rewind_key` / `play_pause_key` | null | 键盘备用绑定，绕开游戏对鼠标钩子的拦截 |
| `hook_restart` / `hook_restart_limit` | true / 30 | 钩子被系统摘掉后自动重挂 |
| `backend` | `adb` | `adb` / `scrcpy`（后者未实现） |

---

## 11. 已知限制 / 待办

- `scrcpy_backend` 只是占位，选它会立刻报"尚未实现"
- 自绘控件（B 站全屏播放器）只能靠 `--calibrate` 的网格点击定位，理论上可能点到别的控件（范围限制在左下角）
- 标定按「包名 + 分辨率 + 方向」隔离；手机改分辨率/缩放后要重跑 `--calibrate`
- 后退精度依赖 `dumpsys media_session` 的 position（部分 App 只在状态变化时才上报，`rewind.py` 已做两次快照补偿）
- `input swipe` 是模拟手指，若滑动区域有 App 自己的手势（弹幕、双击点赞）会误判，用 `swipe_y_ratio` / `swipe_start_ratio` 挪开
- 只在 Windows + 这台安卓机上验证过；macOS/Linux 需自行配 `adb_path`，pynput 权限另需授权
- 多设备时用 `--device` 或 `config.device_serial`（同机装了 MuMu 模拟器时尤其注意）
- 媒体按钮会话可能被别的 App（抖音、QQ 音乐）占着，程序会自动改点击，但**代价是慢一点**；
  想让 85 一直好用，先让 B 站成为当前媒体会话（点一下播放器即可）

---

## 12. 收尾检查清单

改完代码，按顺序过一遍：

1. `python -m compileall -q .`
2. `python -m unittest discover -s tests -v`（必须 83 passed，新增行为要补用例）
3. `python main.py --doctor`（看"媒体按钮会话"+"标定档案"两行）
4. `python main.py --test play --verbose`（真机；结束把视频恢复成播放）
5. 行为变了就同步 `README.md`（用户文档）和本文件（Agent 文档）
6. 删掉临时产物：`.screen*.png`、`config.tap.json`、`logs/*.bak`、`%SystemDrive%`（见 §5.8）
7. 手机保持可用状态（视频在播放，别留在暂停或 seek 到奇怪的位置）

---

## 13. 版本管理（git）

- 仓库就在项目根目录：`<仓库根目录>\.git`，主分支 `main`，已含初始提交
- **git 不在系统 PATH**，完整路径 `E:\elsedownload\Git\cmd\git.exe`（2.56.0）。
  要么用全路径调用，要么把 `E:\elsedownload\Git\cmd` 加进 PATH 后重开终端
- 本仓库已配置本地身份 `<你的名字> <you@example.com>`（只影响这个仓库，不是全局）。要换成自己的：

  ~~~bat
  "E:\elsedownload\Git\cmd\git.exe" config user.email "you@example.com"
  "E:\elsedownload\Git\cmd\git.exe" config user.name "Your Name"
  ~~~

- `.gitignore` 忽略：`__pycache__/`、`*.pyc`、`logs/`、`*.log`、根目录截图 `/*.png`、
  `config.tap.json`、`*.bak`、`.vscode/`、`.idea/` 等（详见文件内注释）
- `.gitattributes`：仓库内统一 LF，`*.bat` 检出时转 CRLF。
  **别提交 CRLF 的 .py**，也别把 `start.bat` 的 CRLF 改成 LF（cmd 对 LF 的 goto/label 处理不可靠）
- `config.json` 是**被跟踪**的默认配置。改键位调参会产生 diff：要么一起提交，要么 `git stash` 暂存
- `logs/` 整个目录不跟踪，程序启动时自己创建
- 提交前至少跑一遍 §12 的 1、2 步（compileall + unittest），提交信息用中文、一句话说清"改了什么、为什么"

### 13.1 远端与网络（本机特有，别踩）

- 远端：`origin` = `https://github.com/LEI-HAI/Android_ADB.git`，`main` 已跟踪 `origin/main`
- **本机直连 GitHub 不通**：`curl https://github.com` 与 `ssh -T git@github.com` 都是超时（20 秒 0 字节）。
  必须先过一个本地代理，系统代理是 `127.0.0.1:7897`（Clash 类工具；注册表 `ProxyEnable=1` 可查）
- 本仓库已写死 `http.proxy=http://127.0.0.1:7897`。代理软件换端口时：

  ~~~bat
  "E:\elsedownload\Git\cmd\git.exe" config http.proxy "http://127.0.0.1:新端口"
  "E:\elsedownload\Git\cmd\git.exe" config --unset http.proxy   :: 不需要代理时
  ~~~

- 凭据：Git Credential Manager 已存 GitHub 账号（系统配置 `credential.helper=manager`），push 不需要输密码。
  在无交互环境跑 git 时加 `GIT_TERMINAL_PROMPT=0`，免得卡在提示符上
- 推送：`git push`（首次是 `git push -u origin main`）。推完用 `git ls-remote origin` 核对远端 HEAD

**2026-11-10 现状**：代理重新打开（7897 有监听），`git push` 直接成功（`e230fd1..9abbd3a`），不用带 `curloptResolve`。
仓库已设为**公开**，加了 topics（adb / android / python / pynput / media-control 等）和 MIT LICENSE。

**2026-10-31 的历史情况（代理已关，靠换 IP 推送）**：`ProxyEnable=0`，7897/7890/10809 全都无人监听，
仓库里写死的 `http.proxy=http://127.0.0.1:7897` 会让 `git push` 直接报
`Failed to connect to github.com:443 over proxy 127.0.0.1 after 1 ms`。此时：

- `https://api.github.com` 直连 200，`https://codeload.github.com:443`、`https://ssh.github.com:443` 通则，
  **只有 `github.com:443` 不通**（DNS 把它解析到 `20.205.243.166`，这个 IP 被墙；`140.82.112.4` 是通的）
- 所以推送用「绕过代理 + 指定 IP」：

  ~~~bat
  "E:\elsedownload\Git\cmd\git.exe" -c http.proxy= -c http.curloptResolve=github.com:443:140.82.112.4 push
  ~~~

  实测 `5f69c92..39cca7f main -> main` 成功。`ssh.github.com:443` 能连但**没有登记 SSH 公钥**
  （`git@ssh.github.com: Permission denied (publickey)`），所以别改走 SSH
- 代理重新打开后，把 `http.proxy` 改回 `http://127.0.0.1:7897` 即可，不用带 `curloptResolve`
