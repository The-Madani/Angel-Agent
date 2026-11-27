"""Encrypted, versioned SQLite backups on GitHub Releases."""

from __future__ import annotations

import asyncio
import gzip
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

import config
from memory import Memory

MAGIC = b"ANGLBK01"
SALT_LEN = 16
NONCE_LEN = 12
TAG_PREFIX = "backup-"
ASSET_SUFFIX = ".db.gz.enc"


class BackupError(RuntimeError):
    pass


@dataclass(slots=True)
class BackupInfo:
    tag: str
    name: str
    size: int
    created_at: str
    url: str


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(passphrase.encode("utf-8"))


def encrypt_blob(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    ciphertext = AESGCM(_derive_key(passphrase, salt)).encrypt(nonce, data, MAGIC)
    return MAGIC + salt + nonce + ciphertext


def decrypt_blob(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < len(MAGIC) + SALT_LEN + NONCE_LEN + 16 or not blob.startswith(MAGIC):
        raise BackupError("فایل بکاپ معتبر نیست")
    salt = blob[len(MAGIC) : len(MAGIC) + SALT_LEN]
    nonce = blob[len(MAGIC) + SALT_LEN : len(MAGIC) + SALT_LEN + NONCE_LEN]
    ciphertext = blob[len(MAGIC) + SALT_LEN + NONCE_LEN :]
    try:
        return AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, ciphertext, MAGIC)
    except Exception as exc:  # InvalidTag
        raise BackupError("رمز بکاپ درست نیست") from exc


class BackupManager:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self.repo = config.GITHUB_BACKUP_REPO
        self.token = config.GITHUB_BACKUP_TOKEN
        self.passphrase = config.BACKUP_PASSPHRASE
        self.keep = config.BACKUP_KEEP
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._ready = False
        self.last: BackupInfo | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._ready and self.repo and self.token and self.passphrase)

    async def start(self) -> None:
        if self._session and not self._session.closed:
            self._ready = True
            return
        if not (self.repo and self.token and self.passphrase):
            return
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", self.repo):
            raise BackupError("GITHUB_BACKUP_REPO باید به شکل owner/repo باشد")
        if len(self.passphrase) < 10:
            raise BackupError("BACKUP_PASSPHRASE باید حداقل ۱۰ کاراکتر باشد")
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120, connect=15),
        )
        self._ready = True

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._ready = False

    # ------------------------------------------------------------------ create
    async def create(self) -> BackupInfo:
        if not self.enabled:
            raise BackupError("بکاپ گیت‌هاب تنظیم نشده")
        async with self._lock:
            blob, raw_size = await self._build_blob()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            tag = f"{TAG_PREFIX}{stamp}"
            asset_name = f"angel-{stamp}{ASSET_SUFFIX}"

            release = await self._api(
                "POST",
                f"/repos/{self.repo}/releases",
                json={
                    "tag_name": tag,
                    "name": f"Angel backup {stamp} UTC",
                    "body": f"Encrypted SQLite snapshot ({raw_size} bytes raw).",
                    "draft": False,
                    "prerelease": False,
                },
            )
            release_id = int(release["id"])
            upload_url = (
                f"https://uploads.github.com/repos/{self.repo}/releases/{release_id}/assets"
                f"?name={asset_name}"
            )
            try:
                await self._upload(upload_url, blob)
                await self._prune()
            except Exception:
                # Do not leave an empty release behind when upload fails.
                try:
                    await self._api("DELETE", f"/repos/{self.repo}/releases/{release_id}")
                except Exception:
                    pass
                raise

            info = BackupInfo(
                tag=tag,
                name=asset_name,
                size=len(blob),
                created_at=release.get("created_at", ""),
                url=release.get("html_url", ""),
            )
            self.last = info
            return info

    async def _build_blob(self) -> tuple[bytes, int]:
        stamp = int(time.time() * 1000)
        tmp = self.memory.path.parent / f".snapshot-{stamp}.db"
        try:
            await asyncio.to_thread(self.memory.backup, tmp)
            raw = await asyncio.to_thread(tmp.read_bytes)
        finally:
            tmp.unlink(missing_ok=True)
        packed = await asyncio.to_thread(gzip.compress, raw, 9)
        blob = await asyncio.to_thread(encrypt_blob, packed, self.passphrase)
        return blob, len(raw)

    # -------------------------------------------------------------------- list
    async def list(self, limit: int = 50) -> list[BackupInfo]:
        if not self.enabled:
            return []
        releases = await self._api("GET", f"/repos/{self.repo}/releases?per_page={limit}")
        result: list[BackupInfo] = []
        for release in releases:
            tag = release.get("tag_name", "")
            if not tag.startswith(TAG_PREFIX):
                continue
            assets = release.get("assets") or []
            asset = next((a for a in assets if str(a.get("name", "")).endswith(ASSET_SUFFIX)), None)
            if not asset:
                continue
            result.append(
                BackupInfo(
                    tag=tag,
                    name=str(asset["name"]),
                    size=int(asset.get("size") or 0),
                    created_at=str(release.get("created_at") or ""),
                    url=str(release.get("html_url") or ""),
                )
            )
        result.sort(key=lambda item: item.tag, reverse=True)
        return result

    # ----------------------------------------------------------------- restore
    async def restore_latest(self) -> dict[str, int | str]:
        if not self.enabled:
            raise BackupError("بکاپ گیت‌هاب تنظیم نشده")
        async with self._lock:
            info, blob = await self._download_latest()

            packed = await asyncio.to_thread(decrypt_blob, blob, self.passphrase)
            raw = await asyncio.to_thread(gzip.decompress, packed)
            if not raw.startswith(b"SQLite format 3\x00"):
                raise BackupError("محتوای بکاپ دیتابیس نیست")

            stamp = int(time.time())
            tmp = self.memory.path.parent / f".restore-{stamp}.db"
            tmp.write_bytes(raw)
            try:
                conn = sqlite3.connect(tmp)
                try:
                    ok = conn.execute("PRAGMA integrity_check").fetchone()
                finally:
                    conn.close()
                if not ok or ok[0] != "ok":
                    raise BackupError("دیتابیس بکاپ سالم نیست")
            except Exception:
                tmp.unlink(missing_ok=True)
                raise

            pre = self.memory.path.parent / f"pre-restore-{stamp}.db"
            await asyncio.to_thread(self.memory.backup, pre)

            self.memory.close()
            for suffix in ("-wal", "-shm"):
                Path(str(self.memory.path) + suffix).unlink(missing_ok=True)
            # os.replace is atomic and keeps the old DB in place if the
            # replacement itself fails. The pre-restore snapshot remains
            # available for an explicit rollback.
            try:
                os.replace(tmp, self.memory.path)
            except Exception:
                self.memory.reopen()
                raise
            try:
                self.memory.reopen()
            except Exception:
                # Best-effort rollback so a failed reopen does not strand the
                # process with a closed/broken live database.
                self.memory.close()
                os.replace(pre, self.memory.path)
                self.memory.reopen()
                raise

            return {
                "messages": self.memory.total_messages(),
                "chats": self.memory.total_chats(),
                "archived": self.memory.total_archived(),
                "pre_restore": str(pre),
                "tag": info.tag,
                "size": info.size,
            }

    # ---------------------------------------------------------------- internals
    async def _download_latest(self) -> tuple[BackupInfo, bytes]:
        releases = await self._api("GET", f"/repos/{self.repo}/releases?per_page=50")
        for release in releases:
            tag = release.get("tag_name", "")
            if not tag.startswith(TAG_PREFIX):
                continue
            asset = next(
                (a for a in release.get("assets") or [] if str(a.get("name", "")).endswith(ASSET_SUFFIX)),
                None,
            )
            if not asset:
                continue
            blob = await self._download_asset(int(asset["id"]))
            info = BackupInfo(
                tag=tag,
                name=str(asset["name"]),
                size=int(asset.get("size") or 0),
                created_at=str(release.get("created_at") or ""),
                url=str(release.get("html_url") or ""),
            )
            return info, blob
        raise BackupError("هیچ بکاپی روی گیت‌هاب پیدا نشد")

    async def _download_asset(self, asset_id: int) -> bytes:
        assert self._session is not None
        url = f"https://api.github.com/repos/{self.repo}/releases/assets/{asset_id}"
        headers = self._headers("application/octet-stream")
        async with self._session.get(url, headers=headers) as resp:
            if resp.status != 200:
                text = (await resp.text())[:200]
                raise BackupError(f"دانلود بکاپ ناموفق بود (HTTP {resp.status}): {text}")
            return await resp.read()

    async def _prune(self) -> None:
        releases = await self._api("GET", f"/repos/{self.repo}/releases?per_page=100")
        backups = [r for r in releases if str(r.get("tag_name", "")).startswith(TAG_PREFIX)]
        backups.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        for release in backups[self.keep :]:
            try:
                await self._api("DELETE", f"/repos/{self.repo}/releases/{release['id']}")
            except BackupError:
                pass

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _api(self, method: str, path: str, **kwargs):
        if self._session is None:
            raise BackupError("کلاینت بکاپ آماده نیست")
        async with self._session.request(
            method, f"https://api.github.com{path}", headers=self._headers(), **kwargs
        ) as resp:
            if resp.status >= 400:
                text = (await resp.text())[:300]
                raise BackupError(f"گیت‌هاب خطا داد (HTTP {resp.status}): {text}")
            if method == "DELETE":
                return None
            return await resp.json()

    async def _upload(self, url: str, blob: bytes) -> None:
        assert self._session is not None
        headers = self._headers("application/vnd.github+json")
        headers["Content-Type"] = "application/octet-stream"
        async with self._session.post(url, headers=headers, data=blob) as resp:
            if resp.status >= 400:
                text = (await resp.text())[:300]
                raise BackupError(f"آپلود بکاپ ناموفق بود (HTTP {resp.status}): {text}")
            await resp.read()
