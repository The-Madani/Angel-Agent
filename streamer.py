"""Telegram-side streaming renderer."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import AsyncIterator

from pyrogram import Client
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.errors import BadRequest, FloodWait, RPCError
from pyrogram.types import LinkPreviewOptions, Message

import config

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

THINKING_TEXT = "در حال فکر کردن…"
ERROR_TEXT = "⚠️ الان نشد جواب بدم. یه بار دیگه بفرست."
CUT_TEXT = "\n\n⚠️ ادامهٔ پاسخ قطع شد."

_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S)
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)


def md_to_html(text: str) -> str:
    stash: dict[str, str] = {}

    def keep(fragment: str) -> str:
        key = f"\x00{len(stash)}\x00"
        stash[key] = fragment
        return key

    text = _CODE_BLOCK.sub(lambda m: keep("<pre>" + html.escape(m.group(1)) + "</pre>"), text)
    text = _INLINE_CODE.sub(lambda m: keep("<code>" + html.escape(m.group(1)) + "</code>"), text)
    text = html.escape(text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _HEADING.sub(r"<b>\1</b>", text)
    for key, fragment in stash.items():
        text = text.replace(key, fragment)
    return text


def split_parts(text: str, limit: int = config.MESSAGE_CHUNK_LIMIT) -> list[str]:
    limit = max(1, limit)
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest.encode("utf-16-le")) // 2 > limit:
        units = 0
        hard_cut = 0
        for index, char in enumerate(rest):
            next_units = units + (2 if ord(char) > 0xFFFF else 1)
            if next_units > limit:
                break
            units = next_units
            hard_cut = index + 1
        cut = rest.rfind("\n", 0, hard_cut)
        if cut < hard_cut // 3:
            cut = rest.rfind(" ", 0, hard_cut)
        if cut < hard_cut // 3:
            boundary = hard_cut
        else:
            boundary = cut + 1
        parts.append(rest[:boundary])
        rest = rest[boundary:]
    if rest:
        parts.append(rest)
    return parts


class TelegramStreamer:
    """Streams one answer into one private chat with minimal API calls."""

    def __init__(self, client: Client, chat_id: int) -> None:
        self.client = client
        self.chat_id = chat_id
        self._chunks: list[str] = []
        self._messages: list[Message] = []
        self._rendered: list[str] = []
        self._last_edit = 0.0
        self._placeholder: Message | None = None
        self._answer_started = False
        self._typing_task: asyncio.Task | None = None
        self._thinking_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self.answer = ""
        self.reasoning_chars = 0
        self.usage: dict | None = None
        self.error: str | None = None
        self.tool_calls: list[dict] = []

    # ------------------------------------------------------------------ public
    async def run(
        self, events: AsyncIterator[tuple[str, object]], *, allow_empty: bool = False
    ) -> str:
        self._stopped.clear()
        self._typing_task = asyncio.create_task(self._typing_loop())
        self._thinking_task = asyncio.create_task(self._thinking_loop())
        started = time.monotonic()
        try:
            await self._typing()
            async for kind, payload in events:
                if kind == "reasoning":
                    self.reasoning_chars += len(str(payload))
                    if (
                        not self._answer_started
                        and self._placeholder is None
                        and time.monotonic() - started >= config.THINKING_HINT_DELAY
                    ):
                        await self._send_placeholder()
                    continue
                if kind == "usage":
                    self.usage = payload if isinstance(payload, dict) else None
                    continue
                if kind == "tool_calls":
                    if isinstance(payload, list):
                        self.tool_calls = payload
                    continue
                if kind == "content":
                    self._chunks.append(str(payload))
                    if not self._answer_started:
                        self._answer_started = True
                        await self._flush()
                        self._last_edit = time.monotonic()
                    elif time.monotonic() - self._last_edit >= config.EDIT_INTERVAL:
                        await self._flush()
                        self._last_edit = time.monotonic()

            if not self._answer_started:
                if allow_empty:
                    return ""
                await self._replace_placeholder(ERROR_TEXT)
                self.error = "empty answer"
                return ""
            await self._flush(final=True)
            return self.answer
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - user-facing guard
            self.error = str(exc)
            log.exception("streaming failed in chat %s", self.chat_id)
            try:
                if self._answer_started and self._messages:
                    partial = self._rendered[-1] + CUT_TEXT
                    await self._edit(self._messages[-1], partial)
                else:
                    await self._replace_placeholder(ERROR_TEXT)
            except Exception:
                log.exception("could not report streaming error in chat %s", self.chat_id)
            raise
        finally:
            self._stopped.set()
            if self._typing_task:
                self._typing_task.cancel()
                await asyncio.gather(self._typing_task, return_exceptions=True)
            if self._thinking_task:
                self._thinking_task.cancel()
                await asyncio.gather(self._thinking_task, return_exceptions=True)

    async def prepare_next_round(self) -> None:
        """Reset model-output state before a follow-up tool round.

        A tool loop uses one Telegram message surface, but every model pass
        must have independent buffers and usage counters. Keep the first
        message so a thinking placeholder or partial answer can be replaced;
        remove additional chunks that may have been sent before the tool call.
        """
        for message in self._messages[1:]:
            try:
                await message.delete()
            except Exception:
                log.debug("could not remove stale streamed chunk in chat %s", self.chat_id)
        self._messages = self._messages[:1]
        self._rendered = self._rendered[:1]
        self._chunks.clear()
        self._answer_started = False
        self._last_edit = 0.0
        self.answer = ""
        self.reasoning_chars = 0
        self.usage = None
        self.error = None
        self.tool_calls = []

    async def show_error(self, text: str = ERROR_TEXT) -> None:
        """Replace a pending thinking hint when the caller has no answer."""
        self.error = "empty answer"
        await self._replace_placeholder(text)

    # ------------------------------------------------------------------ typing
    async def _typing(self) -> None:
        try:
            await self.client.send_chat_action(self.chat_id, ChatAction.TYPING)
        except RPCError:
            pass

    async def _typing_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=config.TYPING_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._typing()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("typing action failed in chat %s", self.chat_id, exc_info=True)

    async def _thinking_loop(self) -> None:
        try:
            await asyncio.sleep(config.THINKING_HINT_DELAY)
            if not self._answer_started and self._placeholder is None:
                await self._send_placeholder()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("thinking hint failed in chat %s", self.chat_id, exc_info=True)

    # --------------------------------------------------------------- rendering
    async def _send_placeholder(self) -> None:
        try:
            placeholder = await self._send(THINKING_TEXT)
            if self._answer_started:
                try:
                    await placeholder.delete()
                except Exception:
                    log.debug("could not remove late thinking hint in chat %s", self.chat_id)
                return
            self._placeholder = placeholder
            self._messages = [self._placeholder]
            self._rendered = [THINKING_TEXT]
        except RPCError:
            log.exception("could not send placeholder in chat %s", self.chat_id)

    async def _replace_placeholder(self, text: str) -> None:
        if self._placeholder is not None:
            await self._edit(self._placeholder, text)
            self._rendered = [text]
        else:
            try:
                msg = await self._send(text)
                self._messages = [msg]
                self._rendered = [text]
            except RPCError:
                log.exception("could not send error message in chat %s", self.chat_id)

    async def _flush(self, final: bool = False) -> None:
        text = "".join(self._chunks)
        self.answer = text
        parts = split_parts(text)

        while len(parts) > len(self._messages):
            index = len(self._messages)
            try:
                msg = await self._send(parts[index])
            except RPCError:
                log.exception("send failed in chat %s", self.chat_id)
                return
            self._messages.append(msg)
            self._rendered.append(parts[index])

        for index, part in enumerate(parts):
            rendered = md_to_html(part) if final else part
            if self._rendered[index] == rendered:
                continue
            ok = await self._edit(self._messages[index], rendered, as_html=final)
            if final and not ok:
                ok = await self._edit(self._messages[index], part)
                rendered = part
            if ok:
                self._rendered[index] = rendered

    # ------------------------------------------------------------------- i/o
    async def _send(self, text: str) -> Message:
        return await self.client.send_message(
            self.chat_id,
            text,
            parse_mode=ParseMode.DISABLED,
            link_preview_options=NO_PREVIEW,
            disable_notification=True,
        )

    async def _edit(self, message: Message, text: str, *, as_html: bool = False) -> bool:
        for attempt in range(2):
            try:
                await message.edit_text(
                    text,
                    parse_mode=ParseMode.HTML if as_html else ParseMode.DISABLED,
                    link_preview_options=NO_PREVIEW,
                )
                return True
            except FloodWait as exc:
                if attempt == 0:
                    await asyncio.sleep(min(float(exc.value), 3.0))
                    continue
                return False
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return True
                if as_html:
                    return False
                log.warning("edit rejected in chat %s: %s", self.chat_id, exc)
                return False
            except RPCError as exc:
                log.warning("edit failed in chat %s: %s", self.chat_id, exc)
                return False
        return False
