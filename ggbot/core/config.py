"""配置加载：读取 config.json，提供 dict 访问与默认兜底。

config.json 实际结构（与文件保持一致）：
- 顶层 appid / appsecret：QQ 机器人凭据
- llm：OpenAI 兼容的对话服务（base_url / api_key / model / request / context）
- embedding：OpenAI 兼容的向量服务（可选；不配置则记忆退回关键词检索）
- behavior / storage / web

LLM 与 embedding 都不绑定厂商：只要兼容 OpenAI 接口，换 base_url / model / api_key 即可。
旧版的 deepseek 段会在加载时自动迁移为 llm。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
EXAMPLE_NAME = "config.example.json"


class Config:
    """config.json 加载器。字段缺失时用默认值兜底，避免启动崩溃。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else BASE_DIR / "db" / "config.json"
        if not self.path.exists():
            example = self.path.with_name(EXAMPLE_NAME)
            if example.exists():
                shutil.copyfile(example, self.path)
                raise RuntimeError(
                    f"已根据模板生成 {self.path}，请填写 appid / appsecret、"
                    "llm.api_key / llm.base_url / llm.model（embedding 可留空）后重新启动"
                )
            raise RuntimeError(f"缺少配置文件 {self.path}，请从 {EXAMPLE_NAME} 复制后填写")
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)
        if self._migrate():
            self.save()

        # QQ 凭据：config 顶层 appid / appsecret / bot_openid
        self.qq = {
            "appid": self.data.get("appid", ""),
            "secret": self.data.get("appsecret", ""),
            "bot_openid": self.data.get("bot_openid", ""),
        }
        self.llm = self.data.get("llm", {})
        self.behavior = self.data.get("behavior", {})
        self.storage_cfg = self.data.get("storage", {})
        self.web_cfg = self.data.get("web", {})

    def _migrate(self) -> bool:
        """旧版 deepseek 段 → 通用 llm 段；补齐 vision 段；返回是否有改动。"""
        changed = False
        legacy = self.data.pop("deepseek", None)
        if legacy is not None:
            llm = self.data.setdefault("llm", {})
            for key, value in legacy.items():
                llm.setdefault(key, value)
            changed = True
        if "vision" not in self.data:
            self.data["vision"] = {
                "enabled": True,
                "base_url": "",
                "api_key": "",
                "model": "",
                "thinking": "",
                "prompt": "简单描述一下图片内容：主体、场景和画面里的关键文字，一两句话，不要客套。",
                "max_chars": 240,
                "max_tokens": 200,
                "timeout_sec": 30,
            }
            changed = True
        else:
            vision = self.data.get("vision")
            if isinstance(vision, dict) and "thinking" not in vision:
                vision["thinking"] = ""
                changed = True
        return changed

    @property
    def model(self) -> str:
        return self.llm.get("model", "")

    @property
    def request(self) -> dict:
        """对话请求参数（temperature/top_p/max_tokens/stop/stream 等，按服务商支持取舍）。"""
        return self.llm.get("request", {})

    @property
    def embedding(self) -> dict:
        """向量化配置（仅读 config.json，项目靠配置文件驱动，无环境变量覆盖）。

        api_key / base_url / model 任一缺失或 enabled=false 时视为未启用，
        记忆改用关键词检索，不影响启动。
        """
        emb = self.data.get("embedding", {}) or {}
        base_url = str(emb.get("base_url", "") or "").strip()
        model = str(emb.get("model", "") or "").strip()
        api_key = str(emb.get("api_key", "") or "").strip()
        return {
            "provider": str(emb.get("provider", "") or "").strip(),
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "batch_size": max(1, int(emb.get("batch_size", 32))),
            "enabled": bool(emb.get("enabled", True)) and bool(base_url and model and api_key),
        }

    @property
    def vision(self) -> dict:
        """识图配置：base_url / api_key / model 留空时沿用 llm 段（同一厂商直接可用）。"""
        v = self.data.get("vision", {}) or {}
        llm = self.llm or {}

        def pick(key: str) -> str:
            return str(v.get(key) or llm.get(key) or "").strip()

        def integer(key: str, default: int) -> int:
            try:
                value = int(v.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        base_url, api_key, model = pick("base_url"), pick("api_key"), pick("model")
        thinking = str(v.get("thinking") or "").strip()
        same_endpoint = base_url.rstrip("/") == str(llm.get("base_url") or "").rstrip("/")
        if not thinking and same_endpoint:
            # 同一服务商时沿用对话的 thinking 设置；识图默认不能把 token 全耗在思考上
            thinking = str((llm.get("request") or {}).get("thinking") or "").strip()
        return {
            "enabled": bool(v.get("enabled", True)) and bool(base_url and api_key and model),
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "thinking": thinking,
            "prompt": str(v.get("prompt") or "简单描述一下图片内容：主体、场景和画面里的关键文字，一两句话，不要客套。"),
            "max_chars": integer("max_chars", 240),
            "max_tokens": integer("max_tokens", 200),
            "timeout_sec": integer("timeout_sec", 30),
        }

    @property
    def context(self) -> dict:
        """上下文预算：max_history_messages / summary_threshold_tokens。"""
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

    def media_dir(self, kind: str) -> Path:
        """媒体子目录（收到的表情包/语音/其他附件分目录存放，不平铺）。

        kind 取值：emoji / voice / video / other。目录名可由
        storage.media_dirs.<kind> 覆盖，缺省与 kind 同名。
        """
        m = self.storage_cfg.get("media_dirs", {}) or {}
        sub = m.get(kind, kind) or kind
        d = self.temp_dir / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        d = self.storage_cfg.get("db_path", "db/ggbot.db")
        p = Path(d)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tool_dir(self) -> Path:
        """Tool 自包含目录（项目根 tool/）：代码、config.json 与持久化数据同目录。"""
        d = BASE_DIR / "tool"
        d.mkdir(parents=True, exist_ok=True)
        return d

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
