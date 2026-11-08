"""面板的资源接口：外壳读数、会话、表情素材、任务、记忆——按资源走的那一半（读快照在 readers）。

一页的完整快照走 GET /api/page/<name>；这里只放写操作，加上两条不进聚合的读：
/api/shell 是每秒轮询的在线态，会话详情是点进某个会话才读的那一条。
鉴权由 router 的依赖统一带上，处理函数里不再各写一遍 `_auth(request, x_token)`。
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ggbot.core.runtime_stats import runtime
from ggbot.llm.memory import KIND_LABELS, clamp_importance, kind_of
from ggbot.util.session import session_label
from ggbot.web.guards import panel_of, protected_router

logger = logging.getLogger("ggbot.web")

# 会话详情一次给最近 100 条、出站回执一次给 10 条
SESSION_DETAIL_LIMIT = 100
SEND_RECEIPTS = 10

router = protected_router()


class MemoryIn(BaseModel):
    content: str
    kind: str = "profile"
    subject: str | None = None
    importance: int = 1
    session_id: str | None = None
    expires_at: str | None = None


class MemoryEditIn(BaseModel):
    content: str | None = None
    importance: int | None = None
    kind: str | None = None
    subject: str | None = None
    expires_at: str | None = None


class EmojiDescIn(BaseModel):
    desc: str = ""


class TaskEditIn(BaseModel):
    due_ts: int | None = None
    instruction: str | None = None


# ---------- 外壳读数与会话 ----------


@router.get("/api/shell")
async def shell(request: Request):
    p = panel_of(request)
    # 在线态只算一次：以前 /api/status 与 /api/stats 各自跑一遍 presence.status()，
    # 面板每秒打两次卡，两份读数还可能对不上
    live = p.presence.status()
    out = p.storage.stats()
    out["online_sessions"] = live.get("online_sessions", 0)
    out["state"] = live.get("state", "offline")
    out["runtime"] = runtime.snapshot()
    out["env"] = p.env()
    return {"status": live, "stats": out}


@router.get("/api/sessions/{sid}/messages")
async def session_messages(sid: str, request: Request):
    p = panel_of(request)
    row = p.storage.get_session(sid)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    row["label"] = session_label(sid)
    return {
        "session": row,
        "messages": p.storage.get_recent_messages(sid, SESSION_DETAIL_LIMIT),
        # 出站回执：被动回复有没有被平台拒、什么时候降成了主动，看这一份就够，不用翻日志
        "sends": p.storage.recent_sends(sid, SEND_RECEIPTS),
    }


@router.delete("/api/sessions/{sid}/context")
async def session_clear(sid: str, request: Request):
    p = panel_of(request)
    if not p.storage.get_session(sid):
        raise HTTPException(status_code=404, detail="session not found")
    if p.presence is not None:
        await p.presence.forget_session(sid)
    removed = p.storage.clear_session_context(sid)
    logger.info("Web 清理会话上下文 %s: %d 条消息", session_label(sid), removed["messages"])
    return {"ok": True, **removed}


# ---------- 表情包 ----------
# 素材目录是唯一真相，工具与面板读写同一份索引/向量/使用统计；这里只是开了第二条入口，
# 写入全部走 EmojiLibrary（去重、上限、向量重算都在它身上，面板不另存一套状态）。
# 一个统一库：不分「内置/收藏」，图在哪个子目录只体现在 file 里。


@router.post("/api/emojis/import")
async def emoji_import(request: Request, name: str = Query(default=""), desc: str = Query(default="")):
    lib = panel_of(request).emoji_lib_or_503()
    data = await request.body()
    out = await lib.import_image(name, data, desc)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "导入失败")
    return out


@router.put("/emoji/{rel:path}")
async def emoji_desc(rel: str, body: EmojiDescIn, request: Request):
    lib = panel_of(request).emoji_lib_or_503()
    out = await lib.set_desc(rel, body.desc or "")
    if out is None:
        raise HTTPException(404, "素材不在库里")
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "描述没存下")
    return out


@router.delete("/emoji/{rel:path}")
async def emoji_delete(rel: str, request: Request):
    lib = panel_of(request).emoji_lib_or_503()
    if not await lib.remove(rel):
        raise HTTPException(404, "素材不在库里")
    return {"ok": True, "removed": rel}


# 缩略图不放 /api 下：一屏几十张图会被 API 的 IP 限流（240 次/10s）误伤
@router.get("/emoji/{rel:path}", response_class=FileResponse)
async def emoji_image(rel: str, request: Request):
    lib = panel_of(request).emoji_lib()
    path = lib.image_path(rel) if lib is not None else None
    if path is None:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        path,
        headers={"Cache-Control": "private, max-age=3600", "Content-Disposition": "inline"},
    )


# ---------- 任务 ----------
# 表里的那份是唯一真相（巡检循环也读它），所以面板给「看 + 改 + 删 + 清理」，
# 刻意不给新建：面板排任务要的是另一套时间输入，而 when/repeat 的解析只该有一份实现。


@router.post("/api/tasks/{rid}/cancel")
async def cancel_task(rid: int, request: Request):
    storage = panel_of(request).storage
    row = await asyncio.to_thread(storage.get_task, rid)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"#{rid} 已经是 {row['status']}，不用再取消")
    # finish_task 只动 pending 的行，置为 cancelled 后巡检就不会再跑它
    await asyncio.to_thread(storage.finish_task, rid, "cancelled")
    return {"ok": True}


@router.put("/api/tasks/{rid}")
async def edit_task(rid: int, body: TaskEditIn, request: Request):
    """改时间/指令：还在排的行改得了，失败退场的改一次就是重新排上（周期项改的是下一场）。"""
    p = panel_of(request)
    instruction = None if body.instruction is None else str(body.instruction).strip()
    if body.due_ts is None and instruction is None:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    # 上限是任务工具自己的配置（tool/task/config.json），不是面板的数
    tool_cfg = p.tasks().config
    now = int(time.time())
    if instruction is not None:
        cap = max(10, int(tool_cfg.get("max_instruction_chars", 500) or 500))
        if not instruction:
            raise HTTPException(status_code=400, detail="指令不能为空")
        if len(instruction) > cap:
            raise HTTPException(status_code=400, detail=f"指令太长（最多 {cap} 字）")
    if body.due_ts is not None:
        due = int(body.due_ts)
        if due <= now:
            raise HTTPException(status_code=400, detail="新时间得是将来——改到过去等于现在就跑")
        max_days = max(1, int(tool_cfg.get("max_days", 30) or 30))
        if due > now + max_days * 86400:
            raise HTTPException(status_code=400, detail=f"最远只能约 {max_days} 天内的第一次触发")
    row = await asyncio.to_thread(p.storage.get_task, rid)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    if row["status"] not in ("pending", "failed"):
        raise HTTPException(status_code=400, detail=f"#{rid} 已经结束了（{row['status']}），改不了")
    changed = await asyncio.to_thread(p.storage.update_task, rid, body.due_ts, instruction)
    if not changed:
        raise HTTPException(status_code=409, detail=f"#{rid} 刚好被巡检触发了，刷新看看")
    return {"ok": True}


@router.delete("/api/tasks/{rid}")
async def delete_task(rid: int, request: Request):
    """物理删一行：取消才是「不再触发」，删除只是把这条记录从表里拿掉。"""
    if not await asyncio.to_thread(panel_of(request).storage.delete_task, rid):
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True}


@router.delete("/api/tasks")
async def purge_task_history(request: Request, status: str | None = None,
                             session_id: str | None = None, before_days: int | None = None):
    """批量清历史：只碰终态行。条件全空时什么都不删（待触发的要退场得走取消）。"""
    if status == "pending":
        raise HTTPException(status_code=400, detail="待触发的行不能批量删，请先取消")
    if not any((status, session_id, before_days)):
        raise HTTPException(status_code=400,
                            detail="需要 status / session_id / before_days 至少一个条件")
    before_ts = None
    if before_days:
        before_ts = int(time.time()) - max(0, int(before_days)) * 86400
    removed = await asyncio.to_thread(
        panel_of(request).storage.purge_tasks, session_id or None, status or None, before_ts)
    return {"ok": True, "removed": removed}


@router.post("/api/tasks/sweep")
async def sweep_tasks(request: Request):
    """按保留天数与总行数上限清历史（pending 不动），和巡检循环里的定时清理同一条路。"""
    return await asyncio.to_thread(panel_of(request).tasks().sweep)


# ---------- 记忆 ----------


@router.post("/api/memories")
async def memory_add(body: MemoryIn, request: Request):
    memory = panel_of(request).memory
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content 不能为空")
    out = await asyncio.to_thread(memory.commit, [{
        "content": content,
        "kind": kind_of(body.kind),
        "subject": body.subject or "",
        "importance": clamp_importance(body.importance),
        "session_id": body.session_id or None,
        "expires_at": body.expires_at or None,
    }])
    return {"ok": True, "written": out["written"], "updated": out["updated"],
            "dropped": out["dropped"], "retired": out["retired"]}


@router.put("/api/memories/{memory_id}")
async def memory_edit(memory_id: str, body: MemoryEditIn, request: Request):
    memory = panel_of(request).memory
    if not any(v is not None for v in (body.content, body.importance,
                                       body.kind, body.subject, body.expires_at)):
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    if body.kind is not None and body.kind not in KIND_LABELS:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {' 或 '.join(KIND_LABELS)}")
    try:
        ok = await asyncio.to_thread(
            memory.update,
            memory_id,
            body.content,
            None if body.importance is None else clamp_importance(body.importance),
            body.subject,
            body.kind,
            body.expires_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="memory not found")
    return {"ok": True}


@router.post("/api/memories/sweep")
async def memory_sweep(request: Request):
    return await asyncio.to_thread(panel_of(request).memory.sweep)


@router.delete("/api/memories")
async def memory_delete_group(request: Request, subject: str | None = None,
                              kind: str | None = None, session_id: str | None = None):
    """整组清理：按「谁 / 哪类 / 哪个会话」删。三个条件全空时什么都不删（清空整库走 reset）。"""
    p = panel_of(request)
    if not any((subject, kind, session_id)):
        raise HTTPException(status_code=400, detail="需要 subject / kind / session_id 至少一个条件")
    if kind is not None and kind not in KIND_LABELS:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {' 或 '.join(KIND_LABELS)}")
    removed = p.storage.delete_memories_by(session_id or None, subject or None, kind or None)
    return {"ok": True, "removed": removed}


@router.delete("/api/memories/{memory_id}")
async def memory_delete(memory_id: str, request: Request):
    panel_of(request).storage.delete_memory(memory_id)
    return {"ok": True}
