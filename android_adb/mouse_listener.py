"""全局鼠标监听：识别侧键、做防抖、把动作丢进后台队列。

ADB 命令有几十到几百毫秒的耗时，如果在监听回调里直接执行，会阻塞后续事件，
所以这里用一条独立线程按顺序执行（队列 + 单消费者），既不会丢动作也不会卡监听。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Dict, Optional

from pynput import keyboard, mouse

from .config import Config
from .controller import ACTION_LABELS, Action
from .hotkeys import canonical, parse_combo

log = logging.getLogger(__name__)


class Debouncer:
    """按"按键名 + 时间窗"做防抖：窗口内的重复按下直接丢弃。"""

    def __init__(self, window_ms: int) -> None:
        self._window = max(0, int(window_ms)) / 1000.0
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self._window <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._window:
                return False
            self._last[key] = now
            return True


class ActionDispatcher(threading.Thread):
    """单线程消费者：串行执行动作，避免多个 adb 命令互相抢占。"""

    def __init__(
        self,
        handler: Callable[[Action], bool],
        maxsize: int = 16,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(name="action-dispatcher", daemon=True)
        self._handler = handler
        self._log = logger or log
        self._queue: "queue.Queue[Action]" = queue.Queue(maxsize=maxsize)
        self._stopped = threading.Event()

    def submit(self, action: Action) -> bool:
        if self._stopped.is_set():
            return False
        try:
            self._queue.put_nowait(action)
            return True
        except queue.Full:
            self._log.warning("指令队列已满，丢弃本次 %s（连按过快）", ACTION_LABELS.get(action, action))
            return False

    def run(self) -> None:
        while not self._stopped.is_set():
            try:
                action = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if action is None:  # 停止哨兵
                break
            try:
                self._handler(action)
            except Exception:
                self._log.exception("处理动作 %s 时出错", action)

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self.join(timeout=1.5)


class MouseHook:
    """pynput 全局鼠标监听，只关心按下（pressed=True）的侧键。"""

    def __init__(
        self,
        config: Config,
        dispatcher: ActionDispatcher,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._cfg = config
        self._dispatcher = dispatcher
        self._log = logger or log
        self._listener: Optional[mouse.Listener] = None
        self._debounce = Debouncer(config.debounce_ms)
        self._unknown: "set[str]" = set()
        self._mapping: Dict[str, Action] = {
            config.rewind_button: Action.REWIND,
            config.play_pause_button: Action.PLAY_PAUSE,
        }

    def start(self) -> None:
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.daemon = True
        self._listener.start()
        self._log.info(
            "鼠标监听已启动：%s -> %s，%s -> %s",
            self._cfg.rewind_button.upper(),
            ACTION_LABELS[Action.REWIND],
            self._cfg.play_pause_button.upper(),
            ACTION_LABELS[Action.PLAY_PAUSE],
        )

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        # 回调里任何异常都会让 pynput 的监听线程直接结束，所以统一兜住
        try:
            self._handle_click(x, y, button, pressed)
        except Exception:
            self._log.exception("处理鼠标事件时出错（已忽略，监听继续）")

    def _handle_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if not pressed:
            return
        name = getattr(button, "name", str(button))
        self._log.debug("鼠标事件：%s（%s）", name, "按下" if pressed else "抬起")
        action = self._mapping.get(name)
        if action is None:
            self._log.debug("忽略鼠标键：%s（当前只映射 %s）", name, "/".join(sorted(self._mapping)))
            self._unknown.add(name)
            return
        if not self._debounce.allow(name):
            self._log.debug("防抖：%.0fms 内重复的 %s 已忽略", self._cfg.debounce_ms, name)
            return
        self._log.info("检测到鼠标侧键 %s -> %s", name.upper(), ACTION_LABELS[action])
        self._dispatcher.submit(action)

    @property
    def is_alive(self) -> bool:
        return bool(self._listener and self._listener.is_alive())

    def restart(self) -> None:
        """钩子被系统摘下后重新挂上。"""
        self.stop()
        time.sleep(0.2)
        self.start()

    def stop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        self._listener = None
        try:
            listener.stop()
            listener.join(timeout=1.0)
        except Exception:
            pass


class KeyboardHook:
    """可选的键盘备用绑定：把 "ctrl+alt+left" 这类组合映射成动作。

    适用场景：
    - 游戏 / 反作弊拦截了全局鼠标钩子；
    - 鼠标驱动把侧键改成了键盘宏（驱动里设成 ctrl+alt+left，这里绑定同一组合）。
    """

    def __init__(
        self,
        config: Config,
        dispatcher: ActionDispatcher,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._cfg = config
        self._dispatcher = dispatcher
        self._log = logger or log
        pairs = []
        if config.rewind_key:
            pairs.append((str(config.rewind_key), Action.REWIND))
        if config.play_pause_key:
            pairs.append((str(config.play_pause_key), Action.PLAY_PAUSE))
        self._bindings = [(parse_combo(spec), spec, action) for spec, action in pairs]
        self._pressed: "set[str]" = set()
        self._debounce = Debouncer(config.debounce_ms)
        self._listener: Optional[keyboard.Listener] = None

    @property
    def enabled(self) -> bool:
        return bool(self._bindings)

    def describe(self) -> str:
        return "，".join(
            f"{spec.upper()} -> {ACTION_LABELS[action]}" for _combo, spec, action in self._bindings
        )

    def start(self) -> None:
        if not self.enabled:
            return
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.daemon = True
        self._listener.start()
        self._log.info("键盘备用绑定已启动：%s", self.describe())

    def _on_press(self, key) -> None:
        try:
            self._handle_press(key)
        except Exception:
            self._log.exception("处理键盘事件时出错（已忽略，监听继续）")

    def _handle_press(self, key) -> None:
        if key is None:
            return
        self._pressed.add(canonical(key))
        for combo, spec, action in self._bindings:
            if not combo <= self._pressed:
                continue
            if not self._debounce.allow(spec):
                self._log.debug("防抖：%.0fms 内重复的 %s 已忽略", self._cfg.debounce_ms, spec)
                break
            self._log.info("检测到快捷键 %s -> %s", spec.upper(), ACTION_LABELS[action])
            self._dispatcher.submit(action)
            break

    def _on_release(self, key) -> None:
        try:
            if key is None:
                return
            self._pressed.discard(canonical(key))
        except Exception:
            self._log.exception("处理键盘抬起事件时出错（已忽略）")

    @property
    def is_alive(self) -> bool:
        return bool(self._listener and self._listener.is_alive())

    def restart(self) -> None:
        self.stop()
        time.sleep(0.2)
        self.start()

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is None:
            return
        try:
            listener.stop()
            listener.join(timeout=1.0)
        except Exception:
            pass


class HookSupervisor(threading.Thread):
    """看门狗：钩子线程意外停止时自动重启，避免程序「悄悄停掉」。

    Windows 会在锁屏、休眠唤醒、UAC 安全桌面、驱动更新、反作弊临时卸载钩子等
    场景下把全局钩子摘掉，pynput 的监听线程随之结束。没有看门狗时，主循环发现
    「监听线程没了」就直接退出——表现为控制台窗口一闪就消失、程序自己停了。

    这里改成：发现线程死了就重启（默认最多连续重启 30 次），一直不行才退出，
    并且把原因写进日志文件。
    """

    def __init__(
        self,
        hooks: list,
        restart: bool = True,
        limit: int = 30,
        interval: float = 1.0,
        on_give_up: Optional[Callable[[], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(name="hook-supervisor", daemon=True)
        self._hooks = [hook for hook in hooks if hook is not None]
        self._restart = restart
        self._limit = max(0, int(limit))
        self._interval = max(0.2, float(interval))
        self._on_give_up = on_give_up
        self._log = logger or log
        self._stop_event = threading.Event()
        self._failures: Dict[int, int] = {id(hook): 0 for hook in self._hooks}

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            for hook in list(self._hooks):
                self._check(hook)

    def _check(self, hook) -> None:
        name = type(hook).__name__
        try:
            alive = hook.is_alive
        except Exception:
            alive = False
        if alive:
            self._failures[id(hook)] = 0
            return

        count = self._failures.get(id(hook), 0) + 1
        self._failures[id(hook)] = count

        if not self._restart:
            self._log.error("%s 已停止，且 config 里关闭了自动重启（hook_restart=false）", name)
            self._give_up()
            return
        if self._limit and count > self._limit:
            self._log.error("%s 已连续重启 %d 次仍未恢复，程序退出", name, count - 1)
            self._give_up()
            return

        self._log.warning(
            "%s 意外停止，第 %d 次自动重启（锁屏、休眠唤醒、UAC 弹窗、驱动更新都会触发）",
            name, count,
        )
        try:
            hook.restart()
        except Exception:
            self._log.exception("重启 %s 失败", name)
            return
        if hook.is_alive:
            self._log.info("%s 已恢复监听，侧键可继续使用", name)

    def _give_up(self) -> None:
        callback = self._on_give_up
        if callback is not None:
            try:
                callback()
            except Exception:
                self._log.exception("看门狗回调出错")

    def stop(self) -> None:
        self._stop_event.set()


class InputMonitor:
    """把所有鼠标 / 键盘事件打印出来，用于排查"为什么游戏里侧键没反应"。

    用法：python main.py --monitor
    在游戏窗口里点一下侧键：
        - 控制台有输出  -> 钩子正常，问题在手机端或映射配置
        - 控制台无输出  -> 被游戏/反作弊拦了，需要以管理员身份运行
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._log = logger or log
        self._mouse: Optional[mouse.Listener] = None
        self._keyboard = None
        self._stop = threading.Event()
        self._clicks = 0

    def start(self) -> None:
        from pynput import keyboard

        self._mouse = mouse.Listener(on_click=self._on_click)
        self._keyboard = keyboard.Listener(on_press=self._on_press)
        for listener in (self._mouse, self._keyboard):
            listener.daemon = True
            listener.start()

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if not pressed:
            return
        self._clicks += 1
        name = getattr(button, "name", str(button))
        self._log.info("[鼠标] %-6s (x=%d, y=%d)  累计 %d 次", name, x, y, self._clicks)

    def _on_press(self, key) -> bool:
        name = getattr(key, "name", None) or getattr(key, "char", None) or str(key)
        self._log.info("[键盘] %s", name)
        if name == "esc":
            self._stop.set()
            return False
        return True

    def run(self, duration: Optional[float] = None) -> None:
        deadline = None if not duration else time.monotonic() + duration
        try:
            while not self._stop.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self._stop.wait(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        for listener in (self._mouse, self._keyboard):
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception:
                pass
        self._mouse = None
        self._keyboard = None
