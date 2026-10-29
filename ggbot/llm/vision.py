"""图片理解：把收到的图片交给支持视觉的 OpenAI 兼容模型转成一句文字描述。

配置在 config.json 的 vision 段；base_url / api_key / model 留空时沿用 llm 段，
同一家厂商直接可用。识别失败不影响消息本身，正文保留原来的 [图片 文件名] 标记。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
from pathlib import Path

from ggbot.core.runtime_stats import runtime
from ggbot.llm.llm import cost_value, format_cost, session_extra

logger = logging.getLogger("ggbot.vision")

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class VisionClient:
    """识图客户端。

    模型与提示词参数每次现读 cfg.vision，面板改完即生效；
    连接参数（api_key / base_url / timeout_sec）在建连时固定，改了要重启。
    """

    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self._client = client
        if cfg.vision["enabled"] and client is None:
            self._client = self._connect()
        elif not cfg.vision["enabled"]:
            logger.debug("识图未启用：vision 配置不完整或 enabled=false")

    def _connect(self):
        from openai import OpenAI

        v = self.cfg.vision
        return OpenAI(
            api_key=v["api_key"],
            base_url=v["base_url"],
            timeout=v["timeout_sec"],
            max_retries=0,
        )

    @property
    def client(self):
        return self._client

    @property
    def enabled(self) -> bool:
        """现读：启动时关着、面板刚打开的，这里按当前配置补建连接。"""
        if not self.cfg.vision["enabled"]:
            return False
        if self._client is None:
            try:
                self._client = self._connect()
            except Exception as e:  # noqa: BLE001
                logger.warning("识图连接失败：vision 配置不完整（%s）", e)
                return False
        return True

    @property
    def model(self) -> str:
        return self.cfg.vision["model"]

    @property
    def thinking(self) -> str:
        return self.cfg.vision.get("thinking") or ""

    @property
    def prompt(self) -> str:
        return self.cfg.vision["prompt"]

    @property
    def max_chars(self) -> int:
        return self.cfg.vision["max_chars"]

    @property
    def max_tokens(self) -> int:
        return self.cfg.vision["max_tokens"]

    @property
    def pricing(self) -> dict:
        return self.cfg.llm.get("pricing") or {}

    def describe(self, path: Path | str, session: str | None = None) -> str | None:
        """同步调用（由调用方放进线程池）；返回纯文字描述，失败返回 None。"""
        if not self.enabled or self.client is None:
            return None
        p = Path(path)
        extra = session_extra(session)
        try:
            if not p.is_file() or p.stat().st_size > MAX_IMAGE_BYTES:
                logger.warning("识图跳过 %s：文件缺失或超过 %dMB",
                               p.name, MAX_IMAGE_BYTES // 1048576, extra=extra)
                return None
            data = p.read_bytes()
        except OSError as e:
            logger.warning("识图读取失败 %s: %s", p.name, e, extra=extra)
            return None
        mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": self.prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }]
        last_err: Exception | None = None
        kwargs = {"model": self.model, "messages": messages,
                  "max_tokens": self.max_tokens, "stream": False}
        if self.thinking:
            kwargs["extra_body"] = {"thinking": {"type": self.thinking}}
        for attempt in range(2):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                text = self._clean((resp.choices[0].message.content or ""))
                self._record(resp, session)
                return text or None
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt == 0:
                    logger.warning("识图失败，重试一次 %s: %s", p.name, e, extra=extra)
        logger.warning("识图失败 %s: %s", p.name, last_err, extra=extra)
        return None

    def _clean(self, text: str) -> str:
        text = re.sub(r"\s+", " ", (text or "").strip()).strip("\"'“”")
        return text[: self.max_chars]

    def _record(self, resp, session: str | None) -> None:
        """识图的 tokens/费用单独计一行；描述正文由「图片已识别」行输出，避免重复。"""
        usage = getattr(resp, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None) if usage is not None else None
        done = getattr(usage, "completion_tokens", None) if usage is not None else None
        cached = None
        details = getattr(usage, "prompt_tokens_details", None) if usage is not None else None
        if details is not None:
            cached = getattr(details, "cached_tokens", None)
        if cached is None and usage is not None:
            cached = getattr(usage, "prompt_cache_hit_tokens", None)
        fee = cost_value(self.pricing, prompt, cached, int(done or 0)) if done is not None else None
        runtime.llm(prompt, done, cached, fee, str(self.pricing.get("currency") or "¥"),
                    vision=True)
        cost = format_cost(self.pricing, fee)
        hit = (f"{cached}/{prompt} ({int(cached) * 100 // int(prompt)}%)"
               if cached is not None and prompt else "-")
        logger.debug("识图 %s 请求 %s | 输出 %s | 命中 %s | 费用 %s",
                     self.model,
                     prompt if prompt is not None else "-",
                     done if done is not None else "-",
                     hit, cost or "-",
                     extra={"session": session} if session else {})
