"""Tools the model can call on its own: read a link, search, tell the time.

Everything here is plain text in / plain text out, because the router only
understands text tool results.
"""

from __future__ import annotations

import asyncio
import html as _html
import ipaddress
import json
import logging
import math
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urljoin, urlparse

import aiohttp

import config

log = logging.getLogger(__name__)

MAX_URL_BYTES = 4 * 1024 * 1024
MAX_PAGE_CHARS = config.TOOL_MAX_CHARS
MAX_LINKS = 5
TIMEOUT = config.TOOL_TIMEOUT
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"}

TEHRAN = timezone(timedelta(hours=3, minutes=30), "Asia/Tehran")


class ToolError(RuntimeError):
    """A tool failed in a way the model should be told about."""


# --------------------------------------------------------------------- helpers
def html_to_text(html: str) -> str:
    low = html.lower()
    start = 0
    for marker in ("mw-parser-output", "mw-content-text", "<article", "<main"):
        idx = low.find(marker)
        if idx >= 0:
            gt = html.find(">", idx)
            if gt >= 0:
                start = gt + 1
                break

    end = len(html)
    for em in ("catlinks", "printfooter", "mw-data-after-content", "mw-footer"):
        j = low.find(em, start)
        if start < j < end:
            tag_start = html.rfind("<", start, j)
            if tag_start >= start:
                end = min(end, tag_start)
            else:
                end = min(end, j)

    return _strip_tags(html[start:end])


def _strip_tags(html: str) -> str:
    # Drop whole noisy blocks first.
    html = re.sub(
        r"<(script|style|noscript|nav|header|footer|aside|form|button|svg)\b[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.S | re.I,
    )
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</(p|div|li|tr|h[1-6]|table|section|article)\s*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = _html.unescape(text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"\{\{[^{}]*\}\}", " ", text)
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_private(host: str) -> bool:
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host or host in BLOCKED_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not addr.is_global


async def _assert_public_host(host: str) -> None:
    """Reject literal and DNS-resolved private addresses before fetching."""
    if _is_private(host):
        raise ToolError("این آدرس داخلی است و اجازهٔ دسترسی ندارم.")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ToolError("دامنه پیدا نشد.") from exc
    addresses = {str(info[4][0]) for info in infos if info[4]}
    if not addresses or any(_is_private(address) for address in addresses):
        raise ToolError("این آدرس داخلی است و اجازهٔ دسترسی ندارم.")


