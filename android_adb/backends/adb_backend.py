"""ADB 后端：播放/暂停走"媒体按键 + 点击兜底"，后退用可切换的策略。

- 播放/暂停：默认是"先发 keyevent 85（KEYCODE_MEDIA_PLAY_PAUSE），没生效就点
  画面左下角的播放/暂停按钮"（见 android_adb/play_pause.py）。原因是安卓在视频
  暂停一段时间后会把"媒体按钮会话"置空，此时 85 没有任何应用接收；想回到
  "只发媒体键"的老行为，把 config.json 的 play_pause_mode 改成 media_key 即可。
- 后退：默认走"横滑手势"（rewind_mode=swipe），因为 B 站等客户端的媒体会话
  没有实现 ACTION_REWIND，keyevent 89 发过去没有任何效果；
  需要兼容别的播放器时，把 rewind_mode 改成 media_key 即可。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..adb_client import AdbClient, AdbError
from ..media_state import parse_media_session, pick_state
from ..config import Config
from ..play_pause import PlayPauser, make_play_pauser
from ..rewind import Rewinder, make_rewinder
from .base import MediaBackend

log = logging.getLogger(__name__)


class AdbMediaBackend(MediaBackend):
    name = "adb"

    def __init__(self, client: AdbClient, config: Config, logger: Optional[logging.Logger] = None):
        self._client = client
        self._cfg = config
        self._log = logger or log
        self._rewinder: Rewinder = make_rewinder(client, config, self._log)
        self._play_pauser: PlayPauser = make_play_pauser(client, config, self._log)

    def describe(self) -> str:
        return (
            f"ADB（播放/暂停 {self._play_pauser.describe()}；"
            f"后退 {self._rewinder.describe()}）"
        )

    def play_pause(self) -> None:
        self._play_pauser.play_pause()

    def rewind(self) -> None:
        self._rewinder.rewind()

    def close(self) -> None:
        self._rewinder.close()
        self._play_pauser.close()

    # ------------------------------------------------------------------
    def describe_device(self) -> str:
        """启动时报告屏幕尺寸，方便确认横屏/竖屏。"""
        try:
            width, height = self._client.display_size()
        except AdbError as exc:
            return f"读取屏幕分辨率失败：{exc}"
        return f"手机屏幕 {width}x{height}"

    def media_summary(self) -> Optional[str]:
        """当前媒体会话的一句话描述；拿不到时返回 None。"""
        try:
            states = parse_media_session(self._client.media_session_dump())
        except AdbError:
            return None
        state = pick_state(states)
        if state is None:
            return None
        label = {1: "已停止", 2: "已暂停", 3: "播放中"}.get(state.state, f"state={state.state}")
        return f"{state.package or '未知应用'}（{label}，{state.position_ms / 1000:.0f}s）"
