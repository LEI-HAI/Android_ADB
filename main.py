#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Android ADB 鼠标侧键控制器 —— 程序入口。

常用命令：
    python main.py                  启动全局监听（默认连接第一台设备）
    python main.py --dry-run        不连手机，只打印将要发送的命令
    python main.py --doctor         环境自检（Python / pynput / adb / 设备）
    python main.py --list-devices   查看已连接设备
    python main.py --send-key 224   发送单个按键（调试用）
    python main.py --test play      测试一次播放/暂停
    python main.py --test rewind    测试一次后退
    python main.py --calibrate      自动标定按钮位置与滑动比例
    python main.py --monitor        打印所有鼠标/键盘事件（排查侧键失效）
    python main.py --duration 5     监听 5 秒后自动退出（调试用）

退出方式：Ctrl+C，或组合键 Ctrl+Alt+Q（config.json 的 exit_key 可改）。
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import faulthandler
import logging
import os
import sys
import threading
import time

from pathlib import Path
from typing import List, Optional, Tuple

from android_adb import __version__
from android_adb.adb_client import (
    AdbClient,
    AdbError,
    AdbNotFoundError,
    DeviceError,
    DeviceInfo,
    find_adb,
)
from android_adb.backends.adb_backend import AdbMediaBackend
from android_adb.backends.base import BackendError, MediaBackend
from android_adb.backends.dry_run import DryRunBackend
from android_adb.backends.scrcpy_backend import ScrcpyMediaBackend
from android_adb.calibrate import Calibrator
from android_adb.config import Config, ConfigError
from android_adb.controller import Action, MediaController
from android_adb.hotkeys import ExitHotkey
from android_adb.logger import setup_logging
from android_adb.media_state import media_button_session, parse_media_session, pick_state
from android_adb.profiles import ProfileStore
from android_adb.mouse_listener import (
    ActionDispatcher,
    HookSupervisor,
    InputMonitor,
    KeyboardHook,
    MouseHook,
)

log = logging.getLogger("main")

#提权
# def is_admin():
#     try:
#         return ctypes.windll.shell32.IsUserAnAdmin()
#     except:
#         return False

# if not is_admin():
#     # 如果不是管理员，则请求提权并重新运行当前脚本
#     ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
#     sys.exit()

_BANNER = """
============================================================
  Android ADB 鼠标侧键控制器  v{version}
  侧键1 -> 后退 {seconds:g} 秒          侧键2 -> 播放 / 暂停
============================================================
  提示：游戏 / 全屏程序里侧键无效时，请用「以管理员身份运行」
        启动 start.bat；仍无效可执行 python main.py --monitor 排查
============================================================
""".strip("\n")


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
def _disable_quick_edit() -> None:
    """关掉控制台的「快速编辑模式」。

    cmd 窗口默认开着快速编辑：用鼠标在窗口里点一下就会进入"选择文本"状态，
    此时任何往控制台写日志的操作都会被阻塞（直到你按 Esc 或点一下）。
    监听回调在写日志时被卡住超过 300ms，Windows 就会按 LowLevelHooksTimeout
    把全局鼠标钩子静默摘掉——表现就是"程序还在，但侧键彻底没反应"。
    """
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = [ctypes.c_int]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        mode = ctypes.c_uint32(0)
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        new_mode = (mode.value & ~0x0040) & ~0x0020  # 去掉 QUICK_EDIT_MODE / INSERT_MODE
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


def _prepare_console() -> None:
    """Windows 控制台默认可能是 GBK，统一改成 UTF-8，避免中文/符号报错。"""
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
        _disable_quick_edit()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_CRASH_FILES: list = []
_LOG_PATH: Optional[Path] = None


def resolve_log_path(cfg: Config) -> Optional[Path]:
    """日志文件路径（相对项目根目录），关闭时返回 None。"""
    if not cfg.log_file:
        return None
    target = Path(cfg.log_file).expanduser()
    if not target.is_absolute():
        target = Path(__file__).resolve().parent / target
    return target


