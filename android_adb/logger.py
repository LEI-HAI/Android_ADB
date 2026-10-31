"""日志：控制台用中文短格式，文件里记全（带线程名），方便事后查"为什么退出"。"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional, Tuple

_LEVEL_CN = {
    "DEBUG": "调试",
    "INFO": "信息",
    "WARNING": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重",
}


class _ChineseFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.levelcn = _LEVEL_CN.get(record.levelname, record.levelname)
        return super().format(record)


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """轮转失败也不影响用的 RotatingFileHandler。

    Windows 上把 controller.log 改名，常常被杀毒软件、日志查看器或别的进程
    短暂占用而失败（PermissionError: WinError 32），标准 handler 会把异常直接
    以 "--- Logging error ---" 的形式刷到控制台，而且每条日志都重试一次。
    这里退化成"原地清空"，并且只提示一次。
    """

    def doRollover(self) -> None:  # noqa: D401 - 覆盖父类
        try:
            super().doRollover()
        except OSError:
            self._truncate_in_place()

    def _truncate_in_place(self) -> None:
        try:
            if self.stream:
                self.stream.close()
        finally:
            self.stream = None
        try:
            self.stream = self._open()
            self.stream.truncate(0)
            self.stream.seek(0)
        except OSError:
            pass

    def handleError(self, record: logging.LogRecord) -> None:
        if getattr(self, "_error_reported", False):
            return
        self._error_reported = True
        sys.stderr.write("提示：日志文件写入失败（可能被杀毒软件或其它程序占用），程序照常运行。\n")


_CONSOLE_FORMAT = "%(asctime)s | %(levelcn)s | %(message)s"
_FILE_FORMAT = "%(asctime)s | %(levelcn)-4s | %(threadName)-18s | %(name)s | %(message)s"


def setup_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    file_level: str = "DEBUG",
    max_bytes: int = 1_000_000,
    backup_count: int = 3,
) -> Tuple[logging.Logger, Optional[Path]]:
    """初始化根日志器；重复调用会替换旧的 handler，避免日志重复打印。

    返回 (根日志器, 实际使用的日志文件路径)；没有写文件时路径为 None。
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    console_level = getattr(logging, str(level).upper(), logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(_ChineseFormatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    written: Optional[Path] = None
    if log_file:
        target = Path(log_file)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            file_handler = _SafeRotatingFileHandler(
                target,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(getattr(logging, str(file_level).upper(), logging.DEBUG))
            file_handler.setFormatter(
                _ChineseFormatter(_FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
            )
            root.addHandler(file_handler)
            written = target
        except OSError as exc:
            root.warning("无法写入日志文件 %s：%s", target, exc)

    # pynput 在个别环境下会刷一堆无关日志
    logging.getLogger("pynput").setLevel(logging.WARNING)
    return root, written
