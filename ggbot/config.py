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
        self.path = Path(path) if path else BASE_DIR / "db" / "config.json"
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)

        # QQ 凭据：config 顶层 appid / appsecret / bot_openid
        self.qq = {
            "appid": self.data.get("appid", ""),
            "secret": self.data.get("appsecret", ""),
            "bot_openid": self.data.get("bot_openid", ""),
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
    def embedding(self) -> dict:
        """向量化配置（仅读 config.json，项目靠配置文件驱动，无环境变量覆盖）。"""
        emb = self.data.get("embedding", {})
        return {
            "provider": emb.get("provider", "siliconflow"),
            "base_url": emb.get("base_url", "https://api.siliconflow.cn/v1"),
            "model": emb.get("model", "Qwen/Qwen3-Embedding-4B"),
            "api_key": emb.get("api_key", ""),
            "batch_size": max(1, int(emb.get("batch_size", 32))),
        }

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
        d = self.storage_cfg.get("temp_dir", "temp")
        p = Path(d)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        d = self.storage_cfg.get("db_path", "db/ggbot.db")
        p = Path(d)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def persona_path(self) -> Path:
        """内核人设（owner 定义，AI 只读），位于 db/ 目录。"""
        return BASE_DIR / "db" / "persona_core.md"

    @property
    def logs_dir(self) -> Path:
        """日志目录：随 storage.temp_dir 走（temp/logs），目录自动创建。"""
        d = self.temp_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "ggbot.log"

    def get_by_path(self, dotted: str):
        """按 'a.b.c' 读取嵌套配置。"""
        node = self.data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    def set_by_path(self, dotted: str, value) -> bool:
        """按 'a.b.c' 更新嵌套配置（dict 引用共享，Presence/LLM 等运行对象热生效）。"""
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            return False
        node[parts[-1]] = value
        return True

    def save(self) -> None:
        """写回 config.json（保留缩进与中文）。"""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
            f.write("\n")
