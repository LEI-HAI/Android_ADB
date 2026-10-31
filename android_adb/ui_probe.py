"""无障碍节点探测：用 uiautomator dump 找播放/暂停按钮、进度条。

什么时候有用
------------
绝大多数播放器的按钮是标准控件（ImageView/Button + content-desc），系统会把它们
导出成 XML。找得到就能按节点的真实中心点，跟分辨率、方向完全无关 —— 这是换手机、
换 App 之后最省事的一条路。

什么时候没用
------------
B 站的全屏播放器底部控制条是自绘的 SurfaceView，XML 里只有 38 个节点，一个可点的
按钮都没有（实测）。这时只能退回比例坐标 / 标定档案，所以本模块只当"一条策略"，
失败必须安静地返回 None，让上层继续往下试。
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .adb_client import AdbClient

log = logging.getLogger(__name__)

#: dump 文件放在手机上的位置（每次覆盖，不会堆积）
DUMP_PATH = "/sdcard/window_dump.xml"

#: 播放/暂停按钮常见的文本与描述（中英文 + 常见播放器写法）
PLAY_PAUSE_WORDS = (
    "播放/暂停",
    "播放暂停",
    "暂停",
    "继续播放",
    "恢复播放",
    "播放",
    "play/pause",
    "playpause",
    "pause",
    "resume",
    "play",
)

#: 精确命中这些描述时优先级最高
_PLAY_EXACT = {"播放/暂停", "播放暂停", "暂停", "继续播放", "恢复播放", "play/pause", "playpause", "pause", "resume", "play"}

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


class UiProbeError(RuntimeError):
    """拿不到无障碍节点（dump 失败 / 输出不是 XML）。"""


@dataclass(frozen=True)
class UiNode:
    """uiautomator XML 里的一个节点（只保留用得到的属性）。"""

    class_name: str = ""
    text: str = ""
    content_desc: str = ""
    resource_id: str = ""
    clickable: bool = False
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def label(self) -> str:
        """人看得懂的标签：优先 content-desc，其次 text。"""
        return (self.content_desc or self.text or "").strip()

    @property
    def center(self) -> Tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def width(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0])

    @property
    def height(self) -> int:
        return max(0, self.bounds[3] - self.bounds[1])


def _parse_bounds(raw: str) -> Tuple[int, int, int, int]:
    match = _BOUNDS_RE.search(raw or "")
    if not match:
        return (0, 0, 0, 0)
    return tuple(int(v) for v in match.groups())  # type: ignore[return-value]


def parse_dump(xml_text: str) -> List[UiNode]:
    """把 uiautomator 的 XML 解析成节点列表；不是 XML 就抛 UiProbeError。"""
    text = (xml_text or "").strip()
    if not text:
        raise UiProbeError("uiautomator dump 输出为空")
    start = text.find("<hierarchy")
    if start < 0:
        raise UiProbeError(f"uiautomator dump 没有返回 XML：{text[:120]!r}")
    try:
        root = ET.fromstring(text[start:])
    except ET.ParseError as exc:
        raise UiProbeError(f"解析 uiautomator XML 失败：{exc}") from exc

    nodes: List[UiNode] = []
    for element in root.iter("node"):
        attrs = element.attrib
        nodes.append(
            UiNode(
                class_name=attrs.get("class", "") or "",
                text=attrs.get("text", "") or "",
                content_desc=attrs.get("content-desc", "") or "",
                resource_id=attrs.get("resource-id", "") or "",
                clickable=(attrs.get("clickable", "false").lower() == "true"),
                bounds=_parse_bounds(attrs.get("bounds", "")),
            )
        )
    return nodes


def score_play_pause_node(node: UiNode) -> int:
    """给一个节点打分：像"播放/暂停按钮"的分数高，0 表示不像。"""
    label = node.label.lower()
    if not label or node.width < 8 or node.height < 8:
        return 0

    score = 0
    class_name = node.class_name.lower()
    looks_like_control = (
        node.clickable or "button" in class_name or "image" in class_name
    )
    if label in _PLAY_EXACT:
        score += 10
    elif any(word in label for word in PLAY_PAUSE_WORDS):
        # 只是"文案里带播放"（如"播放列表里没有更多视频了"）不算按钮
        if not looks_like_control:
            return 0
        score += 6
    else:
        return 0

    # "发个友善的弹幕见证当下"之类含"弹幕/播放"的长文本，不可能是按钮
    if len(label) > 16:
        score -= 4
    if node.clickable:
        score += 2
    if "button" in node.class_name.lower():
        score += 1
    if "image" in node.class_name.lower():
        score += 1
    return max(0, score)


def find_play_pause_node(nodes: List[UiNode]) -> Optional[UiNode]:
    """挑出最像播放/暂停按钮的节点。"""
    best: Optional[UiNode] = None
    best_score = 0
    for node in nodes:
        score = score_play_pause_node(node)
        if score > best_score:
            best, best_score = node, score
    return best if best_score >= 6 else None


def find_seek_bar_node(nodes: List[UiNode], min_width: int = 200) -> Optional[UiNode]:
    """找一个足够宽的进度条节点（SeekBar/ProgressBar），没有就返回 None。"""
    best: Optional[UiNode] = None
    for node in nodes:
        name = node.class_name.lower()
        if "seekbar" not in name and "progressbar" not in name:
            continue
        if node.width < min_width or node.height < 4:
            continue
        if best is None or node.width > best.width:
            best = node
    return best


class UiProber:
    """对一台已绑定的设备做 uiautomator dump，并把节点变成坐标。"""

    #: dump 可能因为界面动画失败一次，重试一次基本就稳了
    _RETRIES = 2

    def __init__(self, client: AdbClient, logger: Optional[logging.Logger] = None):
        self._client = client
        self._log = logger or log

    def dump_nodes(self) -> List[UiNode]:
        """执行 uiautomator dump 并解析；失败抛 UiProbeError。"""
        last_error: Optional[Exception] = None
        for attempt in range(1, self._RETRIES + 1):
            try:
                out = self._client.shell("uiautomator", "dump", DUMP_PATH)
                if "dumped to" not in (out or "").lower() and attempt < self._RETRIES:
                    raise UiProbeError(f"uiautomator dump 未成功：{out[:120]!r}")
                xml = self._client.shell("cat", DUMP_PATH)
                return parse_dump(xml)
            except Exception as exc:  # noqa: BLE001 - dump 失败属于常态，交给上层降级
                last_error = exc
                self._log.debug("uiautomator dump 第 %d 次失败：%s", attempt, exc)
        raise UiProbeError(f"无法获取界面节点：{last_error}")

    def play_pause_point(self) -> Optional[Tuple[int, int]]:
        """播放/暂停按钮的中心坐标；找不到返回 None。"""
        node = find_play_pause_node(self.dump_nodes())
        if node is None:
            return None
        self._log.debug("无障碍节点命中播放/暂停按钮：%r %s", node.label, node.bounds)
        return node.center
