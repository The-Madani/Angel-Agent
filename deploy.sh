#!/usr/bin/env bash
# Deploy Angel Agent to Railway from this directory.
# Requires: Railway CLI logged in, a linked project (or run `railway init` first).
set -euo pipefail

SERVICE="${SERVICE:-angel-bot}"
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "فایل .env پیدا نشد."
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

: "${BOT_TOKEN:?BOT_TOKEN is missing in .env}"
: "${ROUTER_API_KEY:?ROUTER_API_KEY is missing in .env}"
: "${ADMIN_IDS:?ADMIN_IDS is missing in .env}"

command -v railway >/dev/null || {
  echo "Railway CLI نصب نیست. اول این را اجرا کن:"
  echo "  bash <(curl -fsSL railway.com/install.sh)"
  exit 1
}

railway whoami >/dev/null 2>&1 || railway login

if ! railway status >/dev/null 2>&1; then
  echo "هیچ پروژه‌ای به این پوشه لینک نیست."
  echo "اول یکی از این دو را بزن:"
  echo "  railway init        # پروژه جدید"
  echo "  railway link        # اتصال به پروژه موجود"
  exit 1
fi

echo "→ سرویس: $SERVICE"
railway service "$SERVICE" >/dev/null 2>&1 || railway add --service "$SERVICE"

echo "→ ولوم /data"
railway volume add --mount-path /data -s "$SERVICE" >/dev/null 2>&1 \
  && echo "  volume created" \
  || echo "  volume already exists (یا ساختش رد شد)"

echo "→ متغیرها"
printf '%s' "$BOT_TOKEN" | railway variable set BOT_TOKEN --stdin -s "$SERVICE" --skip-deploys >/dev/null
printf '%s' "$ROUTER_API_KEY" | railway variable set ROUTER_API_KEY --stdin -s "$SERVICE" --skip-deploys >/dev/null
if [ -n "${GITHUB_BACKUP_TOKEN:-}" ]; then
  printf '%s' "$GITHUB_BACKUP_TOKEN" | railway variable set GITHUB_BACKUP_TOKEN --stdin -s "$SERVICE" --skip-deploys >/dev/null
fi
if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
  printf '%s' "$BACKUP_PASSPHRASE" | railway variable set BACKUP_PASSPHRASE --stdin -s "$SERVICE" --skip-deploys >/dev/null
fi

railway variable set -s "$SERVICE" --skip-deploys \
  ADMIN_IDS="$ADMIN_IDS" \
  API_ID="${API_ID:-2040}" \
  API_HASH="${API_HASH:-b18441a1ff607e10a989891a5462e627}" \
  ROUTER_BASE_URL="$ROUTER_BASE_URL" \
  MODEL="${MODEL:-fbot}" \
  DB_PATH=/data/history.db \
  GITHUB_BACKUP_REPO="${GITHUB_BACKUP_REPO:-}" \
  BACKUP_KEEP="${BACKUP_KEEP:-14}" \
  BACKUP_AUTO_HOURS="${BACKUP_AUTO_HOURS:-24}" \
  RAILWAY_DEPLOYMENT_DRAINING_SECONDS=20 >/dev/null

echo "→ دیپلوی"
railway up -s "$SERVICE"

echo
echo "تمام. لاگ‌ها: railway logs -s $SERVICE"
echo "یادت نره: بعد از بالا اومدن، توی پیوی ربات /restore confirm را بزن."