"""SQLite-backed conversation memory with rolling summaries."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable

import config


class Memory:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id         INTEGER PRIMARY KEY,
                summary         TEXT    NOT NULL DEFAULT '',
                summary_updated INTEGER NOT NULL DEFAULT 0,
                created_at      INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS archived_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at INTEGER NOT NULL,
                archived_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_archived_chat ON archived_messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS stats (
                chat_id          INTEGER PRIMARY KEY,
                requests         INTEGER NOT NULL DEFAULT 0,
                prompt_tokens    INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                last_latency_ms  INTEGER NOT NULL DEFAULT 0,
                updated_at       INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS facts (
                chat_id    INTEGER NOT NULL,
                key        TEXT    NOT NULL,
                value      TEXT    NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, key)
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                text       TEXT    NOT NULL,
                due_at     INTEGER NOT NULL,
                sent       INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, due_at);
            """
        )
        self.db.commit()

    # --------------------------------------------------------------- messages
    def add_message(self, chat_id: int, role: str, content: str) -> int:
        now = int(time.time())
        cur = self.db.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, now),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO chats (chat_id, created_at) VALUES (?, ?)", (chat_id, now)
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def delete_message(self, message_id: int) -> None:
        self.db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        self.db.commit()

    def count(self, chat_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def get_context(self, chat_id: int, char_budget: int) -> tuple[str, list[dict[str, str]]]:
        summary_row = self.db.execute(
            "SELECT summary FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        summary = (summary_row[0] or "") if summary_row else ""

        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 400",
            (chat_id,),
        ).fetchall()

        used = 0
        picked: list[dict[str, str]] = []
        for role, content in rows:  # newest first, then reversed below
            size = len(content)
            if picked and used + size > char_budget:
                break
            picked.append({"role": role, "content": content})
            used += size
        picked.reverse()
        return summary, picked

    def oldest_messages(self, chat_id: int, limit: int) -> list[tuple[int, str, str]]:
        rows = self.db.execute(
            "SELECT id, role, content FROM messages WHERE chat_id = ? ORDER BY id ASC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [(int(i), str(r), str(c)) for i, r, c in rows]

    def replace_with_summary(
        self, chat_id: int, message_ids: Iterable[int], summary: str
    ) -> None:
        ids = list(message_ids)
        now = int(time.time())
        with self.db:  # single transaction
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.db.execute(
                    f"""
                    INSERT INTO archived_messages (chat_id, role, content, created_at, archived_at)
                    SELECT chat_id, role, content, created_at, ?
                    FROM messages WHERE id IN ({placeholders})
                    """,
                    [now, *ids],
                )
                self.db.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})", ids
                )
            self.db.execute(
                """
                INSERT INTO chats (chat_id, summary, summary_updated, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    summary = excluded.summary,
                    summary_updated = excluded.summary_updated
                """,
                (chat_id, summary, now, now),
            )

    def clear(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self.db.execute(
                "UPDATE chats SET summary = '', summary_updated = 0 WHERE chat_id = ?",
                (chat_id,),
            )

    def summary(self, chat_id: int) -> str:
        row = self.db.execute(
            "SELECT summary FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return (row[0] or "") if row else ""

    def total_messages(self) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0]) if row else 0

    def total_chats(self) -> int:
        row = self.db.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()
        return int(row[0]) if row else 0

    def total_archived(self) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM archived_messages").fetchone()
        return int(row[0]) if row else 0

    def reopen(self) -> None:
        """Reconnect after the underlying database file was replaced."""
        try:
            self.db.close()
        except sqlite3.Error:
            pass
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # ------------------------------------------------------------------ stats
    def bump_stats(
        self,
        chat_id: int,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        reasoning_tokens: int = 0,
        latency_ms: int = 0,
    ) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute(
                """
                INSERT INTO stats (chat_id, requests, prompt_tokens, completion_tokens,
                                   reasoning_tokens, last_latency_ms, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    requests = requests + 1,
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                    last_latency_ms = excluded.last_latency_ms,
                    updated_at = excluded.updated_at
                """,
                (chat_id, prompt_tokens, completion_tokens, reasoning_tokens, latency_ms, now),
            )

    def stats(self, chat_id: int) -> dict[str, int]:
        row = self.db.execute(
            """
            SELECT requests, prompt_tokens, completion_tokens, reasoning_tokens, last_latency_ms
            FROM stats WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        keys = ("requests", "prompt_tokens", "completion_tokens", "reasoning_tokens", "last_latency_ms")
        if not row:
            return dict.fromkeys(keys, 0)
        return dict(zip(keys, (int(v or 0) for v in row)))

    def global_stats(self) -> dict[str, int]:
        messages = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()
        rows = self.db.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()
        return {
            "chats": int(rows[0]) if rows else 0,
            "messages": int(messages[0]) if messages else 0,
            "archived": self.total_archived(),
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def backup(self, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        self.db.commit()
        self.db.execute("VACUUM INTO ?", (str(target),))
        return target

    def close(self) -> None:
        try:
            self.db.commit()
        finally:
            self.db.close()

    # ------------------------------------------------------------------- facts
    def set_fact(self, chat_id: int, key: str, value: str) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute(
                """
                INSERT INTO facts (chat_id, key, value, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (chat_id, key.strip()[:80], value.strip()[:2000], now),
            )

    def get_facts(self, chat_id: int) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT key, value FROM facts WHERE chat_id = ? ORDER BY updated_at DESC",
            (chat_id,),
        ).fetchall()
        return [(str(k), str(v)) for k, v in rows]

    def delete_fact(self, chat_id: int, key: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM facts WHERE chat_id = ? AND key = ?", (chat_id, key.strip()[:80])
        )
        self.db.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------------- reminders
    def add_reminder(self, chat_id: int, text: str, due_at: int) -> int:
        now = int(time.time())
        cur = self.db.execute(
            "INSERT INTO reminders (chat_id, text, due_at, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, text.strip()[:1000], due_at, now),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def due_reminders(self, limit: int = 20) -> list[tuple[int, int, str]]:
        now = int(time.time())
        rows = self.db.execute(
            "SELECT id, chat_id, text FROM reminders WHERE sent = 0 AND due_at <= ? ORDER BY due_at ASC LIMIT ?",
            (now, limit),
        ).fetchall()
        return [(int(i), int(c), str(t)) for i, c, t in rows]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        self.db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
        self.db.commit()

    def list_reminders(self, chat_id: int) -> list[tuple[int, str, int]]:
        rows = self.db.execute(
            "SELECT id, text, due_at FROM reminders WHERE chat_id = ? AND sent = 0 ORDER BY due_at ASC",
            (chat_id,),
        ).fetchall()
        return [(int(i), str(t), int(d)) for i, t, d in rows]

    # ------------------------------------------------------------------ search
    def search_archive(self, chat_id: int, query: str, limit: int = 20) -> list[tuple[str, str, int]]:
        """Search both active and archived messages (the whole history)."""
        pattern = f"%{query.strip()[:200]}%"
        rows = self.db.execute(
            """
            SELECT role, content, created_at FROM (
                SELECT role, content, created_at FROM archived_messages
                WHERE chat_id = ? AND content LIKE ?
                UNION ALL
                SELECT role, content, created_at FROM messages
                WHERE chat_id = ? AND content LIKE ?
            )
            ORDER BY created_at DESC LIMIT ?
            """,
            (chat_id, pattern, chat_id, pattern, limit),
        ).fetchall()
        return [(str(r), str(c), int(t)) for r, c, t in rows]