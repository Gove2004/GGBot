"""邮件 Tool：通过 SMTP 发送邮件（文本/HTML + 附件）。

安全边界（LLM 可调用，必须收紧）：
- 收件人只能来自 allowed_recipients；未配置白名单时只允许 default_to；
- 附件只能引用 temp/ 下的运行时文件，禁止配置、数据库、日志与密钥文件；
- 数量/大小受限，避免被当作外发通道滥用。

配置：tool/mail/config.json（含授权码，gitignored，与代码同目录）
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from ..base import Tool
from ggbot.core.config import BASE_DIR
from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools.email")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BLOCKED_SUFFIXES = {".db", ".db-wal", ".db-shm", ".lock", ".env", ".py", ".bat", ".sh"}
BLOCKED_NAMES = {"config.json", ".env"}


def _as_list(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,，;；\s]+", str(v or "")) if x.strip()]


class SendEmail(Tool):
    name = "send_email"
    data_name = "mail"
    description = (
        "通过 SMTP 发送邮件。用户明确要求发邮件时直接调用，不要在调用前自己判断能不能发；"
        "收件人直接传用户给的地址，工具会校验白名单并返回具体原因。"
        "支持纯文本/HTML 正文与多个附件（附件只能是 temp 目录下已生成的文件）"
    )
    parameters = {
        "type": "object",
        "properties": {
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "description": "收件人邮箱列表；省略时发到配置的 default_to",
            },
            "subject": {"type": "string", "description": "邮件主题"},
            "body": {"type": "string", "description": "邮件正文"},
            "html": {"type": "boolean", "description": "正文是否按 HTML 渲染，默认 false"},
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "附件路径列表（仅限 temp/ 下的文件）",
            },
        },
        "required": ["subject", "body"],
    }
    default_config = {
        "enabled": True,
        "smtp_server": "smtp.qq.com",
        "smtp_port": 465,
        "use_ssl": True,
        "sender_email": "",
        "auth_code": "",
        "default_to": [],
        "allowed_recipients": [],
        "max_attachments": 3,
        "max_attachment_mb": 10,
    }
    prompt_hint = (
        "用户要发邮件就直接调 send_email，别先自己断言发不了；收件人只能从白名单/默认收件人里选；"
        "绝不把配置、密钥、数据库当附件外发。"
    )

    def is_enabled(self) -> bool:
        if not super().is_enabled():
            return False
        c = self.config
        return bool(c.get("sender_email")) and bool(c.get("auth_code"))

    def _recipients(self, args: dict) -> tuple[list[str] | None, str]:
        cfg = self.config
        default = _as_list(cfg.get("default_to"))
        allowed = _as_list(cfg.get("allowed_recipients"))
        requested = _as_list(args.get("to")) or default
        if not requested:
            return None, "未指定收件人，且未配置 default_to"
        if allowed:
            pool = allowed
        elif default:
            pool = default
        else:
            return None, "未配置收件人白名单（allowed_recipients / default_to）"
        pool_l = {x.lower() for x in pool}
        bad = [r for r in requested if r.lower() not in pool_l]
        if bad:
            return None, f"收件人不在白名单: {', '.join(bad)}"
        invalid = [r for r in requested if not EMAIL_RE.match(r)]
        if invalid:
            return None, f"邮箱格式不合法: {', '.join(invalid)}"
        return requested, ""

    def _attachments(self, args: dict) -> tuple[list[Path] | None, str]:
        cfg = self.config
        raw = args.get("attachments") or []
        if isinstance(raw, str):
            raw = [raw]
        paths = [str(x).strip() for x in raw if str(x).strip()]
        max_n = max(0, int(cfg.get("max_attachments", 3) or 0))
        if len(paths) > max_n:
            return None, f"附件过多（最多 {max_n} 个）"
        temp_root = Path(self.rt.cfg.temp_dir).resolve()
        max_mb = max(0.0, float(cfg.get("max_attachment_mb", 10) or 0))
        out: list[Path] = []
        for raw_path in paths:
            p = Path(raw_path)
            if not p.is_absolute():
                project = BASE_DIR / p
                p = project if project.exists() else temp_root / p
            try:
                p = p.resolve()
            except OSError as e:
                return None, f"附件路径无效: {e}"
            if not p.is_relative_to(temp_root):
                return None, "附件只能使用 temp 目录下的文件"
            if "logs" in p.parts or p.name in BLOCKED_NAMES or p.suffix.lower() in BLOCKED_SUFFIXES:
                return None, f"禁止发送该文件: {p.name}"
            if not p.is_file():
                return None, f"附件不存在: {p.name}"
            if max_mb and p.stat().st_size > max_mb * 1024 * 1024:
                return None, f"附件过大（>{max_mb:g}MB）: {p.name}"
            out.append(p)
        return out, ""

    def _send(self, to: list[str], subject: str, body: str, is_html: bool, files: list[Path]) -> None:
        cfg = self.config
        msg = MIMEMultipart()
        msg["From"] = cfg["sender_email"]
        msg["To"] = ", ".join(to)
        msg["Subject"] = Header(subject, "utf-8")
        msg.attach(MIMEText(body, "html" if is_html else "plain", "utf-8"))
        for p in files:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(p.read_bytes())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{p.name}"')
            msg.attach(part)

        server = str(cfg.get("smtp_server") or "smtp.qq.com")
        port = int(cfg.get("smtp_port") or 465)
        timeout = 30
        if bool(cfg.get("use_ssl", True)):
            smtp = smtplib.SMTP_SSL(server, port, timeout=timeout)
        else:
            smtp = smtplib.SMTP(server, port, timeout=timeout)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
        try:
            smtp.login(cfg["sender_email"], cfg["auth_code"])
            smtp.sendmail(cfg["sender_email"], to, msg.as_string())
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass

    async def run(self, args: dict, ctx: dict) -> dict:
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "")
        if not subject or not body.strip():
            return {"ok": False, "error": "subject 与 body 不能为空"}
        if len(subject) > 200:
            return {"ok": False, "error": "主题过长（>200 字）"}
        if len(body) > 100_000:
            return {"ok": False, "error": "正文过长"}
        to, err = self._recipients(args)
        if to is None:
            return {"ok": False, "error": err}
        files, err = self._attachments(args)
        if files is None:
            return {"ok": False, "error": err}
        try:
            await asyncio.to_thread(
                self._send, to, subject, body, bool(args.get("html", False)), files
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("邮件发送失败: %s", e)
            return {"ok": False, "error": f"邮件发送失败: {e}"}
        logger.info("发送邮件: %.40s -> %s（附件 %d）", subject, ", ".join(to), len(files),
                    extra={"session": session_label(ctx.get("session_id"))})
        return {"ok": True, "sent_to": to, "attachments": len(files)}


def get_tools(rt):
    return [SendEmail(rt)]
