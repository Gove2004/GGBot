"""bash：归属等级（L5）专属的 shell 通道，用来查服务器状态、跑运维命令。

边界（这是一条真的能在宿主机上执行命令的路，收紧的地方都写在这里）：
- 门槛 L5（归属，即机器人主人）；执行器按本轮触发者校验，别人叫模型跑命令一律拒绝。
  和 admin_manage 同一口径：schema 照样下发给所有会话（前缀缓存要一致），拒绝发生在执行期。
- 超时连整个进程组一起收掉：bash 跑管道时会 fork 孙进程，只杀 bash 会留下野进程。
- 输出截断后才回给模型；配置里的真实密钥值先替换成 ***——输出会进上下文、可能进正文与日志，
  「密钥不外发」这条不能因为多了个 shell 就破掉。脱敏只覆盖本项目配置里的密钥，
  宿主机上别的东西（~/.ssh、环境变量）管不到，所以这个工具只给归属等级。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess

from ..base import Tool
from ggbot.core.auth import LEVEL_MAX
from ggbot.core.config import BASE_DIR, SECRET_KEYS
from ggbot.util.logging_setup import clip

logger = logging.getLogger("ggbot.tools.bash")

DEFAULT_CONFIG = {
    "enabled": True,
    "timeout_sec": 60,        # 一条命令最多跑这么久，超时杀进程组
    "max_output_chars": 4000,  # stdout / stderr 各自的上限，超了截断
    "work_dir": "",           # 留空 = 项目根目录
}

MIN_SECRET_LEN = 6  # 太短的值（空串、占位符）替换掉只会把正常输出打得面目全非


def _cap(text: str, n: int) -> str:
    """输出截断不去空白：命令输出里的缩进和换行本身可能就是模型要看的东西。"""
    return text if len(text) <= n else text[: n - 1] + "…"


def _collect_secrets(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in SECRET_KEYS and isinstance(v, str) and len(v) >= MIN_SECRET_LEN:
                out.add(v)
            else:
                _collect_secrets(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_secrets(v, out)


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时收尾：连孙进程一起杀。bash 跑外部命令/管道时会 fork，只杀 bash 的话孙进程还攥着
    输出管道，communicate() 会一直等到它自己跑完（Windows 上实测等满整条命令的时长）。
    POSIX 靠独立进程组，Windows 靠 taskkill /T 顺着父子关系收。
    """
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        return
    with contextlib.suppress(OSError):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    with contextlib.suppress(OSError):
        proc.kill()


def _exec(exe: str, command: str, cwd: str, timeout: float) -> dict:
    """跑一条命令并收干输出；超时把整棵进程树杀掉，不让孙进程留在服务器上。"""
    posix = os.name == "posix"
    proc = subprocess.Popen(
        [exe, "-c", command], cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        start_new_session=posix,  # 自成进程组，超时才杀得干净
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = proc.communicate()
    return {"exit_code": proc.returncode, "stdout": out or "", "stderr": err or "",
            "timed_out": timed_out}


class Bash(Tool):
    name = "bash"
    min_level = LEVEL_MAX
    data_name = "core"
    default_config = DEFAULT_CONFIG
    description = (
        "在机器人所在的宿主机上跑一条 bash 命令，拿回退出码与输出（stdout/stderr）。"
        "查服务状态、看磁盘与日志、跑运维脚本这类事用它，默认工作目录是项目根目录。"
        "只有归属等级（L5，机器人主人）能用：别人叫你跑命令会被执行器直接拒绝，别替他们试。"
        "命令要非交互（不会停下来等输入），一次只做一件事；"
        "输出太长会被截断，要看的部分自己用 grep/tail 收窄。"
        "破坏性操作（删数据、改配置、重启服务）先跟主人确认清楚再跑，别自作主张"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 bash 命令（非交互）"},
        },
        "required": ["command"],
    }

    def _secrets(self) -> set[str]:
        """本项目配置里真实的密钥值：主配置 + 各 toolset 的 config.json。"""
        out: set[str] = set()
        _collect_secrets(getattr(self.rt.cfg, "data", {}), out)
        root = self.rt.tool_root
        if not root.is_dir():
            return out
        for d in sorted(root.iterdir()):
            p = d / "config.json"
            if not d.is_dir() or not p.is_file():
                continue
            try:
                _collect_secrets(json.loads(p.read_text(encoding="utf-8")), out)
            except (OSError, ValueError):
                continue
        return out

    def _mask(self, text: str, secrets: set[str]) -> str:
        for s in sorted(secrets, key=len, reverse=True):
            text = text.replace(s, "***")
        return text

    async def run(self, args: dict, ctx: dict) -> dict:
        command = str(args.get("command") or "").strip()
        if not command:
            return {"ok": False, "error": "command 不能为空"}
        exe = shutil.which("bash")
        if not exe:
            return {"ok": False, "error": "这台机器上找不到 bash（Windows 需要装 Git Bash 并放进 PATH）"}
        cfg = self.config
        timeout = max(1.0, float(cfg.get("timeout_sec", 60) or 60))
        cwd = str(cfg.get("work_dir") or "").strip() or str(BASE_DIR)
        if not os.path.isdir(cwd):
            return {"ok": False, "error": f"工作目录不存在：{cwd}（改 tool/core/config.json 的 work_dir）"}
        try:
            res = await asyncio.to_thread(self._exec_and_mask, exe, command, cwd, timeout)
        except OSError as e:
            logger.warning("bash 执行失败: %s", clip(str(e), 80))
            return {"ok": False, "error": f"命令没能跑起来: {e}"}
        if res["timed_out"]:
            res["error"] = f"命令超时（{timeout:.0f}s），进程树已终止；输出是超时前收到的那部分"
        res["ok"] = not res["timed_out"] and res["exit_code"] == 0
        res["cwd"] = cwd
        return res

    def _exec_and_mask(self, exe: str, command: str, cwd: str, timeout: float) -> dict:
        res = _exec(exe, command, cwd, timeout)
        secrets = self._secrets()
        cap = max(200, int(self.config.get("max_output_chars", 4000) or 4000))
        for k in ("stdout", "stderr"):
            res[k] = _cap(self._mask(res[k], secrets), cap)
        return res


def get_tools(rt):
    return [Bash(rt)]