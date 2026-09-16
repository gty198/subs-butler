#!/bin/bash
# 双击这个 = 手机模式：面板开放给局域网 + 在屏幕上弹出二维码（手机相机扫一下就能开）
cd "$(dirname "$0")" || exit 1
PORT=8899
TOK="$(cat .panel_token 2>/dev/null)"
QR="$(pwd)/phone-access.png"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$PORT/api/data?t=$TOK")
if [ "$code" = "200" ] || [ "$code" = "401" ]; then
  # 已经在跑了，直接把二维码和本机页面打开
  [ -f "$QR" ] && open "$QR"
  open "http://127.0.0.1:$PORT/?t=$TOK"
  exit 0
fi

python3 panel.py --file subscriptions.json --host 0.0.0.0 --port "$PORT" --no-open &
PID=$!
for _ in $(seq 1 40); do
  [ -f "$QR" ] && break
  sleep 0.25
done
[ -f "$QR" ] && open "$QR"
open "http://127.0.0.1:$PORT/?t=$(cat .panel_token 2>/dev/null)"
wait "$PID"
