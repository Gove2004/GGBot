"""GGBot 入口：组装并启动 QQ 网关、Presence 状态机、Web 面板。

直接运行：
    .venv\\Scripts\\python main.py

全部行为由 db/config.json 驱动（web.enable 控制 Web 面板开关），无命令行参数。
"""

from __future__ import annotations

import asyncio
import logging

import botpy

from ggbot.bot import QQBot
from ggbot.config import Config
from ggbot.llm import DeepSeekClient
from ggbot.memory import MemoryStore
from ggbot.presence import PresenceManager
from ggbot.storage import Storage
from ggbot.tools import ToolExecutor
from ggbot.logging_setup import setup_logging


async def main() -> None:
    cfg = Config()
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


if __name__ == "__main__":
    asyncio.run(main())
