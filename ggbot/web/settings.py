"""面板的控制接口：改的是机器人本身——工具启停与门槛、成员等级、配置热更新、人设。

与 resources.py 的分工：那边动的是对话产生的内容（会话/素材/任务/记忆），这边动的是行为。
设置与权限两页的读数在 readers.py，这里只留写入口和它自己的即时回包。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from ggbot.core.auth import (LEVEL_LOCKED, LEVEL_LOCKED_NAME, LEVEL_MAX, LEVEL_NAMES,
                             clamp_level)
from ggbot.core.config import SYSTEM_PROMPT_MAX_CHARS
from ggbot.web.guards import mask_secrets, panel_of, protected_router

logger = logging.getLogger("ggbot.web")

router = protected_router()


class ToolToggleIn(BaseModel):
    enabled: bool


class ToolLevelIn(BaseModel):
    level: int


class ActorIn(BaseModel):
    openid: str
    level: int = 2
    name: str | None = None
    note: str | None = None


class SettingIn(BaseModel):
    path: str
    value: object


class SystemPromptIn(BaseModel):
    prompt: str = ""


# ---------- 工具（插件清单 / 启停 / 门槛） ----------


@router.post("/api/tools/{name}/toggle")
async def toggle_tool(name: str, body: ToolToggleIn, request: Request):
    p = panel_of(request)
    tool = p.tool(name)
    if not tool.toggleable:
        raise HTTPException(status_code=400, detail="该工具随同组工具启停")
    data = p.executor.toggle(name, body.enabled)
    if data is None:
        raise HTTPException(status_code=404, detail="tool not found")
    logger.info("Web 工具启停 %s=%s", name, body.enabled)
    return {"ok": True, "config": mask_secrets(data)}


@router.post("/api/tools/{name}/level")
async def set_tool_level(name: str, body: ToolLevelIn, request: Request):
    p = panel_of(request)
    if not 1 <= int(body.level) <= LEVEL_LOCKED:
        raise HTTPException(
            status_code=400,
            detail=f"工具门槛需在 1-{LEVEL_LOCKED} 之间（{LEVEL_LOCKED} = {LEVEL_LOCKED_NAME}）")
    tool = p.tool(name)
    data = p.executor.set_level(name, body.level)
    level = tool.required_level()
    logger.info("Web 工具门槛 %s = L%d%s", name, level,
                f"（{LEVEL_LOCKED_NAME}）" if level >= LEVEL_LOCKED else "")
    return {"ok": True, "level": level, "config": mask_secrets(data)}


# ---------- 权限（成员等级） ----------


@router.post("/api/actors")
async def set_actor(body: ActorIn, request: Request):
    p = panel_of(request)
    openid = body.openid.strip()
    if not openid:
        raise HTTPException(status_code=400, detail="openid 不能为空")
    if not 0 <= int(body.level) <= LEVEL_MAX:
        raise HTTPException(status_code=400, detail=f"等级需在 0-{LEVEL_MAX} 之间")
    perms = p.perms()
    row = perms.set_level(openid, body.level, note=body.note, name=body.name)
    logger.info("Web 权限设置 %s = L%d %s", openid[:8], row.get("level"),
                LEVEL_NAMES.get(clamp_level(row.get("level")), ""))
    return {"ok": True, "actor": row}


# ---------- 配置（Web 热更新） ----------


@router.post("/api/config")
async def update_config(body: list[SettingIn], request: Request):
    cfg = panel_of(request).cfg
    updated: list[str] = []
    rejected: list[str] = []
    for item in body:
        # 白名单、类型、负值三道校验都收在 Config.apply_setting 里：
        # 「哪些键能改、改成什么样算合法」是配置的契约，面板只负责记账
        bucket = updated if cfg.apply_setting(item.path, item.value) else rejected
        bucket.append(item.path)
    if "web.log_level" in updated:
        from ggbot.util.logging_setup import set_level

        set_level(cfg.data["web"]["log_level"])
    cfg.save()
    logger.info("Web 配置更新: %s（拒绝 %d 项）", updated, len(rejected))
    return {
        "ok": True,
        "updated": updated,
        "rejected": rejected,
        "config": mask_secrets(cfg.data),
    }


# ---------- 系统提示词（人设） ----------


def system_prompt_payload(p) -> dict:
    from ggbot.core.presence import SYSTEM_TEMPLATE

    custom = str(p.cfg.llm.get("system_prompt") or "")
    return {
        "custom": custom,
        "default": SYSTEM_TEMPLATE,
        # 面板编辑框里显示的就是这一份：自定义优先，否则内置默认
        "effective": custom.strip() or SYSTEM_TEMPLATE,
        "is_custom": bool(custom.strip()),
        "max_chars": SYSTEM_PROMPT_MAX_CHARS,
    }


@router.post("/api/system-prompt")
async def update_system_prompt(body: SystemPromptIn, request: Request):
    p = panel_of(request)
    text = body.prompt or ""
    if len(text) > SYSTEM_PROMPT_MAX_CHARS:
        raise HTTPException(
            400, f"系统提示词最长 {SYSTEM_PROMPT_MAX_CHARS} 字，当前 {len(text)} 字"
        )
    if not p.cfg.set_by_path("llm.system_prompt", text):
        raise HTTPException(500, "配置里没有 llm 段，存不下系统提示词")
    p.cfg.save()
    logger.info("Web 系统提示词: %s（%d 字）",
                "自定义" if text.strip() else "回到内置默认", len(text))
    return system_prompt_payload(p)
