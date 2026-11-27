"""Turn files that users send into plain text for the model."""

from __future__ import annotations

import io
import html
import mimetypes
import re
import zipfile
from pathlib import Path

import config

# Telegram bots can download files up to 20 MB.
MAX_FILE_BYTES = config.MAX_FILE_BYTES
# Keep prompts (and the SQLite rows) from exploding on huge files.
MAX_TEXT_CHARS = config.FILE_MAX_TEXT_CHARS

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py",
    ".sh", ".bash", ".zsh", ".sql", ".ini", ".cfg", ".conf", ".toml", ".log",
    ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".kt", ".rb", ".php",
    ".env", ".gitignore", ".dockerfile",
}

SUPPORTED_HINT = (
    "PDF، Word (docx)، Excel (xlsx)، PowerPoint (pptx)، "
    "فایل‌های متنی مثل txt / md / csv / json / کد، و عکس"
)


class FileError(RuntimeError):
    """The file could not be turned into text."""


class UnsupportedFile(FileError):
    pass


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS].rstrip() + "\n\n[… بقیهٔ فایل طولانی بود و خوانده نشد]"


# --------------------------------------------------------------------- formats
def _pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise FileError("کتابخانهٔ PDF نصب نیست.") from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise FileError("این فایل PDF سالم نیست یا ناقص دانلود شده.") from exc
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise FileError("این PDF رمز عبور دارد و باز نشد.") from exc
    pages: list[str] = []
    used = 0
    page_count = min(len(reader.pages), 80)
    for index in range(page_count):
        page = reader.pages[index]
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            part = f"[صفحه {index + 1}]\n{_clean(text)}"
            remaining = MAX_TEXT_CHARS - used
            if remaining <= 0:
                break
            pages.append(part[:remaining])
            used += len(part)
    return "\n\n".join(pages)


def _docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "ignore")
    xml = xml.replace("</w:p>", "\n")
    xml = xml.replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
    text = re.sub(r"<[^>]+>", "", xml)
    return _unescape(text)


def _pptx(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            name for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        )
        for number, name in enumerate(slides, start=1):
            xml = archive.read(name).decode("utf-8", "ignore")
            lines = [_unescape(m) for m in re.findall(r"<a:t[^>]*>(.*?)</a:t>", xml, re.S)]
            body = "\n".join(line for line in lines if line.strip())
            if body.strip():
                parts.append(f"[اسلاید {number}]\n{body}")
    return "\n\n".join(parts)


def _xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            xml = archive.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for si in re.findall(r"<si>(.*?)</si>", xml, re.S):
                shared.append("".join(_unescape(t) for t in re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))

        sheets = sorted(
            name for name in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        out: list[str] = []
        for name in sheets[:3]:  # protect against monster workbooks
            xml = archive.read(name).decode("utf-8", "ignore")
            rows: list[str] = []
            for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S)[:2000]:
                cells: list[str] = []
                for attrs, body in re.findall(r"<c([^>]*)>(.*?)</c>", row, re.S):
                    value = re.search(r"<v>(.*?)</v>", body, re.S)
                    inline = re.search(r"<is>(.*?)</is>", body, re.S)
                    if 't="s"' in attrs and value:
                        try:
                            cells.append(shared[int(value.group(1))])
                        except (ValueError, IndexError):
                            cells.append(value.group(1))
                    elif inline:
                        cells.append("".join(_unescape(t) for t in re.findall(r"<t[^>]*>(.*?)</t>", inline.group(1), re.S)))
                    elif value:
                        cells.append(_unescape(value.group(1)))
                    else:
                        cells.append("")
                line = "\t".join(cell.strip() for cell in cells).strip()
                if line:
                    rows.append(line)
            if rows:
                out.append(f"[شیت {name.rsplit('/', 1)[-1]}]\n" + "\n".join(rows))
    return "\n\n".join(out)


def _text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1256", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _unescape(text: str) -> str:
    return html.unescape(text)