def _install_crash_handlers(log_path: Optional[Path]) -> None:
    """把硬崩溃、主线程异常、子线程异常都写进日志。

    没有这段时，进程如果是被原生错误（访问冲突 / 栈溢出）干掉，或者某个线程
    抛了未捕获异常，控制台只会"窗口一闪就没了"，什么都看不到。
    """
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # 单独一个文件：faulthandler 会一直占着这个句柄，
            # 若指向 controller.log 会让 RotatingFileHandler 无法改名（WinError 32）。
            crash_path = log_path.with_name("crash.log")
            crash_file = open(crash_path, "a", encoding="utf-8", buffering=1)
            _CRASH_FILES.append(crash_file)
            faulthandler.enable(file=crash_file, all_threads=True)
        except OSError:
            pass

    crash_log = logging.getLogger("crash")

    def _excepthook(exc_type, exc, tb) -> None:
        crash_log.critical("主线程未捕获异常：%s", exc, exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook

    def _thread_hook(args) -> None:
        thread_name = getattr(args.thread, "name", "?")
        crash_log.critical(
            "线程 %s 未捕获异常：%s",
            thread_name,
            args.exc_value,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook


def connect_device(cfg: Config, serial: Optional[str] = None) -> Tuple[AdbClient, DeviceInfo]:
    """查找 adb、连接设备并绑定，返回客户端与设备信息。"""
    adb_path = find_adb(cfg)
    log.info("使用 adb：%s", adb_path)
    client = AdbClient(adb_path, timeout=cfg.adb_timeout)

    if cfg.adb_connect:
        log.info("正在连接无线调试设备 %s ...", cfg.adb_connect)
        output = client.connect(cfg.adb_connect)
        if output:
            log.info("adb connect：%s", output.replace("\n", " "))

    device = client.pick_device(serial or cfg.device_serial)
    client.bind(device.serial)
    log.info("已连接设备：%s", device)
    return client, device


def build_backend(cfg: Config) -> MediaBackend:
    """根据配置构造按键后端。"""
    if cfg.backend == "scrcpy":
        return ScrcpyMediaBackend(cfg)
    if cfg.dry_run:
        return DryRunBackend(cfg)
    client, _device = connect_device(cfg)
    return AdbMediaBackend(client, cfg)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_list_devices(cfg: Config) -> int:
    adb_path = find_adb(cfg)
    client = AdbClient(adb_path, timeout=cfg.adb_timeout)
    print(f"adb：{adb_path}")
    devices = client.devices()
    if not devices:
        print("未检测到设备。请检查数据线、USB 调试开关，以及手机上的授权弹窗。")
        return 2
    print("已检测到设备：")
    for device in devices:
        print(f"  - {device.serial:<20} 状态={device.state:<12} 型号={device.model or '未知'}")
    return 0


def _doctor_probe(client: AdbClient, device: DeviceInfo) -> None:
    """连上设备后顺带看一下屏幕与媒体会话，帮助判断后退方式是否可用。"""
    client.bind(device.serial)
    try:
        width, height = client.display_size()
        print(f"[OK] 屏幕：{width}x{height}（rotation={client.rotation()}）")
    except AdbError as exc:
        print(f"[!!] 读取屏幕分辨率失败：{exc}")
    try:
        dump = client.media_session_dump()
    except AdbError as exc:
        print(f"[!!] 读取媒体会话失败：{exc}")
        return
    session = media_button_session(dump)
    if session:
        print(f"[OK] 媒体按钮会话：{session}")
    else:
        print("[--] 媒体按钮会话：null（此刻按媒体键没人接收，程序会自动改用点击画面按钮）")

    state = pick_state(parse_media_session(dump))
    if state is None:
        print("[--] 未发现媒体会话（打开视频后再自检即可看到 position）")
        return
    label = {1: "已停止", 2: "已暂停", 3: "播放中"}.get(state.state, f"state={state.state}")
    print(f"[OK] 媒体会话：{state.package}（{label}，position={state.position_ms / 1000:.1f}s）")


def cmd_doctor(cfg: Config) -> int:
    print("\n=== 环境自检 ===")
    ok = True

    print(f"Python：{sys.version.split()[0]}（{sys.executable}）")
    print(
        f"后退方式：{cfg.rewind_mode}"
        + (f"（目标 {cfg.swipe_seconds:g} 秒，{cfg.swipe_ms_per_px:.0f} ms/px）" if cfg.rewind_mode == "swipe" else "")
    )
    print(
        f"播放/暂停方式：{cfg.play_pause_mode}"
        + (
            f"（点击位置比例 {cfg.play_pause_x_ratio:.3f} / {cfg.play_pause_y_ratio:.3f}）"
            if cfg.play_pause_mode != "media_key"
            else ""
        )
    )
    if cfg.profiles_file:
        store = ProfileStore(cfg.profiles_file)
        keys = store.keys()
        print(
            f"标定档案：{store.path}"
            + (f"（{len(keys)} 条：{', '.join(keys[:3])}）" if keys else "（暂无记录，跑 --calibrate 生成）")
        )
    if sys.version_info < (3, 9):
        ok = False
        print("[!!] 需要 Python 3.9 及以上版本")

    try:
        import pynput  # noqa: F401

        print("[OK] pynput 已安装")
    except Exception as exc:
        ok = False
        print(f"[!!] 无法导入 pynput：{exc}")
        print("     请执行：pip install -r requirements.txt")

    try:
        adb_path = find_adb(cfg)
        print(f"[OK] adb：{adb_path}")
        client = AdbClient(adb_path, timeout=cfg.adb_timeout)
        try:
            print(f"[OK] adb 版本：{client.version()}")
        except AdbError as exc:
            ok = False
            print(f"[!!] 无法执行 adb version：{exc}")

        try:
            devices = client.devices()
        except AdbError as exc:
            ok = False
            print(f"[!!] 读取设备列表失败：{exc}")
            devices = []

        if not devices:
            ok = False
            print("[!!] 未检测到设备：请检查 USB 数据线、USB 调试开关、手机授权弹窗")
        else:
            for device in devices:
                flag = "OK" if device.state == "device" else "!!"
                print(f"[{flag}] 设备 {device.serial} 状态={device.state} 型号={device.model or '未知'}")
            ready = [d for d in devices if d.state == "device"]
            if ready:
                _doctor_probe(client, ready[0])
    except AdbNotFoundError as exc:
        ok = False
        print(f"[!!] {exc}")

    print("\n自检结果：" + ("通过，可以启动主程序" if ok else "存在问题，请先按提示处理"))
    return 0 if ok else 1


def cmd_send_key(cfg: Config, keycode: str) -> int:
    client, device = connect_device(cfg)
    client.keyevent(keycode)
    log.info("已向 %s 发送按键 %s", device, keycode)
    return 0


def cmd_test(cfg: Config, which: str) -> int:
    backend = build_backend(cfg)
    controller = MediaController(backend)
    print(f"测试后端：{backend.describe()}")
    if which in ("rewind", "all"):
        controller.handle(Action.REWIND)
        time.sleep(0.6)
    if which in ("play", "all"):
        controller.handle(Action.PLAY_PAUSE)
    return 0


def cmd_calibrate(cfg: Config) -> int:
    """自动标定：找到点得动的播放按钮，并实测后退的滑动换算比例。"""
    print("\n=== 标定（请先在手机上把视频停在全屏播放页面）===")
    backend = build_backend(cfg)
    if not isinstance(backend, AdbMediaBackend):
        print("[!!] 标定只支持 adb 后端")
        return 1
    calibrator = Calibrator(backend.client, cfg)
    try:
        result = calibrator.run()
    finally:
        backend.close()

    point = result.get("point")
    print()
    if point is None:
        print("[!!] 没找到能切换播放/暂停的位置（已试过界面节点和网格点击）")
        print("     确认手机停在播放页面、屏幕没有锁，然后重试；仍失败请手动改 config.json 的比例坐标")
        return 1
    x, y, source = point
    print(f"[OK] 播放/暂停按钮：绝对坐标 ({x}, {y})，来源={source}")

    ratio = result.get("ms_per_px")
    if ratio is None:
        print("[--] 后退比例未测出（位置信息读不到或滑动没生效），仍沿用配置里的值")
    else:
        px = int(round(cfg.swipe_seconds * 1000 / ratio))
        print(f"[OK] 后退换算：{ratio:.2f} ms/px（后退 {cfg.swipe_seconds:g} 秒约滑 {px} 像素）")
    print(f"结果已写入：{cfg.profiles_file or 'profiles.json'}（键 {result.get('key')}）")
    print("以后换手机 / 换 App，跑一次本命令即可")
    return 0


def _log_device_info(backend: MediaBackend) -> None:
    """启动时把手机状态说清楚，省得用户猜"为什么没反应"。"""
    if not isinstance(backend, AdbMediaBackend):
        return
    log.info("%s", backend.describe_device())
    summary = backend.media_summary()
    if summary:
        log.info("当前媒体会话：%s", summary)
    else:
        log.info("当前未检测到正在播放的媒体会话（先打开视频，再按侧键）")


def cmd_monitor(cfg: Config, duration: Optional[float] = None) -> int:
    print("=== 输入监视模式 ===")
    print("在游戏窗口里点一下鼠标侧键，观察下面是否出现 [鼠标] x1 / x2：")
    print("  有输出 -> 钩子正常，问题在手机端或 config.json 的按键映射")
    print("  无输出 -> 被游戏 / 反作弊拦截，请「以管理员身份运行」start.bat")
    print("按 Esc（仅结束监视）或 Ctrl+C 退出\n")
    monitor = InputMonitor()
    try:
        monitor.start()
    except Exception as exc:
        log.error("启动输入监视失败：%s", exc)
        return 1
    monitor.run(duration)
    log.info("监视结束")
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _human_uptime(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {sec} 秒"
    return f"{sec} 秒"


def run_listener(cfg: Config, duration: Optional[float] = None) -> int:
    backend = build_backend(cfg)
    controller = MediaController(backend)
    _log_device_info(backend)
    dispatcher = ActionDispatcher(controller.handle)
    dispatcher.start()

    stop_event = threading.Event()
    fatal_event = threading.Event()

    hook = MouseHook(cfg, dispatcher)
    key_hook = KeyboardHook(cfg, dispatcher)
    try:
        hook.start()
        if key_hook.enabled:
            key_hook.start()
    except Exception as exc:
        log.error("启动监听失败：%s", exc)
        key_hook.stop()
        dispatcher.stop()
        controller.close()
        return 1

    # 看门狗：锁屏 / 休眠唤醒 / UAC 弹窗 / 驱动更新都会让 Windows 摘掉全局钩子，
    # 以前这里会直接退出（窗口一闪就没了），现在改成自动重启。
    watched = [hook]
    if key_hook.enabled:
        watched.append(key_hook)
    supervisor = HookSupervisor(
        watched,
        restart=cfg.hook_restart,
        limit=cfg.hook_restart_limit,
        on_give_up=fatal_event.set,
    )
    supervisor.start()

    exit_hotkey: Optional[ExitHotkey] = None
    if cfg.enable_exit_hotkey:
        try:
            exit_hotkey = ExitHotkey(cfg.exit_key, on_trigger=stop_event.set)
            exit_hotkey.start()
            if not exit_hotkey.has_modifier:
                log.warning(
                    "退出键 %s 是单键，全局生效：任何程序里按到它都会退出本程序"
                    "（例如游戏里按 %s 打开菜单，控制器就直接关了）。建议改成组合键，如 ctrl+alt+q",
                    exit_hotkey.spec.upper(),
                    exit_hotkey.spec.upper(),
                )
        except Exception as exc:
            log.warning("退出快捷键不可用（%s），请使用 Ctrl+C 退出", exc)

    log.info("后端：%s", backend.describe())
    if cfg.hook_restart:
        limit_text = f"最多连续 {cfg.hook_restart_limit} 次" if cfg.hook_restart_limit else "不限次数"
        log.info("看门狗已启动：监听意外停止会自动重启（%s）", limit_text)
    log.info(
        "监听中…… 按 %s 或 Ctrl+C 退出",
        cfg.exit_key.upper() if cfg.enable_exit_hotkey else "Ctrl+C",
    )

    started = time.monotonic()
    next_beat = started + cfg.heartbeat_seconds if cfg.heartbeat_seconds else None
    deadline = started + duration if duration else None
    try:
        while not stop_event.is_set() and not fatal_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            stop_event.wait(0.4)
            if next_beat is not None and time.monotonic() >= next_beat:
                next_beat = time.monotonic() + cfg.heartbeat_seconds
                log.info("心跳：监听中（已运行 %s）", _human_uptime(time.monotonic() - started))
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在退出……")
    finally:
        supervisor.stop()
        key_hook.stop()
        hook.stop()
        if exit_hotkey is not None:
            exit_hotkey.stop()
        dispatcher.stop()
        controller.close()

    if fatal_event.is_set():
        log.error(
            "监听钩子反复重启仍无法恢复，程序退出。日志：%s",
            resolve_log_path(cfg) or "（未开启文件日志）",
        )
        return 4
    log.info("已退出")
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="用鼠标侧键控制安卓手机的播放/暂停与后退 5 秒（基于 ADB）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py\n"
            "  python main.py --dry-run\n"
            "  python main.py --doctor\n"
            "  python main.py --device <serial>\n"
        ),
    )
    parser.add_argument("--config", type=str, default=None, help="配置文件路径，默认使用项目下的 config.json")
    parser.add_argument("--device", type=str, default=None, help="指定设备序列号（多设备时使用）")
    parser.add_argument("--backend", choices=("adb", "scrcpy"), default=None, help="按键后端，默认 adb")
    parser.add_argument("--dry-run", action="store_true", help="演练模式：只打印命令，不真正发送")
    parser.add_argument("--list-devices", action="store_true", help="列出已连接设备后退出")
    parser.add_argument("--doctor", action="store_true", help="环境自检后退出")
    parser.add_argument("--send-key", type=str, default=None, metavar="KEYCODE", help="发送单个按键后退出，如 85 或 KEYCODE_MEDIA_PLAY_PAUSE")
    parser.add_argument("--test", choices=("play", "rewind", "all"), nargs="?", const="play", default=None, help="测试一次动作后退出")
    parser.add_argument("--calibrate", action="store_true", help="自动标定播放按钮位置与后退滑动比例（写入 profiles.json）")
    parser.add_argument("--monitor", action="store_true", help="监视并打印所有鼠标/键盘事件（排查侧键失效）")
    parser.add_argument("--duration", type=float, default=None, metavar="SECONDS", help="监听指定秒数后自动退出（调试用）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def _log_exit() -> None:
    """进程结束（无论什么原因）都留一行，方便和日志里的最后一条对照。"""
    try:
        logging.getLogger("main").debug("进程结束（atexit）")
    except Exception:
        pass


def _run(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    _prepare_console()

    try:
        cfg = Config.load(Path(args.config).expanduser() if args.config else None)
    except (ConfigError, OSError) as exc:
        print(f"[错误] 配置无效：{exc}")
        return 1

    if args.device:
        cfg.device_serial = args.device
    if args.backend:
        cfg.backend = args.backend
    if args.dry_run:
        cfg.dry_run = True
    if args.verbose:
        cfg.log_level = "DEBUG"

    log_path = resolve_log_path(cfg)
    global _LOG_PATH
    _LOG_PATH = log_path
    setup_logging(cfg.log_level, log_path, cfg.log_file_level)
    _install_crash_handlers(log_path)
    atexit.register(_log_exit)
    log.debug(
        "启动：Python %s，工作目录 %s，配置 %s",
        sys.version.split()[0],
        os.getcwd(),
        Path(args.config).expanduser() if args.config else "config.json",
    )
    if log_path is not None:
        log.debug("日志文件：%s", log_path)

    if not (
        args.doctor
        or args.list_devices
        or args.send_key
        or args.test
        or args.monitor
        or args.calibrate
    ):
        _prepare_console()
        print(_BANNER.format(version=__version__, seconds=cfg.swipe_seconds))
        if log_path is not None:
            print(f"  日志文件：{log_path}")
        if cfg.dry_run:
            log.info("演练模式：不会真正向手机发送指令")

    try:
        if args.list_devices:
            return cmd_list_devices(cfg)
        if args.doctor:
            return cmd_doctor(cfg)
        if args.send_key:
            return cmd_send_key(cfg, args.send_key)
        if args.test:
            return cmd_test(cfg, args.test)
        if args.calibrate:
            return cmd_calibrate(cfg)
        if args.monitor:
            return cmd_monitor(cfg, duration=args.duration)
        return run_listener(cfg, duration=args.duration)
    except KeyboardInterrupt:
        log.info("已退出")
        return 0
    except AdbNotFoundError as exc:
        log.error("%s", exc)
        return 3
    except DeviceError as exc:
        log.error("%s", exc)
        return 2
    except ConfigError as exc:
        log.error("配置无效：%s", exc)
        return 1
    except BackendError as exc:
        log.error("%s", exc)
        return 3
    except Exception as exc:  # 兜底：绝不让窗口「一闪就没」
        log.exception("未预期的错误：%s", exc)
        return 5


_EXIT_HINTS = {
    1: "配置/参数有问题",
    2: "没检测到手机（检查数据线、USB 调试、手机上的授权弹窗）",
    3: "adb 或控制后端出错（先跑 python main.py --doctor）",
    4: "鼠标/键盘钩子反复重启仍失败，多半被游戏或安全软件拦了（试试以管理员身份运行）",
    5: "程序内部出现未预期的错误（详情见日志）",
}


def _explain_exit(code: int) -> None:
    """出错退出时，把「为什么退出、日志在哪」打在窗口里。

    由 Python 自己打印而不是交给 start.bat，因为 python 退出后控制台代码页已经
    变成 65001，批处理的 echo 再往外写就容易和 python 的输出互相覆盖/乱码。
    """
    if code == 0:
        return
    log_path = _LOG_PATH
    try:
        print()
        print("=" * 62)
        print(f"  程序已退出，退出码 {code}：{_EXIT_HINTS.get(code, '未知原因')}")
        if log_path is not None:
            print(f"  日志文件：{log_path}")
        print("  排查命令：python main.py --doctor（查手机）")
        print("            python main.py --monitor（查鼠标钩子）")
        print("=" * 62)
        sys.stdout.flush()
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    try:
        code = _run(argv)
    except KeyboardInterrupt:
        print()
        code = 0
    except Exception:
        code = 5
        try:
            logging.getLogger("main").exception("未处理异常")
        except Exception:
            pass
    _explain_exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
