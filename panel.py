#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅管家 · 前端面板 panel.py

本地网页面板, 读写和 subs.py 同一份数据文件。

用法:
  python3 panel.py                              # 真实数据, http://127.0.0.1:8787, 自动开浏览器
  python3 panel.py --file examples.json         # 拿示例数据试玩
  python3 panel.py --port 8788 --no-open        # 指定端口 / 不自动开浏览器
  python3 panel.py --host 0.0.0.0               # 手机同局域网也能访问(谨慎)

只监听本机 127.0.0.1, 不对外暴露。纯标准库, 无依赖。
"""
from __future__ import print_function

import argparse
import contextlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import webbrowser
import zlib
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import subs as S            # noqa: E402  复用 subs.py 的日期/周期/存储逻辑

MONTHLY_FACTOR = {          # 折算成"每月多少钱", 用来算月支出
    "weekly": 52.0 / 12, "biweekly": 26.0 / 12, "monthly": 1.0,
    "quarterly": 1 / 3.0, "semiannual": 1 / 6.0, "yearly": 1 / 12.0,
}


# ------------------------------------------------------------------ 计算层
def prev_due(d, sub):
    """上一个周期日, 用来画周期进度条"""
    cyc = sub.get("cycle") or "monthly"
    if cyc == "custom_days":
        return d - timedelta(days=int(sub.get("cycle_days") or 30))
    if cyc in S.CYCLES_SIMPLE:
        return d - timedelta(days=S.CYCLES_SIMPLE[cyc])
    return S.add_months(d, -S.CYCLES_MONTHS.get(cyc, 1), sub.get("anchor_day"))


def monthly_amount(sub):
    amt = sub.get("amount")
    if amt in (None, "", 0):
        return None
    try:
        amt = float(amt)
    except (TypeError, ValueError):
        return None
    cyc = sub.get("cycle") or "monthly"
    if cyc == "custom_days":
        f = 30.44 / float(sub.get("cycle_days") or 30)
    else:
        f = MONTHLY_FACTOR.get(cyc, 1.0)
    return amt * f


def fmt_num(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def parse_date_quiet(raw):
    """解析日期; 失败返回 None —— 面板里不能让 subs.die() 往 stderr 喷错误"""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return S.parse_date(raw)
    except SystemExit:
        return None


def money_txt(amt, currency):
    cur = (currency or S.DEFAULT_CURRENCY).upper()
    return "%s%s" % (S.CUR_SYMBOL.get(cur, cur + " "), fmt_num(amt))


MANUAL_CURRENCIES = ["USD", "CNY", "HKD", "JPY", "EUR", "GBP", "SGD", "TWD", "KRW"]


def currency_options():
    """币种下拉: 默认币种排第一 (跟着 subs.py 的 DEFAULT_CURRENCY 走)"""
    order = [S.DEFAULT_CURRENCY] + [c for c in MANUAL_CURRENCIES if c != S.DEFAULT_CURRENCY]
    return "".join('<option value="%s">%s</option>' % (c, c) for c in order)


def build_payload():
    data = S.load()
    today = date.today()
    rows = []
    for sub in data["subscriptions"]:
        d, n, label = S.status_of(sub, today)
        amt = sub.get("amount")
        currency = (sub.get("currency") or S.DEFAULT_CURRENCY).upper()
        price = "—"
        if amt not in (None, "", 0):
            try:
                price = money_txt(float(amt), currency)
            except (TypeError, ValueError):
                price = str(amt)
        try:
            p0, p1 = prev_due(d, sub), d
            span = (p1 - p0).days or 1
            prog = (today - p0).days / float(span)
        except Exception:
            prog = 0.0
        prog = max(0.0, min(1.0, prog))
        m = monthly_amount(sub)
        rows.append({
            "id": sub.get("id"), "name": sub.get("name"),
            "amount": amt, "currency": currency,
            "price": price, "cycle": sub.get("cycle") or "monthly",
            "cycle_label": S.cycle_label(sub),
            "cycle_days": sub.get("cycle_days"),
            "next_due": d.isoformat(), "days": n, "status_label": label,
            "overdue": n < 0, "soon": 0 <= n <= 7,
            "auto_renew": bool(sub.get("auto_renew", True)),
            "active": bool(sub.get("active", True)),
            "category": sub.get("category") or "", "notes": sub.get("notes") or "",
            "monthly": (fmt_num(m) if m else None),
            "monthly_txt": (money_txt(m, currency) if m else None),
            "progress": round(prog * 100, 1),
        })
    rows.sort(key=lambda r: (not r["active"], r["days"]))
    active = [r for r in rows if r["active"]]
    totals = {}
    for r in active:
        m = monthly_amount({"amount": r["amount"], "cycle": r["cycle"],
                            "cycle_days": r["cycle_days"]})
        if m:
            totals[r["currency"]] = totals.get(r["currency"], 0.0) + m
    monthly_txt = " + ".join(money_txt(v, k) for k, v in sorted(totals.items())) or "—"
    return {
        "file": os.path.abspath(S.STORE),
        "today": today.isoformat(),
        "rows": rows,
        "stats": {
            "total": len(rows), "active": len(active),
            "soon": len([r for r in active if 0 <= r["days"] <= 7]),
            "overdue": len([r for r in active if r["days"] < 0]),
            "inactive": len([r for r in rows if not r["active"]]),
            "monthly": monthly_txt,
            "yearly": " + ".join(money_txt(v * 12, k) for k, v in sorted(totals.items())) or "—",
        },
    }


# ------------------------------------------------------------------ 动作层
def do_action(payload):
    act = payload.get("action")
    key = payload.get("key")
    data = S.load()
    subs = data["subscriptions"]

    def find(k):
        k = str(k or "").strip().lower()
        for x in subs:
            if str(x.get("id", "")).lower() == k:
                return x
        for x in subs:
            if str(x.get("name", "")).lower() == k:
                return x
        for x in subs:
            if k and k in str(x.get("name", "")).lower():
                return x
        return None

    if act == "renew":
        sub = find(key)
        if not sub:
            return False, "找不到该订阅"
        today = date.today()
        old = S.parse_date(sub["next_due"])
        new, guard = old, 0
        while guard == 0 or new <= today:
            new = S.advance(new, sub)
            guard += 1
            if guard > 400:
                break
        sub["next_due"] = new.isoformat()
        sub.setdefault("history", []).append(
            {"renewed_on": today.isoformat(), "from": old.isoformat(),
             "to": new.isoformat(), "via": "panel"})
        sub["history"] = sub["history"][-20:]
        S.save(data)
        return True, "%s 已续费 → %s" % (sub["name"], new.isoformat())

    if act == "roll":
        today = date.today()
        moved = 0
        for sub in subs:
            if not sub.get("active", True) or not sub.get("auto_renew", True):
                continue
            d = S.parse_date(sub["next_due"])
            if d >= today:
                continue
            old = d
            guard = 0
            while d <= today and guard < 400:
                d = S.advance(d, sub)
                guard += 1
            sub["next_due"] = d.isoformat()
            sub.setdefault("history", []).append(
                {"renewed_on": today.isoformat(), "from": old.isoformat(),
                 "to": d.isoformat(), "auto": True})
            sub["history"] = sub["history"][-20:]
            moved += 1
        if moved:
            S.save(data)
        return True, ("已顺延 %d 个自动续费订阅" % moved) if moved else "没有需要顺延的订阅"

    if act == "toggle":
        sub = find(key)
        if not sub:
            return False, "找不到该订阅"
        field = payload.get("field") or "active"
        sub[field] = not bool(sub.get(field, True))
        S.save(data)
        label = {"active": ("启用" if sub[field] else "停用"),
                 "auto_renew": ("改为自动续费" if sub[field] else "改为手动续费")}
        return True, "%s %s" % (sub["name"], label.get(field, "已切换"))

    if act == "remove":
        sub = find(key)
        if not sub:
            return False, "找不到该订阅"
        data["subscriptions"] = [x for x in subs if x is not sub]
        S.save(data)
        return True, "已删除 %s" % sub.get("name")

    if act in ("add", "edit"):
        f = payload.get("fields") or {}
        name = (f.get("name") or "").strip()
        if not name:
            return False, "名称不能为空"
        cycle = f.get("cycle") or "monthly"
        if cycle == "custom_days" and not f.get("cycle_days"):
            return False, "自定义周期需要填天数"
        due = (f.get("next_due") or "").strip()
        if act == "add":
            sub = {"id": S.make_id(name, subs, None), "name": name,
                   "amount": None, "currency": (f.get("currency") or S.DEFAULT_CURRENCY).upper(),
                   "cycle": cycle, "cycle_days": f.get("cycle_days") or None,
                   "next_due": None, "auto_renew": bool(f.get("auto_renew", True)),
                   "category": (f.get("category") or "").strip() or None,
                   "notes": (f.get("notes") or "").strip() or None,
                   "active": True, "anchor_day": None,
                   "created_at": date.today().isoformat(), "history": []}
            subs.append(sub)
        else:
            sub = find(key)
            if not sub:
                return False, "找不到该订阅"
            sub["name"] = name
            sub["currency"] = (f.get("currency") or S.DEFAULT_CURRENCY).upper()
            sub["cycle"] = cycle
            sub["cycle_days"] = f.get("cycle_days") or None
            sub["auto_renew"] = bool(f.get("auto_renew", True))
            sub["category"] = (f.get("category") or "").strip() or None
            sub["notes"] = (f.get("notes") or "").strip() or None
        if f.get("amount") not in (None, "", "null"):
            try:
                sub["amount"] = float(f["amount"])
            except (TypeError, ValueError):
                return False, "金额得是数字"
        else:
            sub["amount"] = None
        try:
            if due:
                d = parse_date_quiet(due)
                if d is None:
                    return False, "日期看不懂, 试试 2026-10-03 或 10/3"
            elif not sub.get("next_due"):
                d = S.advance(date.today(), sub)
            else:
                d = None
            if d:
                sub["next_due"] = d.isoformat()
                sub["anchor_day"] = d.day
        except SystemExit:
            return False, "日期看不懂, 试试 2026-10-03 或 10/3"
        if not sub.get("next_due"):
            return False, "需要下次扣费日期"
        S.save(data)
        return True, ("已添加 %s" % sub["name"]) if act == "add" else ("已更新 %s" % sub["name"])

    return False, "未知操作"


# ------------------------------------------------------------------ HTTP 层
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>订阅管家</title>
<meta name="theme-color" content="#0b0e14">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="订阅管家">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="icon" href="/icon.png">
<link rel="apple-touch-icon" href="/icon.png">
<style>
:root{
  --bg:#0b0e14; --panel:#141926; --panel2:#1b2233; --line:#243049;
  --tx:#e8edf7; --dim:#8a97b1; --accent:#5b8cff; --good:#2ed573;
  --warn:#ffb84d; --bad:#ff5f6d; --radius:14px;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#1a2440 0%,var(--bg) 55%);
  color:var(--tx);font:14px/1.5 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif;
  padding:22px 22px 60px;min-height:100vh}
h1{font-size:20px;margin:0;letter-spacing:.5px}
.wrap{max-width:1100px;margin:0 auto}
header{display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between;
  margin-bottom:18px}
.meta{color:var(--dim);font-size:12px;word-break:break-all}
.meta code{background:#0e1421;border:1px solid var(--line);padding:1px 6px;border-radius:6px}
.btns{display:flex;gap:8px;flex-wrap:wrap}
button{cursor:pointer;font:inherit;color:var(--tx);background:var(--panel2);
  border:1px solid var(--line);border-radius:9px;padding:7px 12px;transition:.15s}
button:hover{border-color:#3a4c74;transform:translateY(-1px)}
button.primary{background:linear-gradient(180deg,#5b8cff,#3f6fe6);border-color:#4b7bf0;font-weight:600}
button.danger:hover{border-color:var(--bad);color:var(--bad)}
button.small{padding:5px 9px;font-size:12px;border-radius:8px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px}
.kpi .k{color:var(--dim);font-size:12px;margin-bottom:6px}
.kpi .v{font-size:22px;font-weight:700;letter-spacing:.3px}
.kpi.warn .v{color:var(--warn)} .kpi.bad .v{color:var(--bad)} .kpi.good .v{color:var(--good)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tab{padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:transparent;
  color:var(--dim);font-size:13px}
.tab.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.list{display:flex;flex-direction:column;gap:10px}
.row{display:grid;grid-template-columns:minmax(180px,1.6fr) minmax(120px,1fr) minmax(150px,1.2fr) auto;
  gap:14px;align-items:center;background:var(--panel);border:1px solid var(--line);
  border-left:3px solid var(--line);border-radius:var(--radius);padding:13px 15px}
.row.over{border-left-color:var(--bad)} .row.soon{border-left-color:var(--warn)}
.row.ok{border-left-color:var(--good)} .row.off{opacity:.5;border-left-color:#3a4560}
.name{font-weight:600;font-size:15px}
.tags{margin-top:4px;display:flex;gap:6px;flex-wrap:wrap}
.tag{font-size:11px;color:var(--dim);border:1px solid var(--line);border-radius:6px;padding:1px 6px}
.tag.manual{color:var(--warn);border-color:#4a3a1c}
.sub{color:var(--dim);font-size:12px;margin-top:3px}
.price b{font-size:16px} .price span{color:var(--dim);font-size:12px}
.price em{display:block;color:var(--dim);font-size:11px;font-style:normal;margin-top:2px}
.due .date{font-variant-numeric:tabular-nums;font-size:13px}
.pill{display:inline-block;margin-top:5px;font-size:12px;padding:2px 8px;border-radius:999px}
.pill.over{background:rgba(255,95,109,.15);color:var(--bad)}
.pill.soon{background:rgba(255,184,77,.15);color:var(--warn)}
.pill.ok{background:rgba(46,213,115,.13);color:var(--good)}
.bar{height:4px;background:#0d1320;border-radius:999px;margin-top:8px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent)}
.row.over .bar i{background:var(--bad)} .row.soon .bar i{background:var(--warn)}
.ops{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.empty{background:var(--panel);border:1px dashed var(--line);border-radius:var(--radius);
  padding:40px;text-align:center;color:var(--dim)}
.empty h3{color:var(--tx);margin:0 0 6px}
footer{margin-top:22px;color:var(--dim);font-size:12px;text-align:center;line-height:1.9}
.modal{position:fixed;inset:0;background:rgba(4,7,13,.72);display:none;align-items:center;
  justify-content:center;padding:18px;z-index:20}
.modal.show{display:flex}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;
  padding:20px;width:100%;max-width:520px;max-height:88vh;overflow:auto}
.card h2{margin:0 0 14px;font-size:17px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
label{display:block;font-size:12px;color:var(--dim);margin-bottom:5px}
input,select{width:100%;background:#0e1421;border:1px solid var(--line);color:var(--tx);
  border-radius:9px;padding:9px 10px;font:inherit}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.full{grid-column:1/-1}
.checks{display:flex;gap:18px;margin:14px 0 4px;color:var(--tx);font-size:13px}
.checks label{display:flex;align-items:center;gap:6px;margin:0;color:var(--tx)}
.checks input{width:auto}
.modal .btns{justify-content:flex-end;margin-top:18px}
.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,20px);opacity:0;
  background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:10px 16px;
  transition:.25s;pointer-events:none;box-shadow:0 10px 30px rgba(0,0,0,.45);max-width:90vw}
.toast.show{opacity:1;transform:translate(-50%,0)}
.toast.bad{border-color:var(--bad);color:var(--bad)}
@media(max-width:760px){.row{grid-template-columns:1fr}.ops{justify-content:flex-start}
  .grid{grid-template-columns:1fr}}
</style></head>
<body><div class="wrap">
<header>
  <div>
    <h1>🔔 订阅管家</h1>
    <div class="meta" id="meta"></div>
  </div>
  <div class="btns">
    <button id="btn-roll">↻ 顺延过期项</button>
    <button id="btn-add" class="primary">+ 添加订阅</button>
  </div>
</header>
<section class="kpis" id="kpis"></section>
<nav class="tabs" id="tabs"></nav>
<main class="list" id="list"></main>
<footer id="foot"></footer>
</div>

<div class="modal" id="modal"><form class="card" id="form">
  <h2 id="form-title">添加订阅</h2>
  <div class="grid">
    <div class="full"><label>名称 *</label><input name="name" required placeholder="Netflix 高级会员"></div>
    <div><label>金额</label><input name="amount" inputmode="decimal" placeholder="68"></div>
    <div><label>币种</label>
      <select name="currency">%%CURRENCY_OPTIONS%%</select></div>
    <div><label>扣费周期</label>
      <select name="cycle" id="cycle">
        <option value="monthly">每月</option><option value="yearly">每年</option>
        <option value="quarterly">每季度</option><option value="semiannual">每半年</option>
        <option value="weekly">每周</option><option value="biweekly">每两周</option>
        <option value="custom_days">自定义天数</option>
      </select></div>
    <div id="wrap-days" style="display:none"><label>周期天数</label>
      <input name="cycle_days" inputmode="numeric" placeholder="45"></div>
    <div><label>下次扣费日 *</label><input name="next_due" placeholder="2026-10-03 或 10/3"></div>
    <div><label>分类</label><input name="category" placeholder="影音 / 工具 / 云存储"></div>
    <div class="full"><label>备注</label><input name="notes" placeholder="拼车 4 人分摊"></div>
  </div>
  <div class="checks">
    <label><input type="checkbox" name="auto_renew" checked> 自动续费</label>
    <span style="color:var(--dim);font-size:12px">取消勾选 = 到期需手动付款，提醒会更醒目</span>
  </div>
  <div class="btns">
    <button type="button" id="btn-cancel">取消</button>
    <button type="submit" class="primary">保存</button>
  </div>
</form></div>
<div class="toast" id="toast"></div>

<script>
let DATA=null, FILTER='all', EDIT=null;
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const nf = n => Number(n).toLocaleString('zh-CN',{maximumFractionDigits:2});

function toast(msg, ok){
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (ok === false ? ' bad' : '');
  clearTimeout(t._h);
  t._h = setTimeout(()=>{ t.className = 'toast'; }, 2800);
}

async function load(){
  DATA = await (await fetch('/api/data')).json();
  render();
}

async function act(payload){
  const r = await (await fetch('/api/action', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})).json();
  toast(r.msg || (r.ok ? '完成' : '失败'), r.ok);
  await load();
}

function rows_for(f){
  if (!DATA) return [];
  const R = DATA.rows;
  if (f === 'due')  return R.filter(r => r.active && r.days >= 0 && r.days <= 7);
  if (f === 'over') return R.filter(r => r.active && r.overdue);
  if (f === 'off')  return R.filter(r => !r.active);
  return R;
}

function render(){
  const S = DATA.stats;
  $('#meta').innerHTML = '数据文件 <code>' + esc(DATA.file) + '</code> · 今天 ' + DATA.today;
  $('#kpis').innerHTML = [
    ['订阅数', S.active + ' 个', 'good'],
    ['7 天内到期', S.soon + ' 个', S.soon ? 'warn' : ''],
    ['已过期', S.overdue + ' 个', S.overdue ? 'bad' : ''],
    ['每月折算支出', S.monthly, ''],
    ['每年折算支出', S.yearly, ''],
  ].map(([k, v, c]) => '<div class="kpi ' + c + '"><div class="k">' + k +
     '</div><div class="v">' + esc(v) + '</div></div>').join('');

  const tabs = [['all','全部',DATA.rows.length], ['due','7 天内到期',rows_for('due').length],
    ['over','已过期',rows_for('over').length], ['off','已停用',rows_for('off').length]];
  $('#tabs').innerHTML = tabs.map(([k, t, n]) =>
    '<button class="tab ' + (FILTER===k?'on':'') + '" data-filter="' + k + '">' +
    t + ' <b>' + n + '</b></button>').join('');

  const R = rows_for(FILTER);
  if (!R.length){
    $('#list').innerHTML = '<div class="empty"><h3>' +
      (DATA.rows.length ? '这个筛选下没有订阅' : '还没有订阅') +
      '</h3><p>点右上角「+ 添加订阅」，或者直接在 Telegram 里跟我说一声</p>' +
      '<button class="primary" onclick="openForm(null)">+ 添加第一个订阅</button></div>';
  } else {
    $('#list').innerHTML = R.map(r => {
      const cls = !r.active ? 'off' : (r.overdue ? 'over' : (r.days <= 7 ? 'soon' : 'ok'));
      const pcls = !r.active ? 'ok' : (r.overdue ? 'over' : (r.days <= 7 ? 'soon' : 'ok'));
      const tags = [ esc(r.cycle_label) ];
      if (r.category) tags.push(esc(r.category));
      tags.push(r.auto_renew ? '自动续费' : '手动续费');
      if (!r.active) tags.push('已停用');
      return '<div class="row ' + cls + '">' +
        '<div><div class="name">' + esc(r.name) + '</div>' +
          '<div class="tags">' + tags.map((t,i) => '<span class="tag' +
            (i === 2 && !r.auto_renew ? ' manual' : '') + '">' + t + '</span>').join('') + '</div>' +
          (r.notes ? '<div class="sub">' + esc(r.notes) + '</div>' : '') + '</div>' +
        '<div class="price"><b>' + esc(r.price) + '</b><span>/' + esc(r.cycle_label) + '</span>' +
          (r.monthly_txt ? '<em>≈ ' + esc(r.monthly_txt) + '/月</em>' : '') + '</div>' +
        '<div class="due"><div class="date">' + esc(r.next_due) + '</div>' +
          '<span class="pill ' + pcls + '">' + esc(r.status_label) + '</span>' +
          '<div class="bar"><i style="width:' + r.progress + '%"></i></div></div>' +
        '<div class="ops">' +
          '<button class="small" data-act="renew" data-key="' + esc(r.id) + '">已续费</button>' +
          '<button class="small" data-act="toggle" data-field="' +
            (r.auto_renew ? 'auto_renew' : 'auto_renew') + '" data-key="' + esc(r.id) + '">' +
            (r.auto_renew ? '转手动' : '转自动') + '</button>' +
          '<button class="small" data-act="edit" data-key="' + esc(r.id) + '">编辑</button>' +
          '<button class="small" data-act="toggle" data-field="active" data-key="' +
            esc(r.id) + '">' + (r.active ? '停用' : '启用') + '</button>' +
          '<button class="small danger" data-act="remove" data-key="' + esc(r.id) + '">删除</button>' +
        '</div></div>';
    }).join('');
  }
  $('#foot').innerHTML = '每天 09:00 Telegram 自动提醒（7 天窗口） · 数据存于本机 subscriptions.json，' +
    '面板和命令行 subs.py 共用同一份数据';
}

function openForm(sub){
  EDIT = sub;
  const f = $('#form');
  f.reset();
  $('#form-title').textContent = sub ? '编辑：' + sub.name : '添加订阅';
  if (sub){
    f.name.value = sub.name || '';
    f.amount.value = (sub.amount === null || sub.amount === undefined) ? '' : sub.amount;
    f.currency.value = sub.currency || '%%DEFAULT_CURRENCY%%';
    f.cycle.value = sub.cycle || 'monthly';
    f.cycle_days.value = sub.cycle_days || '';
    f.next_due.value = sub.next_due || '';
    f.category.value = sub.category || '';
    f.notes.value = sub.notes || '';
    f.auto_renew.checked = !!sub.auto_renew;
  }
  toggleDays();
  $('#modal').classList.add('show');
  setTimeout(()=>f.name.focus(), 50);
}
function toggleDays(){
  $('#wrap-days').style.display = ($('#cycle').value === 'custom_days') ? '' : 'none';
}

$('#btn-add').onclick = () => openForm(null);
$('#btn-cancel').onclick = () => $('#modal').classList.remove('show');
$('#cycle').onchange = toggleDays;
$('#modal').onclick = e => { if (e.target.id === 'modal') $('#modal').classList.remove('show'); };
$('#btn-roll').onclick = () => act({action:'roll'});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') $('#modal').classList.remove('show');
  if (e.key === 'n' && !/input|select|textarea/i.test(e.target.tagName)) openForm(null);
  if (e.key === 'r' && !/input|select|textarea/i.test(e.target.tagName)) load();
});
$('#tabs').onclick = e => {
  const b = e.target.closest('[data-filter]');
  if (b){ FILTER = b.dataset.filter; render(); }
};
$('#list').onclick = e => {
  const b = e.target.closest('button[data-act]');
  if (!b) return;
  const a = b.dataset.act, key = b.dataset.key;
  if (a === 'edit'){
    const r = DATA.rows.find(x => x.id === key);
    const full = Object.assign({}, r, {cycle_days: r.cycle_days});
    // 编辑需要原始字段(含停用项), 直接用 row 数据即可
    openForm(full);
    return;
  }
  if (a === 'remove'){
    const r = DATA.rows.find(x => x.id === key);
    if (!confirm('确定删除「' + (r ? r.name : key) + '」？')) return;
    act({action:'remove', key});
    return;
  }
  act({action:a, key, field: b.dataset.field});
};
$('#form').onsubmit = e => {
  e.preventDefault();
  const f = e.target;
  const fields = {
    name: f.name.value.trim(), amount: f.amount.value.trim(),
    currency: f.currency.value, cycle: f.cycle.value,
    cycle_days: f.cycle_days.value.trim(), next_due: f.next_due.value.trim(),
    category: f.category.value.trim(), notes: f.notes.value.trim(),
    auto_renew: f.auto_renew.checked,
  };
  const payload = {action: EDIT ? 'edit' : 'add', fields};
  if (EDIT) payload.key = EDIT.id;
  $('#modal').classList.remove('show');
  act(payload);
};
load();
if (location.hash === '#add') setTimeout(()=>openForm(null), 120);
</script></body></html>
"""


