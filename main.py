"""GGBot 入口：组装并启动 QQ 网关、Presence 状态机、Web 面板。

直接运行：
    .venv\\Scripts\\python main.py            # 正常启动
    .venv\\Scripts\\python main.py --reset-chat   # 清理聊天数据（旧污染/翻旧账来源），再正常启动

全部行为由 db/config.json 驱动（web.enable 控制 Web 面板开关）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import botpy

from ggbot.core.config import Config
from ggbot.core.presence import PresenceManager
from ggbot.core.storage import Storage
from tool import ToolExecutor
from ggbot.llm.llm import LLMClient
from ggbot.llm.memory import MemoryStore
from ggbot.qq.bot import QQBot
from ggbot.util.logging_setup import setup_logging

ROOT_DIR = Path(__file__).resolve().parent


def _rel(p: Path | str) -> str:
    """项目内路径显示为相对路径，便于日志阅读。"""
    try:
        return Path(p).resolve().relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return str(p)


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


def _backup_db(db_path: Path) -> Path | None:
    """重置前用 SQLite 在线备份 API 生成快照（包含 WAL 中的最新数据）。"""
    import sqlite3
    import time as _time

    if not Path(db_path).exists():
        return None
    bak = Path(db_path).with_name(f"{Path(db_path).name}.bak-{_time.strftime('%Y%m%d_%H%M%S')}")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(bak)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return bak


def _wipe_db(db_path: Path) -> None:
    """删除数据库及 WAL/SHM（调用前需保证没有其他进程在使用）。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        try:
            p.unlink()
        except FileNotFoundError:
            pass


async def main(cfg: Config | None = None) -> None:
    cfg = cfg or Config()
    if not (cfg.qq["appid"] and cfg.qq["secret"]):
        raise RuntimeError("缺少 QQ 凭据：请在 db/config.json 填写 appid / appsecret")
    lock_path = Path(cfg.db_path).parent / ".bot.lock"
    _acquire_singleton(lock_path)
    log_level = getattr(cfg, "_log_level", None) or (cfg.web_cfg or {}).get("log_level", "INFO")
    setup_logging(cfg.log_file, log_level)
    log = logging.getLogger("ggbot.main")
    log.info("日志就绪: %s (level=%s)", _rel(cfg.log_file), log_level)

    if getattr(cfg, "_reset_db", False):
        bak = _backup_db(Path(cfg.db_path))
        _wipe_db(Path(cfg.db_path))
        log.info("数据库已重置（聊天/记忆全部清空）%s",
                 f"，备份: {_rel(bak)}" if bak else "，原库不存在")

    storage = Storage(cfg.db_path)
    log.info("存储就绪: %s", _rel(cfg.db_path))

    if getattr(cfg, "_reset_chat", False):
        n = storage.reset_chat_data()
        log.info("聊天数据已清理: messages=%d sessions=%d（记忆保留）", n["messages"], n["sessions"])

    memory = MemoryStore(cfg, storage)
    if memory.enabled:
        log.info("记忆就绪: %s @ %s", memory.model, memory.base_url)
    else:
        log.info("记忆就绪: 关键词模式（未配置 embedding，可用 embedding.enabled 开启向量检索）")

    llm = LLMClient(cfg)
    log.info("LLM 就绪: %s @ %s", cfg.model, cfg.llm.get("base_url"))
    vision = cfg.vision
    if vision["enabled"]:
        log.info("识图就绪: %s @ %s", vision["model"], vision["base_url"])
    else:
        log.info("识图未启用: 图片只保留 [图片] 占位（检查 vision 配置）")

    intents = botpy.Intents(public_messages=True, public_guild_messages=True, message_audit=True)
    bot = QQBot(cfg, presence=None, intents=intents)
    executor = ToolExecutor(cfg, storage, memory, bot)
    presence = PresenceManager(cfg, storage, llm, executor, bot, memory)
    bot.presence = presence

    await presence.start()

    async def _temp_cleanup_loop():
        """temp 目录过期文件清理（storage.temp_ttl_hours，默认 24h），每小时巡检一次。"""
        while True:
            try:
                bot.cleanup_temp()
            except Exception as e:  # noqa: BLE001
                log.warning("Temp 清理失败: %s", e)
            await asyncio.sleep(3600)

    cleanup_task = asyncio.create_task(_temp_cleanup_loop())

    web_task = None
    if (cfg.web_cfg or {}).get("enable", True):
        from ggbot.web import start_web

        web_task = asyncio.create_task(start_web(cfg, storage, memory, presence))

    try:
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
    parser = argparse.ArgumentParser(description="GGBot")
    parser.add_argument("--reset-chat", action="store_true",
                        help="清理 messages/sessions 聊天数据（保留记忆）后启动")
    parser.add_argument("--reset-db", action="store_true",
                        help="备份后删除整个数据库（聊天/记忆全部清空）再启动")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None,
                        help="覆盖 config web.log_level（start 脚本默认 DEBUG）")
    args = parser.parse_args()

    async def _run() -> None:
        cfg = Config()
        cfg._reset_chat = args.reset_chat
        cfg._reset_db = args.reset_db
        cfg._log_level = args.log_level
        await main(cfg)

    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        print(f"[GGBot] 启动失败: {exc}")
        sys.exit(1)
