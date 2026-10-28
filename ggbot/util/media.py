"""本地媒体的签名 URL：把 temp/ 与 tool/ 下的文件临时暴露到公网。

QQ 富媒体上传优先走 base64 file_data 直传，本地文件一般不需要公网地址；
只有超过 10MB 的本地文件才退回公网 URL（QQ 单请求体过大）。
这里生成带 HMAC 签名与有效期的路径，由 Web 面板的 /media/ 路由读取；
只允许媒体扩展名，绝不暴露 config.json、数据库或日志。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import quote

MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".m4v",
    ".wav", ".mp3", ".silk", ".flac", ".ogg", ".m4a", ".amr",
}
MAX_TTL = 7 * 24 * 3600
DEFAULT_TTL = 3600


def _secret(cfg) -> str:
    return str((cfg.web_cfg or {}).get("token") or "") or str((cfg.qq or {}).get("secret") or "")


def _roots(cfg) -> tuple[Path, Path]:
    temp = Path(cfg.temp_dir).resolve()
    tools = Path(cfg.tool_dir).resolve()
    return temp, tools


def _encode(cfg, p: Path) -> str | None:
    temp, tools = _roots(cfg)
    if p.is_relative_to(temp):
        rel = p.relative_to(temp)
        if rel.parts and rel.parts[0] == "logs":
            return None
        return "t/" + rel.as_posix()
    if p.is_relative_to(tools):
        return "d/" + p.relative_to(tools).as_posix()
    return None


def resolve_local(cfg, rel: str) -> Path | None:
    """把签名 URL 中的相对路径还原为受信任的本地文件。"""
    if not rel or ".." in Path(rel).parts or "\\" in rel:
        return None
    temp, tools = _roots(cfg)
    tag, _, rest = rel.partition("/")
    if not rest:
        return None
    root = temp if tag == "t" else tools if tag == "d" else None
    if root is None:
        return None
    if tag == "t" and Path(rest).parts and Path(rest).parts[0] == "logs":
        return None
    try:
        p = (root / rest).resolve()
    except OSError:
        return None
    if not p.is_relative_to(root) or not p.is_file():
        return None
    if p.suffix.lower() not in MEDIA_EXTS or p.name in {"config.json", ".env"}:
        return None
    return p


def sign_path(cfg, path: Path | str, ttl: int = DEFAULT_TTL) -> str | None:
    """返回 /media/<exp>/<sig>/<rel>；路径不在受信任目录/非媒体文件时返回 None。"""
    secret = _secret(cfg)
    if not secret:
        return None
    p = Path(path)
    if not p.is_absolute():
        from ggbot.core.config import BASE_DIR

        p = BASE_DIR / p
    try:
        p = p.resolve()
    except OSError:
        return None
    if not p.is_file() or p.suffix.lower() not in MEDIA_EXTS or p.name in {"config.json", ".env"}:
        return None
    rel = _encode(cfg, p)
    if rel is None:
        return None
    exp = int(time.time()) + max(60, min(int(ttl), MAX_TTL))
    sig = hmac.new(secret.encode("utf-8"), f"{exp}:{rel}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"/media/{exp}/{sig}/{quote(rel)}"


def verify_path(cfg, exp: int, sig: str, rel: str) -> Path | None:
    secret = _secret(cfg)
    if not secret or not rel:
        return None
    now = int(time.time())
    if exp < now or exp > now + MAX_TTL:
        return None
    want = hmac.new(secret.encode("utf-8"), f"{exp}:{rel}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig or ""):
        return None
    return resolve_local(cfg, rel)


def public_media_url(cfg, path: Path | str, ttl: int = DEFAULT_TTL) -> str | None:
    """把本地文件转成公网可下载的签名 URL（需配置 storage.public_base_url；仅大文件回退用）。"""
    base = str((cfg.storage_cfg or {}).get("public_base_url") or "").strip()
    if not base:
        return None
    signed = sign_path(cfg, path, ttl)
    if not signed:
        return None
    return base.rstrip("/") + signed
