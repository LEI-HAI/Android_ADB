"""后端抽象接口。

新增一种控制方式（例如 Scrcpy、蓝牙 HID、串口宏键盘）时，
只需继承 MediaBackend 并实现 play_pause / rewind 两个方法。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BackendError(RuntimeError):
    """后端执行失败（设备断开、命令报错、后端不可用等）。"""


class BackendUnavailable(BackendError):
    """后端尚未实现或被禁用。"""


class MediaBackend(ABC):
    name = "base"

    @abstractmethod
    def play_pause(self) -> None:
        """播放 / 暂停。"""

    @abstractmethod
    def rewind(self) -> None:
        """后退（默认目标为 5 秒）。"""

    def describe(self) -> str:
        """给日志用的一行说明。"""
        return self.name

    def close(self) -> None:
        """释放资源，默认什么都不做。"""
