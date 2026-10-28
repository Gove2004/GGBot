"""日志系统：多等级、文件轮转、控制台输出、第三方库降噪。

- 输出：temp/logs/ggbot.log（RotatingFileHandler，5MB × 5 备份）+ stderr 控制台
- 格式（所有日志统一）：`时间 | 等级 | 位置 | 会话 | 内容`
  - 会话列优先取 logger extra={"session": ...}（业务代码显式传入，8 位 id）；
    未传时从内容兜底识别（会话X / 群=xx / sender=xx），否则 System
- 等级：root 按配置设置（默认 INFO，可调 DEBUG/WARNING/ERROR）；set_level 热切换
- 第三方库（botpy/openai/httpx/uvicorn）默认 WARNING，避免刷屏
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

FMT = "%(asctime)s | %(levelname)-5s | %(name)-15s | %(session)-10s | %(message)s"

# 第三方库降噪（默认等级）
NOISY_LOGGERS = (
    "botpy", "openai", "httpx", "httpx2", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access", "asyncio",
)


def extract_session(content: str) -> str:
    """从日志内容兜底提取会话标识（8 位 id），提取不到归 System。"""
    m = re.search(r"会话([^\s，,：:]+)", content)
    if m:
        return m.group(1)
    m = re.search(r"群=([^\s,]+)", content)
    if m:
        return "群" + m.group(1)[:8]
    m = re.search(r"(?:sender|user|target)=([^\s,]+)", content)
    if m:
        return m.group(1)[:8]
    return "System"


class SessionFormatter(logging.Formatter):
    """每条日志附加「会话」列：优先业务显式传入，其次从内容兜底提取。"""

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, "session", None):
            try:
                record.session = extract_session(record.getMessage())
            except Exception:  # noqa: BLE001
                record.session = "System"
        return super().format(record)


def setup_logging(log_file: Path, level: str = "INFO", console: bool = True) -> None:
    """初始化根 logger：文件（轮转）+ 可选控制台。幂等，可重复调用。"""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())

    # 清除已有 handlers，避免重复
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass

    formatter = SessionFormatter(FMT)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        root.addHandler(ch)

    # 第三方库降噪
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def set_level(level: str) -> None:
    """热切换根日志等级（Web 面板调用）。"""
    logging.getLogger().setLevel(level.upper())


def tail_log(log_file: Path, lines: int = 200, level: str = "INFO") -> list[str]:
    """读取日志文件尾部 N 行，可选按等级过滤（匹配 "| LEVEL |" 段）。"""
    if not log_file.exists():
        return []
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return []
    tail = all_lines[-lines:]
    if level and level.upper() != "DEBUG":
        marker = f"| {level.upper()}"
        tail = [ln for ln in tail if marker in ln]
    return [ln.rstrip("\n") for ln in tail]
