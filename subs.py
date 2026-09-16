#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅管家 subs.py — 本地订阅 / 会员续费管理 + 到期提醒

数据文件: 与本脚本同目录的 subscriptions.json
          (可用环境变量 SUBS_FILE 指向别处)

常用命令:
  list                     列出全部订阅(含剩余天数)
  due [-d N]               查看到期订阅, 默认未来 7 天内(含已过期)
  check [-d N]             同 due, 但无到期时完全静默 —— 专给 cron 用
  add -n 名称 -a 金额 -c 周期 --next-due 日期
  renew <id|名称> [--date 日期]   标记已续费并自动顺延周期
  roll                     把所有"已过期且自动续费"的订阅顺延到未来
  edit <id|名称> [--amount ..] ...
  remove <id|名称>
  reset --yes              清空所有订阅

金额可为空(只提醒不记账); 周期: weekly|biweekly|monthly|quarterly|
semiannual|yearly|custom_days(配合 --cycle-days N)
"""
from __future__ import print_function

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.environ.get("SUBS_FILE", os.path.join(HERE, "subscriptions.json"))

CYCLE_LABEL = {
    "weekly": "每周",
    "biweekly": "每两周",
    "monthly": "每月",
    "quarterly": "每季度",
    "semiannual": "每半年",
    "yearly": "每年",
    "custom_days": "每{d}天",
}
CYCLES_SIMPLE = {
    "weekly": 7,
    "biweekly": 14,
}
CYCLES_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "semiannual": 6,
    "yearly": 12,
}
CUR_SYMBOL = {
    "CNY": "¥", "RMB": "¥", "USD": "$", "EUR": "€", "GBP": "£",
    "HKD": "HK$", "JPY": "¥", "SGD": "S$", "TWD": "NT$", "KRW": "₩",
}
# 默认币种: 新增订阅没指定时用它 (可用环境变量 SUBS_DEFAULT_CURRENCY 覆盖)
DEFAULT_CURRENCY = (os.environ.get("SUBS_DEFAULT_CURRENCY") or "USD").upper()


# ---------------------------------------------------------------- 基础工具
def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def parse_date(s):
    """宽松解析日期: 2026-09-25 / 2026/9/25 / 9-25 / 9/25 (无年份取今年或明年)"""
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    for fmt in ("%m-%d", "%m/%d", "%m.%d"):
        try:
            d = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        today = date.today()
        d = d.replace(year=today.year)
        if d < today - timedelta(days=1):          # 已经过去很久了 -> 理解为明年
            d = d.replace(year=today.year + 1)
        return d
    die("无法解析日期: %r  (示例: 2026-09-25 或 9/25)" % s)


def days_in_month(y, m):
    if m == 12:
        return 31
    return (date(y, m + 1, 1) - timedelta(days=1)).day


def add_months(d, n, anchor_day=None):
    """加 n 个月, 自动处理月末(1/31 + 1 月 -> 2/28)"""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    day = anchor_day or d.day
    return date(y, m, min(day, days_in_month(y, m)))


def advance(d, sub):
    """按订阅的周期把日期 d 往后推一个周期"""
    cyc = sub.get("cycle") or "monthly"
    if cyc == "custom_days":
        return d + timedelta(days=int(sub.get("cycle_days") or 30))
    if cyc in CYCLES_SIMPLE:
        return d + timedelta(days=CYCLES_SIMPLE[cyc])
    months = CYCLES_MONTHS.get(cyc, 1)
    return add_months(d, months, sub.get("anchor_day"))


def cycle_label(sub):
    cyc = sub.get("cycle") or "monthly"
    if cyc == "custom_days":
        return "每%s天" % (sub.get("cycle_days") or 30)
    return CYCLE_LABEL.get(cyc, cyc)


def money(sub):
    amt = sub.get("amount")
    if amt in (None, "", 0):
        return "—"
    try:
        amt = float(amt)
    except (TypeError, ValueError):
        return str(sub.get("amount"))
    cur = (sub.get("currency") or DEFAULT_CURRENCY).upper()
    txt = ("%.2f" % amt).rstrip("0").rstrip(".")
    return "%s%s" % (CUR_SYMBOL.get(cur, cur + " "), txt)


# ---------------------------------------------------------------- 数据读写
def load():
    if not os.path.exists(STORE):
        return {"subscriptions": []}
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:                     # JSONDecodeError 是它的子类
        die("数据文件格式错误: %s\n%s" % (STORE, e))
    if isinstance(data, list):
        data = {"subscriptions": data}
    data.setdefault("subscriptions", [])
    return data


def save(data):
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)


def slug(name):
    s = "".join(ch.lower() if (ch.isalnum() and ch.isascii()) else "-" for ch in name)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "sub"


def make_id(name, subs, wanted=None):
    base = wanted or slug(name)
    existing = {s.get("id") for s in subs}
    if base not in existing:
        return base
    i = 2
    while "%s-%d" % (base, i) in existing:
        i += 1
    return "%s-%d" % (base, i)


def find_sub(data, key):
    k = str(key).strip().lower()
    subs = data["subscriptions"]
    for s in subs:
        if str(s.get("id", "")).lower() == k:
            return s
    for s in subs:
        if str(s.get("name", "")).lower() == k:
            return s
    hits = [s for s in subs if k in str(s.get("name", "")).lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        die("找不到订阅: %s  (先跑 `subs.py list` 看 id)" % key)
    die("%s 匹配到多个订阅: %s  (请用 id 精确指定)"
        % (key, ", ".join(s.get("id", "?") for s in hits)))


def status_of(sub, today=None):
    today = today or date.today()
    d = parse_date(sub["next_due"])
    n = (d - today).days
    if n < 0:
        return d, n, "已过期 %d 天" % abs(n)
    if n == 0:
        return d, n, "今天到期"
    if n == 1:
        return d, n, "明天到期"
    return d, n, "还有 %d 天" % n


def total_by_currency(subs):
    tot = {}
    for s in subs:
        try:
            amt = float(s.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if amt == 0:
            continue
        cur = (s.get("currency") or DEFAULT_CURRENCY).upper()
        tot[cur] = tot.get(cur, 0.0) + amt
    out = []
    for cur, v in sorted(tot.items()):
        txt = ("%.2f" % v).rstrip("0").rstrip(".")
        out.append("%s%s" % (CUR_SYMBOL.get(cur, cur + " "), txt))
    return " + ".join(out)


# ---------------------------------------------------------------- 命令实现
def cmd_list(args):
    data = load()
    subs = [s for s in data["subscriptions"] if args.all or s.get("active", True)]
    if not subs:
        print("(还没有订阅)  用 `subs.py add -n 名称 -a 金额 -c monthly --next-due 2026-09-25` 添加")
        return
    subs.sort(key=lambda s: parse_date(s["next_due"]))
    print("共 %d 个订阅 —— %s" % (len(subs), STORE))
    for s in subs:
        d, n, label = status_of(s)
        flags = []
        if not s.get("active", True):
            flags.append("已停用")
        flags.append("自动续费" if s.get("auto_renew", True) else "手动续费")
        if s.get("category"):
            flags.append(str(s["category"]))
        mark = "!!" if n < 0 else ("!" if n <= 3 else "  ")
        print("%s [%s] %s  %s/%s  下次扣费 %s (%s)  %s"
              % (mark, s.get("id"), s.get("name"), money(s), cycle_label(s),
                 d.isoformat(), label, " · ".join(flags)))
        if s.get("notes"):
            print("     └ %s" % s["notes"])


def _due_list(data, days):
    today = date.today()
    out = []
    for s in data["subscriptions"]:
        if not s.get("active", True):
            continue
        d, n, _ = status_of(s, today)
        if n <= days:
            out.append((n, s, d))
    out.sort(key=lambda x: x[0])
    return today, out


def _due_report(data, days, for_cron):
    today, items = _due_list(data, days)
    if not items:
        if for_cron:
            return ""
        return "✅ 最近 %d 天内没有需要续费的订阅" % days
    overdue = [x for x in items if x[0] < 0]
    soon = [x for x in items if x[0] >= 0]
    lines = []
    head = "🔔 订阅续费提醒"
    if overdue:
        head += " —— %d 个已到期!" % len(overdue)
    lines.append(head)
    lines.append("")
    for n, s, d in items:
        _, _, label = status_of(s, today)
        icon = "⚠️" if n < 0 else ("🔴" if n <= 2 else "🟡")
        pay = "自动续费" if s.get("auto_renew", True) else "需手动续费"
        amount = money(s)
        price = ("%s/%s" % (amount, cycle_label(s))) if amount != "—" else cycle_label(s)
        lines.append("%s %s  %s  下次扣费 %s (%s · %s)"
                     % (icon, s.get("name"), price, d.isoformat(), label, pay))
        if not s.get("auto_renew", True):
            lines.append("     ↳ 不续的话记得去取消/降级")
        if s.get("notes"):
            lines.append("     ↳ %s" % s["notes"])
    tot = total_by_currency([s for _, s, _ in items])
    if tot:
        lines.append("")
        lines.append("合计待扣: %s" % tot)
    if for_cron:
        lines.append("")
        lines.append("-- 已续费就回我一句「XX 已续费」，我帮你顺延到下一周期。")
    else:
        lines.append("")
        lines.append("已续费: subs.py renew <id>   查全部: subs.py list")
    return "\n".join(lines)


def cmd_due(args):
    print(_due_report(load(), args.days, False))


def cmd_check(args):
    out = _due_report(load(), args.days, True)
    if out:
        print(out)


def cmd_add(args):
    data = load()
    subs = data["subscriptions"]
    cycle = args.cycle
    if cycle == "custom_days" and not args.cycle_days:
        die("周期 custom_days 需要同时给 --cycle-days N")
    sub = {
        "id": make_id(args.name, subs, args.id),
        "name": args.name,
        "amount": args.amount,
        "currency": (args.currency or DEFAULT_CURRENCY).upper(),
        "cycle": cycle,
        "cycle_days": args.cycle_days,
        "next_due": None,
        "auto_renew": not args.no_auto_renew,
        "category": args.category,
        "notes": args.notes,
        "active": True,
        "anchor_day": None,
        "created_at": date.today().isoformat(),
    }
    today = date.today()
    if args.next_due:
        due = parse_date(args.next_due)
        note = ""
    else:
        due = advance(today, sub)
        note = "  (未指定日期, 按周期设为下个周期)"
    sub["next_due"] = due.isoformat()
    sub["anchor_day"] = due.day
    subs.append(sub)
    save(data)
    print("✅ 已添加 [%s] %s  %s/%s  下次扣费 %s%s"
          % (sub["id"], sub["name"], money(sub), cycle_label(sub), sub["next_due"], note))


def cmd_renew(args):
    data = load()
    sub = find_sub(data, args.key)
    today = parse_date(args.on) if args.on else date.today()
    old = parse_date(sub["next_due"])
    new = old
    guard = 0
    while guard == 0 or new <= today:        # 至少顺延一个周期(提前续费也算)
        new = advance(new, sub)
        guard += 1
        if guard >= 400:
            sys.exit("周期阈值异常, 请检查 cycle 配置")
    if args.date:
        new = parse_date(args.date)
        sub["anchor_day"] = new.day          # 显式指定日期时, 月度锚点跟着走
    sub["next_due"] = new.isoformat()
    sub.setdefault("history", []).append(
        {"renewed_on": today.isoformat(), "from": old.isoformat(), "to": new.isoformat()})
    sub["history"] = sub["history"][-20:]
    save(data)
    print("✅ %s 已续费: %s → %s  (下次扣费 %s)"
          % (sub["name"], old.isoformat(), new.isoformat(), status_of(sub, today)[2]))


def cmd_roll(args):
    data = load()
    today = date.today()
    moved = []
    for s in data["subscriptions"]:
        if not s.get("active", True) or not s.get("auto_renew", True):
            continue
        d = parse_date(s["next_due"])
        if d >= today:
            continue
        old = d
        guard = 0
        while d <= today and guard < 400:
            d = advance(d, s)
            guard += 1
        s["next_due"] = d.isoformat()
        s.setdefault("history", []).append(
            {"renewed_on": today.isoformat(), "from": old.isoformat(),
             "to": d.isoformat(), "auto": True})
        s["history"] = s["history"][-20:]
        moved.append((s["name"], old.isoformat(), d.isoformat()))
    if moved:
        save(data)
        for name, o, n in moved:
            print("↻ %s 自动续费顺延: %s → %s" % (name, o, n))
    else:
        print("没有需要顺延的自动续费订阅")


def cmd_edit(args):
    data = load()
    sub = find_sub(data, args.key)
    changed = []
    for field in ("name", "amount", "currency", "cycle", "cycle_days",
                  "category", "notes"):
        val = getattr(args, field)
        if val is not None:
            if field == "currency":
                val = val.upper()
            sub[field] = val
            changed.append(field)
    if args.next_due:
        sub["next_due"] = parse_date(args.next_due).isoformat()
        sub["anchor_day"] = parse_date(args.next_due).day
        changed.append("next_due")
    if args.auto_renew is not None:
        sub["auto_renew"] = args.auto_renew
        changed.append("auto_renew")
    if args.active is not None:
        sub["active"] = args.active
        changed.append("active")
    if not changed:
        die("没给任何要改的字段 (试试 --amount / --next-due / --notes ...)")
    save(data)
    print("✅ 已更新 [%s] %s: %s" % (sub["id"], sub["name"], ", ".join(changed)))


def cmd_remove(args):
    data = load()
    sub = find_sub(data, args.key)
    data["subscriptions"] = [s for s in data["subscriptions"] if s is not sub]
    save(data)
    print("🗑  已删除 [%s] %s" % (sub.get("id"), sub.get("name")))


def cmd_reset(args):
    if not args.yes:
        die("这会删掉所有订阅, 确认请加 --yes")
    save({"subscriptions": []})
    print("已清空所有订阅")


def cmd_ids(args):
    """输出所有 id, 方便脚本消费"""
    for s in load()["subscriptions"]:
        print(s.get("id"))


# ---------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(
        prog="subs.py", description="订阅管家 —— 订阅续费管理与到期提醒")
    p.add_argument("--file", help="指定数据文件 (默认 %s)" % STORE)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("list", help="列出全部订阅")
    sp.add_argument("--all", action="store_true", help="含已停用的")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("due", help="查看到期订阅")
    sp.add_argument("-d", "--days", type=int, default=7, help="提前几天提醒 (默认 7)")
    sp.set_defaults(func=cmd_due)

    sp = sub.add_parser("check", help="静默版 due, 给 cron 用")
    sp.add_argument("-d", "--days", type=int, default=7)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("add", help="新增订阅")
    sp.add_argument("-n", "--name", required=True)
    sp.add_argument("-a", "--amount", type=float, default=None)
    sp.add_argument("-c", "--cycle", default="monthly",
                    choices=list(CYCLES_SIMPLE) + list(CYCLES_MONTHS) + ["custom_days"])
    sp.add_argument("--cycle-days", type=int, default=None, dest="cycle_days")
    sp.add_argument("--next-due", default=None, dest="next_due", help="下次扣费日")
    sp.add_argument("--currency", default=DEFAULT_CURRENCY,
                    help="默认 %s, 可填 CNY/USD/HKD/JPY/EUR..." % DEFAULT_CURRENCY)
    sp.add_argument("--category", default=None)
    sp.add_argument("--notes", default=None)
    sp.add_argument("--id", default=None)
    sp.add_argument("--no-auto-renew", action="store_true")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("renew", help="标记已续费并顺延")
    sp.add_argument("key")
    sp.add_argument("--date", default=None, help="直接指定下次扣费日")
    sp.add_argument("--on", default=None, help="以哪天为准顺延 (默认今天)")
    sp.set_defaults(func=cmd_renew)

    sp = sub.add_parser("roll", help="过期且自动续费的订阅, 全部顺延到未来")
    sp.set_defaults(func=cmd_roll)

    sp = sub.add_parser("edit", help="修改订阅字段")
    sp.add_argument("key")
    sp.add_argument("--name", default=None)
    sp.add_argument("-a", "--amount", type=float, default=None)
    sp.add_argument("--currency", default=None)
    sp.add_argument("-c", "--cycle", default=None,
                    choices=list(CYCLES_SIMPLE) + list(CYCLES_MONTHS) + ["custom_days"])
    sp.add_argument("--cycle-days", type=int, default=None, dest="cycle_days")
    sp.add_argument("--next-due", default=None, dest="next_due")
    sp.add_argument("--category", default=None)
    sp.add_argument("--notes", default=None)
    group = sp.add_mutually_exclusive_group()
    group.add_argument("--auto-renew", dest="auto_renew", action="store_true")
    group.add_argument("--no-auto-renew", dest="auto_renew", action="store_false")
    sp.set_defaults(auto_renew=None)
    group2 = sp.add_mutually_exclusive_group()
    group2.add_argument("--active", dest="active", action="store_true")
    group2.add_argument("--inactive", dest="active", action="store_false")
    sp.set_defaults(active=None)
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("remove", help="删除订阅")
    sp.add_argument("key")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("reset", help="清空所有订阅")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("ids", help="列出所有 id")
    sp.set_defaults(func=cmd_ids)
    return p


def main(argv=None):
    global STORE
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        p.print_help()
        return 0
    if args.file:
        STORE = os.path.abspath(os.path.expanduser(args.file))
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
