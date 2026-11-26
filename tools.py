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
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

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
    if not host or host in BLOCKED_HOSTS:
        return True
    # Only block literal IP addresses; resolving hostnames is flaky in some
    # environments and would wrongly block legitimate sites.
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


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
    for index, page in enumerate(reader.pages[:80], start=1):
        try:
            body = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            body = ""
        if body.strip():
            pages.append(f"[صفحه {index}]\n{body.strip()}")
    if not pages:
        raise ToolError("متنی داخل این PDF پیدا نشد (احتمالاً اسکن‌شده است).")
    return "\n\n".join(pages)


# ---------------------------------------------------------------------- reader
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
    return url


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
    if "wikipedia.org" in host and parsed.path.startswith("/wiki/"):
        title = unquote(parsed.path[len("/wiki/"):]).replace("_", " ")
        if title:
            return await _wikipedia(host, title)

    if await asyncio.to_thread(_is_private, host):
        raise ToolError("این آدرس داخلی است و اجازهٔ دسترسی ندارم.")

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.8"}
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT, connect=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                final = str(resp.url)
                if await asyncio.to_thread(_is_private, urlparse(final).hostname or ""):
                    raise ToolError("این آدرس داخلی است و اجازهٔ دسترسی ندارم.")
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if resp.status >= 400:
                    raise ToolError(f"سایت جواب نداد (کد {resp.status}).")
                raw = await resp.read()
                if len(raw) > MAX_URL_BYTES:
                    raise ToolError("صفحه خیلی بزرگ بود.")
    except asyncio.TimeoutError as exc:
        raise ToolError("سایت دیر جواب داد و قطع شد.") from exc
    except aiohttp.ClientError as exc:
        raise ToolError(f"اتصال به سایت نشد ({type(exc).__name__}).") from exc

    title = ""
    if content_type == "application/pdf" or url.lower().endswith(".pdf"):
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

    if content_type != "application/pdf":
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
def _clean_snippet(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _unwrap(href: str) -> str:
    if href.startswith("//duckduckgo.com/l/") or "uddg=" in href:
        query = parse_qs(urlparse("https:" + href if href.startswith("//") else href).query)
        target = (query.get("uddg") or [""])[0]
        if target:
            return unquote(target)
    return "https:" + href if href.startswith("//") else href


def parse_search(html: str) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    blocks = re.split(r'class="result results_links', html)
    for block in blocks[1:]:
        link = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = _unwrap(link.group(1))
        if url in seen:
            continue
        seen.add(url)
        title = _clean_snippet(link.group(2))
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        body = _clean_snippet(snippet.group(1)) if snippet else ""
        results.append((title, url, body))
        if len(results) >= MAX_LINKS:
            break
    return results


async def web_search(query: str) -> str:
    """Search the web and return the top results as text.

    Uses Brave Search API when BRAVE_API_KEY is set, otherwise falls back to
    DuckDuckGo (which may be rate-limited or captcha'd from server IPs).
    """
    query = (query or "").strip()
    if not query:
        raise ToolError("عبارت جست‌وجو خالی بود.")

    if config.BRAVE_API_KEY:
        return await _brave_search(query)
    return await _duckduckgo_search(query)


async def _brave_search(query: str) -> str:
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": config.BRAVE_API_KEY,
    }
    url = "https://api.search.brave.com/res/v1/web/search"
    params = {"q": query, "count": str(MAX_LINKS)}
    try:
        timeout = aiohttp.ClientTimeout(total=config.TOOL_TIMEOUT, connect=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, params=params) as resp:
                if resp.status >= 400:
                    raise ToolError(f"جست‌وجو جواب نداد (کد {resp.status}).")
                data = await resp.json(content_type=None)
    except asyncio.TimeoutError as exc:
        raise ToolError("جست‌وجو دیر جواب داد و قطع شد.") from exc
    except aiohttp.ClientError as exc:
        raise ToolError(f"جست‌وجو نشد ({type(exc).__name__}).") from exc

    results = ((data.get("web") or {}).get("results") or [])[:MAX_LINKS]
    if not results:
        raise ToolError("نتیجه‌ای برای این عبارت پیدا نشد.")
    lines = [f"نتایج جست‌وجو برای «{query}»:"]
    for index, item in enumerate(results, start=1):
        title = item.get("title") or ""
        link = item.get("url") or ""
        body = item.get("description") or ""
        lines.append(f"{index}. {title}\n   {link}" + (f"\n   {body}" if body else ""))
    return "\n".join(lines)


