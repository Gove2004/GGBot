"""出站：一句话要发出去，「能不能发、以什么身份发、发完记什么账」都在这一处。

从 presence 里抽出来的三件事：
- 被动回复锚点的选择与 msg_seq 续排：账落在 reply_anchors 表里，重启也接着上次排，
  不会因为进程一 restart 又从 1 开始、撞上平台拒绝的重复 msg_id+msg_seq；
- 主动消息的本地频控闸门：平台群聊硬限制 20 条/分钟、1000 条/天，这里再留余量；
- 富媒体来源判定：temp/ 与 tool/ 下的本地文件走 base64 file_data 直传，超过 10MB 才退回签名公网 URL。

speak（提醒到点）和回合内回复走同一条 open_turn → 发送 → commit_turn，回执也记在同一处（sends 表），
排查「被动回复是不是被平台拒了」不用去翻日志。
"""

from __future__ import annotations

import time
from pathlib import Path

from ggbot.util.media import public_media_url

# 平台被动回复窗口（2026-10 官方文档口径）：群里一条消息 5 分钟内最多回 5 次，单聊 60 分钟最多 4 次。
# 次数用满或过了窗口就只能改主动发送，所以这两个数是「这条锚点还能不能用」的判断依据。
PASSIVE_WINDOWS = {"group": (300, 5), "c2c": (3600, 4)}

# 富媒体直传的上限：QQ 单请求体过大就改用 URL 让平台自己拉
MAX_INLINE_UPLOAD = 10 * 1024 * 1024

SENDS_KEEP = 2000
"""发送回执只留最近这么多条：它是排查用的流水，不是要长期攒的账。"""


def derive_target(session_id: str) -> tuple[str, str]:
    """从 session_id（如 group:g_xxx / c2c:u_xxx）反推发送目标与场景。

    sessions.key 里存的是完整 openid，库里的消息行只留 session_id，因此重启后或
    只按会话发言（提醒到点）时需要这条推导兜底。
    """
    sid = session_id or ""
    if ":" in sid:
        prefix, _, rest = sid.partition(":")
        return rest, ("group" if prefix == "group" else "c2c")
    return sid, "group"


def _ts(offset_sec: float) -> str:
    """相对现在偏移若干秒的时间戳（负数=过去，正数=将来），口径与 messages.ts 一致。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_sec))


class AnchorLedger:
    """被动回复锚点这本账：哪条消息还能拿来回、下一条 msg_seq 排到几。"""

    def __init__(self, storage):
        self.storage = storage

    def pick(self, sid: str, msgs: list[dict], scene: str) -> tuple[dict | None, int]:
        """还能用的锚点 + 它的下一个 msg_seq；没有锚点这一轮就只能主动发送。

        优先本轮批次里最新一条可回的消息，批次里没有就回看库里窗口内最近一条——普通群消息
        也带 msg_id，官方文档写明可以被动回复（被平台拒的是它的 event_id，所以那条路径不传）。
        只读，不占用序号。
        """
        window, cap = PASSIVE_WINDOWS.get(scene, PASSIVE_WINDOWS["group"])
        anchor = next((m for m in reversed(msgs or [])
                       if m.get("passive_ok") and m.get("msg_id")), None)
        if anchor is None:
            anchor = self.storage.latest_replyable_message(sid, _ts(-window))
        if not anchor or not anchor.get("msg_id"):
            return None, 1
        seq = self._next_seq(sid, str(anchor["msg_id"]), window)
        # 一条消息在窗口里最多回 cap 次（群 5 / 私聊 4），回满了就换主动，别再碰平台
        return (None, 1) if seq > cap else (anchor, seq)

    def _next_seq(self, sid: str, msg_id: str, window: int) -> int:
        row = self.storage.get_reply_anchor(sid, msg_id)
        if not row or (row.get("expires_at") or "") < _ts(0):
            return 1  # 没见过这条锚点，或它上一轮记的窗口已经过了：序号从头排
        return int(row.get("next_seq") or 1)

    def commit(self, sid: str, ctx: dict) -> None:
        """记下这一轮真正用到的锚点序号：发送每成功一条，outbound/媒体都会把 msg_seq 往上加。"""
        mid = ctx.get("msg_id")
        if not mid:
            return
        scene = ctx.get("scene") or "group"
        window, _cap = PASSIVE_WINDOWS.get(scene, PASSIVE_WINDOWS["group"])
        # expires_at 只在第一次记这条锚点时写（存储层的 UPSERT 不覆盖它）：
        # 窗口从这条消息第一次被回起算，反复回同一条不会把窗口越拖越长
        self.storage.save_reply_anchor(
            sid, str(mid), event_id=ctx.get("event_id"), scene=scene,
            next_seq=int(ctx.get("msg_seq") or 1), expires_at=_ts(window),
        )
        self.storage.prune_reply_anchors()


class ProactiveGate:
    """主动消息的本地闸门：先问它，被拦下的轮次根本不去碰平台。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._turns: dict[str, list[float]] = {}
        self._day: dict[str, tuple[str, int]] = {}

    @property
    def _p(self) -> dict:
        return self.cfg.presence

    def block_reason(self, sid: str) -> str | None:
        """返回拦截原因，None 表示可以主动发。"""
        if not self._p.get("proactive_enabled", True):
            return "主动消息已关闭"
        now = time.monotonic()
        window = [t for t in self._turns.get(sid, []) if now - t < 60]
        self._turns[sid] = window
        if len(window) >= int(self._p.get("proactive_per_minute", 6)):
            return "主动消息每分钟上限"
        day = time.strftime("%Y-%m-%d")
        d, n = self._day.get(sid, (day, 0))
        if n >= int(self._p.get("proactive_per_day", 1000)) and d == day:
            return "主动消息每日上限"
        return None

    def note(self, sid: str) -> None:
        self._turns.setdefault(sid, []).append(time.monotonic())
        day = time.strftime("%Y-%m-%d")
        d, n = self._day.get(sid, (day, 0))
        self._day[sid] = (day, n + 1 if d == day else 1)


