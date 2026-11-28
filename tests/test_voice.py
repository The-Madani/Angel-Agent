"""Audio multipart transport and voice-to-answer flow without a Telegram login."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiohttp import web
from pyrogram.enums import ChatType

import config
import tools
from handlers import BotHandlers
from memory import Memory
from router import Router, RouterError


class TranscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "voice.ogg"
        self.audio.write_bytes(b"OggS-test-audio")
        self.requests = []
        self.status = 200
        self.response = {"text": "سلام، ساعت چنده؟"}
        self.invalid_json = False
        self.block = False
        self.release = asyncio.Event()
        self.addCleanup(self.release.set)
        app = web.Application()
        app.router.add_post("/v1/audio/transcriptions", self.transcription)
        app.router.add_post("/v1/chat/completions", self.chat)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        overrides = patch.multiple(
            config, ROUTER_BASE_URL=f"http://127.0.0.1:{port}/v1",
            ROUTER_API_KEY="test-key", TRANSCRIPTION_MODEL="gemini/gemini-2.5-flash",
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.router = Router()
        self.addAsyncCleanup(self.router.close)

    async def transcription(self, request):
        record = {"headers": dict(request.headers)}
        reader = await request.multipart()
        while part := await reader.next():
            if part.name == "file":
                record["filename"] = part.filename
                record["mime_type"] = part.headers.get("Content-Type")
                record["file"] = bytes(await part.read())
            else:
                record[part.name] = await part.text()
        self.requests.append(record)
        if self.block:
            await self.release.wait()
        if self.invalid_json:
            return web.Response(text="invalid JSON")
        return web.json_response(self.response, status=self.status)

    async def chat(self, request):
        self.assertEqual(request.content_type, "application/json")
        self.assertEqual(request.headers["Accept"], "text/event-stream")
        self.assertEqual((await request.json())["model"], config.MODEL)
        return web.Response(
            text='data: {"choices":[{"delta":{"content":"سلام"}}]}\n\ndata: [DONE]\n\n',
            content_type="text/event-stream",
        )

    async def test_multipart_upload_and_chat_headers_remain_independent(self) -> None:
        text = await self.router.transcribe(self.audio, filename="voice.ogg", mime_type="audio/ogg")
        self.assertEqual(text, self.response["text"])
        request = self.requests[0]
        self.assertTrue(request["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(request["headers"]["Accept"], "application/json")
        self.assertEqual(request["model"], "gemini/gemini-2.5-flash")
        self.assertEqual(request["file"], self.audio.read_bytes())
        self.assertEqual((request["filename"], request["mime_type"]), ("voice.ogg", "audio/ogg"))
        events = [event async for event in self.router.stream([{"role": "user", "content": "سلام"}])]
        self.assertIn(("content", "سلام"), events)

    async def test_document_mime_type_is_inferred_from_audio_extension(self) -> None:
        await self.router.transcribe(self.audio, filename="recording.mp3", mime_type="application/octet-stream")
        self.assertEqual(self.requests[0]["mime_type"], "audio/mpeg")

    async def test_provider_error_and_invalid_or_empty_transcripts(self) -> None:
        self.status = 429
        self.response = {"error": "private provider request data"}
        with self.assertRaises(RouterError) as caught:
            await self.router.transcribe(self.audio)
        self.assertIn("429", str(caught.exception))
        self.assertNotIn("private provider", str(caught.exception))
        self.status = 200
        for response in ({"text": "  "}, {"text": None}, {"text": []}, {"error": "bad"}, []):
            with self.subTest(response=response):
                self.response = response
                with self.assertRaises(RouterError):
                    await self.router.transcribe(self.audio)
        self.invalid_json = True
        with self.assertRaises(RouterError):
            await self.router.transcribe(self.audio)

    async def test_size_is_checked_before_any_upload(self) -> None:
        with patch.object(config, "AUDIO_MAX_BYTES", 3):
            with self.assertRaises(RouterError):
                await self.router.transcribe(self.audio)
        self.audio.write_bytes(b"")
        with self.assertRaises(RouterError):
            await self.router.transcribe(self.audio)
        self.assertEqual(self.requests, [])

    async def test_transcription_timeout_is_reported(self) -> None:
        self.block = True
        try:
            with patch.object(config, "TRANSCRIPTION_TIMEOUT", 0.05):
                with self.assertRaisesRegex(RouterError, "طول کشید"):
                    await self.router.transcribe(self.audio)
        finally:
            self.release.set()


def message(*, chat_id=11, message_id=1, **overrides):
    values = dict(
        id=message_id, chat=NS(id=chat_id, type=ChatType.PRIVATE),
        from_user=NS(id=chat_id, is_bot=False, is_self=False),
        outgoing=False, service=None, sender_chat=None, via_bot=None,
        text=None, caption=None, document=None, photo=None, voice=None, audio=None,
        reply_to_message=None,
    )
    values.update(overrides)
    return NS(**values)


class SentMessage:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.edits.append(text)

    async def delete(self):
        pass


class Client:
    def __init__(self):
        self.sent = []
        self.downloads = []

    async def send_message(self, chat_id, text, **kwargs):
        sent = SentMessage(text)
        self.sent.append(sent)
        return sent

    async def send_chat_action(self, *args, **kwargs):
        pass

    async def download_media(self, msg, file_name):
        path = Path(file_name)
        path.write_bytes(b"OggS-test-audio")
        self.downloads.append(path)
        return str(path)


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Memory(str(Path(self.tmp.name) / "history.db"))
        self.addCleanup(self.memory.close)
        self.client = Client()
        self.router = NS(transcribe=AsyncMock(return_value="سلام، ساعت چنده؟"), stream=self.stream)
        self.requests = []
        self.handler = BotHandlers(
            self.client, self.memory, self.router, NS(schedule=lambda *_: None), NS(), "bot"
        )
        self.addCleanup(tools.set_memory, None)
        self.voice = NS(mime_type="audio/ogg", file_size=15)

    async def stream(self, messages, **kwargs):
        self.requests.append(messages)
        yield "content", "جواب ویس"

    async def test_private_voice_becomes_normal_turn_in_the_same_message(self) -> None:
        msg = message(voice=self.voice)
        await self.handler.on_private_message(self.client, msg)
        task = self.handler._tasks[11]
        await task
        self.assertEqual(self.router.transcribe.await_count, 1)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0].text, "جواب ویس")
        self.assertTrue(any("فکر کردن" in edit for edit in self.client.sent[0].edits))
        user_turn = self.requests[0][-1]["content"]
        self.assertIn("سلام، ساعت چنده؟", user_turn)
        self.assertIn("متن ویس", user_turn)
        self.assertEqual(self.memory.count(11), 2)
        self.assertIn("سلام، ساعت چنده؟", self.memory.get_context(11, 10000)[1][0]["content"])
        self.assertTrue(all(not path.exists() for path in self.client.downloads))

    async def test_audio_and_audio_documents_keep_caption(self) -> None:
        for index, field in enumerate(("audio", "document"), 1):
            media = NS(file_name="note.mp3", mime_type="audio/mpeg", file_size=15)
            msg = message(message_id=index, caption="خلاصه کن", **{field: media})
            await self.handler._respond(self.client, msg, "", msg.caption)
            self.assertIn("خلاصه کن", self.requests[-1][-1]["content"])
        self.assertEqual(self.router.transcribe.await_count, 2)

    async def test_quoted_voice_is_visible_after_reset(self) -> None:
        self.memory.add_message(11, "user", "قبلی")
        self.memory.clear(11)
        msg = message(text="این ویس چی می‌گه؟", reply_to_message=message(voice=self.voice))
        await self.handler._respond(self.client, msg, msg.text, "")
        self.assertIn("سلام، ساعت چنده؟", self.requests[0][-1]["content"])
        self.assertIn("این ویس چی می‌گه؟", self.requests[0][-1]["content"])
        self.assertEqual(len(self.client.sent), 1)

    async def test_spoken_reminder_reaches_the_existing_tool_loop(self) -> None:
        self.router.transcribe.return_value = "هر روز یادم بنداز ورزش کنم؛ اولین بار یک دقیقه دیگه."

        async def stream(messages, **kwargs):
            if messages[-1]["role"] != "tool":
                yield "tool_calls", [{
                    "id": "reminder-call", "type": "function", "function": {
                        "name": "set_reminder",
                        "arguments": '{"text":"ورزش","repeat":"daily","minutes_from_now":1}',
                    },
                }]
            else:
                yield "content", "روتین ورزش تنظیم شد."

        self.router.stream = stream
        await self.handler._respond(self.client, message(voice=self.voice), "", "")
        self.assertEqual(self.memory.list_reminders(11)[0][3], 1440)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0].text, "روتین ورزش تنظیم شد.")

    async def test_failed_transcription_edits_status_and_does_not_call_chat(self) -> None:
        self.router.transcribe.side_effect = RouterError("صدای قابل تشخیص نبود.")
        with self.assertLogs("handlers", level="WARNING"):
            await self.handler._respond(self.client, message(voice=self.voice), "", "")
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("صدای قابل تشخیص نبود", self.client.sent[0].text)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.memory.count(11), 0)
        self.assertTrue(all(not path.exists() for path in self.client.downloads))

    async def test_large_voice_is_rejected_without_downloading(self) -> None:
        self.voice.file_size = config.AUDIO_MAX_BYTES + 1
        with self.assertLogs("handlers", level="WARNING"):
            await self.handler._respond(self.client, message(voice=self.voice), "", "")
        self.assertEqual(self.client.downloads, [])
        self.router.transcribe.assert_not_awaited()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("حجم", self.client.sent[0].text)

    async def test_cancellation_cleans_up_the_download(self) -> None:
        started = asyncio.Event()

        async def transcribe(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.router.transcribe.side_effect = transcribe
        task = asyncio.create_task(self.handler._respond(self.client, message(voice=self.voice), "", ""))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(all(not path.exists() for path in self.client.downloads))

    async def test_voice_still_rejects_non_private_and_bot_messages(self) -> None:
        for fields in (
            {"chat": NS(id=-1, type=ChatType.GROUP)},
            {"from_user": NS(id=2, is_bot=True, is_self=False)},
            {"from_user": NS(id=2, is_bot=False, is_self=True)},
            {"outgoing": True}, {"service": True}, {"sender_chat": NS(id=-1)},
        ):
            await self.handler.on_private_message(self.client, message(voice=self.voice, **fields))
        self.assertEqual(self.handler._tasks, {})
        self.assertEqual(self.client.downloads, [])


if __name__ == "__main__":
    unittest.main()
