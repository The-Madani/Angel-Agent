"""Entrypoint: Angel AI Telegram bot backed by 9router."""

from __future__ import annotations

import asyncio
import logging
import signal

import uvloop
from pyrogram import Client

import config
from backups import BackupError, BackupManager
from compactor import Compactor
from handlers import BotHandlers
from memory import Memory
from router import Router

log = logging.getLogger("angel")


async def auto_backup(manager: BackupManager, stop: asyncio.Event) -> None:
    hours = config.BACKUP_AUTO_HOURS
    if hours <= 0 or not manager.enabled:
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=hours * 3600)
            return
        except asyncio.TimeoutError:
            pass
        try:
            info = await manager.create()
            log.info("auto backup created: %s (%s bytes)", info.tag, info.size)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auto backup failed")


async def main() -> None:
    config.validate()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("pyrogram").setLevel(logging.DEBUG if config.DEBUG else logging.INFO)

    router = Router()
    await router.start()
    memory = Memory(config.DB_PATH)
    compactor = Compactor(memory, router)
    backups = BackupManager(memory)
    try:
        await backups.start()
    except Exception:
        log.exception("GitHub backups are disabled (check the backup settings)")

    app = Client(
        "angel-ai",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        in_memory=True,
        workers=8,
    )

    await app.start()
    me = await app.get_me()
    handlers = BotHandlers(app, memory, router, compactor, backups, me.username or "")
    handlers.register(app)

    if config.DEBUG:
        from pyrogram.handlers import RawUpdateHandler

        async def _raw(client, update, users, chats):  # pragma: no cover - debug
            log.info("RAW update: %s", type(update).__name__)

        app.add_handler(RawUpdateHandler(_raw))
    log.info("bot @%s started | model=%s | db=%s", me.username, config.MODEL, config.DB_PATH)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    async def watchdog(handlers: BotHandlers) -> None:
        """Keep the MTProto link healthy and prove it in the logs."""
        last = handlers.handled
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=180)
                return
            except asyncio.TimeoutError:
                pass

            healthy = app.is_connected
            if healthy:
                try:
                    await asyncio.wait_for(app.get_me(), timeout=15)
                except Exception:
                    log.exception("health check failed")
                    healthy = False

            if not healthy:
                log.warning("client unhealthy, reconnecting…")
                try:
                    await app.stop()
                    await app.start()
                    log.info("reconnected")
                except Exception:
                    log.exception("reconnect failed; will retry in 3m")

            log.info(
                "watchdog: connected=%s handled=%s (+%s in last 3m)",
                app.is_connected,
                handlers.handled,
                handlers.handled - last,
            )
            last = handlers.handled

    watcher = asyncio.create_task(watchdog(handlers))
    backup_task = asyncio.create_task(auto_backup(backups, stop))
    await stop.wait()
    watcher.cancel()
    backup_task.cancel()
    await asyncio.gather(watcher, backup_task, return_exceptions=True)
    log.info("shutting down")

    await app.stop()
    await router.close()
    await backups.close()
    memory.close()


if __name__ == "__main__":
    uvloop.run(main())