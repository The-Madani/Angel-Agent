<p align="center">
  <img src="assets/angel.jpg" alt="Angel Agent" width="320">
</p>

<h1 align="center">Angel Agent</h1>

<p align="center" dir="rtl">
  ربات تلگرام دستیار هوش مصنوعی فارسی<br>
  متصل به <a href="https://9router.com">9router</a> · استریم زنده · حافظهٔ SQLite · بکاپ رمزنگاری‌شده روی GitHub
</p>

## امکانات

- چت خصوصی فارسی، لحن گرم و پاسخ پیش‌فرض کوتاه؛ توضیح مفصل وقتی کاربر بخواهد
- استریم زندهٔ پاسخ؛ متن فکر کردن مدل (`reasoning_content`) نمایش داده نمی‌شود
- وضعیت زندهٔ «در حال فکر کردن…» با ثانیه‌شمار، داخل همان پیامی که جواب نهایی جایگزینش می‌شود
- حافظهٔ هر چت جدا در SQLite + خلاصه‌سازی خودکار گفتگوهای قدیمی
- آرشیو کامل پیام‌های قدیمی (بعد از خلاصه‌سازی حذف نمی‌شوند، فقط از context مدل کنار می‌روند)
- حافظهٔ «واقعیت‌ها»: چیزهای ثابت (اسم، شهر، علاقه‌ها، قرارها) جدا از خلاصه ذخیره و در هر پاسخ به مدل داده می‌شوند
- یادآور یک‌باره و روتین روزانه/هفتگی/با فاصلهٔ دلخواه، از حرف طبیعی کاربر؛ نمایش و لغو یادآورها
- ابزارهای وب برای مدل: خواندن لینک، جست‌وجو در اینترنت، ساعت تهران، جست‌وجو در تاریخچهٔ گفتگو
- فایل و عکس: PDF، Word، Excel، PowerPoint، متن/کد، عکس؛ PDF اسکن‌شده به تصویر تبدیل و با بینایی مدل خوانده می‌شود
- ویس و فایل صوتی: تبدیل خودکار گفتار به متن با 9router و پاسخ به محتوای آن؛ متن در حافظهٔ گفتگو ذخیره می‌شود
- ریپلای: متن/فایل/عکس/ویس پیامِ نقل‌شده هم دیده می‌شود (حتی بعد از `/reset`)
- بکاپ رمزنگاری‌شده روی GitHub Releases + بازگردانی با `/restore`
- پنل ادمین، ضدحلقه (پیام خودش/ربات‌ها/کانال/کامنت)، rate limit

## اجرای محلی

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # مقادیر را پر کن
.venv/bin/python bot.py
```

## متغیرها: کجا و چه چیزی را پر کن

دو جا می‌توانی متغیر بگذاری:

- **محلی** (توسعه): فایل `.env` در ریشهٔ پروژه (از `.env.example` کپی کن). این فایل در `.gitignore` است و هرگز commit نمی‌شود.
- **Railway** (تولید): داشبورد پروژه → سرویس `angel-bot` → تب **Variables**. یا با CLI:
  `railway variable set -s angel-bot "KEY=VALUE"`.

> اگر هم `.env` داشته باشی و هم Railway Variables، روی Railway مقدارِ Variable اولویت دارد (متغیر محیطی واقعی بر فایل `.env` مقدم است).

### ۱. الزامی — بدون این‌ها ربات بالا نمی‌آید

`config.validate()` فقط این دو را چک می‌کند و اگر خالی باشند با پیام خطا خارج می‌شود.

| کلید | از کجا بگیرم | توضیح |
|---|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` یا `/token` | توکن ربات |
| `ROUTER_API_KEY` | داشبورد 9router → API Keys | همین یک کلید برای **چت**، **جست‌وجوی وب** و **تبدیل صدا به متن** استفاده می‌شود؛ کلید جدا لازم نیست |