# ------------------------------------------------------------------ 手机/安全
STATE = {"token": None}
TOKEN_FILE = os.path.join(HERE, ".panel_token")


def load_or_make_token(explicit=None, disabled=False):
    """访问令牌: 让手机能访问, 又不会被局域网里别的设备随便改数据。
    优先级: --token > 环境变量 SUBS_PANEL_TOKEN > .panel_token 文件 > 新生成并落盘(重启后不变)"""
    if disabled:
        return None
    if explicit:
        return explicit.strip()
    env = os.environ.get("SUBS_PANEL_TOKEN")
    if env:
        return env.strip()
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    t = secrets.token_hex(4)
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(t)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


def _is_lan_ip(ip):
    """能用手机连的地址: 只认常见内网段, 顺手排掉 VPN 假 IP(198.18/198.19) 和 link-local"""
    if not ip or ip.count(".") != 3:
        return False
    if ip.startswith(("198.18.", "198.19.", "169.254.", "127.")):
        return False
    a, b = ip.split(".")[0], ip.split(".")[1]
    try:
        a, b = int(a), int(b)
    except ValueError:
        return False
    if a == 192 and b == 168:
        return True
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    return False


def lan_ip():
    """本机局域网 IP (手机连同一个 Wi-Fi 就能访问)。
    优先看默认路由走哪张网卡 —— 开了代理/VPN(TUN) 时 UDP 猜测法会拿到 198.18.x.x 假地址, 所以不用它当首选。"""
    try:
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"interface:\s*(\S+)", out)
        if m:
            ip = subprocess.run(["ipconfig", "getifaddr", m.group(1)], capture_output=True,
                                text=True, timeout=5).stdout.strip()
            if _is_lan_ip(ip):
                return ip
    except Exception:
        pass
    for dev in ("en0", "en1", "en2", "en3", "en4", "en5", "en6"):
        try:
            ip = subprocess.run(["ipconfig", "getifaddr", dev], capture_output=True,
                                text=True, timeout=5).stdout.strip()
            if _is_lan_ip(ip):
                return ip
        except Exception:
            pass
    try:                                     # 兜底: UDP 探测, 但过滤掉假地址段
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if _is_lan_ip(ip):
            return ip
    except Exception:
        pass
    return None