def normalize_url(url: str) -> str:
    url = (url or "").strip().strip("<>\"'")
    if not url:
        raise ToolError("لینک خالی بود.")
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
            url = "https://" + url
        else:
            raise ToolError("لینک معتبر نبود.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("لینک معتبر نبود.")
    if parsed.username is not None or parsed.password is not None:
        raise ToolError("لینک‌های دارای نام کاربری یا رمز عبور قابل خواندن نیستند.")
    return url


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_PAGE_CHARS:
        return text
    return text[:MAX_PAGE_CHARS].rstrip() + "\n\n[… بقیهٔ صفحه طولانی بود و خوانده نشد]"


def _decode(raw: bytes, content_type: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _pdf(raw: bytes) -> str:
    import io

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise ToolError("این فایل PDF سالم نبود و باز نشد.") from exc
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ToolError("این PDF رمز عبور دارد و باز نشد.") from exc
    pages: list[str] = []
    used = 0
    for index in range(min(len(reader.pages), 80)):
        page = reader.pages[index]
        try:
            body = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            body = ""
        if body.strip():
            part = f"[صفحه {index + 1}]\n{body.strip()}"
            remaining = MAX_PAGE_CHARS - used
            if remaining <= 0:
                break
            pages.append(part[:remaining])
            used += len(part)
    if not pages:
        raise ToolError("متنی داخل این PDF پیدا نشد (احتمالاً اسکن‌شده است).")
    return "\n\n".join(pages)


async def _wikipedia(host: str, title: str) -> str:
    from urllib.parse import quote

    api = f"https://{host}/w/api.php?action=query&prop=extracts&explaintext=1&redirects=1&titles={quote(title)}&format=json"
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=TIMEOUT, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(api) as resp:
                if resp.status >= 400:
                    raise ToolError(f"ویکی‌پدیا جواب نداد (کد {resp.status}).")
                data = await resp.json(content_type=None)
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        raise ToolError("اتصال به ویکی‌پدیا نشد.") from exc

    pages = (data.get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), {})
    extract = (page.get("extract") or "").strip()
    resolved = page.get("title") or title
    if not extract or page.get("missing"):
        raise ToolError(f"صفحهٔ «{title}» در ویکی‌پدیا پیدا نشد.")
    return f"عنوان: {resolved} (ویکی‌پدیا)\n\n{_clip(extract)}"


async def read_link(url: str) -> str:
    """Download a page and return its readable text."""
    url = normalize_url(url)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (host == "wikipedia.org" or host.endswith(".wikipedia.org")) and parsed.path.startswith("/wiki/"):
        title = unquote(parsed.path[len("/wiki/"):]).replace("_", " ")
        if title:
            await _assert_public_host(host)
            return await _wikipedia(host, title)

    await _assert_public_host(host)

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.8"}
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT, connect=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            current = url
            for _ in range(6):
                current_url = urlparse(current)
                if current_url.scheme not in ("http", "https") or not current_url.hostname:
                    raise ToolError("لینک به یک آدرس قابل خواندن redirect نشد.")
                await _assert_public_host(current_url.hostname)
                async with session.get(current, allow_redirects=False) as resp:
                    if 300 <= resp.status < 400:
                        location = resp.headers.get("Location")
                        if not location:
                            raise ToolError("لینک redirect ناقص بود.")
                        current = normalize_url(urljoin(current, location))
                        continue
                    final = str(resp.url)
                    final_url = urlparse(final)
                    await _assert_public_host(final_url.hostname or "")
                    content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                    if resp.status >= 400:
                        raise ToolError(f"سایت جواب نداد (کد {resp.status}).")
                    raw = await resp.content.read(MAX_URL_BYTES + 1)
                    if len(raw) > MAX_URL_BYTES:
                        raise ToolError("صفحه خیلی بزرگ بود.")
                    break
            else:
                raise ToolError("تعداد redirectهای لینک بیش از حد بود.")
    except asyncio.TimeoutError as exc:
        raise ToolError("سایت دیر جواب داد و قطع شد.") from exc
    except aiohttp.ClientError as exc:
        raise ToolError(f"اتصال به سایت نشد ({type(exc).__name__}).") from exc

    title = ""
    is_pdf = content_type == "application/pdf" or final_url.path.lower().endswith(".pdf")
    if is_pdf:
        body = await asyncio.to_thread(_pdf, raw)
    elif content_type.startswith("image/"):
        raise ToolError("این لینک یک عکس است؛ متن ندارد. از کاربر بخواه خودش عکس را بفرستد.")
    elif content_type and not (
        content_type.startswith("text/")
        or content_type in ("application/json", "application/xml", "application/xhtml+xml")
        or content_type.endswith("+json")
        or content_type.endswith("+xml")
    ):
        raise ToolError(f"این نوع فایل ({content_type}) را نمی‌توانم بخوانم.")

    if not is_pdf:
        text = _decode(raw, content_type)
        if content_type in ("text/html", "application/xhtml+xml", "") or "<html" in text[:400].lower():
            title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
            if title_match:
                title = re.sub(r"\s+", " ", title_match.group(1)).strip()
            text = html_to_text(text)
        body = text

    body = _clip(body)
    if not body:
        raise ToolError("متنی از این صفحه پیدا نشد.")
    if title:
        return f"عنوان صفحه: {title}\nآدرس: {final}\n\n{body}"
    return f"آدرس: {final}\n\n{body}"


# ---------------------------------------------------------------------- search
async def web_search(query: str) -> str:
    """Search the web via the 9router /search endpoint (tinyfish)."""
    query = (query or "").strip()
    if not query:
        raise ToolError("عبارت جست‌وجو خالی بود.")

    url = f"{config.ROUTER_BASE_URL}/search"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.ROUTER_API_KEY}",
    }
    payload = {
        "model": "tinyfish",
        "query": query,
        "search_type": "web",
        "max_results": MAX_LINKS,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=config.TOOL_TIMEOUT, connect=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    raise ToolError(f"جست‌وجو جواب نداد (کد {resp.status}: {body}).")
                data = await resp.json(content_type=None)
    except asyncio.TimeoutError as exc:
        raise ToolError("جست‌وجو دیر جواب داد و قطع شد.") from exc
    except aiohttp.ClientError as exc:
        raise ToolError(f"جست‌وجو نشد ({type(exc).__name__}).") from exc

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        results = []
    if not results:
        raise ToolError("نتیجه‌ای برای این عبارت پیدا نشد.")
    lines = [f"نتایج جست‌وجو برای «{query}»:"]
    valid = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        title = _clip(str(item.get("title") or ""))[:500]
        link = str(item.get("url") or "").strip()
        body = _clip(str(item.get("snippet") or ""))[:800]
        if not title and not link:
            continue
        valid += 1
        index = valid
        lines.append(f"{index}. {title}\n   {link}" + (f"\n   {body}" if body else ""))
        if valid >= MAX_LINKS:
            break
    if not valid:
        raise ToolError("نتیجهٔ قابل استفاده‌ای پیدا نشد.")
    return "\n".join(lines)



# ------------------------------------------------------------------------ time
def get_time() -> str:
    now = datetime.now(TEHRAN)
    return now.strftime("%Y-%m-%d %H:%M | %A")


# --------------------------------------------------------------------- registry
SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_link",
            "description": (
                "متن یک صفحهٔ اینترنتی را می‌خواند. وقتی کاربر لینک فرستاده یا "
                "خودت می‌خواهی از یک آدرس چیزی بخوانی، از این ابزار استفاده کن."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "آدرس کامل صفحه با http یا https"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "در اینترنت جست‌وجو می‌کند و چند نتیجهٔ برتر را برمی‌گرداند. "
                "برای خبر، قیمت، آب‌وهوا یا هر چیز تازه‌ای که ممکن است جوابش را ندانی."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "عبارت جست‌وجو"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "تاریخ و ساعت فعلی تهران را می‌دهد.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": (
                "یک واقعیت ثابت دربارهٔ کاربر یا گفتگو را برای همیشه ذخیره می‌کند "
                "(مثل نام، علاقه‌مندی‌ها، قرارها، پروژه‌ها). از این ابزار وقتی استفاده کن "
                "که کاربر چیزی را دربارهٔ خودش گفت که یادگیری در آینده کمک می‌کند."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "عنوان کوتاه (مثلاً «شهر»، «علاقه»)"},
                    "value": {"type": "string", "description": "مقدار کامل"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_facts",
            "description": "واقعیت‌های ذخیره‌شده دربارهٔ کاربر را برمی‌گرداند.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_fact",
            "description": "یک واقعیت ذخیره‌شده را پاک می‌کند.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "کلید واقعیت برای حذف"}
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "یادآور یک‌باره یا تکرارشونده می‌سازد. برای «هر روز» repeat=daily، "
                "برای «هر هفته» repeat=weekly و برای «هر چند دقیقه/ساعت/روز» "
                "repeat=interval و interval_minutes را بده. زمان اولین نوبت را با "
                "یکی از iso_time یا minutes_from_now مشخص کن؛ زمان بدون منطقهٔ زمانی "
                "به وقت تهران است. در حالت interval، بدون زمان شروع، اولین نوبت یک "
                "فاصله بعد از الان است. اگر ساعت روتین روزانه/هفتگی معلوم نیست، از کاربر بپرس."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "متن یادآور"},
                    "minutes_from_now": {
                        "type": "integer", "minimum": 1,
                        "description": "اولین نوبت چند دقیقه دیگر باشد؛ هم‌زمان با iso_time نده",
                    },
                    "iso_time": {
                        "type": "string",
                        "description": "زمان اولین نوبت به صورت ISO؛ بدون timezone یعنی تهران (2030-01-01T09:00)",
                    },
                    "repeat": {
                        "type": "string",
                        "enum": ["none", "daily", "weekly", "interval"],
                        "description": "نوع تکرار؛ پیش‌فرض none (یک‌باره)",
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 525600,
                        "description": "فقط برای repeat=interval: فاصلهٔ تکرار به دقیقه (مثلاً هر ۲ ساعت = ۱۲۰، هر ۳ روز = ۴۳۲۰)",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "یادآورهای فعال همین کاربر، شماره، زمان نوبت بعد و دورهٔ تکرارشان را نشان می‌دهد.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": (
                "یادآور یک‌باره یا روتین همین کاربر را با شماره لغو می‌کند. "
                "اگر شماره مشخص نیست، اول list_reminders را بخوان و یادآور موردنظر را پیدا کن."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "integer", "minimum": 1, "description": "شمارهٔ یادآور از list_reminders"}
                },
                "required": ["reminder_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": (
                "در کل آرشیو پیام‌های گذشته (حتی پیام‌های قدیمی که از حافظهٔ فعال "
                "حذف شده‌اند) جست‌وجو می‌کند."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "عبارت جست‌وجو"}
                },
                "required": ["query"],
            },
        },
    },
]