async def _duckduckgo_search(query: str) -> str:
    from urllib.parse import quote

    encoded = quote(query, safe="")
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.8"}
    urls = [
        "https://lite.duckduckgo.com/lite/?q=" + encoded,
        "https://html.duckduckgo.com/html/?q=" + encoded,
    ]

    last_error: Exception | None = None
    html_text = ""
    for url in urls:
        for attempt in range(2):
            try:
                timeout = aiohttp.ClientTimeout(total=config.TOOL_TIMEOUT, connect=10)
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status >= 400:
                            last_error = ToolError(f"جست‌وجو جواب نداد (کد {resp.status}).")
                            break
                        html_text = await resp.text(errors="ignore")
                        if html_text and len(html_text) > 500:
                            break
            except asyncio.TimeoutError as exc:
                last_error = ToolError("جست‌وجو دیر جواب داد و قطع شد.")
            except aiohttp.ClientError as exc:
                last_error = ToolError(f"جست‌وجو نشد ({type(exc).__name__}).")
            if html_text and len(html_text) > 500:
                break
        if html_text and len(html_text) > 500:
            break

    if not (html_text and len(html_text) > 500):
        raise last_error or ToolError("جست‌وجو نشد.")

    # Detect bot challenge pages.
    low = html_text.lower()
    if "anomaly" in low or "challenge" in low or "captcha" in low:
        raise ToolError("موتور جست‌وجو فعلاً درخواست ربات را بلاک کرده است.")

    results = await asyncio.to_thread(parse_search, html_text)
    if not results:
        results = await asyncio.to_thread(_parse_lite, html_text)
    if not results:
        raise ToolError("نتیجه‌ای برای این عبارت پیدا نشد.")
    lines = [f"نتایج جست‌وجو برای «{query}»:"]
    for index, (title, link, body) in enumerate(results, start=1):
        lines.append(f"{index}. {title}\n   {link}" + (f"\n   {body}" if body else ""))
    return "\n".join(lines)


def _parse_lite(html: str) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    # DuckDuckGo Lite uses single quotes for class attributes.
    pattern = r'<a[^>]*href="([^"]+)"[^>]*class=[\"\']result-link[\"\'][^>]*>(.*?)</a>'
    for m in re.finditer(pattern, html, re.S):
        url = _unwrap(m.group(1))
        if url in seen:
            continue
        seen.add(url)
        results.append((_clean_snippet(m.group(2)), url, ""))
        if len(results) >= MAX_LINKS:
            break
    if not results:
        for m in re.finditer(r'<a[^>]*href="(https?://[^"]+)"[^>]*>([^<]{4,80})</a>', html):
            url = m.group(1)
            if url in seen or "duckduckgo" in url:
                continue
            seen.add(url)
            results.append((_clean_snippet(m.group(2)), url, ""))
            if len(results) >= MAX_LINKS:
                break
    return results


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
]

async def call(name: str, arguments: str) -> str:
    """Run one tool call and return its text result (never raises)."""
    try:
        data = json.loads(arguments or "{}")
        if not isinstance(data, dict):
            raise ToolError("ورودی ابزار نامعتبر بود.")
    except (ValueError, ToolError):
        return "خطا: ورودی ابزار JSON نامعتبر بود."

    try:
        if name == "read_link":
            return await read_link(str(data.get("url") or ""))
        if name == "web_search":
            return await web_search(str(data.get("query") or ""))
        if name == "get_time":
            return get_time()
        return f"خطا: ابزار «{name}» وجود ندارد. فقط read_link، web_search و get_time معتبرند."
    except ToolError as exc:
        return f"خطا: {exc}"
    except Exception:  # noqa: BLE001 - a tool must never kill the chat
        log.exception("tool %s failed", name)
        return "خطا: اجرای این ابزار نشد."


def describe(call_name: str, arguments: str) -> str:
    """Short Persian label for logs / debugging."""
    try:
        data = json.loads(arguments or "{}")
    except ValueError:
        data = {}
    target = data.get("url") or data.get("query") or ""
    return f"{call_name}({str(target)[:60]})"