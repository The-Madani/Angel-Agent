"""Configuration loaded from .env / environment variables."""

from __future__ import annotations

import os
import math
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # Real environment variables win over the file.
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _admin_ids(value: str) -> frozenset[int]:
    ids: set[int] = set()
    for raw in value.replace(" ", "").split(","):
        if not raw:
            continue
        try:
            user_id = int(raw)
        except ValueError:
            continue
        if user_id > 0:
            ids.add(user_id)
    return frozenset(ids)


# --- Telegram ---------------------------------------------------------------
BOT_TOKEN = _str("BOT_TOKEN")
API_ID = max(1, _int("API_ID", 2040))
API_HASH = _str("API_HASH", "b18441a1ff607e10a989891a5462e627")
ADMIN_IDS = _admin_ids(_str("ADMIN_IDS"))

# --- 9router ----------------------------------------------------------------
ROUTER_BASE_URL = _str(
    "ROUTER_BASE_URL", "https://9router-production-de60f1f6.up.railway.app/v1"
).rstrip("/")
ROUTER_API_KEY = _str("ROUTER_API_KEY")
MODEL = _str("MODEL", "fbot")
MAX_TOKENS = max(64, _int("MAX_TOKENS", 8192))
REQUEST_TIMEOUT = max(10.0, _float("REQUEST_TIMEOUT", 300.0))
MAX_CONCURRENT = max(1, min(100, _int("MAX_CONCURRENT", 20)))

