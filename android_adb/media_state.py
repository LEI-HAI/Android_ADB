"""解析 `adb shell dumpsys media_session` 的播放状态。

后退（seek）是否成功，靠 PlaybackState 里的 position 变化来判断；
因此这里把 dumpsys 的文本解析成结构化数据，方便测试与复用。

Android 里一段会话大致长这样：

    Sessions Stack - have 1 sessions:
      * Stack #0:
        ...
          package=tv.danmaku.bili
          ...
          active=true
          flags=3
          state=PlaybackState {state=3, position=102310, buffered position=0,
                               speed=1.0, updated=410264938, actions=823, ...}

注意：没有在播放的会话（例如 telecom 的媒体按钮会话）可能没有 state 行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

# PlaybackState 的 state 字段含义
STATE_NONE = 0
STATE_STOPPED = 1
STATE_PAUSED = 2
STATE_PLAYING = 3

_STATE_LINE_RE = re.compile(r"state=PlaybackState {")
_PACKAGE_RE = re.compile(r"^\s*package=(\S+)\s*$")
_ACTIVE_RE = re.compile(r"^\s*active=(true|false)\s*$")
_FIELD_RES = {
    "state": re.compile(r"\bstate=(-?\d+)"),
    "position": re.compile(r"\bposition=(-?\d+)"),
    "speed": re.compile(r"\bspeed=(-?[\d.]+)"),
    "updated": re.compile(r"\bupdated=(\d+)"),
}


@dataclass(frozen=True)
class PlaybackState:
    """一条媒体会话的播放状态快照。"""

    package: str = ""
    state: int = STATE_NONE
    position_ms: int = 0
    speed: float = 0.0
    updated_ms: int = 0
    active: bool = False

    @property
    def is_playing(self) -> bool:
        return self.state == STATE_PLAYING and self.speed > 0

    def position_at(self, elapsed_realtime_ms: int) -> int:
        """把快照推进到指定时刻（elapsedRealtime 时间轴）。"""
        if not self.is_playing:
            return self.position_ms
        advanced = (elapsed_realtime_ms - self.updated_ms) * self.speed
        return self.position_ms + int(max(0, advanced))

    def advance_between(self, other: "PlaybackState", wall_gap_ms: Optional[int] = None) -> int:
        """从 self 到 other 之间，视频时间跳变了多少毫秒（已扣除自然播放）。

        两次快照都带设备自己的 updated 时间戳，理论上可用

            delta = (pos1 - pos0) - (updated1 - updated0) * speed

        扣掉中间自然播放走过的时间，剩下的才是 seek 造成的跳变。但实测有两类
        干扰，这里都做了处理：

        1. 暂停时不少播放器（B 站就是）仍然上报 speed=1.0，此时位置并不会自己
           走，所以只要两个快照都不是播放态，就把 speed 视为 0。
        2. updated 只在事件发生时刷新，两次快照之间的时间差可能远大于真实间
           隔（实测出现过 8 秒差对应 2.4 秒真实时间）。传了 wall_gap_ms（调用方
           用单调时钟量出的真实间隔）时，超过 3 倍或 4 秒的差值改用墙钟估算。

        self 是滑动前、other 是滑动后；返回负数表示后退。
        """
        speed = max(self.speed or 0.0, other.speed or 0.0)
        if not (self.is_playing or other.is_playing):
            speed = 0.0

        gap = float(other.updated_ms - self.updated_ms)
        if gap < 0:
            gap = 0.0
        if wall_gap_ms is not None and gap > max(wall_gap_ms * 3.0, 4000.0):
            gap = float(max(wall_gap_ms, 0))
        return int((other.position_ms - self.position_ms) - gap * speed)


def _parse_state_line(line: str) -> Optional[dict]:
    fields = {}
    for name, regex in _FIELD_RES.items():
        match = regex.search(line)
        if match:
            fields[name] = match.group(1)
    if "position" not in fields:
        return None
    try:
        return {
            "state": int(fields.get("state", STATE_NONE)),
            "position_ms": int(fields["position"]),
            "speed": float(fields.get("speed", 0.0) or 0.0),
            "updated_ms": int(fields.get("updated", 0) or 0),
        }
    except ValueError:
        return None


def parse_media_session(text: str) -> List[PlaybackState]:
    """把 dumpsys 文本解析成会话列表（只保留带 state 行的会话）。"""
    states: List[PlaybackState] = []
    package = ""
    active = False
    for line in (text or "").splitlines():
        pkg_match = _PACKAGE_RE.match(line)
        if pkg_match:
            package = pkg_match.group(1)
            active = False
            continue
        active_match = _ACTIVE_RE.match(line)
        if active_match:
            active = active_match.group(1) == "true"
            continue
        if not _STATE_LINE_RE.search(line):
            continue
        parsed = _parse_state_line(line)
        if parsed is None:
            continue
        states.append(PlaybackState(package=package, active=active, **parsed))
    return states


def pick_state(
    states: Sequence[PlaybackState],
    prefer_package: Optional[str] = None,
) -> Optional[PlaybackState]:
    """挑一条最可能"正在放视频"的会话。

    优先级：指定包名 > active 会话 > 位置最大的会话。
    """
    if not states:
        return None
    if prefer_package:
        for item in states:
            if item.package == prefer_package:
                return item
    for item in states:
        if item.active:
            return item
    return max(states, key=lambda item: item.position_ms)


_MEDIA_BUTTON_RE = re.compile(r"^\s*Media button session is (.+?)\s*$", re.MULTILINE)


def media_button_session(text: str) -> Optional[str]:
    """解析 "Media button session is ..." —— 系统当前把媒体键投递给谁。

    值为 null（或整行缺失）时返回 None，表示此刻按媒体键没有任何应用会收到。
    实测（vivo + B 站）：视频暂停一段时间后该字段会变成 null，此时 keyevent 85
    发出去完全没反应，需要换一种触发方式（例如点画面上的按钮）。
    """
    match = _MEDIA_BUTTON_RE.search(text or "")
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.lower() == "null":
        return None
    return value