def local_hostname():
    try:
        return subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def make_qr(url, path):
    """把访问地址做成二维码 PNG (手机相机扫一下直接打开)。依赖 segno, 没有就跳过。"""
    exe = shutil.which("segno") or os.path.expanduser("~/.local/bin/segno")
    if os.path.exists(exe):
        try:
            subprocess.run([exe, url, "-o", path, "--scale", "12", "--border", "3"],
                           check=True, capture_output=True, timeout=30)
            return path
        except Exception:
            pass
    try:                                  # 退一步: python 模块
        import segno as _segno
        _segno.make(url).save(path, scale=12, border=3)
        return path
    except Exception:
        return None


# ------------------------------------------------------------------ 图标(纯 stdlib 画 PNG)
_ICON_CACHE = {}


def _png_bytes(width, height, rows):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def icon_png(size=180):
    if size in _ICON_CACHE:
        return _ICON_CACHE[size]
    bg, accent, badge = (20, 25, 38), (91, 140, 255), (255, 184, 77)
    c = (size - 1) / 2.0
    ring_r, ring_w = size * 0.30, size * 0.085
    br, bx, by = size * 0.115, size * 0.71, size * 0.29
    rad = size * 0.22
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx = max(rad - x, x - (size - 1 - rad), 0)
            dy = max(rad - y, y - (size - 1 - rad), 0)
            if dx * dx + dy * dy > rad * rad:          # 圆角外
                row += b"\x00\x00\x00\x00"
                continue
            col = bg
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if abs(d - ring_r) <= ring_w / 2:          # 圆环 = 铃铛
                col = accent
            if abs(x - c) <= size * 0.022 and c + ring_r < y <= c + ring_r + size * 0.09:
                col = accent                            # 铃铛下摆
            if ((x - bx) ** 2 + (y - by) ** 2) ** 0.5 <= br:
                col = badge                             # 未读小红点
            row += bytes(col) + b"\xff"
        rows.append(bytes(row))
    data = _png_bytes(size, size, rows)
    _ICON_CACHE[size] = data
    return data