### ۲. توصیه‌شده برای استقرار

| کلید | مقدار نمونه | توضیح |
|---|---|---|
| `DB_PATH` | `/data/history.db` روی Railway، `./data/history.db` محلی | مسیر SQLite؛ روی Railway باید داخل ولوم باشد |
| `ROUTER_BASE_URL` | `https://9router-production-de60f1f6.up.railway.app/v1` | آدرس 9router (با `/v1`) |
| `MODEL` | `fbot` | نام مدل در 9router |
| `ADMIN_IDS` | `123456789` | آیدی عددی ادمین‌ها، با کاما. بقیه دستورهای ادمین را نمی‌بینند |
| `RESTORE_ON_START` | `1` | اگر دیتابیس خالی باشد، موقع بالا آمدن آخرین بکاپ گیت‌هاب را خودکار برمی‌گرداند |

### ۳. بکاپ (اختیاری — بدون این‌ها ربات کار می‌کند، فقط بکاپ گیت‌هاب غیرفعال است)

| کلید | از کجا بگیرم | توضیح |
|---|---|---|
| `GITHUB_BACKUP_REPO` | — | مثل `owner/angel-backups`؛ ریپوی **private** جدا از ریپوی کد |
| `GITHUB_BACKUP_TOKEN` | GitHub → Settings → Developer settings → **Fine-grained tokens** | فقط `Contents: Read and write` روی همان ریپوی بکاپ؛ به ریپوی کد دسترسی نده |
| `BACKUP_PASSPHRASE` | خودت بساز (مثلاً `openssl rand -base64 24`) | رمز AES-GCM؛ **گم شود بکاپ‌ها باز نمی‌شوند** — یک نسخهٔ آفلاین نگه دار |
| `BACKUP_KEEP` | `14` | تعداد بکاپ‌های نگه‌داری‌شده |
| `BACKUP_AUTO_HOURS` | `24` | فاصلهٔ بکاپ خودکار؛ `0` = خاموش |

### ۴. تنظیم دقیق (اختیاری — پیش‌فرض‌ها خوب‌اند)

| کلید | پیش‌فرض | توضیح |
|---|---|---|
| `TOOLS_ENABLED` | `1` | ابزارهای وب/حافظه/یادآور برای مدل |
| `TOOL_MAX_ROUNDS` | `2` | حداکثر دور صدا زدن ابزار در یک پاسخ |
| `TOOL_TIMEOUT` | `20` | مهلت هر ابزار (ثانیه) |
| `TOOL_MAX_CHARS` | `24000` | سقف متن برگشتی از لینک/فایل |
| `TOOL_RESULT_MAX_CHARS` | `6000` | سقف نتیجهٔ هر ابزار داخل context مدل |
| `MAX_TOKENS` | `8192` | سقف توکن پاسخ |
| `CONTEXT_MAX_CHARS` | `24000` | سقف کاراکتر تاریخچه در هر درخواست |
| `SUMMARY_INPUT_MAX_CHARS` | `48000` | سقف متن ورودی خلاصه‌ساز |
| `HISTORY_ATTACHMENT_MAX_CHARS` | `12000` | سقف متن فایل ذخیره‌شده در history |
| `HISTORY_MAX_MESSAGES` | `60` | آستانهٔ خلاصه‌سازی خودکار |
| `HISTORY_KEEP_MESSAGES` | `20` | چند پیام تازه بعد از خلاصه‌سازی بماند |
| `EDIT_INTERVAL` | `0.6` | حداقل فاصلهٔ ادیت پیام استریم (ثانیه) |
| `EDIT_INTERVAL_MAX` | `2.0` | سقف فاصلهٔ ادیت بعد از `FloodWait` (ثانیه) |
| `EDIT_MIN_DELTA` | `12` | حداقل کاراکتر جدید برای ادیت بعدی |
| `THINKING_HINT_DELAY` | `0.8` | بعد از چند ثانیه وضعیت «در حال فکر کردن…» در همان پیام نمایش داده شود |
| `THINKING_TICK_INTERVAL` | `2.0` | فاصلهٔ به‌روزرسانی ثانیه‌شمار وضعیت (ثانیه) |
| `TRANSCRIPTION_MODEL` | `gemini/gemini-2.5-flash` | مدل تبدیل صدا در `/v1/audio/transcriptions` |
| `TRANSCRIPTION_TIMEOUT` | `120` | مهلت دانلود صدا و مهلت درخواست تبدیل، هرکدام به ثانیه |
| `AUDIO_MAX_BYTES` | `20971520` | سقف حجم صدا؛ حداکثر ۲۰ مگابایت و تابع `MAX_FILE_BYTES` |
| `RATE_MAX_MESSAGES` | `8` | سقف پیام کاربر در `RATE_WINDOW` (پیش‌فرض ۱۰ ثانیه) |
| `SYSTEM_PROMPT` | (متن پیش‌فرض در `config.py`) | جایگزینی کامل شخصیت؛ خالی = پیش‌فرض |
| `DEBUG` | خالی | `1` = لاگ کامل آپدیت‌ها |

