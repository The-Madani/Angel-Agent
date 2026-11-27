"""Turn files that users send into plain text for the model."""

from __future__ import annotations

import io
import mimetypes
import re
import zipfile
from pathlib import Path

# Telegram bots can download files up to 20 MB.
MAX_FILE_BYTES = 20 * 1024 * 1024
# Keep prompts (and the SQLite rows) from exploding on huge files.
MAX_TEXT_CHARS = 60_000

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
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            pages.append(f"[صفحه {index}]\n{text}")
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
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


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
    """Render PDF pages to PNG bytes so a vision model can read scanned docs."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise FileError("تبدیل PDF به تصویر ممکن نیست (کتابخانه نصب نیست).") from exc

    images: list[bytes] = []
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as exc:  # noqa: BLE001
        raise FileError("این PDF برای تبدیل به تصویر باز نشد.") from exc

    try:
        for index in range(min(len(doc), max_pages)):
            page = doc[index]
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil()
            buffer = io.BytesIO()
            pil.save(buffer, format="JPEG", quality=85, optimize=True)
            images.append(buffer.getvalue())
    finally:
        doc.close()
    return images