class Outbox:
    """一轮发言的出站入口：开一个 turn（决定被动/主动）、记发送回执、收尾落账。"""

    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.anchors = AnchorLedger(storage)
        self.gate = ProactiveGate(cfg)

    def target_for(self, sid: str) -> tuple[str, str]:
        """会话发送目标：从 sessions.key 反查完整 openid，查不到才退回按 id 推导。"""
        t = self.storage.session_target(sid)
        if t and t[0]:
            return t
        return derive_target(sid)

    def open_turn(self, sid: str, *, target: str, scene: str,
                  msgs: list[dict] | None = None) -> tuple[dict | None, str | None]:
        """给出这一轮的发送上下文，以及被拦下的原因（ctx 为 None 时这轮发不出去）。

        一律先试被动回复：有还能用的锚点就挂上它，不占主动额度；锚点过期或回满次数才走主动，
        主动受本地闸门约束（提醒这类回合外发言拿不到锚点时也会被它挡住，由调用方下一轮重试）。
        """
        anchor, seq = self.anchors.pick(sid, msgs or [], scene)
        passive = anchor is not None
        if not passive:
            blocked = self.gate.block_reason(sid)
            if blocked:
                return None, blocked
        return ({"session_id": sid, "target": target, "scene": scene, "passive": passive,
                 "msg_id": anchor.get("msg_id") if passive else None,
                 "event_id": anchor.get("event_id") if passive else None,
                 "msg_seq": seq}, None)

    def commit_turn(self, sid: str, ctx: dict) -> None:
        """这一轮发完了：锚点序号落账、主动计数落账、回执流水按上限瘦身。"""
        self.anchors.commit(sid, ctx)
        if not ctx.get("passive"):
            self.gate.note(sid)
        self.storage.prune_sends(SENDS_KEEP)

    def record(self, ctx: dict, *, kind: str, ok: bool, via: str | None = None,
               detail: str = "") -> None:
        """一条发送回执：via 记的是这次实际走的路（被动/主动/被动失败后降级）。"""
        self.storage.add_send(
            ctx.get("session_id"), msg_id=ctx.get("msg_id"), msg_seq=ctx.get("msg_seq"),
            via=via or ("passive" if ctx.get("passive") else "proactive"),
            kind=kind, ok=ok, detail=detail,
        )

    def sent_count(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        """最近的发送回执（面板与 CLI 都读这一份，不再去翻日志）。"""
        return self.storage.recent_sends(session_id, limit)


def resolve_media_source(cfg, source: str) -> tuple[Path | None, str | None, str, str | None]:
    """判定富媒体来源，返回 (本地文件, 公网 URL, 展示名, 错误)。

    本地文件只允许 temp/ 与 tool/media/emoji/：否则低等级发送者可以用 send_image 读任意本机文件。
    本地一般直接以 base64 file_data 上传（QQ 官方接口支持，无需公网地址），
    只有超过 MAX_INLINE_UPLOAD 的文件才退回签名链接，避免请求体过大。
    """
    if source.startswith(("http://", "https://")):
        return None, source, "公网素材", None
    p = Path(source).expanduser()
    if not p.is_absolute():
        p = Path(cfg.temp_dir) / p
    p = p.resolve()
    temp_root = Path(cfg.temp_dir).resolve()
    tool_raw = getattr(cfg, "tool_dir", None)
    tool_root = Path(tool_raw).resolve() if tool_raw else None
    inside = None
    for root in (temp_root, tool_root):
        if root is not None and (p == root or root in p.parents):
            inside = root
            break
    if inside is None:
        return None, None, "", "本地媒体只允许 temp/ 与 tool/media/emoji/ 素材"
    rel = p.relative_to(inside)
    if inside == temp_root and rel.parts[:1] == ("logs",):
        return None, None, "", "本地媒体不能发送日志文件"
    if inside == tool_root:
        if rel.parts[:2] != ("media", "emoji"):
            return None, None, "", "本地媒体只允许 temp/ 与 tool/media/emoji/ 素材"
        if p.name == "config.json" or p.suffix.lower() in {".key", ".pem"}:
            return None, None, "", "本地媒体不能发送配置/密钥文件"
    if not p.is_file():
        return None, None, p.name, f"本地文件不存在: {p.name}"
    if p.stat().st_size > MAX_INLINE_UPLOAD:
        url = public_media_url(cfg, p)
        if not url:
            return None, None, p.name, (
                f"文件超过 {MAX_INLINE_UPLOAD // 1048576}MB，需要配置 storage.public_base_url 才能发送"
            )
        return None, url, p.name, None
    return p, None, p.name, None