برای OCR و تصاویر هم `OCR_MAX_PAGES`، `OCR_MAX_IMAGE_BYTES`، `OCR_MAX_IMAGE_DIM`، `IMAGE_MAX_BYTES` و `IMAGE_MAX_DIM` قابل تنظیم‌اند؛ پیش‌فرض‌ها برای جلوگیری از درخواست‌های سنگین مناسب‌اند.

### نمونهٔ کامل `.env`

```dotenv
# --- الزامی ---
BOT_TOKEN=123456:AA...
ROUTER_API_KEY=sk-...

# --- توصیه‌شده ---
ADMIN_IDS=123456789
API_ID=2040
API_HASH=b18441a1ff607e10a989891a5462e627
ROUTER_BASE_URL=https://9router-production-de60f1f6.up.railway.app/v1
MODEL=fbot
DB_PATH=./data/history.db
RESTORE_ON_START=1

# --- بکاپ (اختیاری) ---
GITHUB_BACKUP_REPO=owner/angel-backups
GITHUB_BACKUP_TOKEN=github_pat_...
BACKUP_PASSPHRASE=...
BACKUP_KEEP=14
BACKUP_AUTO_HOURS=24

# --- تنظیم دقیق (اختیاری) ---
TOOLS_ENABLED=1
TOOL_MAX_ROUNDS=2
TOOL_TIMEOUT=20
```

### متغیرهایی که روی Railway ست شده‌اند

```
DB_PATH=/data/history.db
MODEL=fbot
ROUTER_BASE_URL=https://9router-production-de60f1f6.up.railway.app/v1
RESTORE_ON_START=1
TOOLS_ENABLED=1
TOOL_MAX_ROUNDS=2
TOOL_TIMEOUT=20
TOOL_MAX_CHARS=24000
MESSAGE_CHUNK_LIMIT=3800
```
به‌علاوهٔ `BOT_TOKEN`، `ROUTER_API_KEY`، `ADMIN_IDS`، `GITHUB_BACKUP_*` و `BACKUP_PASSPHRASE` (مقادیرشان فقط در Railway Variables است، نه در گیت).

## دستورات

- `/start` `/help` — راهنما
- `/reset` — پاک کردن حافظهٔ همان چت
- `/id` — آیدی کاربر/چت
- ادمین: `/stats` `/compact` `/ping` `/backup` `/backups` `/restore`

برای صحبت با ربات، ویس یا فایل صوتی (مثل OGG، MP3، WAV یا M4A) بفرست. ربات متن گفتار را مثل پیام متنی پردازش می‌کند؛ مثلاً می‌توانی با ویس هم سؤال بپرسی یا روتین روزانه بسازی. وضعیت «تبدیل صدا به متن» و جواب در همان پیام نمایش داده می‌شوند. فایل صوتی پس از پردازش از فضای موقت پاک می‌شود و فقط متن آن در حافظه می‌ماند. برای دریافت خودِ متن هم بگو «فقط متن این ویس رو بنویس» یا روی ویس ریپلای کن.