async def call(name: str, arguments: str, chat_id: int = 0) -> str:
    """Run one tool call and return its text result (never raises)."""
    try:
        data = json.loads(arguments or "{}")
        if not isinstance(data, dict):
            raise ToolError("ورودی ابزار نامعتبر بود.")
    except (TypeError, ValueError, ToolError):
        return "خطا: ورودی ابزار JSON نامعتبر بود."

    try:
        return await asyncio.wait_for(_dispatch(name, data, chat_id), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        return "خطا: اجرای ابزار طول کشید و متوقف شد."
    except ToolError as exc:
        return f"خطا: {exc}"
    except Exception:  # noqa: BLE001 - a tool must never kill the chat
        log.exception("tool %s failed", name)
        return "خطا: اجرای این ابزار نشد."


async def _dispatch(name: str, data: dict, chat_id: int) -> str:
    if name == "read_link":
        return await read_link(str(data.get("url") or ""))
    if name == "web_search":
        return await web_search(str(data.get("query") or ""))
    if name == "get_time":
        return get_time()
    if name == "remember_fact":
        return remember_fact(chat_id, str(data.get("key") or ""), str(data.get("value") or ""))
    if name == "recall_facts":
        return recall_facts(chat_id)
    if name == "forget_fact":
        return forget_fact(chat_id, str(data.get("key") or ""))
    if name == "search_history":
        return search_history(chat_id, str(data.get("query") or ""))
    if name == "set_reminder":
        return await set_reminder(chat_id, data)
    if name == "list_reminders":
        return list_reminders(chat_id)
    if name == "cancel_reminder":
        return cancel_reminder(chat_id, data.get("reminder_id"))
    return f"خطا: ابزار «{name}» وجود ندارد."


def describe(call_name: str, arguments: str) -> str:
    """Short Persian label for logs / debugging."""
    try:
        data = json.loads(arguments or "{}")
    except ValueError:
        data = {}
    target = data.get("url") or data.get("query") or data.get("key") or data.get("text") or ""
    return f"{call_name}({str(target)[:60]})"


# ------------------------------------------------------- stateful tool bodies
# These need the Memory instance; handlers.py injects it at startup via
# tools.set_memory(memory) so the tool layer stays decoupled from storage.
_MEMORY = None


def set_memory(memory) -> None:
    global _MEMORY
    _MEMORY = memory


def remember_fact(chat_id: int, key: str, value: str) -> str:
    if _MEMORY is None:
        return "خطا: حافظه در دسترس نیست."
    key = key.strip()
    value = value.strip()
    if not key or not value:
        return "خطا: کلید و مقدار نمی‌توانند خالی باشند."
    _MEMORY.set_fact(chat_id, key, value)
    return f"ذخیره شد: {key} = {value[:120]}"


def recall_facts(chat_id: int) -> str:
    if _MEMORY is None:
        return "خطا: حافظه در دسترس نیست."
    facts = _MEMORY.get_facts(chat_id)
    if not facts:
        return "هنوز واقعیتی ذخیره نشده است."
    lines = [f"{i}. {k}: {v[:200]}" for i, (k, v) in enumerate(facts, 1)]
    return "واقعیت‌های ذخیره‌شده:\n" + "\n".join(lines)


def forget_fact(chat_id: int, key: str) -> str:
    if _MEMORY is None:
        return "خطا: حافظه در دسترس نیست."
    if _MEMORY.delete_fact(chat_id, key):
        return f"پاک شد: {key}"
    return f"واقعیتی با کلید «{key}» پیدا نشد."


def search_history(chat_id: int, query: str) -> str:
    if _MEMORY is None:
        return "خطا: حافظه در دسترس نیست."
    query = query.strip()
    if not query:
        return "خطا: عبارت جست‌وجو خالی بود."
    rows = _MEMORY.search_archive(chat_id, query)
    if not rows:
        return f"چیزی در آرشیو با «{query}» پیدا نشد."
    lines = [f"نتایج آرشیو برای «{query}»:"]
    for i, (role, content, ts) in enumerate(rows, 1):
        who = "کاربر" if role == "user" else "ربات"
        stamp = datetime.fromtimestamp(ts, TEHRAN).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{i}. [{stamp}] {who}: {content[:200]}")
    return "\n".join(lines)


async def set_reminder(chat_id: int, data: dict) -> str:
    if _MEMORY is None:
        return "خطا: حافظه در دسترس نیست."
    text = str(data.get("text") or "").strip()
    if not text:
        return "خطا: متن یادآور خالی بود."

    repeat = data.get("repeat", "none")
    periods = {"none": 0, "daily": 1440, "weekly": 10080}
    if not isinstance(repeat, str) or repeat not in (*periods, "interval"):
        return "خطا: نوع تکرار باید none، daily، weekly یا interval باشد."
    interval = data.get("interval_minutes")
    if repeat == "interval":
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or not 1 <= interval <= 525600
        ):
            return "خطا: فاصلهٔ تکرار باید عدد صحیحی بین ۱ و ۵۲۵۶۰۰ دقیقه باشد."
        period = interval
    else:
        if interval is not None:
            return "خطا: interval_minutes فقط برای repeat=interval است."
        period = periods[repeat]

    now = int(time.time())
    due = 0
    minutes = data.get("minutes_from_now")
    iso = str(data.get("iso_time") or "").strip()
    if minutes is not None and iso:
        return "خطا: فقط یکی از minutes_from_now یا iso_time را برای اولین نوبت مشخص کن."
    if minutes is not None:
        try:
            minutes_value = float(minutes)
            if (
                isinstance(minutes, bool)
                or not math.isfinite(minutes_value)
                or minutes_value <= 0
            ):
                raise ValueError
            due = now + int(minutes_value * 60)
        except (TypeError, ValueError, OverflowError):
            return "خطا: فاصله تا اولین نوبت باید تعداد دقیقهٔ مثبت و معتبر باشد."
    elif iso:
        try:
            parsed = datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=TEHRAN)
            due = int(parsed.timestamp())
        except (ValueError, OverflowError, OSError):
            return "خطا: زمان ISO نامعتبر بود."
        if period and due <= now:
            seconds = period * 60
            due += ((now - due) // seconds + 1) * seconds
    elif repeat == "interval":
        due = now + period * 60

    if due <= now:
        return "خطا: زمان اولین نوبت باید در آینده باشد؛ minutes_from_now یا iso_time را مشخص کن."
    try:
        when = datetime.fromtimestamp(due, TEHRAN).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "خطا: زمان یادآور خارج از محدودهٔ معتبر است."
    rid = _MEMORY.add_reminder(chat_id, text, due, repeat_minutes=period)
    return f"یادآور #{rid} تنظیم شد: «{text[:80]}» — {_repeat_label(period)}؛ نوبت بعد: {when} (تهران)"


def _repeat_label(minutes: int) -> str:
    if not minutes:
        return "یک‌بار"
    if minutes == 1440:
        return "هر روز"
    if minutes == 10080:
        return "هر هفته"
    for unit, label in ((10080, "هفته"), (1440, "روز"), (60, "ساعت")):
        if minutes % unit == 0:
            return f"هر {minutes // unit} {label}"
    return f"هر {minutes} دقیقه"


def list_reminders(chat_id: int) -> str:
    if _MEMORY is None:
        return "خطا: حافظه در دسترس نیست."
    items = _MEMORY.list_reminders(chat_id)
    if not items:
        return "هیچ یادآور فعالی نداری."
    lines = ["یادآورهای فعال:"]
    for rid, text, due, period in items:
        when = datetime.fromtimestamp(due, TEHRAN).strftime("%Y-%m-%d %H:%M")
        lines.append(f"• #{rid} — {_repeat_label(period)} — نوبت بعد: {when} (تهران) — {text[:80]}")
    return "\n".join(lines)


def cancel_reminder(chat_id: int, reminder_id: int) -> str:
    if _MEMORY is None:
        return "خطا: حافظه در دسترس نیست."
    if (
        isinstance(reminder_id, bool)
        or not isinstance(reminder_id, int)
        or not 1 <= reminder_id <= 2**63 - 1
    ):
        return "خطا: شمارهٔ یادآور باید عدد صحیح مثبت و معتبر باشد."
    if _MEMORY.cancel_reminder(chat_id, reminder_id):
        return f"یادآور #{reminder_id} لغو شد."
    return "یادآور فعالی با این شماره در این گفتگو پیدا نشد."
