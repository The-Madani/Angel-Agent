<p align="center">
  <img src="assets/angel.jpg" alt="Angel Agent" width="320">
</p>

<h1 align="center">Angel Agent</h1>

<p align="center" dir="rtl">
  ربات تلگرام دستیار هوش مصنوعی فارسی<br>
  متصل به <a href="https://9router.com">9router</a> · استریم زنده · حافظهٔ SQLite · بکاپ رمزنگاری‌شده روی GitHub
</p>

## امکانات

- چت خصوصی فارسی، لحن گرم و پاسخ کوتاه
- استریم زندهٔ پاسخ؛ متن فکر کردن مدل (`reasoning_content`) نمایش داده نمی‌شود
- حافظهٔ هر چت جدا در SQLite + خلاصه‌سازی خودکار گفتگوهای قدیمی
- آرشیو کامل پیام‌های قدیمی (بعد از خلاصه‌سازی حذف نمی‌شوند، فقط از context مدل کنار می‌روند)
- حافظهٔ «واقعیت‌ها»: چیزهای ثابت (اسم، شهر، علاقه‌ها، قرارها) جدا از خلاصه ذخیره و در هر پاسخ به مدل داده می‌شوند
- یادآور: ربات از حرف طبیعی کاربر یادآور می‌سازد («۲ ساعت دیگه یادم بنداز…») و سر وقت پیام می‌دهد
- ابزارهای وب برای مدل: خواندن لینک، جست‌وجو در اینترنت، ساعت تهران، جست‌وجو در تاریخچهٔ گفتگو
- فایل و عکس: PDF، Word، Excel، PowerPoint، متن/کد، عکس؛ PDF اسکن‌شده به تصویر تبدیل و با بینایی مدل خوانده می‌شود
- ریپلای: متن/فایل/عکس پیامِ نقل‌شده هم دیده می‌شود (حتی بعد از `/reset`)
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
| `TOOLS_ENABLED` | ابزارهای وب/حافظه/یادآور برای مدل (پیش‌فرض ۱) |
| `TOOL_MAX_ROUNDS` | حداکثر دور صدا زدن ابزار در هر پاسخ (پیش‌فرض ۲) |
| `TOOL_TIMEOUT` | مهلت هر ابزار به ثانیه (پیش‌فرض ۲۰) |

## دستورات

- `/start` `/help` — راهنما
- `/reset` — پاک کردن حافظهٔ همان چت
- `/id` — آیدی کاربر/چت
- ادمین: `/stats` `/compact` `/ping` `/backup` `/backups` `/restore`

## ابزارهای مدل (بدون دستور)

مدل خودش تصمیم می‌گیرد کدام ابزار را صدا بزند؛ لازم نیست کاربر دستور بزند.

| ابزار | کار |
|---|---|
| `read_link` | خواندن متن یک لینک (HTML/PDF/JSON/XML)؛ ویکی‌پدیا از API |
| `web_search` | جست‌وجوی وب از طریق `9router /search` (مدل `tinyfish`) |
| `get_time` | تاریخ و ساعت تهران |
| `remember_fact` / `recall_facts` / `forget_fact` | حافظهٔ واقعیت‌های ثابت هر چت |
| `set_reminder` / `list_reminders` | ساخت و دیدن یادآور؛ ارسال سر وقت توسط حلقهٔ پس‌زمینه |
| `search_history` | جست‌وجو در همهٔ پیام‌های قبلی (فعال + آرشیو) |

مثال‌هایی که مدل خودش ابزار را انتخاب می‌کند:

- «اسم من فرشته‌ست و کرمانشاه زندگی می‌کنم» → `remember_fact`
- «من کجا زندگی می‌کنم؟» → واقعیت‌ها مستقیم در context هستند، بدون ابزار
- «۲ ساعت دیگه یادم بنداز آب بخورم» → `set_reminder` (بدون دستور `/remind`)
- «هوای کرمانشاه چطوره؟» → `web_search` + `read_link`
- «پیدا کن قبلاً دربارهٔ سفر چی گفتم» → `search_history`

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