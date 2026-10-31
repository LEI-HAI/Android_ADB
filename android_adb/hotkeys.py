"""键盘快捷键：退出键，以及把 "ctrl+alt+left" 解析成按键集合。

单独一个模块，方便以后扩展成"切换播放器 / 调整后退步长"等组合键。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from pynput import keyboard

log = logging.getLogger(__name__)


_MODIFIERS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "win": "cmd",
    "cmd": "cmd",
    "super": "cmd",
}

# 左右修饰键统一成同一边，方便写 "ctrl+alt+left" 之类的组合
_KEY_ALIASES = {
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
}


def canonical(key) -> str:
    """把 pynput 的按键对象归一化成字符串：ctrl_l -> ctrl、f8 -> f8、'a' -> a。"""
    name = getattr(key, "name", None)
    if name:
        name = name.lower()
        return _KEY_ALIASES.get(name, name)
    char = getattr(key, "char", None)
    if char:
        return char.lower()
    return str(key)


def resolve_key(name: str):
    """把配置里的字符串解析成 pynput 的按键对象：esc / f8 / 单个字符。"""
    key_name = (name or "esc").strip().lower()
    if hasattr(keyboard.Key, key_name):
        return getattr(keyboard.Key, key_name)
    if len(key_name) == 1:
        return keyboard.KeyCode.from_char(key_name)
    raise ValueError(f"无法识别的按键：{name!r}（可选 esc、f8、单字符等）")


def parse_combo(spec: str) -> frozenset:
    """把 "ctrl+alt+left" / "f9" / "x" 解析成按键名集合，用于组合键判断。"""
    parts = [p.strip().lower() for p in str(spec or "").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError(f"按键组合为空：{spec!r}")
    names = set()
    for part in parts:
        if part in _MODIFIERS:
            names.add(_MODIFIERS[part])
            continue
        # 校验单个键名是否合法
        resolve_key(part)
        names.add(part)
    if names <= {"ctrl", "alt", "shift", "cmd"}:
        raise ValueError(f"{spec!r} 只有修饰键，还需要一个普通键，例如 ctrl+alt+f9")
    return frozenset(names)


class ExitHotkey:
    """全局退出快捷键，支持组合键（推荐）和单键。

    注意这是**全局**监听：在任何程序里按到这个键都会退出本程序。所以默认用
    ctrl+alt+q，而不是单个 Esc —— Esc 在游戏菜单、浏览器、对话框里都要按，
    按一下就把控制器关掉，属于明显的误伤。
    """

    def __init__(self, key_name: str, on_trigger: Callable[[], None]):
        self._spec = str(key_name or "").strip().lower()
        self._combo = parse_combo(self._spec)
        self._on_trigger = on_trigger
        self._listener: Optional[keyboard.Listener] = None
        self._pressed = set()
        self._armed = True
        self._triggered = threading.Event()

    @property
    def spec(self) -> str:
        return self._spec

    @property
    def has_modifier(self) -> bool:
        """是否带 Ctrl/Alt/Shift/Win。不带的话任何程序里按到都会退出。"""
        return bool(self._combo & {"ctrl", "alt", "shift", "cmd"})

    def start(self) -> None:
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()

    def _on_press(self, key) -> Optional[bool]:
        try:
            self._pressed.add(canonical(key))
            if not self._armed or not self._combo <= self._pressed:
                return None
            self._armed = False
            self._triggered.set()
            log.info("收到退出快捷键 %s，正在退出……", self._spec.upper())
            try:
                self._on_trigger()
            finally:
                return False  # 返回 False 让 pynput 停止键盘监听
        except Exception:  # pragma: no cover - 防御性
            log.exception("处理退出快捷键时出错")
            return None

    def _on_release(self, key) -> None:
        try:
            self._pressed.discard(canonical(key))
            self._armed = not (self._combo <= self._pressed)
        except Exception:  # pragma: no cover - 防御性
            log.exception("处理退出快捷键松开事件时出错")

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
