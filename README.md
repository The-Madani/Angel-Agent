<p align="center">
  <img src="assets/angel.jpg" alt="Angel Agent" width="320">
</p>

<h1 align="center">Angel Agent</h1>

<p align="center">
ربات تلگرام دستیار هوش مصنوعی فارسی
<br>
استریم زنده · حافظهٔ SQLite · بکاپ رمزنگاری‌شده روی GitHub
<br><br>
<a href="https://9router.com">9router</a>
</p>

## امکانات

- چت خصوصی فارسی، لحن گرم و پاسخ کوتاه
- استریم زندهٔ پاسخ؛ متن فکر کردن مدل (`reasoning_content`) نمایش داده نمی‌شود
- حافظهٔ هر چت جدا در SQLite + خلاصه‌سازی خودکار گفتگوهای قدیمی
- آرشیو کامل پیام‌های قدیمی (بعد از خلاصه‌سازی حذف نمی‌شوند، فقط از context مدل کنار می‌روند)
- بکاپ رمزنگاری‌شده روی GitHub Releases + بازگردانی با `/restore`
- پنل ادمین، ضدحلقه (پیام خودش/ربات‌ها/کانال/کامنت)، rate limit

## اجرای محلی

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # مقادیر را پر کن
.venv/bin/python bot.py
```

## متغیرها (`.env` یا Railway Variables)

| کلید | توضیح |
|---|---|
| `BOT_TOKEN` | توکن ربات از BotFather |
| `ADMIN_IDS` | آیدی‌های ادمین، با کاما |
| `API_ID` / `API_HASH` | MTProto (پیش‌فرض‌ها کار می‌کنند) |
| `ROUTER_BASE_URL` | آدرس 9router (با `/v1`) |
| `ROUTER_API_KEY` | کلید 9router |
| `MODEL` | مثل `fbot` |
| `DB_PATH` | `./data/history.db` محلی، `/data/history.db` روی Railway |
| `GITHUB_BACKUP_REPO` | `owner/repo` ریپوی بکاپ (private) |
| `GITHUB_BACKUP_TOKEN` | fine-grained PAT، فقط `Contents: Read and write` روی همان ریپو |
| `BACKUP_PASSPHRASE` | رمز رمزنگاری بکاپ؛ گم شود، بکاپ باز نمی‌شود |
| `BACKUP_KEEP` | تعداد بکاپ نگه‌داری‌شده (پیش‌فرض ۱۴) |
| `BACKUP_AUTO_HOURS` | فاصلهٔ بکاپ خودکار (پیش‌فرض ۲۴، صفر = خاموش) |
| `RESTORE_ON_START` | اگر ۱ باشد و دیتابیس خالی باشد، موقع بالا آمدن آخرین بکاپ را خودکار ریستور می‌کند |

## دستورات

- `/start` `/help` — راهنما
- `/reset` — پاک کردن حافظهٔ همان چت
- `/id` — آیدی کاربر/چت
- ادمین: `/stats` `/compact` `/ping` `/backup` `/backups` `/restore`

## بکاپ و ریستور

- `/backup`: `VACUUM INTO` → gzip → AES-GCM → آپلود به‌صورت Release asset تاریخ‌دار؛ قدیمی‌ترها خودکار حذف می‌شوند.
- خودکار هر ۲۴ ساعت (اگر `BACKUP_AUTO_HOURS` صفر نباشد).
- `/backups`: لیست بکاپ‌ها.
- `/restore` سپس `/restore confirm`: آخرین بکاپ دانلود، رمزگشایی، `integrity_check`، یک نسخهٔ موقت از DB فعلی، سپس جایگزینی و ادامهٔ کار.

DB خام هرگز در گیت commit نمی‌شود؛ فقط نسخهٔ رمزنگاری‌شده در ریپوی private بکاپ قرار می‌گیرد.

## استقرار روی Railway

```bash
bash <(curl -fsSL railway.com/install.sh)   # یک‌بار
railway login
railway init        # یا railway link برای پروژهٔ موجود
./deploy.sh
```

`deploy.sh` سرویس `angel-bot`، ولوم `/data`، متغیرها و دیپلوی را انجام می‌دهد. برای دیپلوی خودکار با push، بعد از اولین دیپلوی:

```bash
railway service source connect --repo The-Madani/Angel-Agent --branch main --service angel-bot
```

### هر بار با اکانت جدید Railway

1. `railway login` با اکانت جدید
2. `railway init` (یا `railway link`) و `railway add --service angel-bot`
3. در Settings سرویس، منطقه را **US East** بگذار (قبل از ساخت ولوم)
4. `./deploy.sh`
5. در پیوی ربات: `/restore confirm`

بکاپ‌ها روی GitHub می‌مانند، پس با `/restore` همهٔ تاریخچه برمی‌گردد.

## نکات امنیتی

- ریپوی کد (`Angel-Agent`) می‌تواند public باشد؛ `.env`، دیتابیس و لاگ‌ها git-ignore شده‌اند.
- ریپوی `angel-backups` باید **private** بماند.
- توکن بکاپ را فقط به همان ریپوی بکاپ محدود کن.
- `BACKUP_PASSPHRASE` را در یک جای امن (پسورد منیجر) نگه دار.