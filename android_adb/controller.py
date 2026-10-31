"""动作编排层：把"鼠标动作"翻译成"后端调用"，并统一打日志、兜住异常。

这一层不关心按键是走 adb 还是 scrcpy，方便替换后端。
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from .backends.base import BackendError, MediaBackend


class Action(str, Enum):
    PLAY_PAUSE = "play_pause"
    REWIND = "rewind"


ACTION_LABELS = {
    Action.PLAY_PAUSE: "播放/暂停",
    Action.REWIND: "后退 5 秒",
}


class MediaController:
    def __init__(self, backend: MediaBackend, logger: Optional[logging.Logger] = None):
        self._backend = backend
        self._log = logger or logging.getLogger(__name__)

    @property
    def backend(self) -> MediaBackend:
        return self._backend

    def handle(self, action: Action) -> bool:
        """执行一个动作；返回是否成功。异常在这里被吞掉，避免弄死监听线程。"""
        label = ACTION_LABELS.get(action, str(action))
        try:
            if action is Action.PLAY_PAUSE:
                self._log.info("发送播放/暂停指令")
                self._backend.play_pause()
            elif action is Action.REWIND:
                self._log.info("发送后退指令")
                self._backend.rewind()
            else:
                self._log.warning("未知动作：%s", action)
                return False
            return True
        except BackendError as exc:
            self._log.error("%s失败：%s", label, exc)
        except Exception as exc:  # 兜底，防止线程因意外退出
            self._log.exception("%s时发生未预期的错误：%s", label, exc)
        return False

    def close(self) -> None:
        try:
            self._backend.close()
        except Exception:
            pass
