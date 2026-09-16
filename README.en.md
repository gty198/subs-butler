# subs-butler

[![tests](https://github.com/gty198/subs-butler/actions/workflows/tests.yml/badge.svg)](https://github.com/gty198/subs-butler/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](subs.py)

> Local subscription renewal tracker — **CLI + web panel + renewal reminders**. Zero dependencies, one JSON file, no network calls.

Track every subscription you pay for, and get reminded *before* it charges you again.

```
🔔 订阅续费提醒 —— 1 个已到期!

⚠️ 香港 VPS  $12/每月  下次扣费 2026-09-14 (已过期 1 天 · 需手动续费)
     ↳ 不续的话记得去取消/降级
🟡 Netflix 高级会员  ¥68/每月  下次扣费 2026-09-18 (还有 3 天 · 自动续费)

合计待扣: ¥89 + $12
```

![panel](docs/screenshot-main.png)

## Why

SaaS quietly auto-renews. This is a 1-file JSON + a CLI + an optional local web panel that tells you
"these 3 things charge you in the next 7 days, total $89", and stays completely **silent** when there's nothing due.

## Features

- **Zero dependencies** — Python 3.8+ stdlib only; data lives in a plain JSON file
- **Offline by design** — the web panel binds `127.0.0.1`; LAN mode is opt-in and token-protected
- **All cycles** — weekly / biweekly / monthly / quarterly / semiannual / yearly / every N days
- **Multi-currency** — per-currency totals; amount optional (remind me without tracking money)
- **No month-end drift** — a subscription on the 31st stays on the 31st (clamped in short months)
- **Silent reminders** — `subs.py check` prints nothing when nothing is due, so cron never nags you
- **Web panel** — dashboard, cycle progress bars, filters, add/edit/renew/disable/delete
- **Phone friendly** — same-Wi-Fi QR code + "Add to Home Screen"; Cloudflare Tunnel for remote use
- **63 unit tests** — including a real HTTP server exercising token auth (401 / cookie / write permissions)

## Quick start

```bash
git clone https://github.com/gty198/subs-butler.git
cd subs-butler

python3 subs.py add -n "Netflix" -a 68 -c monthly --next-due 2026-09-18 --category video
python3 subs.py add -n "ChatGPT Plus" -a 20 -c monthly --next-due 9/17 --no-auto-renew
python3 subs.py list

python3 panel.py            # opens http://127.0.0.1:8899/
python3 subs.py check -d 7  # silent unless something is due (use this in cron)
```

```cron
0 9 * * * /usr/bin/python3 $HOME/subs-butler/subs.py check -d 7 | /usr/bin/mail -s "subs due" you@example.com
```

See `examples/reminder-cron.sh` for a drop-in script (macOS Notification Center + Telegram Bot).

## CLI

| Command | What it does |
|---|---|
| `subs.py list [--all]` | List subscriptions, sorted by due date |
| `subs.py due [-d 7]` | Items due within N days (overdue always included) |
| `subs.py check [-d 7]` | Same as `due`, but **zero output** when nothing is due — for cron |
| `subs.py add -n NAME -a AMOUNT -c CYCLE --next-due DATE` | Add |
| `subs.py renew <id\|name> [--date DATE]` | Mark renewed, roll forward one cycle |
| `subs.py roll` | Roll every overdue auto-renew item into the future |
| `subs.py edit <id\|name> [-a 50] [--next-due DATE] [--inactive]` | Edit / disable |
| `subs.py remove <id\|name>` | Delete |

Dates are forgiving: `2026-09-18`, `2026/9/18`, `9/18` (past month/day rolls to next year).

## Web panel & phone access

```bash
python3 panel.py                    # local only (127.0.0.1:8899, auto-picks another port if busy)
python3 panel.py --host 0.0.0.0     # LAN: prints a phone URL, writes a QR code, enables token auth
```

LAN mode generates a token (persisted in `.panel_token`, stable across restarts) and requires
`?t=<token>` once per device, then remembers it via cookie. Requests without it get 401 — reads *and* writes.
Remote access: `cloudflared tunnel --url http://127.0.0.1:8899`.

## Data format

```json
{
  "subscriptions": [{
    "id": "netflix", "name": "Netflix", "amount": 68.0, "currency": "USD",
    "cycle": "monthly", "cycle_days": null, "next_due": "2026-09-18",
    "auto_renew": true, "category": "video", "notes": "shared with 4 people",
    "active": true, "anchor_day": 18, "history": []
  }]
}
```

## Tests

```bash
python3 -m unittest discover -s tests -t .      # 63 tests, ~4s
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT
