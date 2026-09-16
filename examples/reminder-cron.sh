#!/usr/bin/env bash
# 订阅到期提醒 —— 定时任务用的示例脚本（没到期就一句话都不说）
#
# crontab 用法（每天 9:00）:
#   0 9 * * * $HOME/subs-butler/examples/reminder-cron.sh >> /tmp/subs-reminder.log 2>&1
#
# 环境变量:
#   SUBS_DIR     项目目录（默认脚本的上层目录）
#   SUBS_WINDOW  提前几天提醒（默认 7）
#   MAC_NOTIFY   1=在 macOS 弹通知中心横幅（默认 1，非 macOS 自动跳过）
#   TG_BOT_TOKEN / TG_CHAT_ID  填了就同时推 Telegram（可选）
set -u

DIR="${SUBS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
WINDOW="${SUBS_WINDOW:-7}"

OUT="$(python3 "$DIR/subs.py" check -d "$WINDOW")"
[ -z "$OUT" ] && exit 0                 # 没有要续的: 静静退出

echo "$OUT"                             # cron 会把 stdout 邮给你(如果配了 MAILTO)

# --- macOS 通知中心 ---
if [ "${MAC_NOTIFY:-1}" = "1" ] && command -v osascript >/dev/null 2>&1; then
  BODY="$(printf '%s' "$OUT" | sed -n '3p;5p' | tr -d '"' | tr '\n' ' ')"
  [ -n "$BODY" ] && osascript -e "display notification \"$BODY\" with title \"订阅续费提醒\"" >/dev/null 2>&1 || true
fi

# --- Telegram（可选）---
if [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ]; then
  curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
    -d chat_id="${TG_CHAT_ID}" --data-urlencode text="$OUT" >/dev/null || true
fi