## ابزارهای مدل (بدون دستور)

مدل خودش تصمیم می‌گیرد کدام ابزار را صدا بزند؛ لازم نیست کاربر دستور بزند.

| ابزار | کار |
|---|---|
| `read_link` | خواندن متن یک لینک (HTML/PDF/JSON/XML)؛ ویکی‌پدیا از API |
| `web_search` | جست‌وجوی وب از طریق `9router /search` (مدل `tinyfish`) |
| `get_time` | تاریخ و ساعت تهران |
| `remember_fact` / `recall_facts` / `forget_fact` | حافظهٔ واقعیت‌های ثابت هر چت |
| `set_reminder` / `list_reminders` / `cancel_reminder` | ساخت، دیدن زمان بعدی و دورهٔ تکرار، و لغو یادآورهای همان چت؛ ارسال توسط حلقهٔ پس‌زمینه |
| `search_history` | جست‌وجو در همهٔ پیام‌های قبلی (فعال + آرشیو) |

مثال‌هایی که مدل خودش ابزار را انتخاب می‌کند:

- «اسم من ساراست و توی شیراز زندگی می‌کنم» → `remember_fact`
- «من کجا زندگی می‌کنم؟» → واقعیت‌ها مستقیم در context هستند، بدون ابزار
- «۲ ساعت دیگه یادم بنداز آب بخورم» → `set_reminder` (بدون دستور `/remind`)
- «هر روز ساعت ۹ یادم بنداز ورزش کنم» → یادآور روزانه
- «هر جمعه ساعت ۱۸ یادم بنداز برنامهٔ هفته رو بنویسم» → یادآور هفتگی
- «هر ۲ ساعت یادم بنداز آب بخورم» → تکرار هر ۱۲۰ دقیقه، شروع از ۲ ساعت دیگر
- «یادآورهای من رو نشون بده» / «یادآور ورزش رو لغو کن» → نمایش / لغو
- «هوای شیراز چطوره؟» → `web_search` + `read_link`
- «پیدا کن قبلاً دربارهٔ سفر چی گفتم» → `search_history`

زمان یادآورها به وقت تهران است. اگر ساعت روتین مشخص نباشد، ربات می‌پرسد. زمان‌بندی‌ها در SQLite ذخیره می‌شوند و بعد از ری‌استارت هم ادامه دارند؛ دیتابیس قبلی خودکار ارتقا می‌یابد. حلقهٔ یادآورها هر ۳۰ ثانیه بررسی می‌کند. بعد از قطعی، هر یادآور عقب‌افتاده یک بار ارسال می‌شود و روتین به اولین نوبت آینده برمی‌گردد؛ ساعت اصلی آن جابه‌جا نمی‌شود.

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
- هیچ کلیدی در کد هاردکد نمی‌شود؛ همه از `config.py` → env خوانده می‌شوند. اگر ریپو را public می‌کنی، فقط `.env.example` (بدون مقدار) در گیت است.
- ریپوی `angel-backups` باید **private** بماند.
- توکن بکاپ را فقط به همان ریپوی بکاپ محدود کن.
- `BACKUP_PASSPHRASE` را در یک جای امن (پسورد منیجر) نگه دار.
- `ROUTER_API_KEY` را در متن/چت/اسکرین‌شات عمومی نگذار؛ اگر لو رفت، در داشبورد 9router کلید را revoke و کلید تازه بساز و در Railway Variables و `.env` عوض کن.

## مجوز

این پروژه تحت [GNU General Public License نسخهٔ ۳ (GPL-3.0-only)](LICENSE) منتشر می‌شود.
