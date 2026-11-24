"""Message and command handlers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

import config
from backups import BackupError, BackupManager
from compactor import Compactor
from memory import Memory
from router import Router
from streamer import TelegramStreamer

log = logging.getLogger(__name__)

USER_HELP = """👋 سلام!
هر سؤالی داری همین‌جا بپرس، جواب می‌دم.

دستورها:
/reset — پاک کردن حافظهٔ گفتگو
/help — همین راهنما"""

ADMIN_HELP = """

🛠 پنل ادمین:
/stats — آمار مصرف و حافظه
/compact — خلاصه‌سازی دستی
/backup — بکاپ روی گیت‌هاب
/backups — لیست بکاپ‌ها
/restore — بازگردانی آخرین بکاپ
/ping — تست سرعت مدل
/id — آیدی"""

RATE_WARNING = "یه کم آرومتر 🙂"


class BotHandlers:
    def __init__(
        self,
        client: Client,
        memory: Memory,
        router: Router,
        compactor: Compactor,
        backups: BackupManager,
        username: str,
    ) -> None:
        self.client = client
        self.memory = memory
        self.router = router
        self.compactor = compactor
        self.backups = backups
        self.username = username
        self._tasks: dict[int, asyncio.Task] = {}
        self._seen: set[tuple[int, int]] = set()
        self._seen_order: deque[tuple[int, int]] = deque()
        self._rate: dict[int, deque[float]] = {}
        self._rate_warned: dict[int, float] = {}
        self._mention = re.compile(re.escape(f"@{username}"), re.IGNORECASE) if username else None
        self._restore_until = 0.0
        self.handled = 0

    def register(self, app: Client) -> None:
        app.add_handler(MessageHandler(self.on_private_message, filters.private))

    # ------------------------------------------------------------------ filter
    async def on_private_message(self, client: Client, message: Message) -> None:
        if config.DEBUG:
            log.info(
                "update: chat=%s type=%s user=%s text=%r",
                message.chat.id,
                message.chat.type,
                message.from_user.id if message.from_user else None,
                (message.text or "")[:40],
            )
        if not self._eligible(message):
            return
        if not self._remember(message):
            return
        self.handled += 1

        text = (message.text or "").strip()
        if not text:
            return

        if text.startswith("/"):
            await self._command(client, message, text)
            return

        user_id = message.from_user.id
        allowed, warn = self._rate_check(user_id)
        if not allowed:
            if warn:
                await self._reply(message, RATE_WARNING)
            return

        cleaned = self._strip_mention(text)
        if not cleaned:
            return

        await self._cancel(message.chat.id)
        task = asyncio.create_task(self._answer(client, message, cleaned))
        self._tasks[message.chat.id] = task
        task.add_done_callback(
            lambda t, cid=message.chat.id: self._tasks.pop(cid, None)
            if self._tasks.get(cid) is t
            else None
        )

    def _eligible(self, message: Message) -> bool:
        if message.chat.type != ChatType.PRIVATE:
            return False
        if message.outgoing or message.service:
            return False
        user = message.from_user
        if user is None or user.is_bot or user.is_self:
            return False
        if message.sender_chat is not None or message.via_bot is not None:
            return False
        return bool((message.text or "").strip())

    def _remember(self, message: Message) -> bool:
        key = (message.chat.id, message.id)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > 4000:
            self._seen.discard(self._seen_order.popleft())
        return True

    def _rate_check(self, user_id: int) -> tuple[bool, bool]:
        """Return (allowed, should_warn)."""
        now = time.monotonic()
        window = self._rate.setdefault(user_id, deque())
        while window and now - window[0] > config.RATE_WINDOW:
            window.popleft()
        if len(window) < config.RATE_MAX_MESSAGES:
            window.append(now)
            return True, False
        if now - self._rate_warned.get(user_id, 0.0) > 8.0:
            self._rate_warned[user_id] = now
            return False, True
        return False, False

    def _strip_mention(self, text: str) -> str:
        if self._mention:
            text = self._mention.sub(" ", text)
        return " ".join(text.split())

    # ------------------------------------------------------------------ answer
    async def _answer(self, client: Client, message: Message, text: str) -> None:
        chat_id = message.chat.id
        started = time.perf_counter()
        msg_id = self.memory.add_message(chat_id, "user", text)
        streamer = TelegramStreamer(client, chat_id)
        request_messages = self._build_messages(chat_id)

        try:
            answer = await streamer.run(self.router.stream(request_messages))
        except asyncio.CancelledError:
            self.memory.delete_message(msg_id)
            raise
        except Exception:
            self.memory.delete_message(msg_id)
            log.warning("answer failed in chat %s", chat_id, exc_info=True)
            return

        if not answer.strip():
            self.memory.delete_message(msg_id)
            if not streamer.error:
                await self._reply(message, "⚠️ پاسخی تولید نشد. دوباره بفرست.")
            return

        self.memory.add_message(chat_id, "assistant", answer)

        usage = streamer.usage or {}
        details = usage.get("completion_tokens_details") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or estimate_tokens(request_messages))
        completion_tokens = int(usage.get("completion_tokens") or estimate_tokens(answer))
        reasoning_tokens = int(
            usage.get("reasoning_tokens") or details.get("reasoning_tokens") or streamer.reasoning_chars // 3
        )

        self.memory.bump_stats(
            chat_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        self.compactor.schedule(chat_id)

    def _build_messages(self, chat_id: int) -> list[dict[str, str]]:
        summary, recent = self.memory.get_context(chat_id, config.CONTEXT_MAX_CHARS)
        messages: list[dict[str, str]] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
        if summary:
            messages.append(
                {"role": "system", "content": "خلاصهٔ گفتگوهای قبلی (برای زمینه):\n" + summary}
            )
        messages.extend(recent)
        return messages

    async def _cancel(self, chat_id: int) -> None:
        task = self._tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _cancel_all(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ----------------------------------------------------------------- commands
    async def _command(self, client: Client, message: Message, text: str) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0][1:].split("@", 1)[0].lower()
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        is_admin = message.from_user.id in config.ADMIN_IDS

        if command in ("start", "help"):
            await self._reply(message, USER_HELP + (ADMIN_HELP if is_admin else ""))
        elif command == "reset":
            await self._cancel(message.chat.id)
            self.memory.clear(message.chat.id)
            await self._reply(message, "🧹 حافظهٔ این گفتگو پاک شد.")
        elif command == "id":
            await self._reply(
                message,
                f"🪪 آیدی تو: `{message.from_user.id}`\nآیدی چت: `{message.chat.id}`",
            )
        elif command not in ("stats", "compact", "backup", "backups", "restore", "ping"):
            await self._reply(message, "دستور ناشناخته. /help")
        elif not is_admin:
            await self._reply(message, "این دستور فقط برای ادمین است.")
        elif command == "stats":
            await self._stats(message)
        elif command == "compact":
            await self._compact(message)
        elif command == "backup":
            await self._backup(message)
        elif command == "backups":
            await self._backups(message)
        elif command == "restore":
            await self._restore(message, arg)
        elif command == "ping":
            await self._ping(message)

    async def _stats(self, message: Message) -> None:
        chat_id = message.chat.id
        mine = self.memory.stats(chat_id)
        total = self.memory.global_stats()
        summary = self.memory.summary(chat_id)
        kb = total["bytes"] / 1024
        await self._reply(
            message,
            (
                f"📊 آمار این چت\n"
                f"پیام‌ها: {self.memory.count(chat_id)}\n"
                f"درخواست‌ها: {mine['requests']}\n"
                f"توکن ورودی: {mine['prompt_tokens']:,}\n"
                f"توکن خروجی: {mine['completion_tokens']:,} "
                f"(فکر کردن: {mine['reasoning_tokens']:,})\n"
                f"آخرین پاسخ: {mine['last_latency_ms']:,} ms\n"
                f"خلاصه: {len(summary):,} کاراکتر\n\n"
                f"🌍 کل ربات\n"
                f"چت‌ها: {total['chats']:,}\n"
                f"پیام‌ها: {total['messages']:,}\n"
                f"حجم دیتابیس: {kb:.1f} KB\n"
                f"مدل: {config.MODEL}\n"
                f"بکاپ گیت‌هاب: {'فعال' if self.backups.enabled else 'غیرفعال'}"
            ),
        )

    async def _compact(self, message: Message) -> None:
        result = await self.compactor.compact(message.chat.id, force=True)
        if not result:
            await self._reply(message, "برای خلاصه‌سازی، پیام کافی وجود ندارد.")
            return
        await self._reply(
            message,
            (
                f"✅ خلاصه‌سازی انجام شد.\n"
                f"پیام‌ها: {result['before']} → {result['after']}\n"
                f"خلاصه: {result['summary_chars']:,} کاراکتر"
            ),
        )

    async def _backup(self, message: Message) -> None:
        if not self.backups.enabled:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = Path(config.DB_PATH).parent / f"backup-{stamp}.db"
            try:
                self.memory.backup(path)
                await self.client.send_document(
                    message.chat.id,
                    str(path),
                    caption=f"💾 بکاپ دیتابیس — {stamp}",
                )
            except Exception:
                log.exception("backup failed")
                await self._reply(message, "⚠️ بکاپ گرفتن نشد.")
            finally:
                path.unlink(missing_ok=True)
            return

        await self._reply(message, "⏳ در حال ساخت و آپلود بکاپ…")
        try:
            info = await self.backups.create()
        except BackupError as exc:
            log.warning("github backup failed: %s", exc)
            await self._reply(message, f"⚠️ بکاپ نشد: {exc}")
            return
        except Exception:
            log.exception("github backup failed")
            await self._reply(message, "⚠️ بکاپ گیت‌هاب ناموفق بود.")
            return

        await self._reply(
            message,
            (
                f"✅ بکاپ ساخته شد\n"
                f"🏷 {info.tag}\n"
                f"📦 {info.size / 1024:.1f} KB (رمزنگاری‌شده)\n"
                f"🔗 {info.url}"
            ),
        )

    async def _backups(self, message: Message) -> None:
        if not self.backups.enabled:
            await self._reply(message, "بکاپ گیت‌هاب تنظیم نشده.")
            return
        try:
            infos = await self.backups.list(10)
        except BackupError as exc:
            await self._reply(message, f"⚠️ {exc}")
            return
        except Exception:
            log.exception("listing backups failed")
            await self._reply(message, "⚠️ گرفتن لیست بکاپ‌ها نشد.")
            return
        if not infos:
            await self._reply(message, "هنوز هیچ بکاپی روی گیت‌هاب نیست.")
            return
        lines = ["📦 آخرین بکاپ‌ها:"]
        for info in infos:
            stamp = info.tag.removeprefix("backup-")
            lines.append(f"• {stamp} — {info.size / 1024:.1f} KB")
        lines.append(f"\n🔗 {infos[0].url}")
        await self._reply(message, "\n".join(lines))

    async def _restore(self, message: Message, arg: str) -> None:
        if not self.backups.enabled:
            await self._reply(message, "بکاپ گیت‌هاب تنظیم نشده.")
            return
        if arg != "confirm":
            self._restore_until = time.monotonic() + 120
            await self._reply(
                message,
                (
                    "⚠️ این کار دیتابیس فعلی را با آخرین بکاپ گیت‌هاب جایگزین می‌کند.\n"
                    "قبل از جایگزینی، از دیتابیس فعلی هم یک نسخهٔ موقت گرفته می‌شود.\n"
                    "برای تأیید تا ۲ دقیقه: /restore confirm"
                ),
            )
            return
        if time.monotonic() > self._restore_until:
            await self._reply(message, "⌛️ زمان تأیید تمام شد. دوباره /restore را بزن.")
            return
        self._restore_until = 0.0

        await self._reply(message, "⏳ در حال دانلود و بازگردانی…")
        await self._cancel_all()
        self.compactor.cancel_pending()
        try:
            result = await self.backups.restore_latest()
        except BackupError as exc:
            log.warning("restore failed: %s", exc)
            await self._reply(message, f"⚠️ بازگردانی نشد: {exc}")
            return
        except Exception:
            log.exception("restore failed")
            await self._reply(message, "⚠️ بازگردانی ناموفق بود.")
            return

        await self._reply(
            message,
            (
                f"✅ بازگردانی انجام شد از {result['tag']}\n"
                f"چت‌ها: {result['chats']:,}\n"
                f"پیام‌ها: {result['messages']:,}\n"
                f"نسخهٔ قبلی: {Path(result['pre_restore']).name}"
            ),
        )

    async def _ping(self, message: Message) -> None:
        try:
            ms = await asyncio.wait_for(self.router.ping(), timeout=25)
            await self._reply(message, f"⏱ اولین توکن: {ms:,.0f} ms")
        except asyncio.TimeoutError:
            await self._reply(message, "⚠️ مدل در زمان مقرر جواب نداد.")
        except Exception:
            log.warning("ping failed", exc_info=True)
            await self._reply(message, "⚠️ مدل جواب نداد.")

    # -------------------------------------------------------------------- utils
    async def _reply(self, message: Message, text: str) -> None:
        try:
            await message.reply(text, parse_mode=None)
        except Exception:
            log.warning("reply failed in chat %s", message.chat.id, exc_info=True)


def estimate_tokens(messages: list[dict[str, str]] | str) -> int:
    if isinstance(messages, str):
        return max(1, len(messages) // 3)
    return max(1, sum(len(m.get("content", "")) for m in messages) // 3)