"""GGBot 入口：组装并启动 QQ 网关、Presence 状态机、Web 面板。

直接运行：
    .venv\\Scripts\\python main.py

全部行为由 db/config.json 驱动（web.enable 控制 Web 面板开关），无命令行参数。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import botpy

from ggbot.bot import QQBot
from ggbot.config import Config
from ggbot.llm import DeepSeekClient
from ggbot.memory import MemoryStore
from ggbot.presence import PresenceManager
from ggbot.storage import Storage
from ggbot.tools import ToolExecutor
from ggbot.logging_setup import setup_logging


def _pid_alive(pid: int) -> bool:
    """Windows 下探测进程是否存活（不借用 psutil）。"""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:  # noqa: BLE001
        return False


def _acquire_singleton(lock_path: Path) -> None:
    """单实例锁：同一台机器只允许一个 bot 进程（防重复启动导致双写 db/双连网关）。"""
    if lock_path.exists():
        try:
            old = int(lock_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            old = 0
        if old and _pid_alive(old):
            print(f"[GGBot] 已有实例运行（pid={old}），请先停止它再启动。")
            sys.exit(2)
        try:
            lock_path.unlink()
        except OSError:
            pass
    lock_path.write_text(str(os.getpid()), encoding="utf-8")


async def main() -> None:
    cfg = Config()
    lock_path = Path(cfg.db_path).parent / ".bot.lock"
    _acquire_singleton(lock_path)
    log_level = (cfg.web_cfg or {}).get("log_level", "INFO")
    setup_logging(cfg.log_file, log_level)
    log = logging.getLogger("ggbot.main")
    log.info("日志系统就绪: %s (level=%s)", cfg.log_file, log_level)

    storage = Storage(cfg.db_path)
    log.info("存储就绪: %s", cfg.db_path)

    log.info("初始化向量记忆（SiliconFlow Embedding）…")
    memory = MemoryStore(cfg, storage)
    log.info("向量记忆就绪: %s", memory.model)

    llm = DeepSeekClient(cfg)
    log.info("LLM 就绪: %s @ %s", cfg.model, cfg.llm.get("base_url"))

    intents = botpy.Intents(public_messages=True, public_guild_messages=True)
    bot = QQBot(cfg, presence=None, intents=intents)
    executor = ToolExecutor(cfg, storage, memory, bot)
    presence = PresenceManager(cfg, storage, llm, executor, bot)
    bot.presence = presence

    await presence.start()

    async def _temp_cleanup_loop():
        """temp 目录过期文件清理（storage.temp_ttl_hours，默认 24h），每小时巡检一次。"""
        while True:
            try:
                bot.cleanup_temp()
            except Exception as e:  # noqa: BLE001
                log.warning("temp 清理失败: %s", e)
            await asyncio.sleep(3600)

    cleanup_task = asyncio.create_task(_temp_cleanup_loop())

    web_task = None
    if (cfg.web_cfg or {}).get("enable", True):
        from ggbot.web import start_web

        web_task = asyncio.create_task(start_web(cfg, storage, memory, presence))

    try:
        log.info("连接 QQ gateway（appid=%s）…", cfg.qq.get("appid"))
        await bot.start(cfg.qq["appid"], cfg.qq["secret"])
    finally:
        await presence.stop()
        cleanup_task.cancel()
        if web_task:
            web_task.cancel()
        storage.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
