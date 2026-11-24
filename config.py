"""Configuration loaded from .env / environment variables."""

from __future__ import annotations

import os
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
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Telegram ---------------------------------------------------------------
BOT_TOKEN = _str("BOT_TOKEN")
API_ID = _int("API_ID", 2040)
API_HASH = _str("API_HASH", "b18441a1ff607e10a989891a5462e627")
ADMIN_IDS: frozenset[int] = frozenset(
    int(x) for x in _str("ADMIN_IDS").replace(" ", "").split(",") if x
)

# --- 9router ----------------------------------------------------------------
ROUTER_BASE_URL = _str(
    "ROUTER_BASE_URL", "https://9router-production-de60f1f6.up.railway.app/v1"
).rstrip("/")
ROUTER_API_KEY = _str("ROUTER_API_KEY")
MODEL = _str("MODEL", "fbot")
MAX_TOKENS = _int("MAX_TOKENS", 8192)
REQUEST_TIMEOUT = _float("REQUEST_TIMEOUT", 180.0)
MAX_CONCURRENT = _int("MAX_CONCURRENT", 20)

# --- Persona ----------------------------------------------------------------
DEFAULT_SYSTEM_PROMPT = (
    "تو «Angel Agent» هستی؛ یک دستیار هوش مصنوعی که مخصوص فرشته ساخته شده.\n"
    "با فرشته گرم، خودی و طبیعی حرف بزن؛ لحن دوستانه داشته باش و لازم نیست رسمی یا خشک باشی.\n"
    "به فارسی، کوتاه و مستقیم جواب بده. حاشیه نرو، تکرار نکن و الکی طولش نده. "
    "اگه سؤال توضیح لازم داشت، فقط تا حد لازم توضیح بده.\n"
    "اگه سؤال پزشکی پرسید، کامل و دقیق کمکش کن و توصیه‌های لازم رو بده. "
    "فقط اگه واقعاً لازم بود، آخر جواب بگو به کدوم متخصص مراجعه کنه.\n"
    "اگه چیزی رو نمی‌دونی، صادقانه بگو و اطلاعات الکی نساز."
)
SYSTEM_PROMPT = _str("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

# --- Memory / summarisation -------------------------------------------------
_db_value = _str("DB_PATH", str(BASE_DIR / "data" / "history.db"))
_db_path = Path(_db_value)
DB_PATH = str(_db_path if _db_path.is_absolute() else BASE_DIR / _db_path)
HISTORY_MAX_MESSAGES = _int("HISTORY_MAX_MESSAGES", 60)
HISTORY_KEEP_MESSAGES = _int("HISTORY_KEEP_MESSAGES", 20)
HISTORY_MIN_TO_COMPACT = _int("HISTORY_MIN_TO_COMPACT", 8)
CONTEXT_MAX_CHARS = _int("CONTEXT_MAX_CHARS", 24000)
SUMMARY_MAX_WORDS = _int("SUMMARY_MAX_WORDS", 200)
SUMMARY_MAX_TOKENS = _int("SUMMARY_MAX_TOKENS", 3000)

# --- UX / speed -------------------------------------------------------------
EDIT_INTERVAL = _float("EDIT_INTERVAL", 1.2)
THINKING_HINT_DELAY = _float("THINKING_HINT_DELAY", 1.5)
TYPING_INTERVAL = _float("TYPING_INTERVAL", 4.0)
MESSAGE_CHUNK_LIMIT = _int("MESSAGE_CHUNK_LIMIT", 3800)
RATE_WINDOW = _float("RATE_WINDOW", 10.0)
RATE_MAX_MESSAGES = _int("RATE_MAX_MESSAGES", 8)

DEBUG = os.environ.get("DEBUG", "").lower() not in ("", "0", "false", "no")

# --- Backups ----------------------------------------------------------------
GITHUB_BACKUP_REPO = _str("GITHUB_BACKUP_REPO")
GITHUB_BACKUP_TOKEN = _str("GITHUB_BACKUP_TOKEN")
BACKUP_PASSPHRASE = _str("BACKUP_PASSPHRASE")
BACKUP_KEEP = max(1, _int("BACKUP_KEEP", 14))
BACKUP_AUTO_HOURS = _float("BACKUP_AUTO_HOURS", 24.0)


def validate() -> None:
    missing = [name for name, value in (("BOT_TOKEN", BOT_TOKEN), ("ROUTER_API_KEY", ROUTER_API_KEY)) if not value]
    if missing:
        raise SystemExit(f"Missing required env values: {', '.join(missing)}")