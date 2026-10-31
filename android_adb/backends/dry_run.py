"""演练后端：不碰手机，只打印将要执行的命令。

适合没插手机时先验证监听、防抖、日志是否正常。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..config import Config
from .base import MediaBackend


class DryRunBackend(MediaBackend):
    name = "dry-run"

    def __init__(self, config: Config, logger: Optional[logging.Logger] = None):
        self._cfg = config
        self._log = logger or logging.getLogger(__name__)

    def describe(self) -> str:
        return "dry-run（只打印命令，不发送）"

    def play_pause(self) -> None:
        self._log.info(
            "[dry-run] adb shell input keyevent %s", self._cfg.play_pause_keycode
        )

    def rewind(self) -> None:
        times = max(1, int(self._cfg.rewind_repeat))
        for index in range(times):
            self._log.info(
                "[dry-run] adb shell input keyevent %s (%d/%d)",
                self._cfg.rewind_keycode,
                index + 1,
                times,
            )
