"""Minimal async client for the 9router OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import AsyncIterator, Iterable

import aiohttp

import config

log = logging.getLogger(__name__)


class RouterError(RuntimeError):
    pass


Message = dict[str, object]


class Router:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._url = f"{config.ROUTER_BASE_URL}/chat/completions"
        self._headers = {
            "Authorization": f"Bearer {config.ROUTER_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    async def start(self) -> None:
        if self._session and not self._session.closed:
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=config.REQUEST_TIMEOUT,
                connect=min(15.0, config.REQUEST_TIMEOUT),
                sock_read=config.REQUEST_TIMEOUT,
            ),
            connector=aiohttp.TCPConnector(
                limit=config.MAX_CONCURRENT * 2,
                limit_per_host=config.MAX_CONCURRENT,
                keepalive_timeout=60,
                ttl_dns_cache=300,
            ),
            headers=self._headers,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------- transcription
    async def transcribe(
        self, path: Path, *, filename: str = "", mime_type: str = ""
    ) -> str:
        """Upload audio as multipart data and return its transcript."""
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
        except OSError as exc:
            raise RouterError("فایل صوتی در دسترس نیست.") from exc
        if not size:
            raise RouterError("فایل صوتی خالی است.")
        if size > config.AUDIO_MAX_BYTES:
            raise RouterError("حجم فایل صوتی بیشتر از حد مجاز است؛ فایل کوتاه‌تری بفرست.")

        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None
        name = Path(filename or path.name).name
        content_type = mime_type.split(";", 1)[0].strip().lower()
        if not content_type.startswith("audio/"):
            content_type = mimetypes.guess_type(name)[0] or content_type or "application/octet-stream"
        if content_type == "video/webm":
            content_type = "audio/webm"
        timeout = aiohttp.ClientTimeout(
            total=config.TRANSCRIPTION_TIMEOUT,
            connect=min(15.0, config.TRANSCRIPTION_TIMEOUT),
        )
        async with self._sem:
            try:
                with path.open("rb") as audio:
                    form = aiohttp.FormData()
                    form.add_field("file", audio, filename=name, content_type=content_type)
                    form.add_field("model", config.TRANSCRIPTION_MODEL)
                    payload = form()
                    # The shared chat session defaults to JSON/SSE. Override
                    # both headers so aiohttp sends the multipart boundary.
                    headers = {
                        "Content-Type": payload.content_type,
                        "Accept": "application/json",
                    }
                    async with self._session.post(
                        f"{config.ROUTER_BASE_URL}/audio/transcriptions",
                        data=payload,
                        headers=headers,
                        timeout=timeout,
                    ) as resp:
                        if resp.status != 200:
                            # Provider error bodies may contain request data;
                            # only expose the status code in logs and replies.
                            raise RouterError(f"تبدیل صدا انجام نشد (کد {resp.status}). دوباره امتحان کن.")
                        try:
                            data = await resp.json(content_type=None)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise RouterError("پاسخ سرویس تبدیل صدا معتبر نبود. دوباره امتحان کن.") from exc
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                raise RouterError("تبدیل صدا طول کشید؛ دوباره امتحان کن یا ویس کوتاه‌تری بفرست.") from exc
            except (aiohttp.ClientError, OSError) as exc:
                raise RouterError("اتصال به سرویس تبدیل صدا نشد. دوباره امتحان کن.") from exc

        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise RouterError("گفتار قابل تشخیصی در این صدا پیدا نشد؛ واضح‌تر ضبط کن و دوباره بفرست.")
        return text.strip()

    # ------------------------------------------------------------------ stream
    async def stream(
        self,
        messages: Iterable[Message],
        *,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[tuple[str, object]]:
        """Yield ("reasoning"|"content"|"tool_call", data) and finally ("usage", dict)."""
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None

        payload: dict[str, object] = {
            "model": config.MODEL,
            "messages": list(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens or config.MAX_TOKENS,
        }
        if tools:
            payload["tools"] = tools

        async with self._sem:
            try:
                async with self._session.post(self._url, json=payload) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300].replace("\n", " ")
                        raise RouterError(f"HTTP {resp.status}: {body}")

                    # Accumulate incomplete tool calls arriving across SSE chunks.
                    tool_calls_acc: dict[int, dict] = {}

                    async for raw in resp.content:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line or line.startswith(":") or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue

                        usage = chunk.get("usage")
                        if usage:
                            yield ("usage", usage)

                        choices = chunk.get("choices") or ()
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            yield ("reasoning", reasoning)
                        content = delta.get("content")
                        if content:
                            yield ("content", content)

                        # Tool calls can come as delta stream
                        deltas = delta.get("tool_calls") or ()
                        for tc in deltas:
                            idx = tc.get("index", 0)
                            acc = tool_calls_acc.setdefault(
                                idx,
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                            )
                            if tc.get("id"):
                                acc["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                acc["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                acc["function"]["arguments"] += fn["arguments"]

                    if tool_calls_acc:
                        calls = [v for k, v in sorted(tool_calls_acc.items())]
                        yield ("tool_calls", calls)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                raise RouterError("request timed out") from exc
            except aiohttp.ClientError as exc:
                raise RouterError(f"connection error: {exc}") from exc

    # ---------------------------------------------------------------- complete
    async def complete(
        self, messages: Iterable[Message], *, max_tokens: int = 2000, retries: int = 2
    ) -> str:
        """Non-streaming completion with a tiny retry policy (summaries etc.)."""
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None

        payload = {
            "model": config.MODEL,
            "messages": list(messages),
            "stream": False,
            "max_tokens": max_tokens,
        }

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self._sem:
                    async with self._session.post(self._url, json=payload) as resp:
                        if resp.status != 200:
                            body = (await resp.text())[:300].replace("\n", " ")
                            raise RouterError(f"HTTP {resp.status}: {body}")
                        data = await resp.json()
                choices = data.get("choices") or ()
                if not choices:
                    raise RouterError("empty choices")
                content = (choices[0].get("message") or {}).get("content") or ""
                if isinstance(content, list):
                    content = "".join(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict)
                    )
                return str(content).strip()
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, RouterError, aiohttp.ClientError, ValueError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(0.8 * (attempt + 1))
        raise RouterError(str(last_error) if last_error else "request failed")

    # -------------------------------------------------------------------- ping
    async def ping(self) -> float:
        """Milliseconds until the first streamed token (reasoning or content)."""
        started = time.perf_counter()
        gen = self.stream([{"role": "user", "content": "سلام"}], max_tokens=64)
        try:
            async for kind, _ in gen:
                if kind in ("reasoning", "content"):
                    return (time.perf_counter() - started) * 1000
            return (time.perf_counter() - started) * 1000
        finally:
            await gen.aclose()
