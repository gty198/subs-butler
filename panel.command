#!/bin/bash
# 双击即可打开「订阅管家」面板 (已在跑就直接开浏览器, 没跑就启动)
cd "$(dirname "$0")" || exit 1
PORT=8899
if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/api/data"; then
  open "http://127.0.0.1:$PORT/"
  exit 0
fi
exec python3 panel.py --file subscriptions.json --port "$PORT"
