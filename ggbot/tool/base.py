"""Tool 插件基座：一切能力都是 Tool。

- Tool：一个可被 LLM 调用的能力（schema + 执行逻辑 + 自己的配置/数据目录）
- ToolRuntime：给 Tool 提供配置、持久化目录、发送能力等公共依赖
- ToolRegistry：自动发现 ggbot/tool/<分类>/ 下的插件，按配置开关动态挂载 schema

分类即能力域：core（对话）/ memory（记忆）/ emoji（表情）/ audio（语音）/
media（图文视频）/ mail（邮件）。新增分类 = 新建一个含插件模块的子包。

每个 Tool 的持久化数据在 db/tool/<name>/，运行产物在 temp/tool/<name>/。
含密钥的 config.json 由 .gitignore 排除，绝不入版本库。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import pkgutil
import threading
from pathlib import Path

from ggbot.util.human import typing_delay_sec
from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools")


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

    def data_dir(self, name: str) -> Path:
        """Tool 持久化目录：db/tool/<name>/（配置、索引、参考音频、素材）。"""
        d = Path(self.cfg.db_path).parent / "tool" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def temp_dir(self, name: str) -> Path:
        """Tool 运行目录：temp/tool/<name>/（生成/下载的临时产物）。"""
        d = Path(self.cfg.temp_dir) / "tool" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def config_path(self, name: str) -> Path:
        """配置文件路径（不建目录；无配置的工具不应留下空目录）。"""
        return Path(self.cfg.db_path).parent / "tool" / name / "config.json"

    # ---------- 配置 ----------

    def load_config(self, name: str, defaults: dict | None = None) -> dict:
        """读取 db/tool/<name>/config.json；缺失时按 defaults 创建并补齐新字段。

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
        if not content:
            return {"ok": False, "error": "content 为空"}
        think = typing_delay_sec(content, getattr(self.cfg, "reply", {}) or {})
        if think > 0:
            await asyncio.sleep(think)
        seq = ctx.get("msg_seq", 1)
        await self.bot.send_text(
            ctx["target"], content, ctx["scene"], ctx.get("msg_id"), seq, ctx.get("event_id")
        )
        ctx["msg_seq"] = seq + 1
        ctx["sent_any"] = True
        sid = ctx.get("session_id")
        if sid:
            self.storage.add_message(sid, "assistant", "bot", content, status="sent")
        logger.info("发送: %.80s", content, extra={"session": session_label(ctx.get("session_id"))})
        return {"ok": True, "sent": content}

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
        return {"ok": False, "error": detail}


class Tool:
    """一个 Tool = 一份 schema + 一个执行器；配置/数据目录由 runtime 提供。"""

    name: str = ""
    category: str = ""  # 所属能力域（由注册表按所在子包填充：core/memory/emoji/audio/media/mail）
    data_name: str = ""  # 数据/配置目录名，默认与工具名一致（send_email -> mail 时可覆盖）
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}
    default_config: dict = {}
    enabled_by_default: bool = True
    prompt_hint: str = ""
    config_owner: bool = True   # 共享配置目录时，只由一个工具展示/承担配置
    toggleable: bool = True     # 共享配置的工具不单独启停

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
        """扫描 ggbot/tool/<分类>/ 下的插件模块（每个模块导出 get_tools(rt)）。"""
        root = importlib.import_module("ggbot.tool")
        for info in pkgutil.iter_modules(root.__path__):
            if info.name.startswith("_") or info.name == "base" or not info.ispkg:
                continue
            try:
                pkg = importlib.import_module(f"ggbot.tool.{info.name}")
            except Exception as e:  # noqa: BLE001
                logger.warning("工具分类 %s 加载失败: %s", info.name, e, exc_info=True)
                continue
            for mod in pkgutil.iter_modules(pkg.__path__):
                if mod.name.startswith("_"):
                    continue
                self._load_module(f"ggbot.tool.{info.name}.{mod.name}", info.name)

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

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values() if t.is_enabled()]

    def hints(self) -> str:
        lines = [
            f"- {t.name}：{t.prompt_hint}"
            for t in self.tools.values()
            if t.is_enabled() and t.prompt_hint
        ]
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
                "config": t.config if t.config_owner else {},
            })
        return out

    def toggle(self, name: str, enabled: bool) -> dict | None:
        tool = self.tools.get(name)
        if tool is None or not tool.toggleable:
            return None
        return self.rt.set_enabled(tool.key, tool.default_config, enabled)
