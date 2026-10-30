"""Tool 插件基座：一切能力都是 Tool。

- Tool：一个可被 LLM 调用的能力（schema + 执行逻辑 + 自己的配置/数据目录）
- ToolRuntime：给 Tool 提供配置、持久化目录、富媒体发送等公共依赖（正文通道不在这里，见 ggbot/core/outbound.py）
- ToolRegistry：自动发现 tool/<分类>/ 下的插件，按配置开关动态挂载 schema

分类即能力域：core（回合控制）/ schedule（定时）/ media（富媒体）/ memory（记忆）/ web（联网）/ mail（邮件）/ admin（管理）。
新增分类 = 新建一个含插件模块的子包。

每个 Tool 声明 min_level（调用门槛，0-5 为发送者等级，6 为永久锁）。全部启用的工具都会下发给模型，
**不按发送者等级裁剪**：工具列表对所有会话一致，才能真正复用厂商的前缀缓存；
门槛由执行器在调用时校验，权限不足直接失败返回。6 级（永久锁）不进任何列表。

tool/ 位于项目根目录且**自包含**：每个 toolset 目录同时存放代码、config.json
与持久化数据（索引、素材、参考音频），整目录复制即可迁移；运行产物在 temp/tool/<name>/。
含密钥的 config.json 由 .gitignore 排除，绝不入版本库。
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
import threading
from pathlib import Path

from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools")


class ToolRuntime:
    """Tool 的公共运行时：配置读写 + 数据目录 + 富媒体发送（正文走 ggbot.core.outbound）。"""

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
    category: str = ""  # 所属能力域（由注册表按所在子包填充：core/media/memory/web/mail/admin）
    data_name: str = ""  # 数据/配置目录名，默认与工具名一致（send_email -> mail 时可覆盖）
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}
    default_config: dict = {}
    enabled_by_default: bool = True
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
                    continue  # 素材/数据目录不是插件包（如 media/emoji、media/audio）
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

    def dispatched(self) -> list[Tool]:
        """下发给模型的全部工具（启用即可见，不看发送者等级），按 (门槛, 分类, 名称) 稳定排序。

        排序固定让所有会话拿到同一份列表：这份列表和静态人设一起构成请求前缀，
        厂商的上下文缓存才命中得了。6 级（永久锁）永远不出现在任何人的列表里。
        """
        rows = [t for t in self.tools.values() if t.is_enabled() and t.required_level() <= 5]
        rows.sort(key=lambda t: (t.required_level(), t.category, t.name))
        return rows

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.dispatched()]

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
