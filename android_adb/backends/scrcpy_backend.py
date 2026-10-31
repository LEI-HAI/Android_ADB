"""Scrcpy 后端（预留接口）。

为什么预留：
    个别 App 不响应 ADB 媒体键（没有注册 MediaSession），但 scrcpy 的按键注入
    走的是 Android 输入系统，兼容性更好。等你确认 ADB 方案对目标播放器无效时，
    再按下面的步骤把这里补全即可，其余代码不用改。

接入提示（scrcpy 2.x）：
    1. 启动 scrcpy 时加参数 --no-playback? 不需要；关键是开启控制通道
       （scrcpy 默认就开启，端口 27183）。
    2. scrcpy 的 control socket 协议：客户端发送 1 字节 type + payload，
       type=0 表示 INJECT_KEYCODE，结构为：
           type(u8) action(u8) keycode(u32) repeat(u32) metaState(u32)
       action: 0=DOWN, 1=UP；keycode 用 Android KeyEvent 常量（85/89）。
    3. 用 socket 连上 127.0.0.1:27183 后，按上面的结构 pack 两帧（DOWN/UP）即可。
    4. 若不想自己实现协议，也可以直接调用 scrcpy 的 --shortcut-mod 快捷键，
       或继续用 adb（本项目的默认方案）。

在实现之前，本类会直接抛 BackendUnavailable，避免"启动了但没反应"的困惑。
"""
from __future__ import annotations

from ..config import Config
from .base import BackendUnavailable, MediaBackend


class ScrcpyMediaBackend(MediaBackend):
    name = "scrcpy"

    def __init__(self, config: Config):
        self._cfg = config
        raise BackendUnavailable(
            "Scrcpy 后端尚未实现（当前只是预留接口）。\n"
            "    请把 config.json 的 backend 改回 \"adb\" 使用默认方案；\n"
            "    若确实需要 scrcpy，请参考 android_adb/backends/scrcpy_backend.py 顶部的接入说明。"
        )

    def describe(self) -> str:
        return "scrcpy（未实现）"

    def play_pause(self) -> None:  # pragma: no cover - 占位
        raise BackendUnavailable("Scrcpy 后端尚未实现")

    def rewind(self) -> None:  # pragma: no cover - 占位
        raise BackendUnavailable("Scrcpy 后端尚未实现")
