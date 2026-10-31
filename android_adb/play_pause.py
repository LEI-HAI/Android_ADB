"""播放/暂停的几种实现：媒体按键、点击画面按钮，以及"先按键、不行就点"的自动兜底。

为什么需要兜底
-------------
安卓把 KEYCODE_MEDIA_PLAY_PAUSE（keyevent 85）交给系统里的"媒体按钮会话"
（dumpsys media_session 中的 "Media button session is ..."）。实测（vivo + B 站）：

- 视频在播放：该会话指向 B 站，85 一直有效；
- 视频暂停一段时间后：该字段会变成 null，85 发出去没有任何应用收到，
  表现就是"第一下能暂停，再按播放没反应"；
- 点一下画面让 App 重新活跃后，会话又回到 B 站，85 恢复正常。

因此自动模式（play_pause_mode = auto）的流程是：

1. 读一次 dumpsys media_session，拿到播放状态与媒体按钮会话；
2. 读不到 / 没有会话      -> 只发媒体键，绝不盲点屏幕；
3. 会话为 null（按键无人接收）-> 直接点画面按钮；
4. 其余情况 -> 先按"按键阶梯"发媒体键（85 -> 79/126/127 等），每次按键后读状态校验；
5. 全都没生效 -> 依次尝试：标定档案里的坐标 -> config.json 的比例坐标 -> 无障碍节点探测。

按键阶梯是为了换 App：有的播放器只认 KEYCODE_HEADSETHOOK(79) 或
KEYCODE_MEDIA_PLAY(126) / KEYCODE_MEDIA_PAUSE(127)，发 85 完全没反应。

点击兜底最多点两次：控件栏自动隐藏时，第一次点击只是把控件唤出来，第二次才
真正切到播放/暂停，所以每次点完都要重新读状态确认。
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence, Tuple

from .adb_client import AdbClient, AdbError
from .backends.base import BackendError
from .config import Config
from .media_state import (
    PlaybackState,
    media_button_session,
    parse_media_session,
    pick_state,
)
from .profiles import ProfileStore, profile_key
from .ui_probe import UiProbeError, UiProber

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
        self._send_key(self._cfg.play_pause_keycode)

    def _send_key(self, keycode: int) -> None:
        try:
            self._client.keyevent(keycode)
        except AdbError as exc:
            raise BackendError(f"发送播放/暂停按键失败：{exc}") from exc


class SmartPlayPauser(PlayPauser):
    """媒体按键阶梯 + 多点来源的点击兜底。

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
        store: Optional[ProfileStore] = None,
        prober: Optional[UiProber] = None,
    ):
        self._client = client
        self._cfg = config
        self._log = logger or log
        self._force_tap = force_tap
        self._store = store
        self._prober = prober
        self._package: Optional[str] = None
        self._size: Optional[Tuple[int, int, int]] = None
        self._size_ts = 0.0
        self._warned_portrait = False
        self._preferred_key: Optional[int] = None

    # ----------------------------- 对外接口 -----------------------------
    def describe(self) -> str:
        cfg = self._cfg
        if self._force_tap:
            return (
                "点击画面按钮"
                f"（{cfg.play_pause_x_ratio:.3f}, {cfg.play_pause_y_ratio:.3f}）"
            )
        keys = "/".join(str(code) for code in self._key_order())
        return f"媒体按键 {keys}，媒体键失效时自动点画面按钮"

    def play_pause(self) -> None:
        state, session = self._snapshot()

        if self._force_tap:
            self._tap_or_raise(state)
            return

        if state is None:
            # 没有可识别的会话（没在放视频，或读不到状态）：只发媒体键，不乱点屏幕
            self._log.debug("没有读到播放状态，直接使用媒体按键")
            self._send_key(self._preferred_key or self._cfg.play_pause_keycode)
            return

        if session is None:
            self._log.debug("系统媒体按钮会话为 null（媒体键无人接收），改为点击画面按钮")
            self._tap_or_raise(state)
            return

        owner = self._session_owner(session)
        if owner and self._package and owner != self._package:
            # 媒体键会被投递给 owner，而不是当前在放视频的 App；
            # 此时发 85 只会打断别的 App（例如后台的抖音），对目标视频毫无作用
            self._log.info(
                "媒体按钮会话被 %s 占着（当前视频是 %s），发媒体键只会打断它，改为点击画面按钮",
                owner,
                self._package,
            )
            self._tap_or_raise(state)
            return

        if self._press_keys(state):
            return

        self._log.info("媒体键没有生效，改为点击画面上的播放/暂停按钮")
        self._tap_or_raise(state)

    # ----------------------------- 按键 -----------------------------
    @staticmethod
    def _session_owner(session: Optional[str]) -> Optional[str]:
        """从 "包名/会话名 (userId=0)" 里取出包名。"""
        if not session:
            return None
        owner = session.split("/", 1)[0].strip()
        return owner or None

    def _key_order(self) -> List[int]:
        """按键尝试顺序：上次成功的 -> 配置的主键 -> 额外键码。"""
        order: List[int] = []
        for code in (
            self._preferred_key,
            self._cfg.play_pause_keycode,
            *self._extra_keycodes(),
        ):
            if code is None:
                continue
            try:
                value = int(code)
            except (TypeError, ValueError):
                continue
            if value not in order:
                order.append(value)
        return order or [85]

    def _extra_keycodes(self) -> Sequence[int]:
        getter = getattr(self._cfg, "extra_keycodes", None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:  # noqa: BLE001 - 配置异常时不影响主流程
                return []
        return []

    def _press_keys(self, before: PlaybackState) -> bool:
        """按阶梯逐个按键，直到状态真的变了。"""
        for keycode in self._key_order():
            self._send_key(keycode)
            if self._confirm(before):
                if self._preferred_key != keycode:
                    self._log.debug("媒体键 %s 生效，后续优先使用它", keycode)
                self._preferred_key = keycode
                return True
            self._log.debug("按键 %s 没有改变播放状态，继续尝试下一个", keycode)
        return False

    def _send_key(self, keycode: int) -> None:
        try:
            self._client.keyevent(keycode)
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

    # ----------------------------- 点击 -----------------------------
    def _tap_or_raise(self, before: Optional[PlaybackState]) -> None:
        if not self._tap_toggle(before):
            raise BackendError(
                "点击播放/暂停按钮没有生效：请确认手机停在视频全屏播放页面，"
                "然后运行 python main.py --calibrate 重新标定按钮位置"
                "（也可以手动调整 config.json 的 play_pause_x_ratio / play_pause_y_ratio）"
            )

    def _tap_toggle(self, before: Optional[PlaybackState]) -> bool:
        try:
            width, height, rotation = self._display()
        except AdbError as exc:
            raise BackendError(f"读取屏幕分辨率失败：{exc}") from exc

        current = before
        for x, y, source in self._tap_candidates(width, height, rotation):
            for attempt in range(1, self._MAX_TAPS + 1):
                self._log.debug(
                    "点击播放/暂停按钮 (%d, %d) 第 %d 次（来源：%s）", x, y, attempt, source
                )
                try:
                    self._client.tap(x, y)
                except AdbError as exc:
                    raise BackendError(f"点击屏幕失败：{exc}") from exc
                self._sleep(self._cfg.play_pause_tap_settle_ms)
                after = self._read_state()
                if after is None:
                    self._remember_point(x, y, width, height, rotation, source)
                    return True
                if current is None or after.state != current.state:
                    self._remember_point(x, y, width, height, rotation, source)
                    return True
                current = after

        self._log.warning(
            "点击 (%d, %d) 附近都没能改变播放状态："
            "请确认手机停在视频播放页面，或运行 python main.py --calibrate 重新标定",
            int(width * self._cfg.play_pause_x_ratio),
            int(height * self._cfg.play_pause_y_ratio),
        )
        return False

    def _tap_candidates(
        self, width: int, height: int, rotation: int
    ) -> List[Tuple[int, int, str]]:
        """点击候选点，按可信度排序：标定档案 -> 配置比例 -> 无障碍节点。"""
        candidates: List[Tuple[int, int, str]] = []

        cached = self._cached_point(width, height, rotation)
        if cached is not None:
            candidates.append((cached[0], cached[1], "profile"))

        candidates.append(
            (
                int(round(width * self._cfg.play_pause_x_ratio)),
                int(round(height * self._cfg.play_pause_y_ratio)),
                "config",
            )
        )

        probed = self._probe_point()
        if probed is not None:
            candidates.append((probed[0], probed[1], "ui_node"))

        # 去掉重复点（按 8 像素取整）
        seen = set()
        unique: List[Tuple[int, int, str]] = []
        for x, y, source in candidates:
            x = max(1, min(width - 1, int(x)))
            y = max(1, min(height - 1, int(y)))
            key = (x // 8, y // 8)
            if key in seen:
                continue
            seen.add(key)
            unique.append((x, y, source))
        return unique

    def _cached_point(
        self, width: int, height: int, rotation: int
    ) -> Optional[Tuple[int, int]]:
        if self._store is None or not self._package:
            return None
        key = profile_key(self._package, width, height, rotation)
        point = self._store.play_pause_point(key)
        if point is None:
            return None
        x_ratio, y_ratio = point
        self._log.debug("使用标定档案里的按钮位置（%s）", key)
        return int(round(width * x_ratio)), int(round(height * y_ratio))

    def _probe_point(self) -> Optional[Tuple[int, int]]:
        if not self._cfg.ui_probe:
            return None
        prober = self._prober
        if prober is None:
            prober = UiProber(self._client, self._log)
            self._prober = prober
        try:
            point = prober.play_pause_point()
        except UiProbeError as exc:
            self._log.debug("无障碍节点探测不可用：%s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - 探测永远不该让主流程挂掉
            self._log.debug("无障碍节点探测异常：%s", exc)
            return None
        if point is None:
            self._log.debug("界面节点里没有找到播放/暂停按钮（多半是自绘控件）")
            return None
        self._log.info("通过无障碍节点找到播放/暂停按钮：(%d, %d)", point[0], point[1])
        return point

    def _remember_point(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        rotation: int,
        source: str,
    ) -> None:
        """把"点了确实生效"的位置记进档案，下次换设备也不用重新试。"""
        if self._store is None or not self._package or width <= 0 or height <= 0:
            return
        key = profile_key(self._package, width, height, rotation)
        try:
            self._store.set_play_pause_point(
                key, x / float(width), y / float(height), source=source
            )
            self._log.debug("已记录按钮位置 %s -> (%.4f, %.4f)", key, x / width, y / height)
        except Exception as exc:  # noqa: BLE001 - 记不下来也不影响本次操作
            self._log.debug("写入标定档案失败：%s", exc)

    # ----------------------------- 屏幕 -----------------------------
    def _display(self) -> Tuple[int, int, int]:
        """当前可用宽高与方向（横屏时宽高自动交换），带缓存。"""
        now = time.monotonic()
        if self._size and now - self._size_ts < self._SIZE_TTL:
            return self._size
        width, height = self._client.screen_size()
        rotation = self._client.rotation()
        if rotation % 2 == 1:
            width, height = height, width
        self._size = (width, height, rotation)
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

    # ----------------------------- 内部：状态 -----------------------------
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


def make_play_pauser(
    client: AdbClient,
    config: Config,
    logger: Optional[logging.Logger] = None,
    store: Optional[ProfileStore] = None,
    prober: Optional[UiProber] = None,
) -> PlayPauser:
    """按 config.play_pause_mode 造一个播放/暂停实现。"""
    mode = str(getattr(config, "play_pause_mode", "auto") or "auto").strip().lower()
    if mode == "media_key":
        return KeyeventPlayPauser(client, config, logger)
    return SmartPlayPauser(
        client, config, logger, force_tap=(mode == "tap"), store=store, prober=prober
    )