# ------------------------------------------------------------------ public API
def extract_text(path: Path, filename: str = "", mime_type: str = "") -> str:
    """Extract readable text from a downloaded file.

    Raises UnsupportedFile when the type is unknown, FileError when it is
    broken or has no readable text.
    """
    suffix = Path(filename or path.name).suffix.lower()
    if not suffix and mime_type:
        suffix = (mimetypes.guess_extension(mime_type.split(";", 1)[0].strip()) or "").lower()
        if suffix == ".jpe":
            suffix = ".jpg"
    try:
        if suffix == ".pdf":
            text = _pdf(path)
        elif suffix == ".docx":
            text = _docx(path)
        elif suffix == ".pptx":
            text = _pptx(path)
        elif suffix == ".xlsx":
            text = _xlsx(path)
        elif suffix == ".doc":
            raise UnsupportedFile("فرمت قدیمی .doc پشتیبانی نمی‌شود؛ با فرمت .docx بفرست.")
        elif suffix in TEXT_SUFFIXES or suffix == "":
            text = _text(path)
        else:
            raise UnsupportedFile(f"فرمت «{suffix}» را نمی‌توانم بخوانم.")
    except zipfile.BadZipFile as exc:
        raise FileError("فایل خراب یا ناقص است.") from exc

    text = _clean(text)
    if not text:
        raise FileError(
            "متنی داخل فایل پیدا نشد"
            + (" (احتمالاً PDF اسکن‌شده است)." if suffix == ".pdf" else ".")
        )
    return _truncate(text)


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_SUFFIXES


def pdf_to_images(path: Path, max_pages: int = 6, scale: float = 2.0) -> list[bytes]:
    """Render a bounded number of PDF pages to JPEG bytes for vision input."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise FileError("تبدیل PDF به تصویر ممکن نیست (کتابخانه نصب نیست).") from exc

    images: list[bytes] = []
    total_bytes = 0
    max_pages = max(1, min(int(max_pages), config.OCR_MAX_PAGES))
    scale = max(0.5, min(float(scale), 3.0))
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as exc:  # noqa: BLE001
        raise FileError("این PDF برای تبدیل به تصویر باز نشد.") from exc

    try:
        for index in range(min(len(doc), max_pages)):
            page = doc[index]
            bitmap = None
            try:
                width, height = page.get_size()
                if min(width, height) <= 0 or max(width, height) > 50_000:
                    raise FileError("ابعاد یکی از صفحه‌های PDF بیش از حد بزرگ است.")
                page_scale = min(
                    scale,
                    config.OCR_MAX_IMAGE_DIM / max(float(width), float(height), 1.0),
                )
                bitmap = page.render(scale=page_scale)
                pil = bitmap.to_pil()
                pil.thumbnail(
                    (config.OCR_MAX_IMAGE_DIM, config.OCR_MAX_IMAGE_DIM)
                )

                encoded: bytes | None = None
                for quality in (85, 72, 60):
                    buffer = io.BytesIO()
                    pil.save(buffer, format="JPEG", quality=quality, optimize=True)
                    candidate = buffer.getvalue()
                    encoded = candidate
                    if len(candidate) <= config.OCR_MAX_IMAGE_BYTES:
                        break
                if encoded is None:
                    continue
                if total_bytes + len(encoded) > config.OCR_MAX_IMAGE_BYTES:
                    break
                images.append(encoded)
                total_bytes += len(encoded)
            finally:
                close_bitmap = getattr(bitmap, "close", None)
                if close_bitmap:
                    close_bitmap()
                close_page = getattr(page, "close", None)
                if close_page:
                    close_page()
    finally:
        doc.close()
    if not images:
        raise FileError("هیچ صفحه‌ای از این PDF برای خواندن تصویر تولید نشد.")
    return images


def optimize_image(raw: bytes) -> bytes:
    """Resize/compress a Telegram image before embedding it as base64."""
    if len(raw) <= config.IMAGE_MAX_BYTES:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as image:
                if max(image.size) <= config.IMAGE_MAX_DIM:
                    return raw
        except Exception:
            return raw

    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 60_000_000:
                raise FileError("ابعاد تصویر برای پردازش بیش از حد بزرگ است.")
            if image.mode in ("RGBA", "LA"):
                background = Image.new("RGB", image.size, "white")
                alpha = image.getchannel("A")
                background.paste(image, mask=alpha)
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((config.IMAGE_MAX_DIM, config.IMAGE_MAX_DIM))

            encoded = raw
            for quality in (85, 72, 60):
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                encoded = buffer.getvalue()
                if len(encoded) <= config.IMAGE_MAX_BYTES:
                    break
            return encoded
    except FileError:
        raise
    except Exception:
        return raw
