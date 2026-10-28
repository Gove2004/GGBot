"""AI 请求装置：DeepSeek Chat Completions（openai SDK）。

- base_url / model / api_key 取自 config.json
- request 子对象仅保留 DeepSeek 官方支持的参数（thinking 走 extra_body）
- 内置重试（网络抖动退避）
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("ggbot.llm")


class LLMError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = cfg.model
        from openai import OpenAI

        self.client = OpenAI(
            api_key=cfg.llm.get("api_key"),
            base_url=cfg.llm.get("base_url", "https://api.deepseek.com"),
            timeout=120,
            max_retries=0,  # 重试由本层控制，便于日志
        )
        self.request = cfg.request

    def _kwargs(self, messages: list, tools=None, tool_choice=None) -> dict:
        kwargs: dict = {"model": self.model, "messages": messages, "stream": False}
        for key in ("temperature", "top_p", "max_tokens", "stop"):
            val = self.request.get(key)
            if val is not None:
                kwargs[key] = val
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        # thinking：OpenAI 兼容层通过 extra_body 传递（enabled / disabled）
        thinking = self.request.get("thinking")
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": thinking}}
        return kwargs

    def chat(self, messages: list, tools=None, tool_choice=None, retries: int = 3):
        """单次补全调用。返回 openai response 对象。"""
        kwargs = self._kwargs(messages, tools, tool_choice)
        logger.debug("LLM 请求 tokens ≈ %d", self.estimate_tokens(str(messages)))
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                usage = getattr(resp, "usage", None)
                done = getattr(usage, "completion_tokens", None) if usage else None
                if done is not None:
                    logger.debug("LLM 响应 tokens ≈ %d", int(done))
                return resp
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("LLM 调用失败（第 %d 次）: %s", attempt + 1, e)
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"LLM 调用重试耗尽: {last_err}")

    def estimate_tokens(self, text: str) -> int:
        """粗略 token 估算：中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。"""
        if not text:
            return 0
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        return int(cjk * 1.0 + other / 4.0) + 8

    def summarize(self, text: str, max_chars: int = 600) -> str:
        """用 LLM 压缩一段文本（用于上下文窗口超限时的历史摘要）。"""
        prompt = (
            "请将以下群聊记录压缩成一份简洁的中文摘要，保留：讨论主题、关键观点、人物关系、"
            "做出的决定、需要记住的细节。不要遗漏被@或明确提问的内容。\n\n" + text
        )
        resp = self.chat(
            [
                {"role": "system", "content": "你是群聊纪要整理助手，输出精炼、结构化。"},
                {"role": "user", "content": prompt},
            ]
        )
        return (resp.choices[0].message.content or "").strip()[:max_chars]
