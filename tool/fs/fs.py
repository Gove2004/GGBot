"""文件能力：给高权限发送者的本机文件读写。

- 根目录沙箱：所有路径解析后必须落在 config.root 内（默认项目根），符号链接按真实路径校验；
- 敏感文件保护：配置、数据库、密钥、.git 等默认拒绝，可通过 deny_globs 调整；
- 输出/读写限额：单次读、写、搜索都有上限，避免把大文件塞进上下文或误写巨量数据；
- 分两级：fs_read（L3，只读）/ fs_write（L4，写入），schema 按发送者等级裁剪。

配置：tool/fs/config.json（与代码同目录）
"""

from __future__ import annotations

import codecs
import fnmatch
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from ..base import Tool
from ggbot.util.text import to_str

DEFAULT_DENY = [
    "db",
    "db/*",
    "tool/*/config.json",
    ".env",
    "*.env",
    "*.pem",
    "*.key",
    "*id_rsa*",
    ".git",
    ".git/*",
    ".venv",
    ".venv/*",
    "__pycache__",
    "__pycache__/*",
]


class FsError(Exception):
    pass


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


class FsBase(Tool):
    data_name = "fs"
    default_config = {
        "enabled": True,
        "root": "",
        "max_read_bytes": 262144,
        "max_write_bytes": 1048576,
        "max_output_chars": 20000,
        "max_results": 200,
        "max_scan_files": 2000,
        "deny_globs": DEFAULT_DENY,
    }

    # ---------- 沙箱 ----------

    def root(self) -> Path:
        base = Path(self.rt.tool_root).resolve().parent
        raw = str(self.config.get("root") or "").strip()
        p = Path(raw).expanduser() if raw else base
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
        if not p.is_dir():
            raise FsError(f"文件根目录不存在: {p}")
        return p

    def deny_globs(self) -> list[str]:
        raw = self.config.get("deny_globs")
        if not isinstance(raw, list):
            return list(DEFAULT_DENY)
        return [str(x).strip() for x in raw if str(x).strip()]

    def resolve(self, raw: str | None) -> tuple[Path, str]:
        root = self.root()
        p = Path((raw or ".").strip()).expanduser()
        if not p.is_absolute():
            p = root / p
        p = p.resolve(strict=False)
        try:
            rel = p.relative_to(root)
        except ValueError as e:
            raise FsError("路径超出允许的根目录") from e
        rel_s = rel.as_posix() or "."
        self._check_denied(p, rel_s)
        return p, rel_s

    def _check_denied(self, path: Path, rel: str) -> None:
        abs_s = path.as_posix()
        for pat in self.deny_globs():
            # 目录整棵受保护时（如 db/*）连目录本身也不露出
            if (
                fnmatch.fnmatch(rel, pat)
                or fnmatch.fnmatch(rel + "/*", pat)
                or fnmatch.fnmatch(abs_s, "*/" + pat)
            ):
                raise FsError(f"路径受保护: {rel}")

    def _child_denied(self, child: Path, root: Path) -> bool:
        try:
            rel = child.relative_to(root).as_posix() or "."
        except ValueError:
            return True
        try:
            self._check_denied(child, rel)
        except FsError:
            return True
        return False

    # ---------- 文本 ----------

    def _read_bytes(self, p: Path) -> tuple[bytes, bool]:
        max_bytes = _int(self.config.get("max_read_bytes"), 262144, 1024, 8 * 1024 * 1024)
        with p.open("rb") as f:
            raw = f.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        if b"\x00" in raw[:8192]:
            raise FsError("二进制文件不支持")
        return raw, truncated

    @staticmethod
    def _decode(raw: bytes) -> tuple[str, str]:
        for enc in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace"), "utf-8?"

    def _clip(self, text: str) -> str:
        limit = _int(self.config.get("max_output_chars"), 20000, 500, 200000)
        if len(text) <= limit:
            return text
        return text[:limit] + "\n…（输出已截断）"

    def _write_bytes(self, p: Path, data: bytes, bom: bool = False) -> None:
        max_bytes = _int(self.config.get("max_write_bytes"), 1048576, 1024, 32 * 1024 * 1024)
        if len(data) > max_bytes:
            raise FsError(f"内容过大（{len(data)} > {max_bytes} 字节）")
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".ggbot-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                if bom and not data.startswith(codecs.BOM_UTF8):
                    f.write(codecs.BOM_UTF8)
                f.write(data)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _write_text(self, p: Path, text: str, bom: bool = False) -> None:
        self._write_bytes(p, text.encode("utf-8"), bom=bom)


