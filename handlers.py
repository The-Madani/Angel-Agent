"""Message and command handlers."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

import config
import files
import tools
from backups import BackupError, BackupManager
from compactor import Compactor
from memory import Memory
from router import Router
from streamer import TelegramStreamer

log = logging.getLogger(__name__)

USER_HELP = """👋 سلام!
هر سؤالی داری همین‌جا بپرس، جواب می‌دم.

می‌تونی فایل هم بفرستی: PDF، Word، Excel، PowerPoint، فایل متنی/کد و عکس.
اسمش رو هم بنویس چی می‌خوای (مثلاً «خلاصه‌ش کن»).
اگه روی یک پیام (مال خودت یا من) ریپلای کنی، همون پیام رو هم می‌بینم.

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
QUOTE_MAX_CHARS = 20_000
TOOL_RESULT_MAX = 12_000


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
        caption = (message.caption or "").strip()
        if not text and not message.document and not message.photo:
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

        await self._cancel(message.chat.id)
        cleaned = self._strip_mention(text) if text else ""
        if text and not cleaned:
            return
        task = asyncio.create_task(self._respond(client, message, cleaned, caption))
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
        return bool((message.text or "").strip()) or message.document is not None or message.photo is not None

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

    # ------------------------------------------------------------------ media
    async def _respond(
        self, client: Client, message: Message, text: str, caption: str
    ) -> None:
        """Build one model turn from the message, its file, and any quoted reply."""
        quote_label, quote_text, quote_images = await self._quote(client, message.reply_to_message)

        attachment: list[dict] = list(quote_images)
        stored: str | None = None

        if message.document is not None:
            parsed = await self._document(client, message)
            if parsed is None:
                return
            name, body = parsed
            question = text or caption or "این فایل رو بخون و خلاصه‌اش رو بگو."
            text = f"فایل «{name}» رو فرستادم. {question}\n\n--- محتوای فایل ---\n{body}"
        elif message.photo is not None:
            raw = await self._photo_bytes(client, message)
            if raw is None:
                return
            attachment.append(self._image_part(raw))
            question = text or caption or "این عکس رو ببین و بگو چی توش هست."
            stored = f"[عکس] {question}"
            text = question

        if not text.strip():
            return

        if quote_text:
            header = f"[در پاسخ به {quote_label}]:\n«{quote_text}»"
            text = header + "\n\n" + text
            if stored:
                stored = header + "\n\n" + stored

        await self._answer(
            client, message, text, attachment=attachment or None, stored=stored
        )

    async def _quote(self, client: Client, quoted: Message | None) -> tuple[str, str, list[dict]]:
        """Text and images of the message the user replied to (if any)."""
        if quoted is None or getattr(quoted, "empty", False) or quoted.service:
            return "", "", []
        is_bot = bool(quoted.from_user and quoted.from_user.is_self) or quoted.outgoing
        label = "پیام خودم" if is_bot else "پیام خودت"
        body = (quoted.text or quoted.caption or "").strip()
        images: list[dict] = []

        if quoted.document is not None:
            parsed = await self._document(client, quoted, report=False)
            if parsed:
                name, content = parsed
                body = (body + "\n\n" if body else "") + f"[فایل «{name}»]\n{content}"
        elif quoted.photo is not None:
            raw = await self._photo_bytes(client, quoted, report=False)
            if raw:
                images.append(self._image_part(raw))
                body = (body + "\n\n" if body else "") + "[عکس]"

        body = body[:QUOTE_MAX_CHARS].strip()
        if not body and not images:
            return "", "", []
        return label, body, images

    async def _document(
        self, client: Client, message: Message, *, report: bool = True
    ) -> tuple[str, str] | None:
        document = message.document
        name = document.file_name or "file"
        size = document.file_size or 0
        if size > files.MAX_FILE_BYTES:
            if report:
                await self._reply(message, "⚠️ فایل بزرگ‌تر از ۲۰ مگابایته و تلگرام اجازهٔ دانلودش رو نمی‌ده.")
            return None

        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / self._safe_name(name)
                downloaded = await client.download_media(message, file_name=str(path))
                if not downloaded:
                    raise files.FileError("فایل دانلود نشد.")
                body = await asyncio.to_thread(files.extract_text, Path(downloaded), name)
        except files.UnsupportedFile as exc:
            if report:
                await self._reply(message, f"⚠️ {exc}\nفرمت‌های پشتیبانی‌شده: {files.SUPPORTED_HINT}.")
            return None
        except files.FileError as exc:
            if report:
                await self._reply(message, f"⚠️ {exc}")
            return None
        except Exception:
            log.exception("reading document failed in chat %s", message.chat.id)
            if report:
                await self._reply(message, "⚠️ خواندن فایل نشد. یه بار دیگه بفرست.")
            return None
        return name, body

    async def _photo_bytes(
        self, client: Client, message: Message, *, report: bool = True
    ) -> bytes | None:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "photo.jpg"
                downloaded = await client.download_media(message, file_name=str(path))
                if not downloaded:
                    raise files.FileError("عکس دانلود نشد.")
                raw = await asyncio.to_thread(Path(downloaded).read_bytes)
        except files.FileError as exc:
            if report:
                await self._reply(message, f"⚠️ {exc}")
            return None
        except Exception:
            log.exception("downloading photo failed in chat %s", message.chat.id)
            if report:
                await self._reply(message, "⚠️ عکس دانلود نشد. یه بار دیگه بفرست.")
            return None

        if len(raw) > files.MAX_FILE_BYTES:
            if report:
                await self._reply(message, "⚠️ عکس خیلی بزرگه.")
            return None
        return raw

    @staticmethod
    def _image_part(raw: bytes) -> dict:
        return {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()},
        }

    @staticmethod
    def _safe_name(name: str) -> str:
        cleaned = re.sub(r"[^\w.\- ]+", "_", Path(name).name).strip(" .")
        return (cleaned or "file")[:120]

    # ------------------------------------------------------------------ answer
    async def _answer(
        self,
        client: Client,
        message: Message,
        text: str,
        *,
        attachment: list[dict] | None = None,
        stored: str | None = None,
    ) -> None:
        chat_id = message.chat.id
        started = time.perf_counter()
        msg_id = self.memory.add_message(chat_id, "user", stored or text)
        streamer = TelegramStreamer(client, chat_id)
        request_messages = self._build_messages(chat_id, attachment)

        prompt_tokens = 0
        completion_tokens = 0
        reasoning_tokens = 0
        rounds = 0
        saw_usage = False

        try:
            while True:
                allow_tools = bool(config.TOOLS_ENABLED) and rounds < config.TOOL_MAX_ROUNDS
                answer = await streamer.run(
                    self.router.stream(
                        request_messages, tools=tools.SCHEMAS if allow_tools else None
                    ),
                    allow_empty=allow_tools,
                )

                usage = streamer.usage or {}
                if usage:
                    saw_usage = True
                    details = usage.get("completion_tokens_details") or {}
                    prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    completion_tokens += int(usage.get("completion_tokens") or 0)
                    reasoning_tokens += int(
                        usage.get("reasoning_tokens") or details.get("reasoning_tokens") or 0
                    )

                calls = streamer.tool_calls
                streamer.tool_calls = []
                if not calls:
                    break

                rounds += 1
                request_messages = await self._run_tools(request_messages, calls)
                log.info(
                    "tools in chat %s (round %s/%s): %s",
                    chat_id,
                    rounds,
                    config.TOOL_MAX_ROUNDS,
                    ", ".join(
                        tools.describe(
                            c.get("function", {}).get("name", ""),
                            c.get("function", {}).get("arguments", ""),
                        )
                        for c in calls
                    ),
                )
        except asyncio.CancelledError:
            self.memory.delete_message(msg_id)
            raise
        except Exception:
            self.memory.delete_message(msg_id)
            log.warning(
                "answer failed in chat %s after %s tool rounds",
                chat_id,
                rounds,
                exc_info=True,
            )
            return

        if not answer.strip():
            self.memory.delete_message(msg_id)
            if not streamer.error:
                await self._reply(message, "⚠️ پاسخی تولید نشد. دوباره بفرست.")
            return

        self.memory.add_message(chat_id, "assistant", answer)

        if not saw_usage:
            prompt_tokens = estimate_tokens(request_messages)
            completion_tokens = estimate_tokens(answer)
        reasoning_tokens = reasoning_tokens or streamer.reasoning_chars // 3

        self.memory.bump_stats(
            chat_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        self.compactor.schedule(chat_id)

    def _build_messages(
        self, chat_id: int, attachment: list[dict] | None = None
    ) -> list[dict]:
        summary, recent = self.memory.get_context(chat_id, config.CONTEXT_MAX_CHARS)
        messages: list[dict] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
        if summary:
            messages.append(
                {"role": "system", "content": "خلاصهٔ گفتگوهای قبلی (برای زمینه):\n" + summary}
            )
        messages.extend(recent)
        if attachment:
            if messages and messages[-1].get("role") == "user":
                messages[-1] = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": str(messages[-1].get("content") or "")},
                        *attachment,
                    ],
                }
            else:
                messages.append({"role": "user", "content": attachment})
        return messages

    async def _run_tools(self, messages: list[dict], calls: list[dict]) -> list[dict]:
        """Run the tool calls the model asked for and append their results."""
        assistant: dict = {"role": "assistant", "content": None, "tool_calls": []}
        for index, call in enumerate(calls):
            fn = call.get("function") or {}
            assistant["tool_calls"].append(
                {
                    "id": str(call.get("id") or f"call_{index}"),
                    "type": "function",
                    "function": {
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or "{}"),
                    },
                }
            )

        out = [*messages, assistant]
        for call in assistant["tool_calls"]:
            fn = call["function"]
            result = await tools.call(fn["name"], fn["arguments"])
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result[:TOOL_RESULT_MAX],
                }
            )
        return out

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
                f"آرشیو کامل: {total['archived']:,}\n"
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
                f"آرشیو: {result['archived']:,}\n"
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


def estimate_tokens(messages: list[dict] | str) -> int:
    if isinstance(messages, str):
        return max(1, len(messages) // 3)
    return max(1, sum(_content_chars(m.get("content")) for m in messages) // 3)


def _content_chars(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(str(part.get("text") or ""))
            for part in content
            if isinstance(part, dict)
        )
    return 0