DENY_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>需要访问令牌</title>
<style>body{background:#0b0e14;color:#e8edf7;font:15px/1.7 -apple-system,"PingFang SC",sans-serif;
padding:40px 22px;max-width:520px;margin:0 auto}h1{font-size:18px}code{background:#141926;
border:1px solid #243049;padding:2px 6px;border-radius:6px;word-break:break-all}</style></head>
<body><h1>🔒 需要访问令牌</h1>
<p>这个面板开了防串门保护：链接里得带上令牌。</p>
<p>用手机扫电脑上生成的那张二维码，或者把完整链接（<code>...?t=xxxxxxxx</code>）复制到浏览器打开一次，
之后这台设备就记住了。</p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "subs-panel/1.0"

    def log_message(self, fmt, *args):        # 安静点
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ---- 访问令牌 ----
    def _authed(self, qs):
        tok = STATE.get("token")
        if not tok:
            return True
        if (qs.get("t") or [""])[0] == tok:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "subs_token" and v == tok:
                return True
        return False

    def _deny(self, want_json=False):
        if want_json:
            self._send(401, json.dumps({"ok": False, "msg": "需要访问令牌 (?t=...)"},
                                       ensure_ascii=False))
        else:
            self._send(401, DENY_PAGE, "text/html; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if not self._authed(qs) and path not in ("/favicon.ico", "/icon.png"):
            self._deny(want_json=path.startswith("/api/"))
            return
        extra = []
        if STATE.get("token") and (qs.get("t") or [""])[0] == STATE["token"]:
            extra.append(("Set-Cookie",
                          "subs_token=%s; Path=/; Max-Age=2592000; SameSite=Lax"
                          % STATE["token"]))
        if path in ("/", "/index.html"):
            page = (PAGE.replace("%%CURRENCY_OPTIONS%%", currency_options())
                        .replace("%%DEFAULT_CURRENCY%%", S.DEFAULT_CURRENCY))
            self._send(200, page, "text/html; charset=utf-8", extra)
        elif path == "/api/data":
            self._send(200, json.dumps(build_payload(), ensure_ascii=False), headers=extra)
        elif path in ("/favicon.ico", "/icon.png"):
            self._send(200, icon_png(180), "image/png")
        else:
            self._send(404, json.dumps({"ok": False, "msg": "not found"}))

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if not self._authed(qs):
            self._deny(want_json=True)
            return
        if parsed.path != "/api/action":
            self._send(404, json.dumps({"ok": False, "msg": "not found"}))
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "msg": "请求格式错误: %s" % e}))
            return
        try:
            ok, msg = do_action(payload)
        except SystemExit:
            ok, msg = False, "数据校验失败"
        except Exception as e:
            ok, msg = False, "出错了: %s" % e
        self._send(200, json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="订阅管家网页面板")
    p.add_argument("--file", default=None, help="数据文件 (默认 subscriptions.json)")
    p.add_argument("--port", type=int, default=8899,
                   help="端口 (默认 8899; 被占用会自动往后找)")
    p.add_argument("--host", default="127.0.0.1",
                   help="127.0.0.1=只有本机能开(默认); 0.0.0.0=局域网/手机也能开")
    p.add_argument("--token", default=None, help="手动指定访问令牌")
    p.add_argument("--no-token", action="store_true", help="不校验令牌 (仅限可信网络)")
    p.add_argument("--qr", default=None, help="二维码 PNG 输出路径 (手机扫码直开)")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = p.parse_args()
    if args.file:
        S.STORE = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.exists(S.STORE):
        S.save({"subscriptions": []})

    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if args.no_token:
        token = None
    elif args.token:
        token = args.token.strip()
    elif not loopback:
        token = load_or_make_token()          # 开了局域网就自动上令牌, 且重启不变
    else:
        token = None
    STATE["token"] = token
    suffix = ("?t=%s" % token) if token else ""

    httpd, port, last_err = None, args.port, None
    for cand in range(args.port, args.port + 15):
        try:
            httpd = ThreadingHTTPServer((args.host, cand), Handler)
            port = cand
            break
        except OSError as e:
            last_err = e
    if httpd is None:
        print("端口 %d-%d 全被占用, 起不来: %s" % (args.port, args.port + 14, last_err))
        return 1

    local_url = "http://127.0.0.1:%d/%s" % (port, suffix)
    if port != args.port:
        print("端口 %d 被占用, 已改用 %d" % (args.port, port))
    print("订阅管家面板已启动: %s" % local_url)
    print("数据文件: %s" % S.STORE)

    phone_url = None
    if not loopback:
        ip = lan_ip()
        if ip:
            phone_url = "http://%s:%d/%s" % (ip, port, suffix)
            print("")
            print("📱 手机访问 (连着同一个 Wi-Fi 直接开):")
            print("   %s" % phone_url)
            host = local_hostname()
            if host:
                print("   备用(路由器换了 IP 也能用): http://%s.local:%d/%s"
                      % (host, port, suffix))
            qr_path = args.qr or os.path.join(HERE, "phone-access.png")
            if make_qr(phone_url, qr_path):
                print("   二维码: %s   ← 手机相机扫一下直接打开" % qr_path)
            print("   浏览器里可「分享 → 添加到主屏幕」，就是个 App 的样子")
        else:
            print("⚠️ 没取到局域网 IP, 检查一下 Wi-Fi")
        if token:
            print("   (已开启令牌保护, 换设备要重新带 ?t=... 打开一次)")
        print("")
        print("🌍 出门在外还想用: 另开一个终端跑")
        print("   cloudflared tunnel --url http://127.0.0.1:%d%s"
              % (port, "     # 会给你一个公网 https 地址" if not token else ""))
    elif args.qr:
        make_qr(local_url, args.qr)

    print("按 Ctrl+C 停止")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(local_url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    main()