class FsRead(FsBase):
    name = "fs_read"
    description = (
        "查看本机文件：list 列目录、stat 看属性、read 读文本、find 按通配符找文件、grep 搜内容。"
        "路径用相对根目录的写法；只能访问权限根目录内的文件，敏感配置/密钥/数据库会被拒绝。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "stat", "read", "find", "grep"],
                "description": "要执行的操作",
            },
            "path": {"type": "string", "description": "相对根目录的路径，默认 .（当前根目录）"},
            "pattern": {
                "type": "string",
                "description": "find 用文件名通配符（如 *.py）；grep 用正则",
            },
            "glob": {"type": "string", "description": "grep 只扫描匹配该通配符的文件，默认 *"},
            "start_line": {"type": "integer", "description": "read 从第几行开始，默认 1"},
            "max_lines": {"type": "integer", "description": "read 最多读多少行，默认 200"},
            "max_results": {"type": "integer", "description": "find/grep/list 的结果上限"},
            "max_depth": {"type": "integer", "description": "find/grep 的递归深度，默认 8"},
            "ignore_case": {"type": "boolean", "description": "grep 是否忽略大小写"},
        },
        "required": ["action"],
    }
    min_level = 3

    async def run(self, args: dict, ctx: dict) -> dict:
        try:
            action = str(args.get("action") or "").strip().lower()
            if action == "list":
                return self._list(args)
            if action == "stat":
                return self._stat(args)
            if action == "read":
                return self._read(args)
            if action == "find":
                return self._find(args)
            if action == "grep":
                return self._grep(args)
            return {"ok": False, "error": f"未知操作: {action}"}
        except (FsError, OSError, ValueError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}

    def _limit(self, args: dict, default: int = 200) -> int:
        return _int(args.get("max_results"), default, 1, 2000)

    def _list(self, args: dict) -> dict:
        p, rel = self.resolve(args.get("path"))
        if not p.exists():
            raise FsError(f"目录不存在: {rel}")
        if not p.is_dir():
            raise FsError(f"不是目录: {rel}")
        root = self.root()
        limit = self._limit(args)
        entries = []
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if child.is_symlink() or self._child_denied(child, root):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            is_dir = child.is_dir()
            entries.append({
                "name": child.name,
                "type": "dir" if is_dir else "file",
                "size": None if is_dir else st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
            if len(entries) >= limit:
                break
        return {"ok": True, "path": rel, "entries": entries, "truncated": len(entries) >= limit}

    def _stat(self, args: dict) -> dict:
        p, rel = self.resolve(args.get("path"))
        if not p.exists():
            raise FsError(f"路径不存在: {rel}")
        st = p.stat()
        return {
            "ok": True,
            "path": rel,
            "type": "dir" if p.is_dir() else "file",
            "size": None if p.is_dir() else st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _read(self, args: dict) -> dict:
        p, rel = self.resolve(args.get("path"))
        if not p.exists():
            raise FsError(f"文件不存在: {rel}")
        if not p.is_file():
            raise FsError(f"不是文件: {rel}")
        raw, truncated = self._read_bytes(p)
        text, enc = self._decode(raw)
        lines = text.splitlines()
        start = _int(args.get("start_line"), 1, 1, max(1, len(lines)))
        count = _int(args.get("max_lines"), 200, 1, 2000)
        chunk = lines[start - 1:start - 1 + count]
        body = "\n".join(f"{i:>6}| {line}" for i, line in enumerate(chunk, start))
        return {
            "ok": True,
            "path": rel,
            "encoding": enc,
            "total_lines": len(lines),
            "start_line": start,
            "truncated": truncated or start - 1 + len(chunk) < len(lines),
            "content": self._clip(body),
        }

    def _find(self, args: dict) -> dict:
        start, rel = self.resolve(args.get("path"))
        if not start.is_dir():
            raise FsError(f"不是目录: {rel}")
        pattern = str(args.get("pattern") or "*").strip() or "*"
        limit = self._limit(args)
        max_depth = _int(args.get("max_depth"), 8, 1, 20)
        root = self.root()
        base_depth = len(start.relative_to(root).parts)
        results = []
        for dirpath, dirnames, filenames in os.walk(start):
            depth = len(Path(dirpath).relative_to(root).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not (Path(dirpath) / d).is_symlink()
                    and not self._child_denied(Path(dirpath) / d, root)
                )
            for name in sorted(filenames):
                child = Path(dirpath) / name
                if child.is_symlink() or self._child_denied(child, root):
                    continue
                child_rel = child.relative_to(root).as_posix()
                if not (fnmatch.fnmatch(child_rel, pattern) or fnmatch.fnmatch(name, pattern)):
                    continue
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
                results.append({"path": child_rel, "size": size})
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        return {"ok": True, "path": rel, "matches": results, "truncated": len(results) >= limit}

    def _grep(self, args: dict) -> dict:
        start, rel = self.resolve(args.get("path"))
        if not start.is_dir():
            raise FsError(f"不是目录: {rel}")
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise FsError("grep 需要 pattern")
        try:
            rx = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as e:
            raise FsError(f"正则无效: {e}") from e
        glob = str(args.get("glob") or "*").strip() or "*"
        limit = self._limit(args, 100)
        max_depth = _int(args.get("max_depth"), 8, 1, 20)
        max_files = _int(self.config.get("max_scan_files"), 2000, 1, 20000)
        max_bytes = _int(self.config.get("max_read_bytes"), 262144, 1024, 8 * 1024 * 1024)
        root = self.root()
        base_depth = len(start.relative_to(root).parts)
        matches = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(start):
            depth = len(Path(dirpath).relative_to(root).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not (Path(dirpath) / d).is_symlink()
                    and not self._child_denied(Path(dirpath) / d, root)
                )
            for name in sorted(filenames):
                if scanned >= max_files or len(matches) >= limit:
                    break
                child = Path(dirpath) / name
                if child.is_symlink() or self._child_denied(child, root):
                    continue
                child_rel = child.relative_to(root).as_posix()
                if not (fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(child_rel, glob)):
                    continue
                try:
                    if child.stat().st_size > max_bytes:
                        continue
                    raw = child.read_bytes()
                except OSError:
                    continue
                if b"\x00" in raw[:8192]:
                    continue
                scanned += 1
                text, _ = self._decode(raw)
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        matches.append({"path": child_rel, "line": i, "text": line.strip()[:300]})
                        if len(matches) >= limit:
                            break
            if scanned >= max_files or len(matches) >= limit:
                break
        return {
            "ok": True,
            "path": rel,
            "matches": matches,
            "scanned": scanned,
            "truncated": len(matches) >= limit or scanned >= max_files,
        }


class FsWrite(FsBase):
    name = "fs_write"
    description = (
        "修改本机文件：write 写入（已存在需 overwrite=true）、append 追加、replace 替换文本、"
        "mkdir 建目录、copy 复制、move 移动。不能删除文件；只能操作权限根目录内的路径。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["write", "append", "replace", "mkdir", "copy", "move"],
                "description": "要执行的操作",
            },
            "path": {"type": "string", "description": "目标文件/目录（相对根目录）"},
            "content": {"type": "string", "description": "write/append 写入的文本"},
            "old": {"type": "string", "description": "replace 要查找的原文"},
            "new": {"type": "string", "description": "replace 替换成的新文本"},
            "count": {"type": "integer", "description": "replace 最多替换几处，默认 1"},
            "src": {"type": "string", "description": "copy/move 的源路径"},
            "dst": {"type": "string", "description": "copy/move 的目标路径"},
            "overwrite": {"type": "boolean", "description": "目标已存在时是否覆盖，默认 false"},
            "recursive": {"type": "boolean", "description": "copy 目录时是否递归，默认 false"},
        },
        "required": ["action"],
    }
    min_level = 4
    config_owner = False
    toggleable = False

    async def run(self, args: dict, ctx: dict) -> dict:
        try:
            action = str(args.get("action") or "").strip().lower()
            if action == "write":
                return self._write(args)
            if action == "append":
                return self._append(args)
            if action == "replace":
                return self._replace(args)
            if action == "mkdir":
                return self._mkdir(args)
            if action == "copy":
                return self._copy(args)
            if action == "move":
                return self._move(args)
            return {"ok": False, "error": f"未知操作: {action}"}
        except (FsError, OSError, ValueError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}

    def _write(self, args: dict) -> dict:
        p, rel = self.resolve(args.get("path"))
        if p.is_dir():
            raise FsError(f"目标是目录: {rel}")
        overwrite = bool(args.get("overwrite"))
        if p.exists() and not overwrite:
            raise FsError("文件已存在（需要覆盖请传 overwrite=true）")
        content = to_str(args.get("content") or "")
        self._write_text(p, content)
        return {"ok": True, "path": rel, "bytes": len(content.encode("utf-8")), "overwritten": overwrite}

    def _append(self, args: dict) -> dict:
        p, rel = self.resolve(args.get("path"))
        if p.exists() and p.is_dir():
            raise FsError(f"目标是目录: {rel}")
        content = to_str(args.get("content") or "")
        data = content.encode("utf-8")
        room = _int(self.config.get("max_write_bytes"), 1048576, 1024, 32 * 1024 * 1024)
        if p.exists() and p.stat().st_size + len(data) > room:
            raise FsError(f"追加后超过上限 {room} 字节")
        if len(data) > room:
            raise FsError(f"内容过大（{len(data)} > {room} 字节）")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("ab") as f:
            f.write(data)
        return {"ok": True, "path": rel, "bytes": len(data)}

    def _replace(self, args: dict) -> dict:
        p, rel = self.resolve(args.get("path"))
        if not p.exists():
            raise FsError(f"文件不存在: {rel}")
        if not p.is_file():
            raise FsError(f"不是文件: {rel}")
        old = to_str(args.get("old") or "")
        if not old:
            raise FsError("replace 需要 old")
        new = to_str(args.get("new") or "")
        count = _int(args.get("count"), 1, 1, 1000)
        raw, truncated = self._read_bytes(p)
        if truncated:
            raise FsError("文件过大，无法整体替换")
        text, _enc = self._decode(raw)
        if old not in text:
            raise FsError("未找到要替换的文本")
        matches = text.count(old)
        updated = text.replace(old, new, count)
        self._write_text(p, updated, bom=raw.startswith(codecs.BOM_UTF8))
        return {"ok": True, "path": rel, "replaced": min(matches, count), "matches": matches}

    def _mkdir(self, args: dict) -> dict:
        p, rel = self.resolve(args.get("path"))
        if p.exists() and not p.is_dir():
            raise FsError(f"已存在同名文件: {rel}")
        existed = p.exists()
        p.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": rel, "created": not existed}

    def _guard_tree(self, src: Path, dst: Path) -> None:
        if src == self.root():
            raise FsError("不能对根目录执行复制/移动")
        try:
            dst.relative_to(src)
        except ValueError:
            return
        raise FsError("目标不能位于源目录内")

    def _copy(self, args: dict) -> dict:
        src, src_rel = self.resolve(args.get("src"))
        dst, dst_rel = self.resolve(args.get("dst"))
        if not src.exists():
            raise FsError(f"源路径不存在: {src_rel}")
        if src.is_symlink():
            raise FsError(f"源是符号链接: {src_rel}")
        if dst.is_dir():
            dst = dst / src.name
            dst_rel = f"{dst_rel.rstrip('/')}/{src.name}"
            self._check_denied(dst, dst_rel)
        self._guard_tree(src, dst)
        overwrite = bool(args.get("overwrite"))
        if dst.exists() and not overwrite:
            raise FsError("目标已存在（需要覆盖请传 overwrite=true）")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if not bool(args.get("recursive")):
                raise FsError("复制目录需要 recursive=true")
            if dst.exists() and overwrite:
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return {"ok": True, "src": src_rel, "dst": dst_rel, "type": "dir" if src.is_dir() else "file"}

    def _move(self, args: dict) -> dict:
        src, src_rel = self.resolve(args.get("src"))
        dst, dst_rel = self.resolve(args.get("dst"))
        if not src.exists():
            raise FsError(f"源路径不存在: {src_rel}")
        if src.is_symlink():
            raise FsError(f"源是符号链接: {src_rel}")
        if dst.is_dir():
            dst = dst / src.name
            dst_rel = f"{dst_rel.rstrip('/')}/{src.name}"
            self._check_denied(dst, dst_rel)
        self._guard_tree(src, dst)
        overwrite = bool(args.get("overwrite"))
        if dst.exists() and not overwrite:
            raise FsError("目标已存在（需要覆盖请传 overwrite=true）")
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {"ok": True, "src": src_rel, "dst": dst_rel}


def get_tools(rt) -> list[Tool]:
    return [FsRead(rt), FsWrite(rt)]
