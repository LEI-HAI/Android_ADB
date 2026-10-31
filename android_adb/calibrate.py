"""标定：自动找出"点得动播放/暂停"的按钮位置，并实测后退的滑动换算比例。

为什么需要标定
--------------
点击兜底用的是比例坐标（config.json 里那对 0.039 / 0.904），是在一台手机上对
B 站量出来的。换手机、换视频平台后按钮位置就变了，点不准还容易点错东西。
标定命令做两件事：

1. 先试 uiautomator 节点（标准控件的播放器能直接拿到真实坐标）；
   拿不到就在"底部左下角"这一小片区域按网格逐点点一下，用播放状态是否翻转来判断；
   成功时把比例坐标写进 profiles.json（键 = 包名 + 分辨率 + 方向）。
2. 横滑一段固定距离，读"实际后退了多少秒"，算出 ms/px 并写进档案；
   以后在新手机上第一次后退就能按目标秒数滑，不用慢慢逼近。

全部操作都只点屏幕左下角的一小块和播放器中部，不影响其它业务。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .adb_client import AdbClient, AdbError
from .backends.base import BackendError
from .config import Config
from .media_state import PlaybackState, parse_media_session, pick_state
from .profiles import ProfileStore, profile_key
from .ui_probe import UiProbeError, UiProber

log = logging.getLogger(__name__)

#: 网格搜索的候选比例：只覆盖播放器底部左下角（绝大多数播放器的播放按钮都在这里）。
#: x 从 3% 扫到 16%，y 从 85% 扫到 93% —— 再往下就是进度条 / 章节条，
#: 点上去会触发 seek（实测在 B 站的章节条上会跳章节），所以停在 93%。
GRID_X = (0.030, 0.040, 0.050, 0.060, 0.075, 0.095, 0.120, 0.160)
GRID_Y = (0.850, 0.870, 0.890, 0.910)


class Calibrator:
    """跑一次标定，把结果写进 profiles.json。"""

    def __init__(
        self,
        client: AdbClient,
        config: Config,
        store: Optional[ProfileStore] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._client = client
        self._cfg = config
        self._log = logger or log
        self._store = store if store is not None else ProfileStore(config.profiles_file, self._log)
        self._package: Optional[str] = None
        self._prober = UiProber(client, self._log) if config.ui_probe else None

    # ------------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        """执行标定，返回 {"package", "point", "ms_per_px", "key"}。"""
        before = self._read_state()
        if before is None:
            raise BackendError(
                "读不到播放状态：请先在手机上打开一个视频（停在全屏播放页面）再标定"
            )
        self._package = (before.package or "").strip() or None
        width, height, rotation = self._display()
        key = profile_key(self._package, width, height, rotation)
        self._log.info("标定目标：%s（%dx%d，rotation=%d）", key, width, height, rotation)

        point = self._find_play_pause_point(before, width, height, rotation)
        result: Dict[str, Any] = {"key": key, "package": self._package, "point": point, "ms_per_px": None}
        if point is None:
            return result

        try:
            result["ms_per_px"] = self._measure_rewind(width, height, key)
        except BackendError as exc:
            self._log.warning("后退比例标定失败：%s", exc)
        return result

    # --------------------------- 播放/暂停 ---------------------------
    def _find_play_pause_point(
        self,
        before: PlaybackState,
        width: int,
        height: int,
        rotation: int,
    ) -> Optional[Tuple[int, int, str]]:
        probed = self._probe_point()
        if probed is not None:
            if self._tap_and_check(before, probed[0], probed[1]):
                self._remember(probed[0], probed[1], width, height, rotation, "ui_node")
                self._ensure_playing()
                return (probed[0], probed[1], "ui_node")
            self._log.info("按无障碍节点点击 (%d, %d) 没有改变播放状态，继续网格搜索", *probed)

        self._log.info("开始网格搜索按钮位置（最多 %d 个点）", len(GRID_X) * len(GRID_Y))
        for x_ratio in GRID_X:
            for y_ratio in GRID_Y:
                x = int(round(width * x_ratio))
                y = int(round(height * y_ratio))
                if self._tap_and_check(before, x, y):
                    self._log.info("命中按钮：(%d, %d) = (%.3f, %.3f)", x, y, x_ratio, y_ratio)
                    self._remember(x, y, width, height, rotation, "grid")
                    self._ensure_playing()
                    return (x, y, "grid")
        self._log.warning("网格搜索没有找到能切换播放状态的按钮")
        return None

    def _probe_point(self) -> Optional[Tuple[int, int]]:
        if self._prober is None:
            return None
        try:
            point = self._prober.play_pause_point()
        except UiProbeError as exc:
            self._log.debug("无障碍节点探测不可用：%s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - 探测失败只是少一条策略
            self._log.debug("无障碍节点探测异常：%s", exc)
            return None
        if point is None:
            self._log.info("界面节点里没有播放/暂停按钮（自绘控件），改用网格搜索")
        return point

    def _tap_and_check(self, before: PlaybackState, x: int, y: int) -> bool:
        """点一下，等控件响应，然后看播放状态是否翻转。"""
        try:
            self._client.tap(x, y)
        except AdbError as exc:
            raise BackendError(f"点击屏幕失败：{exc}") from exc
        self._sleep(self._cfg.play_pause_tap_settle_ms)
        after = self._read_state()
        if after is None:
            return False
        changed = after.state != before.state
        if not changed:
            self._log.debug("点击 (%d, %d) 没有改变状态（state=%s）", x, y, after.state)
        return changed

    def _ensure_playing(self) -> None:
        """标定结束时尽量让视频保持"播放中"，别把用户的视频停在暂停。"""
        state = self._read_state()
        if state is None or state.is_playing:
            return
        point = self._store.play_pause_point(self._key()) if self._store and self._package else None
        if point is None:
            return
        width, height, _rotation = self._display()
        try:
            self._client.tap(int(round(width * point[0])), int(round(height * point[1])))
            self._sleep(self._cfg.play_pause_tap_settle_ms)
        except AdbError:
            return
        after = self._read_state()
        if after is not None and after.is_playing:
            self._log.info("已把视频恢复成播放状态")

    def _remember(self, x, y, width, height, rotation, source: str) -> None:
        if self._store is None or width <= 0 or height <= 0:
            return
        key = profile_key(self._package, width, height, rotation)
        try:
            self._store.set_play_pause_point(
                key, x / float(width), y / float(height), source=source
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("写入标定档案失败：%s", exc)

    # ----------------------------- 后退 -----------------------------
    def _measure_rewind(self, width: int, height: int, key: str) -> Optional[float]:
        before = self._read_state()
        if before is None or before.position_ms is None:
            self._log.warning("读不到播放位置，跳过后退比例标定")
            return None

        px = max(40, int(width * 0.25))
        start_x = int(width * max(0.2, min(0.95, self._cfg.swipe_start_ratio)))
        y = int(height * max(0.1, min(0.9, self._cfg.swipe_y_ratio)))
        px = min(px, start_x - 5)
        if px < 20:
            self._log.warning("可用滑动距离太短（%d 像素），跳过后退比例标定", px)
            return None
        end_x = start_x - px

        self._log.info("滑动 %d 像素测量后退比例……", px)
        try:
            self._client.swipe(start_x, y, end_x, y, self._cfg.swipe_duration_ms)
        except AdbError as exc:
            raise BackendError(f"滑动手势发送失败：{exc}") from exc
        self._sleep(self._cfg.swipe_settle_ms)

        after = self._read_state()
        if after is None:
            self._log.warning("滑动后读不到播放位置，跳过后退比例标定")
            return None
        moved_ms = -before.advance_between(after, wall_gap_ms=self._cfg.swipe_settle_ms)
        if not (100 <= moved_ms <= 180_000):
            self._log.warning("滑动后位置变化异常（%s ms），跳过标定", moved_ms)
            return None

        ms_per_px = moved_ms / float(px)
        if not 1.0 <= ms_per_px <= 500.0:
            self._log.warning("算出的换算比例不合理（%.2f ms/px），跳过标定", ms_per_px)
            return None
        if self._store is not None:
            self._store.set_ms_per_px(key, ms_per_px, source="calibrate")
        self._log.info(
            "实测：%d 像素 -> 后退 %.1f 秒（%.2f ms/px），后退 %g 秒约需 %d 像素",
            px, moved_ms / 1000.0, ms_per_px, self._cfg.swipe_seconds,
            int(round(self._cfg.swipe_seconds * 1000 / ms_per_px)),
        )
        return ms_per_px

    # ----------------------------- 基础 -----------------------------
    def _key(self) -> str:
        width, height, rotation = self._display()
        return profile_key(self._package, width, height, rotation)

    def _read_state(self) -> Optional[PlaybackState]:
        try:
            dump = self._client.media_session_dump()
        except Exception as exc:  # noqa: BLE001
            self._log.debug("读取媒体会话失败：%s", exc)
            return None
        try:
            states = parse_media_session(dump)
        except Exception as exc:  # noqa: BLE001
            self._log.debug("解析媒体会话失败：%s", exc)
            return None
        state = pick_state(states, self._package)
        if state is not None and state.package:
            self._package = state.package
        return state

    def _display(self) -> Tuple[int, int, int]:
        try:
            width, height = self._client.screen_size()
            rotation = self._client.rotation()
        except AdbError as exc:
            raise BackendError(f"读取屏幕分辨率失败：{exc}") from exc
        if rotation % 2 == 1:
            width, height = height, width
        return width, height, rotation

    def _sleep(self, ms: int) -> None:
        if ms > 0:
            time.sleep(ms / 1000.0)