# --- Persona ----------------------------------------------------------------
DEFAULT_SYSTEM_PROMPT = (
    "تو «Angel Agent» هستی؛ یک دستیار هوش مصنوعی که مخصوص فرشته ساخته شده.\n"
    "با فرشته گرم، خودی و طبیعی حرف بزن؛ لحن دوستانه داشته باش و لازم نیست رسمی یا خشک باشی.\n"
    "به فارسی، کوتاه و مستقیم جواب بده. حاشیه نرو، تکرار نکن و الکی طولش نده. "
    "اگه سؤال توضیح لازم داشت، فقط تا حد لازم توضیح بده.\n"
    "اگه سؤال پزشکی پرسید، کامل و دقیق کمکش کن و توصیه‌های لازم رو بده. "
    "فقط اگه واقعاً لازم بود، آخر جواب بگو به کدوم متخصص مراجعه کنه.\n"
    "اگه چیزی رو نمی‌دونی، صادقانه بگو و اطلاعات الکی نساز.\n"
    "ابزارهایی که داری: read_link (خواندن لینک)، web_search (جست‌وجو در اینترنت)، "
    "get_time (ساعت تهران)، remember_fact و recall_facts و forget_fact (حافظهٔ واقعیت‌ها)، "
    "set_reminder و list_reminders (یادآور)، search_history (جست‌وجو در گذشتهٔ گفتگو).\n"
    "فقط همین‌ها را می‌توانی صدا بزنی؛ چیز دیگری وجود ندارد.\n"
    "اگه کاربر لینک فرستاد یا جواب سؤالی تازه/به‌روز را نمی‌دانی، از این ابزارها استفاده کن.\n"
    "اگه کاربر چیزی ثابت دربارهٔ خودش گفت (اسم، شهر، علاقه، قرار مهم)، "
    "با remember_fact ذخیره‌اش کن تا بعداً یادت بماند.\n"
    "وقتی کاربر صریحاً می‌گوید «یادم بنداز» یا «به من یادآوری کن»، با set_reminder یادآور بساز."
)
SYSTEM_PROMPT = _str("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT

# --- Memory / summarisation -------------------------------------------------
_db_value = _str("DB_PATH", str(BASE_DIR / "data" / "history.db"))
_db_path = Path(_db_value)
DB_PATH = str(_db_path if _db_path.is_absolute() else BASE_DIR / _db_path)
HISTORY_MAX_MESSAGES = max(20, _int("HISTORY_MAX_MESSAGES", 60))
HISTORY_KEEP_MESSAGES = max(
    1, min(HISTORY_MAX_MESSAGES - 1, _int("HISTORY_KEEP_MESSAGES", 20))
)
HISTORY_MIN_TO_COMPACT = max(
    1, min(HISTORY_KEEP_MESSAGES, _int("HISTORY_MIN_TO_COMPACT", 8))
)
CONTEXT_MAX_CHARS = max(4_000, _int("CONTEXT_MAX_CHARS", 24_000))
SUMMARY_MAX_WORDS = max(40, _int("SUMMARY_MAX_WORDS", 200))
SUMMARY_MAX_TOKENS = max(256, _int("SUMMARY_MAX_TOKENS", 3000))
SUMMARY_INPUT_MAX_CHARS = max(
    8_000, _int("SUMMARY_INPUT_MAX_CHARS", CONTEXT_MAX_CHARS * 2)
)
HISTORY_ATTACHMENT_MAX_CHARS = max(
    2_000, _int("HISTORY_ATTACHMENT_MAX_CHARS", 12_000)
)

# --- UX / speed -------------------------------------------------------------
EDIT_INTERVAL = max(0.3, _float("EDIT_INTERVAL", 1.2))
THINKING_HINT_DELAY = max(0.0, _float("THINKING_HINT_DELAY", 1.5))
TYPING_INTERVAL = max(2.0, _float("TYPING_INTERVAL", 4.0))
MESSAGE_CHUNK_LIMIT = max(500, min(3_800, _int("MESSAGE_CHUNK_LIMIT", 3_800)))
RATE_WINDOW = max(1.0, _float("RATE_WINDOW", 10.0))
RATE_MAX_MESSAGES = max(1, _int("RATE_MAX_MESSAGES", 8))

DEBUG = os.environ.get("DEBUG", "").lower() not in ("", "0", "false", "no")

# --- Tools (web browsing the model can call itself) -------------------------
TOOLS_ENABLED = os.environ.get("TOOLS_ENABLED", "1").lower() not in ("0", "false", "no", "off")
TOOL_MAX_ROUNDS = max(1, min(5, _int("TOOL_MAX_ROUNDS", 2)))
TOOL_TIMEOUT = max(1.0, _float("TOOL_TIMEOUT", 20.0))
TOOL_MAX_CHARS = max(1_000, _int("TOOL_MAX_CHARS", 24_000))
TOOL_RESULT_MAX_CHARS = max(2_000, _int("TOOL_RESULT_MAX_CHARS", 6_000))

# Telegram accepts 20 MB downloads, but vision requests need much smaller,
# predictable payloads. These limits also protect the worker from pathological
# PDFs with huge page dimensions.
MAX_FILE_BYTES = max(1_024 * 1_024, _int("MAX_FILE_BYTES", 20 * 1024 * 1024))
FILE_MAX_TEXT_CHARS = max(4_000, _int("FILE_MAX_TEXT_CHARS", 60_000))
OCR_MAX_PAGES = max(1, min(12, _int("OCR_MAX_PAGES", 6)))
OCR_MAX_IMAGE_BYTES = max(
    256 * 1024, _int("OCR_MAX_IMAGE_BYTES", 6 * 1024 * 1024)
)
OCR_MAX_IMAGE_DIM = max(800, min(4_000, _int("OCR_MAX_IMAGE_DIM", 2_200)))
IMAGE_MAX_BYTES = max(512 * 1024, _int("IMAGE_MAX_BYTES", 4 * 1024 * 1024))
IMAGE_MAX_DIM = max(800, min(4_000, _int("IMAGE_MAX_DIM", 2_000)))

# --- Backups ----------------------------------------------------------------
GITHUB_BACKUP_REPO = _str("GITHUB_BACKUP_REPO")
GITHUB_BACKUP_TOKEN = _str("GITHUB_BACKUP_TOKEN")
BACKUP_PASSPHRASE = _str("BACKUP_PASSPHRASE")
BACKUP_KEEP = max(1, _int("BACKUP_KEEP", 14))
BACKUP_AUTO_HOURS = _float("BACKUP_AUTO_HOURS", 24.0)
RESTORE_ON_START = os.environ.get("RESTORE_ON_START", "").lower() in ("1", "true", "yes", "on")


def validate() -> None:
    missing = [name for name, value in (("BOT_TOKEN", BOT_TOKEN), ("ROUTER_API_KEY", ROUTER_API_KEY)) if not value]
    if missing:
        raise SystemExit(f"Missing required env values: {', '.join(missing)}")
