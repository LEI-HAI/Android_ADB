"""配置中心：集中管理所有可调参数。

配置优先级（从高到低）：
    1. 命令行参数（--device / --backend / --dry-run 等）
    2. 环境变量（ADB_PATH / ADB_SERIAL / ADB_CONNECT / ADB_BACKEND / ADB_LOG_LEVEL）
    3. 项目根目录下的 config.json
    4. Config 类中的默认值
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"

log = logging.getLogger(__name__)

_OPTIONAL_STR_FIELDS = ("adb_path", "device_serial", "adb_connect")
_INT_FIELDS = (
    "play_pause_keycode",
    "rewind_keycode",
    "rewind_repeat",
    "rewind_interval_ms",
    "rewind_fallback_repeat",
    "swipe_duration_ms",
    "swipe_settle_ms",
    "play_pause_verify_ms",
    "play_pause_tap_settle_ms",
    "debounce_ms",
)
_FLOAT_FIELDS = (
    "swipe_seconds",
    "swipe_ms_per_px",
    "swipe_start_ratio",
    "swipe_y_ratio",
    "play_pause_x_ratio",
    "play_pause_y_ratio",
)


class ConfigError(ValueError):
    """配置内容不合法。"""


@dataclass
class Config:
    """全部运行参数。字段名与 config.json 的键一一对应。"""

    # ---------- ADB ----------
    adb_path: Optional[str] = None            # 为空则自动查找
    device_serial: Optional[str] = None       # 多设备时指定，如 "5889912f"
    adb_connect: Optional[str] = None         # 无线调试地址，如 "192.168.1.10:5555"
    adb_timeout: float = 8.0                  # 单条 adb 命令超时（秒）

    # ---------- 媒体按键 ----------
    play_pause_keycode: int = 85              # KEYCODE_MEDIA_PLAY_PAUSE
    rewind_keycode: int = 89                  # KEYCODE_MEDIA_REWIND
    rewind_repeat: int = 1                    # 一次"后退"连发几次，用于凑出 5 秒
    rewind_interval_ms: int = 120             # 连发间隔（毫秒）
    rewind_fallback_keycode: Optional[int] = 21   # 媒体键无效时改用的键（21 = DPAD_LEFT，null 表示不启用）
    rewind_fallback_repeat: int = 1

    # ---------- 后退方式（B 站等客户端不响应 89，默认改用横滑手势） ----------
    rewind_mode: str = "swipe"                # swipe = 横滑手势（推荐）；media_key = 媒体按键
    swipe_seconds: float = 5.0                # 每次后退的目标秒数
    swipe_ms_per_px: float = 31.0             # 初始换算比例：滑动 1 像素约等于多少毫秒视频
    swipe_start_ratio: float = 0.80           # 滑动起点 x 占屏幕宽度的比例
    swipe_y_ratio: float = 0.55               # 滑动所在高度占屏幕高度的比例（避开顶部标题与底部按钮）
    swipe_duration_ms: int = 200              # 手势持续时长（毫秒）
    swipe_settle_ms: int = 320                # 滑动后等待多久再读取播放位置
    auto_calibrate: bool = True               # 用实际后退秒数自动校准 swipe_ms_per_px

    # ---------- 播放/暂停的兜底方式 ----------
    # 实测：视频暂停一段时间后，安卓的"媒体按钮会话"会变成 null，keyevent 85
    # 就没人接收了（表现为"第一次能暂停，再按播放没反应"）。auto 模式先发媒体键，
    # 发现没生效就点一下画面左下角的播放/暂停按钮。
    play_pause_mode: str = "auto"             # auto = 媒体键 + 点按钮兜底；media_key = 只发媒体键；tap = 只点按钮
    play_pause_x_ratio: float = 0.039         # 播放/暂停按钮中心 x 占屏幕宽度的比例（B 站全屏左下角）
    play_pause_y_ratio: float = 0.904         # 同上，y 占屏幕高度的比例
    play_pause_verify_ms: int = 400           # 发完媒体键后等多久再读播放状态确认
    play_pause_tap_settle_ms: int = 350       # 点完按钮后等多久再读播放状态确认

    # ---------- 鼠标 ----------
    rewind_button: str = "x1"                 # 鼠标侧键1（后退键）
    play_pause_button: str = "x2"             # 鼠标侧键2（前进键）
    debounce_ms: int = 350                    # 防抖窗口（毫秒）

    # ---------- 键盘备用绑定（可选，默认关闭） ----------
    # 有些游戏/反作弊会拦截全局鼠标钩子，或者鼠标驱动把侧键改成了别的键，
    # 这时可以在驱动里把侧键映射成键盘组合，再在这里绑定同一组合。
    rewind_key: Optional[str] = None          # 例如 "ctrl+alt+left"
    play_pause_key: Optional[str] = None      # 例如 "ctrl+alt+space"

    # ---------- 退出 ----------
    # 用组合键而不是单个 Esc：全局监听下，游戏菜单 / 浏览器 / 对话框里的 Esc
    # 都会把本程序关掉（曾经就踩过这个坑）。
    exit_key: str = "ctrl+alt+q"
    enable_exit_hotkey: bool = True

    # ---------- 其它 ----------
    backend: str = "adb"                      # adb / scrcpy

    # ---------- 日志 / 稳定性 ----------
    log_level: str = "INFO"                   # 控制台日志级别
    log_file: Optional[str] = "logs/controller.log"   # 日志文件（相对项目根目录），null 表示不写文件
    log_file_level: str = "DEBUG"             # 文件里记多细（DEBUG 会记每次滑动与重启）
    hook_restart: bool = True                 # 鼠标/键盘钩子意外停止时自动重启
    hook_restart_limit: int = 30              # 连续重启上限（0 = 不限），超过后退出并留下日志
    heartbeat_seconds: int = 300              # 每隔多少秒写一条心跳日志（0 = 关闭）
    dry_run: bool = False                     # 只打印不执行，便于无手机时调试

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """读取配置文件（若存在）并合并环境变量，最后做合法性校验。"""
        cfg = cls()
        target = Path(path) if path else DEFAULT_CONFIG_PATH
        if target.is_file():
            try:
                raw = target.read_text(encoding="utf-8-sig")
                data = json.loads(raw) if raw.strip() else {}
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("读取配置文件 %s 失败，改用默认配置：%s", target, exc)
            else:
                if isinstance(data, dict):
                    cfg.merge(data, source=str(target))
                else:
                    raise ConfigError(f"配置文件 {target} 的顶层必须是 JSON 对象")
        cfg.apply_env()
        cfg.validate()
        return cfg

    def merge(self, data: Dict[str, Any], source: str = "config") -> None:
        """按字段名合并字典，未知字段只警告不报错。"""
        known = {f.name for f in fields(self)}
        for key, value in (data or {}).items():
            if key not in known:
                log.warning("%s 中存在未知配置项 %r，已忽略", source, key)
                continue
            if key in _OPTIONAL_STR_FIELDS and value == "":
                value = None
            setattr(self, key, value)

    def apply_env(self) -> None:
        env_map = {
            "ADB_PATH": "adb_path",
            "ADB_SERIAL": "device_serial",
            "ADB_CONNECT": "adb_connect",
            "ADB_BACKEND": "backend",
            "ADB_LOG_LEVEL": "log_level",
        }
        for env_name, field_name in env_map.items():
            value = os.environ.get(env_name)
            if value:
                setattr(self, field_name, value)

    def validate(self) -> None:
        self.backend = str(self.backend or "adb").strip().lower()
        if self.backend not in ("adb", "scrcpy"):
            raise ConfigError(f"backend 只能是 adb 或 scrcpy，当前为 {self.backend!r}")

        self.rewind_button = str(self.rewind_button or "x1").strip().lower()
        self.play_pause_button = str(self.play_pause_button or "x2").strip().lower()
        if self.rewind_button == self.play_pause_button:
            raise ConfigError("rewind_button 与 play_pause_button 不能是同一个鼠标键")

        for name in _INT_FIELDS:
            setattr(self, name, self._to_int(name, getattr(self, name)))
        if self.rewind_fallback_keycode not in (None, ""):
            self.rewind_fallback_keycode = self._to_int(
                "rewind_fallback_keycode", self.rewind_fallback_keycode
            )
        else:
            self.rewind_fallback_keycode = None

        self.rewind_mode = str(self.rewind_mode or "swipe").strip().lower()
        if self.rewind_mode not in ("swipe", "media_key"):
            raise ConfigError(
                f"rewind_mode 只能是 swipe 或 media_key，当前为 {self.rewind_mode!r}"
            )

        self.play_pause_mode = str(self.play_pause_mode or "auto").strip().lower()
        if self.play_pause_mode not in ("auto", "media_key", "tap"):
            raise ConfigError(
                f"play_pause_mode 只能是 auto、media_key 或 tap，当前为 {self.play_pause_mode!r}"
            )

        for name in _FLOAT_FIELDS:
            try:
                setattr(self, name, float(getattr(self, name)))
            except (TypeError, ValueError):
                raise ConfigError(f"配置项 {name} 必须是数字，当前为 {getattr(self, name)!r}") from None
        if not 0.2 <= self.swipe_seconds <= 120:
            raise ConfigError(f"swipe_seconds 需在 0.2~120 之间，当前为 {self.swipe_seconds}")
        if not 1.0 <= self.swipe_ms_per_px <= 500:
            raise ConfigError(f"swipe_ms_per_px 需在 1~500 之间，当前为 {self.swipe_ms_per_px}")
        self.swipe_start_ratio = min(0.95, max(0.2, self.swipe_start_ratio))
        self.swipe_y_ratio = min(0.9, max(0.1, self.swipe_y_ratio))
        self.swipe_duration_ms = min(2000, max(20, int(self.swipe_duration_ms)))
        self.swipe_settle_ms = min(3000, max(0, int(self.swipe_settle_ms)))
        self.play_pause_x_ratio = min(0.99, max(0.01, self.play_pause_x_ratio))
        self.play_pause_y_ratio = min(0.99, max(0.01, self.play_pause_y_ratio))
        self.play_pause_verify_ms = min(5000, max(0, int(self.play_pause_verify_ms)))
        self.play_pause_tap_settle_ms = min(5000, max(0, int(self.play_pause_tap_settle_ms)))
        self.auto_calibrate = bool(self.auto_calibrate)

        self.debounce_ms = max(0, int(self.debounce_ms))
        self.rewind_interval_ms = max(0, int(self.rewind_interval_ms))
        self.rewind_repeat = max(1, int(self.rewind_repeat))
        self.rewind_fallback_repeat = max(1, int(self.rewind_fallback_repeat))
        try:
            self.adb_timeout = max(1.0, float(self.adb_timeout))
        except (TypeError, ValueError):
            raise ConfigError(f"adb_timeout 必须是数字，当前为 {self.adb_timeout!r}") from None

        for name in ("rewind_key", "play_pause_key"):
            raw = getattr(self, name)
            value = str(raw).strip().lower() if raw not in (None, "") else None
            if value:
                try:
                    from .hotkeys import parse_combo

                    parse_combo(value)
                except ValueError as exc:
                    raise ConfigError(f"{name} 不合法：{exc}") from None
            setattr(self, name, value)

        self.exit_key = str(self.exit_key or "ctrl+alt+q").strip().lower()
        if self.enable_exit_hotkey:
            try:
                from .hotkeys import parse_combo

                parse_combo(self.exit_key)
            except ValueError as exc:
                raise ConfigError(f"exit_key 不合法：{exc}") from None
            for name in ("rewind_key", "play_pause_key"):
                if getattr(self, name) and getattr(self, name) == self.exit_key:
                    raise ConfigError(f"{name} 不能和退出快捷键 exit_key 相同（{self.exit_key}）")

        for name in _OPTIONAL_STR_FIELDS:
            value = getattr(self, name)
            if isinstance(value, str):
                setattr(self, name, value.strip() or None)

        self.log_level = str(self.log_level or "INFO").strip().upper()
        self.log_file_level = str(self.log_file_level or "DEBUG").strip().upper()
        for name in ("log_level", "log_file_level"):
            value = getattr(self, name)
            if value not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                raise ConfigError(f"{name} 只能是 DEBUG/INFO/WARNING/ERROR/CRITICAL，当前为 {value!r}")
        if self.log_file in (None, ""):
            self.log_file = None
        else:
            self.log_file = str(self.log_file).strip() or None
        self.hook_restart = bool(self.hook_restart)
        try:
            self.hook_restart_limit = max(0, int(self.hook_restart_limit))
        except (TypeError, ValueError):
            raise ConfigError(f"hook_restart_limit 必须是整数，当前为 {self.hook_restart_limit!r}") from None
        try:
            self.heartbeat_seconds = max(0, int(self.heartbeat_seconds))
        except (TypeError, ValueError):
            raise ConfigError(f"heartbeat_seconds 必须是整数，当前为 {self.heartbeat_seconds!r}") from None

    @staticmethod
    def _to_int(name: str, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"配置项 {name} 必须是整数，当前为 {value!r}") from None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Optional[Path] = None) -> Path:
        """把当前配置写回 config.json（调试用）。"""
        target = Path(path) if path else DEFAULT_CONFIG_PATH
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target
