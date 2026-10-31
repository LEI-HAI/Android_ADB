"""后退动作的两种实现：媒体按键 / 滑动手势。

为什么要两种？
    B 站等客户端的媒体会话不支持 ACTION_REWIND，keyevent 89 发过去毫无反应；
    而"在播放器上横向滑动"是它们自己实现的 seek 手势，一定有效。
    于是默认走 swipe，同时保留 media_key 作为兼容方案。

滑动版还带闭环校准：
    读播放位置 -> 滑动 -> 再读位置 -> 用实际后退秒数修正"毫秒/像素"的换算，
    下次就能一发退到目标秒数。不同时长的视频换算比例不同，所以这个校准是必要的。
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Optional, Tuple

from .adb_client import AdbClient, AdbError
from .backends.base import BackendError
from .config import Config
from .media_state import PlaybackState, parse_media_session, pick_state

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 抽象
# ---------------------------------------------------------------------------
class Rewinder(ABC):
    """一次"后退 5 秒"的实现。"""

    name = "base"

    @abstractmethod
    def rewind(self) -> None:
        """执行一次后退；失败时抛 BackendError。"""

    def describe(self) -> str:
        return self.name

    def close(self) -> None:  # pragma: no cover - 默认无需清理
        pass


# ---------------------------------------------------------------------------
# 方案一：媒体按键（keyevent 89，靠连发凑时长）
# ---------------------------------------------------------------------------
class KeyeventRewinder(Rewinder):
    name = "media_key"

    def __init__(self, client: AdbClient, config: Config, logger: Optional[logging.Logger] = None):
        self._client = client
        self._cfg = config
        self._log = logger or log

    def describe(self) -> str:
        cfg = self._cfg
        return f"媒体按键（keyevent {cfg.rewind_keycode} x{cfg.rewind_repeat}）"

    def rewind(self) -> None:
        try:
            self._burst(self._cfg.rewind_keycode, self._cfg.rewind_repeat)
        except BackendError as exc:
            fallback = self._cfg.rewind_fallback_keycode
            if fallback is None:
                raise
            self._log.warning("媒体后退键发送失败（%s），尝试改用方向键 %s", exc, fallback)
            self._burst(fallback, self._cfg.rewind_fallback_repeat)

    def _burst(self, keycode: int, repeat: int) -> None:
        times = max(1, int(repeat))
        interval = max(0, int(self._cfg.rewind_interval_ms)) / 1000.0
        for index in range(times):
            self._send(keycode)
            if index + 1 < times and interval > 0:
                time.sleep(interval)

    def _send(self, keycode: int) -> None:
        try:
            self._client.keyevent(keycode)
        except AdbError as exc:
            raise BackendError(str(exc)) from exc


# ---------------------------------------------------------------------------
# 方案二：在播放器上横向滑动（默认）
# ---------------------------------------------------------------------------
class SwipeRewinder(Rewinder):
    name = "swipe"

    _SIZE_TTL = 8.0                  # 屏幕尺寸缓存秒数（转屏后最多 8 秒跟上）
    _MIN_PX = 10                     # 最小滑动距离
    _MS_PER_PX_RANGE = (2.0, 400.0)  # 换算比例的合理区间
    _CORRECT_THRESHOLD_MS = 400      # 误差超过 0.4 秒才补一刀

    def __init__(self, client: AdbClient, config: Config, logger: Optional[logging.Logger] = None):
        self._client = client
        self._cfg = config
        self._log = logger or log
        self._ms_per_px = float(config.swipe_ms_per_px)
        self._size: Optional[Tuple[int, int]] = None
        self._rotation = 0
        self._size_ts = 0.0
        self._package: Optional[str] = None    # 记住上次的会话，避免多会话时误选
        self._warned_no_session = False
        self._warned_portrait = False
        self._last_read_ts = 0.0   # 上次读媒体会话时的单调时钟

    # ------------------------------------------------------------------
    def describe(self) -> str:
        cfg = self._cfg
        extra = "，自动校准" if cfg.auto_calibrate else ""
        return f"滑动后退（目标 {cfg.swipe_seconds:g} 秒，约 {self._ms_per_px:.1f} ms/px{extra}）"

    def rewind(self) -> None:
        cfg = self._cfg
        target_ms = max(200, int(round(cfg.swipe_seconds * 1000)))
        width, height = self._display()

        before = self._read_state()
        px = self._px_for(target_ms, width)
        self._swipe(width, height, px, forward=False)

        moved_ms: Optional[int] = None
        if before is not None:
            self._sleep(cfg.swipe_settle_ms)
            moved_ms = self._delta_ms(before)
            if moved_ms is not None and cfg.auto_calibrate:
                self._update_ratio(px, moved_ms)

            # 误差较大时补一刀，尽量一次到位
            if moved_ms is not None:
                remaining = target_ms - moved_ms
                correction = self._px_for(abs(remaining), width)
                if abs(remaining) >= self._CORRECT_THRESHOLD_MS and correction >= self._MIN_PX:
                    self._log.debug("误差 %.2f 秒，补发 %d 像素", remaining / 1000.0, correction)
                    self._swipe(width, height, correction, forward=remaining < 0)
                    self._sleep(cfg.swipe_settle_ms)
                    total = self._delta_ms(before)
                    if total is not None:
                        moved_ms = total

        if moved_ms is None:
            self._log.info(
                "已向左滑动 %.0f 像素（约后退 %g 秒，未能读取播放位置）",
                px, cfg.swipe_seconds,
            )
        else:
            self._log.info("已后退 %.1f 秒（滑动 %.0f 像素）", moved_ms / 1000.0, px)

    # ------------------------------------------------------------------
    def _delta_ms(self, before: PlaybackState) -> Optional[int]:
        """相对于滑动前的快照，视频一共后退了多少毫秒。"""
        wall_gap_ms = int(max(0.0, (time.monotonic() - self._last_read_ts) * 1000))
        after = self._read_state(prefer=before.package)
        if after is None:
            return None
        moved_ms = -before.advance_between(after, wall_gap_ms=wall_gap_ms)
        if not (100 <= moved_ms <= 180_000):
            self._log.debug("滑动后位置变化异常（%s ms），忽略本次测量", moved_ms)
            return None
        return moved_ms

    def _update_ratio(self, px: int, moved_ms: int) -> None:
        measured = moved_ms / float(px)
        low, high = self._MS_PER_PX_RANGE
        if not (low <= measured <= high):
            return
        old = self._ms_per_px
        self._ms_per_px = max(low, min(high, old * 0.4 + measured * 0.6))
        self._log.debug(
            "校准：本次 %.0fpx -> %.2f 秒，ms/px %.1f -> %.1f",
            px, moved_ms / 1000.0, old, self._ms_per_px,
        )

    def _px_for(self, duration_ms: int, width: int) -> int:
        px = int(round(duration_ms / max(self._ms_per_px, 0.1)))
        limit = max(self._MIN_PX, int(width * 0.7))
        return max(self._MIN_PX, min(px, limit))

    def _display(self) -> Tuple[int, int]:
        now = time.monotonic()
        if self._size is not None and now - self._size_ts <= self._SIZE_TTL:
            return self._size
        try:
            width, height = self._client.screen_size()
            rotation = self._client.rotation()
        except AdbError as exc:
            if self._size is not None:
                self._log.debug("读取屏幕尺寸失败（%s），继续使用缓存值", exc)
                return self._size
            raise BackendError(f"读取屏幕分辨率失败：{exc}") from exc
        if rotation % 2 == 1:
            width, height = height, width
        self._size = (width, height)
        self._rotation = rotation
        self._size_ts = now
        self._log.debug("屏幕 %dx%d（rotation=%d）", width, height, rotation)
        if rotation % 2 == 0 and not self._warned_portrait:
            self._warned_portrait = True
            self._log.warning("当前是竖屏：滑动可能落在评论区而不是播放器，建议在手机上全屏横屏播放")
        return self._size

    def _swipe(self, width: int, height: int, px: int, forward: bool) -> None:
        cfg = self._cfg
        start_x = int(width * max(0.2, min(0.95, cfg.swipe_start_ratio)))
        y = int(height * max(0.1, min(0.9, cfg.swipe_y_ratio)))
        px = max(self._MIN_PX, min(px, start_x - 5))
        end_x = start_x + px if forward else start_x - px
        self._log.debug("input swipe %d %d %d %d %d", start_x, y, end_x, y, cfg.swipe_duration_ms)
        try:
            self._client.swipe(start_x, y, end_x, y, cfg.swipe_duration_ms)
        except AdbError as exc:
            raise BackendError(f"滑动手势发送失败：{exc}") from exc

    def _read_state(self, prefer: Optional[str] = None) -> Optional[PlaybackState]:
        target = prefer or self._package
        try:
            states = parse_media_session(self._client.media_session_dump())
        except AdbError as exc:
            self._log.debug("读取媒体会话失败：%s", exc)
            return None
        if not states:
            if not self._warned_no_session:
                self._warned_no_session = True
                self._log.warning("未检测到正在播放的媒体会话，无法测量后退秒数（将按固定距离滑动）")
            return None
        state = pick_state(states, target)
        if state is not None and state.package:
            self._package = state.package
        self._last_read_ts = time.monotonic()
        return state

    @staticmethod
    def _sleep(ms: int) -> None:
        if ms and ms > 0:
            time.sleep(ms / 1000.0)


# ---------------------------------------------------------------------------
def make_rewinder(
    client: AdbClient,
    config: Config,
    logger: Optional[logging.Logger] = None,
) -> Rewinder:
    """按配置选择后退实现。"""
    if str(config.rewind_mode or "swipe").strip().lower() == "media_key":
        return KeyeventRewinder(client, config, logger)
    return SwipeRewinder(client, config, logger)
