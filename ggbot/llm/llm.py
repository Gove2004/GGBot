"""AI 请求装置：OpenAI 兼容的 Chat Completions（openai SDK）。

- base_url / model / api_key 全部取自 config.json 的 llm 段，不绑定具体厂商
- request 子对象按服务商支持取舍（thinking 属于 DeepSeek 扩展，配置了才会透传）
- 内置重试（网络抖动退避）
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

from ggbot.core.runtime_stats import runtime

logger = logging.getLogger("ggbot.llm")


class LLMError(RuntimeError):
    pass


def session_extra(session: str | None) -> dict:
    """日志「会话」列：调用方传入 8 位标签时归到该会话，否则沿用默认（系统）。"""
    return {"session": session} if session else {}


def price_factor(pricing: dict) -> float:
    """高峰时段返回 1，空闲时段返回 off_peak_multiplier；未配置则恒为 1。"""
    spans = pricing.get("peak_hours")
    try:
        off_peak = float(pricing.get("off_peak_multiplier"))
    except (TypeError, ValueError):
        return 1.0
    if not spans:
        return 1.0
    now = datetime.now()
    if pricing.get("peak_weekdays_only", True) and now.weekday() >= 5:
        return off_peak
    hhmm = now.strftime("%H:%M")
    for span in spans:
        start, _, end = str(span).partition("-")
        if start and end and start.strip() <= hhmm < end.strip():
            return 1.0
    return off_peak


def cost_value(pricing: dict, prompt: int | None, cached: int | None, done: int) -> float | None:
    """按每百万 tokens 单价计算费用数值；未配置单价时返回 None。"""
    if not isinstance(pricing, dict):
        return None

    def num(key):
        val = pricing.get(key)
        if val in (None, ""):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    inp, out = num("input_per_mtok"), num("output_per_mtok")
    cached_price = num("cached_input_per_mtok")
    if cached_price is None:
        cached_price = inp
    if not any((inp, out, cached_price)):
        return None
    inp, out = inp or 0.0, out or 0.0
    cached_price = cached_price or 0.0
    factor = price_factor(pricing)
    inp, out, cached_price = inp * factor, out * factor, cached_price * factor
    prompt = prompt or 0
    cached = min(cached or 0, prompt)
    return ((prompt - cached) * inp + cached * cached_price + done * out) / 1_000_000


def format_cost(pricing: dict, fee: float | None) -> str | None:
    if fee is None:
        return None
    currency = str((pricing or {}).get("currency") or "¥")
    return f"{currency}{fee:.6f}".rstrip("0").rstrip(".")


class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = cfg.model
        api_key = str(cfg.llm.get("api_key") or "").strip()
        base_url = str(cfg.llm.get("base_url") or "").strip()
        if not (api_key and base_url and self.model):
            raise RuntimeError(
                "llm 配置不完整：请在 db/config.json 填写 llm.api_key / llm.base_url / llm.model"
            )
        from openai import OpenAI

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
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

    def chat(self, messages: list, tools=None, tool_choice=None, retries: int = 3,
             base_len: int | None = None, session: str | None = None):
        """单次补全调用。返回 openai response 对象。

        base_len：messages 中属于「历史」的前缀长度，其余视为「新增」
        （本轮消息与工具往返），用于请求行的三段拆解；
        session 是 8 位会话标签，让 tokens/费用行归到同一会话。
        """
        kwargs = self._kwargs(messages, tools, tool_choice)
        parts = self._request_parts(messages, tools, base_len)
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                self._log_usage(getattr(resp, "usage", None), parts, session)
                return resp
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("LLM 调用失败（第 %d 次）: %s", attempt + 1, e,
                               extra=session_extra(session))
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"LLM 调用重试耗尽: {last_err}")

    def _request_parts(self, messages: list, tools, base_len: int | None) -> tuple[int, int, int]:
        """把一次请求拆成 工具 schema / 上下文 / 新增 三部分（估算口径）。"""
        cut = (len(messages) - 1) if base_len is None else int(base_len)
        cut = max(0, min(cut, len(messages)))
        tool_tokens = 0
        if tools:
            try:
                tool_tokens = self.estimate_tokens(json.dumps(tools, ensure_ascii=False))
            except (TypeError, ValueError):
                tool_tokens = 0
        ctx_tokens = self.estimate_messages_tokens(messages[:cut])
        new_tokens = self.estimate_messages_tokens(messages[cut:])
        return tool_tokens, ctx_tokens, new_tokens

    def _log_usage(self, usage, parts: tuple[int, int, int] = (0, 0, 0),
                   session: str | None = None) -> None:
        """请求量以服务商 usage 为准，三段按估算比例缩放、和等于实际。

        口径：请求行 = 服务商输入 tokens + 工具/历史/新增，响应行 = 输出 tokens + 缓存命中 + 费用。
        """
        extra = session_extra(session)
        if usage is None:
            runtime.llm(None, None, None, None, self._currency())
            self._log_request_estimate(*parts, session=session)
            return
        prompt = self._usage_value(usage, "prompt_tokens", "input_tokens")
        done = self._usage_value(usage, "completion_tokens", "output_tokens")
        if done is None:
            runtime.llm(prompt, None, None, None, self._currency())
            return
        done = int(done)
        cached = self._cached_tokens(usage)
        cost_value = self._cost_value(prompt, cached, done)
        runtime.llm(int(prompt) if prompt is not None else None, done, cached,
                    cost_value, self._currency())
        if prompt is None:
            self._log_request_estimate(*parts, session=session)
        else:
            prompt = int(prompt)
            tool_t, ctx_t, new_t = self._scale_parts(parts, prompt)
            logger.debug("LLM 请求 %d tokens (工具 %d | 历史 %d | 新增 %d)",
                         prompt, tool_t, ctx_t, new_t, extra=extra)
        hit = f"{cached}/{prompt} ({cached * 100 // prompt}%)" if cached is not None and prompt else "-"
        cost = self._format_cost(cost_value)
        bits = [f"命中 {hit}", f"费用 {cost or '-'}"]
        logger.debug("LLM 响应 %d tokens (%s)", done, " | ".join(bits), extra=extra)

    def _log_request_estimate(self, tool_tokens: int, ctx_tokens: int, new_tokens: int,
                              session: str | None = None) -> None:
        """服务商未返回 usage 时的估算行，总量明确标注 ≈。"""
        logger.debug("LLM 请求 ≈%d tokens (工具 %d | 历史 %d | 新增 %d)",
                     tool_tokens + ctx_tokens + new_tokens, tool_tokens, ctx_tokens, new_tokens,
                     extra=session_extra(session))

    @staticmethod
    def _scale_parts(parts: tuple[int, int, int], total: int) -> tuple[int, int, int]:
        """把本地估算的三段缩放到服务商实际总数上，保证拆开看时和是对得上的。"""
        tool_t, ctx_t, new_t = max(0, parts[0]), max(0, parts[1]), max(0, parts[2])
        estimate = tool_t + ctx_t + new_t
        if total <= 0 or estimate <= 0:
            return tool_t, ctx_t, new_t
        scale = total / estimate
        out_tool = int(round(tool_t * scale))
        out_ctx = int(round(ctx_t * scale))
        out_new = total - out_tool - out_ctx
        if out_new < 0:
            out_ctx = max(0, out_ctx + out_new)
            out_new = 0
        return out_tool, out_ctx, out_new

    @staticmethod
    def _usage_value(obj, *names):
        for name in names:
            val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if val is not None:
                return val
        return None

    def _cached_tokens(self, usage) -> int | None:
        details = self._usage_value(usage, "prompt_tokens_details")
        if details is not None:
            val = self._usage_value(details, "cached_tokens")
            if val is not None:
                return max(0, int(val))
        # DeepSeek 等厂商直接返回 prompt_cache_hit_tokens
        val = self._usage_value(usage, "prompt_cache_hit_tokens", "cached_tokens")
        return max(0, int(val)) if val is not None else None

    def _cost(self, prompt: int | None, cached: int | None, done: int) -> str | None:
        """按 llm.pricing 的每百万 tokens 单价估算费用；未配置单价时返回 None。"""
        return self._format_cost(self._cost_value(prompt, cached, done))

    def _currency(self) -> str:
        pricing = self.cfg.llm.get("pricing") or {}
        if not isinstance(pricing, dict):
            return "¥"
        return str(pricing.get("currency") or "¥")

    def _format_cost(self, fee: float | None) -> str | None:
        pricing = self.cfg.llm.get("pricing") or {}
        if not isinstance(pricing, dict):
            pricing = {}
        return format_cost(pricing, fee)

    def _cost_value(self, prompt: int | None, cached: int | None, done: int) -> float | None:
        """费用数值（用于运行统计）；未配置单价时返回 None。"""
        return cost_value(self.cfg.llm.get("pricing") or {}, prompt, cached, done)

    @staticmethod
    def _price_factor(pricing: dict) -> float:
        return price_factor(pricing)


    def estimate_tokens(self, text: str) -> int:
        """粗略 token 估算：中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。"""
        if not text:
            return 0
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        return int(cjk * 1.0 + other / 4.0) + 8

    def estimate_messages_tokens(self, messages: list) -> int:
        """按消息正文估算请求 tokens；上下文预算与请求日志共用同一口径。"""
        return sum(self.estimate_tokens(m.get("content") or "") for m in messages)

    def summarize(self, text: str, max_chars: int = 600, session: str | None = None) -> str:
        """用 LLM 压缩一段文本（用于上下文窗口超限时的历史摘要）。"""
        prompt = (
            "请将以下群聊记录压缩成一份简洁的中文摘要，保留：讨论主题、关键观点、人物关系、"
            "做出的决定、需要记住的细节。不要遗漏被@或明确提问的内容。\n\n" + text
        )
        resp = self.chat(
            [
                {"role": "system", "content": "你是群聊纪要整理助手，输出精炼、结构化。"},
                {"role": "user", "content": prompt},
            ],
            session=session,
        )
        return (resp.choices[0].message.content or "").strip()[:max_chars]

    def extract_memories(self, text: str, max_items: int = 6,
                         session: str | None = None) -> list[str]:
        """从一段历史对话中提炼值得长期记住的事实，用于窗口溢出时自动归档。"""
        prompt = (
            "下面是聊天记录。请提取值得长期记住的信息：人物的称呼、身份、喜好、脾气、近况、"
            "约定、重要日子、正在做的事。\n"
            "要求：每条一句完整的中文，自带主语，脱离上下文也能看懂；不记寒暄、口头禅、"
            "玩笑和重复内容；不要猜测或编造。\n"
            f"只输出 JSON 字符串数组，最多 {max_items} 条；没有值得记的就输出 []。\n\n" + text
        )
        resp = self.chat(
            [
                {"role": "system", "content": "你负责从聊天记录里沉淀长期记忆，只输出 JSON 数组。"},
                {"role": "user", "content": prompt},
            ],
            session=session,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.M).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("["), raw.rfind("]")
            if start < 0 or end <= start:
                return []
            try:
                data = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return []
        if isinstance(data, dict):
            data = data.get("memories") or data.get("items") or []
        if not isinstance(data, list):
            return []
        facts = [str(x).strip() for x in data if str(x).strip()]
        return facts[:max_items]
