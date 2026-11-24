"""Automatic rolling summaries of old conversation history."""

from __future__ import annotations

import asyncio
import logging

import config
import prompts
from memory import Memory
from router import Router

log = logging.getLogger(__name__)

MAX_MSG_CHARS = 1500


class Compactor:
    def __init__(self, memory: Memory, router: Router) -> None:
        self.memory = memory
        self.router = router
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ public
    def schedule(self, chat_id: int) -> None:
        """Fire-and-forget compaction; never blocks the reply path."""
        task = asyncio.create_task(self._guarded(chat_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cancel_pending(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def compact(self, chat_id: int, *, force: bool = False) -> dict[str, int] | None:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            return None
        async with lock:
            total = self.memory.count(chat_id)
            if not force and total <= config.HISTORY_MAX_MESSAGES:
                return None

            old_count = total - config.HISTORY_KEEP_MESSAGES
            if old_count < config.HISTORY_MIN_TO_COMPACT:
                return None

            rows = self.memory.oldest_messages(chat_id, old_count)
            if len(rows) < config.HISTORY_MIN_TO_COMPACT:
                return None

            transcript = "\n".join(
                f"{'کاربر' if role == 'user' else 'دستیار'}: {content[:MAX_MSG_CHARS]}"
                for _, role, content in rows
            )
            old_summary = self.memory.summary(chat_id) or "(خالی)"
            prompt = prompts.SUMMARY_INSTRUCTION.format(
                old_summary=old_summary, transcript=transcript
            )

            try:
                summary = await self.router.complete(
                    [
                        {"role": "system", "content": prompts.SUMMARY_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=config.SUMMARY_MAX_TOKENS,
                )
            except Exception:
                log.exception("summary generation failed for chat %s", chat_id)
                return None

            summary = summary.strip()
            if not summary:
                return None

            self.memory.replace_with_summary(chat_id, [row[0] for row in rows], summary)
            result = {
                "before": total,
                "after": total - len(rows),
                "summary_chars": len(summary),
            }
            log.info("compacted chat %s: %s", chat_id, result)
            return result

    # ------------------------------------------------------------------ private
    async def _guarded(self, chat_id: int) -> None:
        try:
            await self.compact(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unexpected compaction error for chat %s", chat_id)