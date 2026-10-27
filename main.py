"""GGBot 入口：组装并启动 QQ 网关、Presence 状态机、Web 面板。

用法：
    .venv\\Scripts\\python main.py            # 正常启动（QQ + Web）
    .venv\\Scripts\\python main.py --no-web   # 仅机器人
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

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
    parser = argparse.ArgumentParser(description="GGBot QQ 群聊机器人")
    parser.add_argument("--no-web", action="store_true", help="不启动 Web 控制面板")
    parser.add_argument("--config", default=None, help="config.json 路径")
    parser.add_argument("--log-level", default=None, help="日志等级 DEBUG/INFO/WARNING/ERROR")
    args = parser.parse_args()

    cfg = Config(args.config)
    log_level = args.log_level or os.environ.get("LOG_LEVEL") or (cfg.web_cfg or {}).get("log_level", "INFO")
    setup_logging(cfg.log_file, log_level)
    log = logging.getLogger("ggbot.main")
    log.info("日志系统就绪: %s (level=%s)", cfg.log_file, log_level)

    cfg = Config(args.config)
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

    web_task = None
    if not args.no_web and (cfg.web_cfg or {}).get("enable", True):
        from ggbot.web import start_web

        web_task = asyncio.create_task(start_web(cfg, storage, memory, presence))

    try:
        log.info("连接 QQ gateway（appid=%s）…", cfg.qq.get("appid"))
        await bot.start(cfg.qq["appid"], cfg.qq["secret"])
    finally:
        await presence.stop()
        if web_task:
            web_task.cancel()
        storage.close()


if __name__ == "__main__":
    asyncio.run(main())
