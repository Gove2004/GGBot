"""配置加载：读取 config.json，提供 dict 访问与默认兜底。

config.json 实际结构（与文件保持一致）：
- 顶层 appid / appsecret：QQ 机器人凭据
- deepseek：LLM 配置（base_url / api_key / model / request / context）
- embedding：向量化配置（SiliconFlow API）
- behavior / storage / web
"""

from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    """config.json 加载器。字段缺失时用默认值兜底，避免启动崩溃。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else BASE_DIR / "config.json"
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)

        # QQ 凭据：config 顶层 appid / appsecret
        self.qq = {
            "appid": self.data.get("appid", ""),
            "secret": self.data.get("appsecret", ""),
        }
        # LLM：deepseek 子对象
        self.llm = self.data.get("deepseek", {})
        self.behavior = self.data.get("behavior", {})
        self.storage_cfg = self.data.get("storage", {})
        self.web_cfg = self.data.get("web", {})

    @property
    def model(self) -> str:
        return self.llm.get("model", "deepseek-flash")

    @property
    def request(self) -> dict:
        """DeepSeek 请求参数（thinking/temperature/top_p/max_tokens/stop/stream）。"""
        return self.llm.get("request", {})

    @property
    def context(self) -> dict:
        """上下文预算：max_input_tokens / max_history_messages / summary_threshold_tokens。"""
        return self.llm.get("context", {})

    @property
    def presence(self) -> dict:
        return self.behavior.get("presence", {})

    @property
    def reply(self) -> dict:
        return self.behavior.get("reply", {})

    @property
    def backlog(self) -> dict:
        return self.behavior.get("backlog", {})

    @property
    def temp_dir(self) -> Path:
        d = self.storage_cfg.get("temp_dir", "Temp")
        p = Path(d)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        d = self.storage_cfg.get("db_path", "ggbot.db")
        p = Path(d)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p
