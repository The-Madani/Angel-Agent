"""Minimal async client for the 9router OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Iterable

import aiohttp

import config

log = logging.getLogger(__name__)


class RouterError(RuntimeError):
    pass


Message = dict[str, str]


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
            timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=config.REQUEST_TIMEOUT),
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

    # ------------------------------------------------------------------ stream
    async def stream(
        self, messages: Iterable[Message], *, max_tokens: int | None = None
    ) -> AsyncIterator[tuple[str, object]]:
        """Yield ("reasoning"|"content", text) and finally ("usage", dict)."""
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None

        payload = {
            "model": config.MODEL,
            "messages": list(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens or config.MAX_TOKENS,
        }

        async with self._sem:
            try:
                async with self._session.post(self._url, json=payload) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300].replace("\n", " ")
                        raise RouterError(f"HTTP {resp.status}: {body}")

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
            except asyncio.CancelledError:
                raise
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
                return content.strip()
            except asyncio.CancelledError:
                raise
            except (RouterError, aiohttp.ClientError, ValueError) as exc:
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