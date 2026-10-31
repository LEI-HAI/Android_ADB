"""ADB 封装：查找 adb、检测设备、执行 shell / keyevent 命令。

只依赖标准库 subprocess，避免额外安装 adbutils。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .config import BASE_DIR, Config

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000  # 调 adb 时不弹黑框


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class AdbError(RuntimeError):
    """adb 命令执行失败。"""


class AdbNotFoundError(AdbError):
    """找不到 adb 可执行文件。"""


class DeviceError(AdbError):
    """设备相关错误（未连接 / 未授权 / 多设备等）。"""


class NoDeviceError(DeviceError):
    pass


class UnauthorizedError(DeviceError):
    pass


class OfflineError(DeviceError):
    pass


class MultipleDevicesError(DeviceError):
    pass


# ---------------------------------------------------------------------------
# 设备信息
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    transport_id: str = ""

    def __str__(self) -> str:
        label = self.model or self.product
        return f"{self.serial}{f' ({label})' if label else ''}"


# ---------------------------------------------------------------------------
# 查找 adb
# ---------------------------------------------------------------------------
def _home_dir() -> Optional[Path]:
    """安全获取用户主目录：在沙箱 / 精简环境里可能没有 USERPROFILE。"""
    try:
        return Path.home()
    except (RuntimeError, OSError):
        pass
    for name in ("USERPROFILE", "HOME"):
        value = os.environ.get(name)
        if value:
            return Path(value)
    return None


def _candidate_paths() -> List[Path]:
    """按优先级返回可能存在的 adb 路径（存在与否由调用方判断）。"""
    candidates: List[Path] = [
        BASE_DIR / "platform-tools" / "adb.exe",
        BASE_DIR / "adb.exe",
        BASE_DIR / "adb" / "adb.exe",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    home = _home_dir()
    if home is not None:
        candidates.append(home / "platform-tools" / "adb.exe")
    candidates += [
        Path("C:/platform-tools/adb.exe"),
        Path("C:/adb/adb.exe"),
    ]
    candidates += _discover_common_bundles()
    return candidates


def _discover_common_bundles() -> List[Path]:
    """在常见目录里顺带找一下其它软件附带的 platform-tools。

    只做一层通配，耗时几毫秒；找不到就算了，不会报错。
    """
    found: List[Path] = []
    home = _home_dir()
    if home is not None:
        roaming = home / "AppData" / "Roaming"
        for pattern in ("*/platform-tools/adb.exe", "*/*/platform-tools/adb.exe"):
            try:
                found.extend(sorted(roaming.glob(pattern)))
            except OSError:
                pass
    for base in (Path("C:/Program Files"), Path("C:/Program Files (x86)")):
        try:
            found.extend(sorted(base.glob("*/adb.exe")))
        except OSError:
            pass
    return found


def find_adb(config: Optional[Config] = None) -> Path:
    """定位 adb：显式配置 > PATH > 常见安装位置。"""
    explicit = []
    env_path = os.environ.get("ADB_PATH")
    if env_path:
        explicit.append(("环境变量 ADB_PATH", Path(env_path).expanduser()))
    if config is not None and config.adb_path:
        explicit.append(("config.json 的 adb_path", Path(config.adb_path).expanduser()))

    for source, candidate in explicit:
        if candidate.is_file():
            return candidate
        raise AdbNotFoundError(f"{source} 指向的文件不存在：{candidate}")

    which = shutil.which("adb")
    if which:
        return Path(which)

    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate

    tried = "\n".join(f"    - {p}" for p in _candidate_paths())
    raise AdbNotFoundError(
        "未找到 adb 可执行文件。请任选一种方式解决：\n"
        "    1) 把 platform-tools 整个目录放到本项目下，即 platform-tools\\adb.exe\n"
        "    2) 把 adb 所在目录加入系统 PATH\n"
        "    3) 在 config.json 里填写 adb_path，或设置环境变量 ADB_PATH\n"
        f"已尝试的位置：\n{tried}"
    )


# ---------------------------------------------------------------------------
# ADB 客户端
# ---------------------------------------------------------------------------
class AdbClient:
    """对一个 adb 可执行文件的薄封装，绑定设备后可发送按键。"""

    def __init__(self, adb_path: Path, timeout: float = 8.0) -> None:
        self.adb_path = str(adb_path)
        self.timeout = timeout
        self._serial: Optional[str] = None

    # ----------------------------- 基础执行 -----------------------------
    @property
    def serial(self) -> Optional[str]:
        return self._serial

    def _popen_kwargs(self) -> Dict[str, object]:
        if IS_WINDOWS:
            return {"creationflags": _CREATE_NO_WINDOW}
        return {}

    def run(
        self,
        *args: object,
        timeout: Optional[float] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        cmd = [self.adb_path] + [str(a) for a in args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                **self._popen_kwargs(),
            )
        except FileNotFoundError as exc:
            raise AdbNotFoundError(f"无法执行 adb：{self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(
                f"adb 命令超时（{timeout or self.timeout:.1f}s）：{' '.join(str(a) for a in args)}"
            ) from exc
        except OSError as exc:
            raise AdbError(f"启动 adb 失败：{exc}") from exc

        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise AdbError(
                f"adb {' '.join(str(a) for a in args)} 执行失败"
                f"（退出码 {proc.returncode}）：{detail or '无输出'}"
            )
        return proc

    def run_text(
        self,
        *args: object,
        check: bool = True,
        timeout: Optional[float] = None,
    ) -> str:
        return (self.run(*args, check=check, timeout=timeout).stdout or "").strip()

    # ----------------------------- 设备管理 -----------------------------
    def version(self) -> str:
        out = self.run_text("version", check=False)
        return out.splitlines()[0].strip() if out else "未知版本"

    def start_server(self) -> None:
        self.run("start-server", check=False)

    @staticmethod
    def _parse_devices(output: str) -> List[DeviceInfo]:
        devices: List[DeviceInfo] = []
        for line in (output or "").splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            attrs = {}
            for token in parts[2:]:
                if ":" in token:
                    key, value = token.split(":", 1)
                    attrs[key] = value
            devices.append(
                DeviceInfo(
                    serial=serial,
                    state=state,
                    model=attrs.get("model", ""),
                    product=attrs.get("product", ""),
                    transport_id=attrs.get("transport_id", ""),
                )
            )
        return devices

    def devices(self) -> List[DeviceInfo]:
        self.start_server()
        return self._parse_devices(self.run_text("devices", "-l"))

    def connect(self, address: str) -> str:
        """连接无线调试设备，例如 192.168.1.10:5555。"""
        return self.run_text("connect", address, check=False)

    def pick_device(self, preferred: Optional[str] = None) -> DeviceInfo:
        """选择并返回一台可用设备；异常信息里面带排查建议。"""
        devices = self.devices()
        if not devices:
            raise NoDeviceError(
                "未检测到任何设备。请依次检查：\n"
                "    1) 数据线是否支持传输数据（不是只能充电的线）\n"
                "    2) 手机是否已开启「开发者选项 - USB 调试」\n"
                "    3) 手机上是否弹出了「允许 USB 调试」并点了允许\n"
                "    4) 在命令行执行 adb devices，确认能看到设备"
            )

        if preferred:
            match = next((d for d in devices if d.serial == preferred), None)
            if match is None:
                serials = ", ".join(d.serial for d in devices)
                raise DeviceError(f"指定的设备 {preferred} 不在线。当前设备：{serials}")
            if match.state != "device":
                raise self._state_error(match)
            return match

        ready = [d for d in devices if d.state == "device"]
        if not ready:
            raise self._state_error(devices[0])

        if len(ready) > 1:
            serials = "\n".join(f"    - {d}" for d in ready)
            raise MultipleDevicesError(
                f"检测到 {len(ready)} 台可用设备，请指定使用哪一台：\n{serials}\n"
                "    方式一：python main.py --device <serial>\n"
                "    方式二：在 config.json 里填写 device_serial"
            )
        return ready[0]

    @staticmethod
    def _state_error(device: DeviceInfo) -> DeviceError:
        if device.state == "unauthorized":
            return UnauthorizedError(
                f"设备 {device.serial} 尚未授权。请在手机上点击「允许 USB 调试」，"
                "必要时先撤销授权再重新插拔数据线。"
            )
        if device.state == "offline":
            return OfflineError(
                f"设备 {device.serial} 处于 offline 状态。请重新插拔数据线，"
                "或执行 adb kill-server 后重试。"
            )
        return DeviceError(f"设备 {device.serial} 当前状态为 {device.state}，无法使用。")

    def bind(self, serial: str) -> "AdbClient":
        self._serial = serial
        return self

    # ----------------------------- 命令下发 -----------------------------
    def _require_serial(self) -> str:
        if not self._serial:
            raise AdbError("尚未绑定设备，请先调用 pick_device()/bind()")
        return self._serial

    def shell(self, *args: object, timeout: Optional[float] = None) -> str:
        serial = self._require_serial()
        return self.run_text("-s", serial, "shell", *args, timeout=timeout)

    def keyevent(self, keycode: object, timeout: Optional[float] = None) -> None:
        """发送单个按键，keycode 可以是 85 或 KEYCODE_MEDIA_PLAY_PAUSE。"""
        code = str(keycode).strip()
        if not code:
            raise AdbError("按键码不能为空")
        self.shell("input", "keyevent", code, timeout=timeout)

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 200,
        timeout: Optional[float] = None,
    ) -> None:
        """在屏幕上从 (x1,y1) 滑到 (x2,y2)，duration_ms 为滑动时长。"""
        cost = max(1.0, int(duration_ms) / 1000.0 + 3.0)
        self.shell(
            "input", "swipe",
            int(x1), int(y1), int(x2), int(y2), int(duration_ms),
            timeout=max(cost, timeout or 0.0),
        )

    def tap(self, x: int, y: int, timeout: Optional[float] = None) -> None:
        """点击屏幕上的一点（屏幕坐标，注意横屏时坐标是旋转后的）。"""
        self.shell("input", "tap", int(x), int(y), timeout=timeout)

    def screen_size(self) -> Tuple[int, int]:
        """物理分辨率（竖屏基准，不带旋转），例如 (1080, 2340)。"""
        out = self.shell("wm", "size")
        match = re.search(r"Override size:\s*(\d+)x(\d+)", out) or re.search(
            r"Physical size:\s*(\d+)x(\d+)", out
        )
        if not match:
            raise AdbError(f"无法解析屏幕分辨率：{out!r}")
        return int(match.group(1)), int(match.group(2))

    def rotation(self) -> int:
        """当前屏幕旋转：0/1/2/3（1 = 顺时针 90 度，即横屏）。

        取值来源按新到旧依次尝试（不同 Android 版本字段不一样）：

        1. `dumpsys input` 里 `Viewport INTERNAL: ... orientation=1`（Android 14+）
        2. `dumpsys input` 里 `SurfaceOrientation: 1`（老版本，Android 13 及以前）
        3. `dumpsys display` 里 `mCurrentOrientation=1`（兜底）

        都读不到才返回 0。Android 16 上 `SurfaceOrientation` 已经没有了，
        只按老字段解析会让横屏被当成竖屏，点击坐标全错。
        """
        out = self.shell("dumpsys", "input")
        match = re.search(r"SurfaceOrientation:\s*(\d)", out)
        if not match:
            match = re.search(r"Viewport[^\n]*?orientation=(\d)", out)
        if not match:
            try:
                match = re.search(
                    r"mCurrentOrientation=(\d)", self.shell("dumpsys", "display")
                )
            except AdbError:
                match = None
        return int(match.group(1)) % 4 if match else 0

    def display_size(self) -> Tuple[int, int]:
        """考虑旋转后的可用宽高，例如横屏时返回 (2340, 1080)。"""
        width, height = self.screen_size()
        if self.rotation() % 2 == 1:
            return height, width
        return width, height

    def media_session_dump(self) -> str:
        """dumpsys media_session 的原始文本，交给 media_state 解析。"""
        return self.shell("dumpsys", "media_session")
