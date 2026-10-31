"""播放/暂停的几种实现：媒体按键、点击画面按钮，以及"先按键、不行就点"的自动兜底。

为什么需要兜底
--------------
安卓把 KEYCODE_MEDIA_PLAY_PAUSE（keyevent 85）交给系统里的"媒体按钮会话"
（dumpsys media_session 中的 "Media button session is ..."）。实测（vivo + B 站）：

- 视频在播放：该会话指向 B 站，85 一直有效；
- 视频暂停一段时间后：该字段会变成 null，85 发出去没有任何应用收到，
  表现就是"第一下能暂停，再按播放没反应"；
- 点一下画面让 App 重新活跃后，会话又回到 B 站，85 恢复正常。

因此自动模式（play_pause_mode = auto）的流程是：

1. 读一次 dumpsys media_session，拿到播放状态与媒体按钮会话；
2. 读不到 / 没有会话      -> 只发媒体键，绝不盲点屏幕；
3. 会话在播放且媒体按钮会话在 -> 直接发媒体键（最可靠的路径，不做额外校验）；
4. 媒体按钮会话为 null（按键无人接收）-> 直接点画面左下角的播放/暂停按钮；
5. 其余情况（例如已暂停但会话还在）-> 先发媒体键，校验状态；没变就点按钮。

点击兜底最多点两次：控件栏自动隐藏时，第一次点击只是把控件唤出来，第二次才
真正切到播放/暂停，所以每次点完都要重新读状态确认。
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Optional, Tuple

from .adb_client import AdbClient, AdbError
from .backends.base import BackendError
from .config import Config
from .media_state import (
    PlaybackState,
    media_button_session,
    parse_media_session,
    pick_state,
)

log = logging.getLogger(__name__)


class PlayPauser(ABC):
    """一次"播放 / 暂停"的实现。"""

    name = "base"

    @abstractmethod
    def play_pause(self) -> None:
        """执行一次播放/暂停；失败时抛 BackendError。"""

    def describe(self) -> str:
        return self.name

    def close(self) -> None:
        """释放资源，默认什么都不做。"""


class KeyeventPlayPauser(PlayPauser):
    """只发媒体按键（原来的做法）。媒体键可用时最省事。"""

    name = "media_key"

    def __init__(
        self,
        client: AdbClient,
        config: Config,
        logger: Optional[logging.Logger] = None,
    ):
        self._client = client
        self._cfg = config
        self._log = logger or log

    def describe(self) -> str:
        return f"媒体按键（keyevent {self._cfg.play_pause_keycode}）"

    def play_pause(self) -> None:
        self._send_key()

    def _send_key(self) -> None:
        try:
            self._client.keyevent(self._cfg.play_pause_keycode)
        except AdbError as exc:
            raise BackendError(f"发送播放/暂停按键失败：{exc}") from exc


class SmartPlayPauser(PlayPauser):
    """媒体按键 + 点击画面按钮的自动兜底。

    force_tap=True 时（play_pause_mode = tap）连媒体键都不发，直接点按钮，
    适合"某台机器上媒体键彻底没反应"的场景。
    """

    name = "auto"

    #: 屏幕尺寸缓存时间（秒）：每次按键都去问 wm size / rotation 太慢
    _SIZE_TTL = 8.0
    #: 点击兜底最多点几次（第一次可能只是把自动隐藏的控件栏唤出来）
    _MAX_TAPS = 2

    def __init__(
        self,
        client: AdbClient,
        config: Config,
        logger: Optional[logging.Logger] = None,
        force_tap: bool = False,
    ):
        self._client = client
        self._cfg = config
        self._log = logger or log
        self._force_tap = force_tap
        self._package: Optional[str] = None
        self._size: Optional[Tuple[int, int]] = None
        self._size_ts = 0.0
        self._warned_portrait = False

    # ----------------------------- 对外接口 -----------------------------
    def describe(self) -> str:
        cfg = self._cfg
        if self._force_tap:
            return (
                "点击画面按钮"
                f"（{cfg.play_pause_x_ratio:.3f}, {cfg.play_pause_y_ratio:.3f}）"
            )
        return (
            f"媒体按键 {cfg.play_pause_keycode}，"
            f"媒体键失效时自动点画面按钮"
            f"（{cfg.play_pause_x_ratio:.3f}, {cfg.play_pause_y_ratio:.3f}）"
        )

    def play_pause(self) -> None:
        state, session = self._snapshot()

        if self._force_tap:
            self._tap_or_raise(state)
            return

        if state is None:
            # 没有可识别的会话（没在放视频，或读不到状态）：只发媒体键，不乱点屏幕
            self._log.debug("没有读到播放状态，直接使用媒体按键")
            self._send_key()
            return

        if state.is_playing and session is not None:
            # 最可靠的路径：播放中且系统确实把媒体键交给某个应用
            self._send_key()
            return

        if session is None:
            self._log.debug("系统媒体按钮会话为 null（媒体键无人接收），改为点击画面按钮")
            self._tap_or_raise(state)
            return

        self._send_key()
        if self._confirm(state):
            return
        self._log.info("媒体键没有生效，改为点击画面上的播放/暂停按钮")
        self._tap_or_raise(state)

    # ----------------------------- 内部实现 -----------------------------
    def _snapshot(self) -> Tuple[Optional[PlaybackState], Optional[str]]:
        """读一次媒体会话：(播放状态, 媒体按钮会话)。失败时返回 (None, None)。"""
        try:
            dump = self._client.media_session_dump()
        except Exception as exc:  # noqa: BLE001 - 读不到状态就退化成"只用媒体键"
            self._log.debug("读取媒体会话失败：%s", exc)
            return None, None
        try:
            state = pick_state(parse_media_session(dump), self._package)
        except Exception as exc:  # noqa: BLE001 - 解析失败同样是未知状态
            self._log.debug("解析媒体会话失败：%s", exc)
            return None, None
        if state is not None and state.package:
            self._package = state.package
        try:
            session = media_button_session(dump)
        except Exception:  # noqa: BLE001
            session = None
        return state, session

    def _read_state(self) -> Optional[PlaybackState]:
        return self._snapshot()[0]

    def _send_key(self) -> None:
        try:
            self._client.keyevent(self._cfg.play_pause_keycode)
        except AdbError as exc:
            raise BackendError(f"发送播放/暂停按键失败：{exc}") from exc

    def _confirm(self, before: PlaybackState) -> bool:
        """发完媒体键后确认状态真的变了。"""
        verify = max(0, int(self._cfg.play_pause_verify_ms))
        for delay in (verify, verify // 2):
            self._sleep(delay)
            after = self._read_state()
            if after is None:
                # 读不到状态就不再拿点击去赌，保持"只发按键"的行为
                return True
            if after.state != before.state:
                return True
        return False

    def _tap_or_raise(self, before: Optional[PlaybackState]) -> None:
        if not self._tap_toggle(before):
            raise BackendError(
                "点击播放/暂停按钮没有生效：请确认手机停在视频全屏播放页面，"
                "或按实际位置调整 config.json 里的 play_pause_x_ratio / "
                "play_pause_y_ratio（也可以先用 python main.py --test play 观察）"
            )

    def _tap_toggle(self, before: Optional[PlaybackState]) -> bool:
        try:
            x, y = self._button_point()
        except AdbError as exc:
            raise BackendError(f"读取屏幕分辨率失败：{exc}") from exc

        current = before
        for attempt in range(1, self._MAX_TAPS + 1):
            self._log.debug("点击播放/暂停按钮 (%d, %d) 第 %d 次", x, y, attempt)
            try:
                self._client.tap(x, y)
            except AdbError as exc:
                raise BackendError(f"点击屏幕失败：{exc}") from exc
            self._sleep(self._cfg.play_pause_tap_settle_ms)
            after = self._read_state()
            if after is None:
                return True
            if current is None or after.state != current.state:
                return True
            current = after

        self._log.warning(
            "点击 (%d, %d) 没能改变播放状态：请确认手机停在视频播放页面，"
            "或调整 play_pause_x_ratio / play_pause_y_ratio（当前 %.3f / %.3f）",
            x, y, self._cfg.play_pause_x_ratio, self._cfg.play_pause_y_ratio,
        )
        return False

    def _button_point(self) -> Tuple[int, int]:
        width, height = self._display()
        x = int(round(width * self._cfg.play_pause_x_ratio))
        y = int(round(height * self._cfg.play_pause_y_ratio))
        return (
            max(1, min(width - 1, x)),
            max(1, min(height - 1, y)),
        )

    def _display(self) -> Tuple[int, int]:
        """当前可用宽高（横屏时自动交换），带缓存。"""
        now = time.monotonic()
        if self._size and now - self._size_ts < self._SIZE_TTL:
            return self._size
        width, height = self._client.screen_size()
        if self._client.rotation() % 2 == 1:
            width, height = height, width
        self._size = (width, height)
        self._size_ts = now
        if height > width and not self._warned_portrait:
            self._warned_portrait = True
            self._log.warning(
                "手机当前是竖屏（%dx%d），播放/暂停按钮的坐标按全屏横屏标定，"
                "竖屏下可能点不准 —— 建议先全屏播放视频",
                width, height,
            )
        return self._size

    @staticmethod
    def _sleep(ms: int) -> None:
        if ms > 0:
            time.sleep(ms / 1000.0)


def make_play_pauser(
    client: AdbClient,
    config: Config,
    logger: Optional[logging.Logger] = None,
) -> PlayPauser:
    """按 config.play_pause_mode 造一个播放/暂停实现。"""
    mode = str(getattr(config, "play_pause_mode", "auto") or "auto").strip().lower()
    if mode == "media_key":
        return KeyeventPlayPauser(client, config, logger)
    return SmartPlayPauser(client, config, logger, force_tap=(mode == "tap"))
