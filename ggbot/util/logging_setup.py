"""日志系统：多等级、文件轮转、控制台输出、第三方库降噪。

- 输出：temp/logs/ggbot.log（RotatingFileHandler，5MB × 5 备份，每次启动截断重写）+ stderr 控制台
- 格式（所有日志统一）：`时间 | 等级 | 会话 | 内容`
  - 会话列优先取 logger extra={"session": ...}（业务代码显式传入，8 位 id）；
    一条回应的每一行（收到、上下文预算、LLM 请求/响应、模型决策、工具调用）
    都必须带上同一个会话标签，否则按会话过滤时会断链
  - 未传时从内容兜底识别（群=xx / 会话X），否则系统00000000（启动、调度等无会话日志）
- 内容前缀约定：收到 [昵称/8位id] @=1|0（入站，@ 只表示这条有没有叫到本机器人，
  被动/主动通道由发送侧决定，这里不重复）、模型…（回合决策）、工具 X（admin=…）（调用与结果）、
  识图/图片已识别（识图开销与正文各一行）、语音已转写（平台 ASR 结果）、
  上线/下线（作息）
- 同一信息只打一行：描述正文只在「图片已识别」出现，语音转写只在「语音已转写」出现，
  tokens/费用只在「识图」「LLM 请求/响应」出现，工具参数只在「工具 X（…）」出现
- 发送成功不再单独打确认行：正文已在工具调用行记过一次，只有失败或降级主动发送才留痕
- LLM 两行只报数量：请求行 = 服务商输入 tokens + 工具/历史/新增三段（本地估算按比例缩放到总数），
  响应行 = 输出 tokens + 缓存命中 + 费用，不打第 N 次
- 等级：root 按配置设置（默认 INFO，可调 DEBUG/WARNING/ERROR）；set_level 热切换
- DEBUG 用于业务排查；第三方库（botpy/openai/httpx/httpcore2/uvicorn）固定 WARNING，避免连接/报文刷屏
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

from ggbot.util.session import SYSTEM_LABEL

FMT = "%(asctime)s | %(levelname)-5s | %(session)-10s | %(message)s"

# 第三方库降噪（它们只在自身 logger 上输出 DEBUG，改代码管不到，只能限等级）
NOISY_LOGGERS = (
    "botpy", "openai", "httpx", "httpx2", "httpcore", "httpcore2", "h11", "hpack",
    "urllib3", "truststore", "anyio", "uvicorn", "uvicorn.error", "uvicorn.access", "asyncio",
)


def clip(text: str, n: int = 80) -> str:
    """日志用截断：超长时保留 n-1 个字符并显式补省略号，避免看起来像话没说完。"""
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def extract_session(content: str) -> str:
    """从日志内容兜底提取会话标识（8 位 id），提取不到归 系统00000000。"""
    m = re.search(r"会话\s*((?:群聊|私聊|系统)[0-9A-Za-z]{8})", content)
    if m:
        return m.group(1)
    m = re.search(r"群=([^\s,]+)", content)
    if m:
        return "群聊" + m.group(1)[:8]
    return SYSTEM_LABEL


class SessionFormatter(logging.Formatter):
    """每条日志附加「会话」列：优先业务显式传入，其次从内容兜底提取。"""

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, "session", None):
            try:
                record.session = extract_session(record.getMessage())
            except Exception:  # noqa: BLE001
                record.session = SYSTEM_LABEL
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
        log_file, mode="w", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
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
