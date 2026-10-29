"""Tool 插件基座：一切能力都是 Tool。

- Tool：一个可被 LLM 调用的能力（schema + 执行逻辑 + 自己的配置/数据目录）
- ToolRuntime：给 Tool 提供配置、持久化目录、发送能力等公共依赖
- ToolRegistry：自动发现 tool/<分类>/ 下的插件，按配置开关动态挂载 schema

分类即能力域：chat（对话与表达）/ knowledge（信息）/ fs（文件）/ mail（邮件）/ admin（管理）。
新增分类 = 新建一个含插件模块的子包。

每个 Tool 声明 min_level（调用门槛，0-5 为发送者等级，6 为永久锁）；注册表按等级裁剪
下发给模型的 schema，执行器再按实际发送者校验一次——前者省 token，后者才是安全边界。

tool/ 位于项目根目录且**自包含**：每个 toolset 目录同时存放代码、config.json
与持久化数据（索引、素材、参考音频），整目录复制即可迁移；运行产物在 temp/tool/<name>/。
含密钥的 config.json 由 .gitignore 排除，绝不入版本库。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import pkgutil
import re
import threading
from pathlib import Path

from ggbot.util.logging_setup import clip
from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools")

# 模型偶尔把媒体写成方括号占位符进正文；这些不是媒体，发送前拦截。
# 词表要覆盖模型自己编出来的叫法（[语音克隆] / [表情包] …）：漏一个就等于把方括号发给用户，
# 而且这条脏文本会入库，被后续历史喂回模型，越用越像"这里的规矩就是这样"
_PLACEHOLDER_WORDS = (
    r"表情|表情包|语音|语音克隆|语音条|克隆语音|图片|视频|文件|邮件"
    r"|emoji|emoticon|sticker|voice|audio|image|photo|video|file|mail"
)
# 入站富媒体在上下文里就长成 `[图片]（描述）` / `[语音 6ce72460]` 这样，模型照抄的是整套形状：
# 括号里的 8 位短标识与紧跟的（描述）一起吞掉，否则余下一截孤立括号更像"这里允许写"
_MEDIA_TOKEN = (
    rf"[\[【(（]\s*(?:{_PLACEHOLDER_WORDS})(?:\s+[0-9a-fA-F]{{6,8}})?\s*[\]】)）]"
    r"(?:\s*[（(][^）)]{0,120}[）)])?"
)
PLACEHOLDER_RE = re.compile(
    rf"^(?:\s*{_MEDIA_TOKEN}\s*[。.!！?？~～…]*\s*)+$",
    re.IGNORECASE,
)
# 同一类占位符夹在正常文字里（“哈哈[表情]”）时直接剔除，避免用户看到方括号
PLACEHOLDER_INLINE_RE = re.compile(_MEDIA_TOKEN, re.IGNORECASE)
# 正文开头的引用标记：[引用 关键词] / 【引用】；允许前面先写 @昵称（head 保留，不参与正文剥离）
QUOTE_RE = re.compile(
    r"^\s*(?P<head>(?:@[\w\u4e00-\u9fff·._-]{1,32}\s*)*)"
    r"[\[【]\s*引用(?:\s*[:：]?\s*([^\]】\n]*?))?\s*[\]】]\s*"
)
# @昵称：允许中文、字母数字、下划线与常见连接符，避免把邮箱/代码里的 @ 当成人
AT_NAME_RE = re.compile(r"(?<![\w@])@([\w\u4e00-\u9fff·._-]{1,32})")


def to_str(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


class ToolRuntime:
    """Tool 的公共运行时：配置读写 + 数据目录 + 回复发送。"""

    def __init__(self, cfg, storage, memory, bot):
        self.cfg = cfg
        self.storage = storage
        self.memory = memory
        self.bot = bot
        self._lock = threading.RLock()
        self._config_cache: dict[str, tuple[int, dict]] = {}

    # ---------- 目录 ----------

    @property
    def tool_root(self) -> Path:
        """Tool 自包含根目录（项目根 tool/），测试可注入 cfg.tool_dir。"""
        root = getattr(self.cfg, "tool_dir", None)
        return Path(root) if root else Path(__file__).resolve().parent

    def data_dir(self, name: str) -> Path:
        """Tool 持久化目录：tool/<name>/（与代码同目录：配置、索引、参考音频、素材）。"""
        d = self.tool_root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def temp_dir(self, name: str) -> Path:
        """Tool 运行目录：temp/tool/<name>/（生成/下载的临时产物）。"""
        d = Path(self.cfg.temp_dir) / "tool" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def config_path(self, name: str) -> Path:
        """配置文件路径（不建目录；无配置的工具不应留下空目录）。"""
        return self.tool_root / name / "config.json"

    # ---------- 配置 ----------

    def load_config(self, name: str, defaults: dict | None = None) -> dict:
        """读取 tool/<name>/config.json；缺失时按 defaults 创建并补齐新字段。

        无默认配置的工具只在文件已存在时读取（首次 toggle 才落盘），
        避免启动时给每个工具都写一份空配置。
        """
        defaults = dict(defaults or {})
        path = self.config_path(name)
        with self._lock:
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                mtime = -1
            cached = self._config_cache.get(name)
            if cached and cached[0] == mtime:
                return cached[1]
            data: dict = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    logger.warning("工具 %s 配置解析失败，使用默认值: %s", name, e)
                    data = {}
            merged = {**defaults, **data}
            if defaults and (not path.exists() or set(defaults) - set(data)):
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except OSError as e:
                    logger.warning("工具 %s 配置写入失败: %s", name, e)
            self._config_cache[name] = (path.stat().st_mtime_ns if path.exists() else -1, merged)
            return merged

    def save_config(self, name: str, data: dict) -> Path:
        path = self.config_path(name)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            self._config_cache.pop(name, None)
        return path

    def set_enabled(self, name: str, defaults: dict, enabled: bool) -> dict:
        data = self.load_config(name, defaults)
        data["enabled"] = bool(enabled)
        self.save_config(name, data)
        return data

    # ---------- 发送 ----------

    async def send_text(self, ctx: dict, content) -> dict:
        content = to_str(content).strip()
        sid = ctx.get("session_id")
        extra = {"session": session_label(sid)}
        content, reference = self._parse_quote(content, ctx)
        if not content:
            return {"ok": False, "error": "content 为空"}
        if PLACEHOLDER_RE.match(content):
            logger.warning("正文只有占位符，已拦截不发送: %s", clip(content, 40), extra=extra)
            return {
                "ok": False,
                "error": "不要用 [表情]/[语音]/[图片] 占位符；发表情调 send_sticker，"
                         "发语音调 send_voice_clone，发图/视频调 send_image/send_video",
            }
        cleaned = PLACEHOLDER_INLINE_RE.sub("", content).strip()
        if cleaned != content:
            # 只报被剔除的占位符，不复读整段正文（工具调用行里已经有了）
            removed = " ".join(dict.fromkeys(PLACEHOLDER_INLINE_RE.findall(content)))
            logger.warning("正文夹带占位符已剔除: %s（余文照常发送）", removed, extra=extra)
            content = cleaned
            if not content:
                return {"ok": False, "error": "content 只剩占位符，请改用媒体工具发送"}
        # 入库与回传给模型的都用这份"人看的"文本；发给 QQ 的 wire 只在群聊里把
        # @昵称 换成官方 at 标记（标记只在 markdown 消息生效，见 QQBot.send_text）
        display = content
        is_group = ctx.get("scene") == "group"
        # 模型偶尔用换行/空行把多句拼进一条，这里拆成多条依次发送，
        # 避免出现"A 空行 B"式的整段回复
        segments = [s.strip() for s in re.split(r"\n+", display) if s.strip()] or [display]
        reply_cfg = getattr(self.cfg, "reply", {}) or {}
        gap = max(0.0, float(reply_cfg.get("segment_gap_sec", 2) or 0))
        for i, seg in enumerate(segments):
            wire = self._apply_mentions(seg, ctx) if is_group else seg
            seq = ctx.get("msg_seq", 1)
            kwargs = {"message_reference": reference} if reference else {}
            try:
                await self.bot.send_text(
                    ctx["target"], wire, ctx["scene"], ctx.get("msg_id"), seq,
                    ctx.get("event_id"), display=seg, **kwargs
                )
            except Exception as e:  # noqa: BLE001
                if not ctx.get("msg_id"):
                    raise
                # 被动回复失败（msg_id 过期等）→ 降级主动发送，确保送达；
                # 主动通道不带引用，避免已失效的消息 ID 再触发一次失败
                logger.warning("被动回复失败，降级主动发送: %s", e, extra=extra)
                await self.bot.send_text(ctx["target"], wire, ctx["scene"], None, seq,
                                         display=seg)
            ctx["msg_seq"] = seq + 1
            ctx["sent_any"] = True
            if sid:
                self.storage.add_message(sid, "assistant", "bot", seg, status="sent")
            if gap and i < len(segments) - 1:
                await asyncio.sleep(gap)
        # 不再单打"发送文本 N 条 / M 字"：工具调用行已经记过一次正文，
        # 送达与否看有没有异常/降级行即可（同一信息只打一行）
        return {"ok": True, "sent": display}

    def _parse_quote(self, content: str, ctx: dict) -> tuple[str, dict | None]:
        """解析开头的 [引用 关键词]：回查本会话消息并生成 QQ message_reference。"""
        m = QUOTE_RE.match(content)
        if not m:
            return content, None
        head = (m.group("head") or "").strip()
        needle = (m.group(2) or "").strip()
        sid = ctx.get("session_id")
        target = None
        if needle and self.storage is not None and sid:
            target = self.storage.find_message_id(sid, needle)
        if not target:
            target = ctx.get("msg_id")
        body = content[m.end():].strip()
        ref = {"message_id": target, "ignore_get_message_error": True} if target else None
        return " ".join(p for p in (head, body) if p), ref

    def _apply_mentions(self, content: str, ctx: dict) -> str:
        """把 @昵称 换成 QQ 的 <qqbot-at-user id="member_openid" />；查不到的人保持原样。

        官方只在 markdown 消息里解析该标记，QQBot.send_text 负责切换通道，
        被拒时退回显示文本；入库与日志用的始终是 @昵称 的可读原文。
        """
        if self.storage is None:
            return content
        sid = ctx.get("session_id")

        def repl(m: re.Match) -> str:
            openid = self.storage.find_sender_openid(m.group(1), sid)
            return f'<qqbot-at-user id="{openid}" />' if openid else m.group(0)

        return AT_NAME_RE.sub(repl, content)

    async def send_media(self, ctx: dict, file_type: int, source: str, label: str) -> dict:
        seq = ctx.get("msg_seq", 1)
        ok, detail = await self.bot.upload_and_send_media(
            ctx["target"], ctx["scene"], file_type, source, ctx.get("msg_id"), seq
        )
        if ok:
            ctx["msg_seq"] = seq + 1
            ctx["sent_any"] = True
            sid = ctx.get("session_id")
            if sid:
                self.storage.add_message(sid, "assistant", "bot", f"[{label}]", status="sent")
            logger.info("发送%s: %s", label, detail,
                        extra={"session": session_label(ctx.get("session_id"))})
            return {"ok": True, "sent": f"{label}已发送: {detail}"}
        logger.warning("发送%s失败: %s", label, detail,
                       extra={"session": session_label(ctx.get("session_id"))})
        return {"ok": False, "error": detail}


class Tool:
    """一个 Tool = 一份 schema + 一个执行器；配置/数据目录由 runtime 提供。"""

    name: str = ""
    category: str = ""  # 所属能力域（由注册表按所在子包填充：chat/knowledge/fs/mail/admin）
    data_name: str = ""  # 数据/配置目录名，默认与工具名一致（send_email -> mail 时可覆盖）
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}
    default_config: dict = {}
    enabled_by_default: bool = True
    prompt_hint: str = ""
    config_owner: bool = True   # 共享配置目录时，只由一个工具展示/承担配置
    toggleable: bool = True     # 共享配置的工具不单独启停
    min_level: int = 1          # 调用门槛（0-5 为发送者等级，6 = 永久锁）；可在 config.levels 覆盖

    def __init__(self, rt: ToolRuntime):
        self.rt = rt

    @property
    def key(self) -> str:
        return self.data_name or self.name

    @property
    def config(self) -> dict:
        return self.rt.load_config(self.key, self.default_config)

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", self.enabled_by_default))

    def required_level(self) -> int:
        """实际门槛：config.levels.<tool> 覆盖 > 类默认；范围 0-6。"""
        data = self.config or {}
        levels = data.get("levels")
        raw = levels.get(self.name) if isinstance(levels, dict) else None
        if raw is None:
            raw = self.min_level
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            raw = self.min_level
        return max(0, min(6, raw))

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def run(self, args: dict, ctx: dict) -> dict:  # pragma: no cover - 抽象
        raise NotImplementedError


class ToolRegistry:
    """自动发现并管理 Tool 插件。"""

    def __init__(self, rt: ToolRuntime):
        self.rt = rt
        self.tools: dict[str, Tool] = {}
        self._discover()

    def _discover(self) -> None:
        """扫描 tool/<分类>/ 下的插件模块（每个模块导出 get_tools(rt)）。"""
        root = importlib.import_module("tool")
        for info in pkgutil.iter_modules(root.__path__):
            if info.name.startswith("_") or info.name == "base" or not info.ispkg:
                continue
            try:
                pkg = importlib.import_module(f"tool.{info.name}")
            except Exception as e:  # noqa: BLE001
                logger.warning("工具分类 %s 加载失败: %s", info.name, e, exc_info=True)
                continue
            for mod in pkgutil.iter_modules(pkg.__path__):
                if mod.name.startswith("_"):
                    continue
                if mod.ispkg and not (Path(pkg.__path__[0]) / mod.name / "__init__.py").is_file():
                    continue  # 素材/数据目录不是插件包（如 chat/emoji、chat/audio）
                self._load_module(f"tool.{info.name}.{mod.name}", info.name)

    def _load_module(self, dotted: str, category: str) -> None:
        try:
            module = importlib.import_module(dotted)
            factory = getattr(module, "get_tools", None)
            if factory is None:
                return
            for tool in factory(self.rt):
                if not tool.category:
                    tool.category = category
                self.register(tool)
        except Exception as e:  # noqa: BLE001
            logger.warning("工具插件 %s 加载失败: %s", dotted, e, exc_info=True)

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool 缺少 name")
        if tool.name in self.tools:
            raise ValueError(f"重复的工具名: {tool.name}")
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        tool = self.tools.get(name)
        if tool is None or not tool.is_enabled():
            return None
        return tool

    def visible(self, level: int) -> list[tuple[Tool, int]]:
        """某等级可见的工具（启用且门槛达标），按 (门槛, 分类, 名称) 稳定排序。

        排序固定是为了让不同等级的工具列表共享前缀：低等级用户的列表是高等级的前缀，
        厂商的前缀缓存命中率更高。6 级工具永远不会出现在任何人的列表里。
        """
        level = max(0, min(6, int(level)))
        rows = [
            (t, t.required_level())
            for t in self.tools.values()
            if t.is_enabled() and level >= t.required_level()
        ]
        rows.sort(key=lambda x: (x[1], x[0].category, x[0].name))
        return rows

    def schemas(self, level: int) -> list[dict]:
        return [t.schema() for t, _ in self.visible(level)]

    def hints(self, level: int) -> str:
        lines = [f"- {t.name}：{t.prompt_hint}" for t, _ in self.visible(level) if t.prompt_hint]
        return "\n".join(lines) or "（无）"

    def list_info(self) -> list[dict]:
        out = []
        for t in self.tools.values():
            out.append({
                "name": t.name,
                "category": t.category,
                "description": t.description,
                "enabled": t.is_enabled(),
                "toggleable": t.toggleable,
                "min_level": t.required_level(),
                "level_default": int(t.min_level),
                "config": t.config if t.config_owner else {},
            })
        return out

    def toggle(self, name: str, enabled: bool) -> dict | None:
        tool = self.tools.get(name)
        if tool is None or not tool.toggleable:
            return None
        return self.rt.set_enabled(tool.key, tool.default_config, enabled)

    def set_level(self, name: str, level: int) -> dict | None:
        """设置工具门槛（0-6）；与类默认相同时删除覆盖，配置保持干净。"""
        tool = self.tools.get(name)
        if tool is None:
            return None
        level = max(0, min(6, int(level)))
        data = dict(self.rt.load_config(tool.key, tool.default_config))
        raw_levels = data.get("levels")
        levels = dict(raw_levels) if isinstance(raw_levels, dict) else {}
        if level == int(tool.min_level):
            levels.pop(name, None)
        else:
            levels[name] = level
        if levels:
            data["levels"] = levels
        else:
            data.pop("levels", None)
        self.rt.save_config(tool.key, data)
        return data
