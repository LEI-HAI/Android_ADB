"""标定缓存：把"这台设备、这个 App、这个方向该点哪里"记进 profiles.json。

为什么需要它
------------
点击位置默认是 config.json 里的比例坐标，只按一台手机+一个 App 标定过。换手机、
换视频平台后坐标就不对了。与其让用户手改 config.json，不如让程序在"某次点击确实
改变了播放状态"之后，把 包名 + 分辨率 + 方向 对应的坐标记下来，下次直接用；
点不准的旧记录也不会影响别的设备/App。

文件坏了不影响主流程：读失败当作空档案，写失败只记一条警告（标定只是加速项）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import BASE_DIR

log = logging.getLogger(__name__)

_VERSION = 1


def profile_key(
    package: Optional[str], width: int, height: int, rotation: int = 0
) -> str:
    """档案键：包名 + 当前可用分辨率 + 方向，例如 `tv.danmaku.bili|2340x1080|r1`。"""
    pkg = (package or "unknown").strip() or "unknown"
    return f"{pkg}|{int(width)}x{int(height)}|r{int(rotation) % 4}"


def resolve_profile_path(name: Optional[str]) -> Path:
    """把配置里的路径变成绝对路径（相对路径以项目根目录为准）。"""
    if not name:
        return BASE_DIR / "profiles.json"
    path = Path(name).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


class ProfileStore:
    """profiles.json 的读写。

    本程序所有 adb 操作都在同一个 worker 线程里串行执行，所以这里不加锁。
    """

    def __init__(
        self, path: Optional[str] = None, logger: Optional[logging.Logger] = None
    ) -> None:
        self.path = resolve_profile_path(path)
        self._log = logger or log
        self._data: Optional[Dict[str, Any]] = None

    # ------------------------------- 读 -------------------------------
    def _load(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data

        data: Dict[str, Any] = {}
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raw = ""
        except OSError as exc:
            self._log.debug("读取标定档案 %s 失败：%s", self.path, exc)
            raw = ""

        if raw.strip():
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._log.warning(
                    "标定档案 %s 不是合法 JSON（%s），本次忽略", self.path, exc
                )
            else:
                if isinstance(loaded, dict):
                    data = loaded

        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            data = {"version": _VERSION, "profiles": {}}
        data.setdefault("version", _VERSION)
        self._data = data
        return data

    def get(self, key: str) -> Dict[str, Any]:
        """某台设备+App 的档案，没有就返回空字典。"""
        entry = self._load()["profiles"].get(key)
        return dict(entry) if isinstance(entry, dict) else {}

    def keys(self) -> List[str]:
        return sorted(self._load()["profiles"])

    # ------------------------------- 写 -------------------------------
    def update(self, key: str, section: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """更新档案里的一个分区（play_pause / rewind）并立即落盘。"""
        data = self._load()
        entry = data["profiles"].setdefault(key, {})
        if not isinstance(entry, dict):
            entry = {}
            data["profiles"][key] = entry
        target = entry.setdefault(section, {})
        if not isinstance(target, dict):
            target = {}
            entry[section] = target
        target.update(values)
        target["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()
        return dict(target)

    def save(self) -> bool:
        data = self._load()
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            self._log.warning("写入标定档案 %s 失败：%s", self.path, exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False
        return True

    # --------------------------- 便捷接口 ---------------------------
    def play_pause_point(self, key: str) -> Optional[Tuple[float, float]]:
        section = self.get(key).get("play_pause")
        if not isinstance(section, dict):
            return None
        try:
            x_ratio = float(section["x_ratio"])
            y_ratio = float(section["y_ratio"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0.0 < x_ratio < 1.0 and 0.0 < y_ratio < 1.0):
            return None
        return x_ratio, y_ratio

    def set_play_pause_point(
        self, key: str, x_ratio: float, y_ratio: float, source: str = "verified"
    ) -> bool:
        self.update(
            key,
            "play_pause",
            {
                "x_ratio": round(float(x_ratio), 4),
                "y_ratio": round(float(y_ratio), 4),
                "source": source,
            },
        )
        return True

    def ms_per_px(self, key: str) -> Optional[float]:
        section = self.get(key).get("rewind")
        if not isinstance(section, dict):
            return None
        try:
            value = float(section["ms_per_px"])
        except (KeyError, TypeError, ValueError):
            return None
        return value if 1.0 <= value <= 500.0 else None

    def set_ms_per_px(self, key: str, value: float, source: str = "measured") -> bool:
        if not 1.0 <= float(value) <= 500.0:
            return False
        self.update(
            key,
            "rewind",
            {"ms_per_px": round(float(value), 2), "source": source},
        )
        return True
