#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""panel.py 的单元测试: 数据/动作层 + 真起一个 HTTP 服务验证令牌与权限。

跑法:
    python3 -m unittest discover -s tests -v
    python3 -m unittest tests.test_panel -v
"""
import http.cookiejar
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import panel  # noqa: E402
import subs  # noqa: E402


class PanelBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="panel-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "subscriptions.json")
        self._old = panel.S.STORE
        panel.S.STORE = self.store
        self.addCleanup(self._restore)
        panel.S.save({"subscriptions": []})
        panel.STATE["token"] = None

    def _restore(self):
        panel.S.STORE = self._old
        panel.STATE["token"] = None

    def add(self, name, amount=10, currency="USD", cycle="monthly", **kw):
        fields = {"name": name, "amount": amount, "currency": currency,
                  "cycle": cycle, "auto_renew": kw.pop("auto_renew", True)}
        fields.update(kw)
        ok, msg = panel.do_action({"action": "add", "fields": fields})
        self.assertTrue(ok, msg)
        return [s for s in panel.S.load()["subscriptions"] if s["name"] == name][0]

    def payload(self):
        return panel.build_payload()


# ------------------------------------------------------------------ 计算/汇总
class TestPayload(PanelBase):
    def test_stats_and_row_shapes(self):
        today = date.today()
        self.add("过期了", next_due=(today - timedelta(days=2)).isoformat())
        self.add("三天后", next_due=(today + timedelta(days=3)).isoformat())
        self.add("六天后", next_due=(today + timedelta(days=6)).isoformat())
        self.add("很久以后", cycle="yearly",
                 next_due=(today + timedelta(days=200)).isoformat())
        p = self.payload()
        self.assertEqual(p["stats"]["total"], 4)
        self.assertEqual(p["stats"]["active"], 4)
        self.assertEqual(p["stats"]["overdue"], 1)
        self.assertEqual(p["stats"]["soon"], 2)          # KPI 与页签口径必须一致
        soon_in_rows = len([r for r in p["rows"] if r["active"] and 0 <= r["days"] <= 7])
        self.assertEqual(soon_in_rows, p["stats"]["soon"])

    def test_rows_sorted_overdue_first_then_by_days(self):
        today = date.today()
        self.add("远的", next_due=(today + timedelta(days=30)).isoformat())
        self.add("近期", next_due=(today + timedelta(days=2)).isoformat())
        self.add("过期", next_due=(today - timedelta(days=1)).isoformat())
        names = [r["name"] for r in self.payload()["rows"]]
        self.assertEqual(names[0], "过期")
        self.assertEqual(names[1], "近期")

    def test_progress_is_clamped_percentage(self):
        today = date.today()
        self.add("过期很久", cycle="monthly",
                 next_due=(today - timedelta(days=40)).isoformat())
        self.add("刚开始", cycle="yearly",
                 next_due=(today + timedelta(days=364)).isoformat())
        rows = {r["name"]: r for r in self.payload()["rows"]}
        self.assertEqual(rows["过期很久"]["progress"], 100.0)
        self.assertGreaterEqual(rows["刚开始"]["progress"], 0.0)
        self.assertLess(rows["刚开始"]["progress"], 5.0)

    def test_monthly_normalisation(self):
        self.add("年付", amount=1200, currency="USD", cycle="yearly",
                 next_due=(date.today() + timedelta(days=100)).isoformat())
        self.add("周付", amount=12, currency="USD", cycle="weekly",
                 next_due=(date.today() + timedelta(days=3)).isoformat())
        p = self.payload()
        self.assertEqual(p["stats"]["monthly"], "$152")       # 1200/12 + 12*52/12
        self.assertEqual(p["stats"]["yearly"], "$1824")
        rows = {r["name"]: r for r in p["rows"]}
        self.assertEqual(rows["年付"]["monthly_txt"], "$100")
        self.assertEqual(rows["年付"]["price"], "$1200")
        self.assertEqual(rows["年付"]["cycle_label"], "每年")

    def test_multi_currency_totals(self):
        self.add("人民币", amount=68, currency="CNY")
        self.add("美元", amount=20, currency="USD")
        self.assertEqual(self.payload()["stats"]["monthly"], "¥68 + $20")

    def test_empty_store_payload(self):
        p = self.payload()
        self.assertEqual(p["rows"], [])
        self.assertEqual(p["stats"]["monthly"], "—")
        self.assertEqual(p["stats"]["total"], 0)

    def test_inactive_counted_separately(self):
        s = self.add("停用项")
        panel.do_action({"action": "toggle", "key": s["id"], "field": "active"})
        p = self.payload()
        self.assertEqual(p["stats"]["inactive"], 1)
        self.assertEqual(p["stats"]["active"], 0)


# ------------------------------------------------------------------ 动作层
class TestActions(PanelBase):
    def test_add_defaults_to_usd_and_anchor(self):
        sub = self.add("Figma", amount=15, currency=None)
        self.assertEqual(sub["currency"], "USD")
        self.assertEqual(sub["anchor_day"], subs.parse_date(sub["next_due"]).day)

    def test_add_rejects_bad_input(self):
        for fields, want in (
            ({"name": "", "cycle": "monthly"}, "名称不能为空"),
            ({"name": "坏周期", "cycle": "custom_days"}, "自定义周期需要填天数"),
            ({"name": "坏金额", "amount": "abc", "cycle": "monthly"}, "金额得是数字"),
            ({"name": "坏日期", "cycle": "monthly", "next_due": "13月45日"}, "日期"),
        ):
            ok, msg = panel.do_action({"action": "add", "fields": fields})
            self.assertFalse(ok, fields)
            self.assertIn(want, msg)

    def test_add_accepts_loose_dates(self):
        ok, _ = panel.do_action({"action": "add", "fields": {
            "name": "松日期", "amount": "9", "cycle": "monthly", "next_due": "10/3"}})
        self.assertTrue(ok)
        sub = panel.S.load()["subscriptions"][0]
        self.assertEqual(subs.parse_date(sub["next_due"]), subs.parse_date("10/3"))

    def test_edit_updates_and_keeps_history(self):
        s = self.add("Spotify", amount=15)
        ok, msg = panel.do_action({"action": "edit", "key": s["id"], "fields": {
            "name": "Spotify 家庭版", "amount": "18", "currency": "USD",
            "cycle": "monthly", "next_due": "2027-03-31"}})
        self.assertTrue(ok, msg)
        row = panel.S.load()["subscriptions"][0]
        self.assertEqual(row["name"], "Spotify 家庭版")
        self.assertEqual(row["amount"], 18.0)
        self.assertEqual(row["next_due"], "2027-03-31")
        self.assertEqual(row["anchor_day"], 31)

    def test_renew_then_roll(self):
        today = date.today()
        a = self.add("提前续", cycle="monthly",
                     next_due=(today + timedelta(days=3)).isoformat())
        ok, _ = panel.do_action({"action": "renew", "key": a["id"]})
        self.assertTrue(ok)
        after = subs.parse_date(panel.S.load()["subscriptions"][0]["next_due"])
        self.assertGreater(after, today + timedelta(days=3))

        b = self.add("过期自动", cycle="monthly",
                     next_due=(today - timedelta(days=5)).isoformat())
        ok, msg = panel.do_action({"action": "roll"})
        self.assertTrue(ok)
        self.assertIn("顺延 1 个", msg)
        rows = {x["id"]: x for x in panel.S.load()["subscriptions"]}
        self.assertGreater(subs.parse_date(rows[b["id"]]["next_due"]), today)

    def test_toggle_active_and_auto_renew(self):
        s = self.add("可切换")
        panel.do_action({"action": "toggle", "key": s["id"], "field": "active"})
        self.assertFalse(panel.S.load()["subscriptions"][0]["active"])
        panel.do_action({"action": "toggle", "key": s["id"], "field": "auto_renew"})
        self.assertFalse(panel.S.load()["subscriptions"][0]["auto_renew"])
        panel.do_action({"action": "toggle", "key": s["id"], "field": "active"})
        self.assertTrue(panel.S.load()["subscriptions"][0]["active"])

    def test_remove_and_unknown_action(self):
        s = self.add("要删的")
        self.assertTrue(panel.do_action({"action": "remove", "key": s["id"]})[0])
        self.assertEqual(panel.S.load()["subscriptions"], [])
        self.assertFalse(panel.do_action({"action": "nope"})[0])

    def test_find_by_id_name_and_prefix(self):
        s = self.add("Netflix 高级会员")
        for key in (s["id"], "Netflix 高级会员", "netflix"):
            ok, msg = panel.do_action({"action": "renew", "key": key})
            self.assertTrue(ok, "%s -> %s" % (key, msg))


# ------------------------------------------------------------------ 页面/图标/工具
class TestPageAndAssets(PanelBase):
    def test_currency_options_default_first(self):
        html = panel.currency_options()
        self.assertTrue(html.startswith('<option value="%s">' % subs.DEFAULT_CURRENCY))
        self.assertLess(html.index('value="USD"'), html.index('value="CNY"'))

    def test_page_has_no_leftover_placeholders(self):
        page = (panel.PAGE.replace("%%CURRENCY_OPTIONS%%", panel.currency_options())
                .replace("%%DEFAULT_CURRENCY%%", subs.DEFAULT_CURRENCY))
        self.assertNotIn("%%", page)
        for token in ("订阅管家", 'name="currency"', "已续费",
                      "apple-mobile-web-app-capable", "subscriptions.json"):
            self.assertIn(token, page)
        self.assertIn("<title>订阅管家</title>", page)

    def test_icon_png_is_a_valid_png(self):
        data = panel.icon_png(180)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        w, h, depth, ctype = struct.unpack(">IIBB", data[16:26])
        self.assertEqual((w, h, depth, ctype), (180, 180, 8, 6))
        i, idat, crc_ok = 8, b"", True
        while i < len(data):
            ln = struct.unpack(">I", data[i:i + 4])[0]
            tag = data[i + 4:i + 8]
            body = data[i + 8:i + 8 + ln]
            crc = struct.unpack(">I", data[i + 8 + ln:i + 12 + ln])[0]
            crc_ok = crc_ok and (zlib.crc32(tag + body) & 0xffffffff) == crc
            if tag == b"IDAT":
                idat += body
            i += 12 + ln
        self.assertTrue(crc_ok, "chunk CRC 校验失败")
        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), h * (1 + w * 4))
        self.assertEqual(raw[1:5], b"\x00\x00\x00\x00", "圆角外的像素应为全透明")
        self.assertLess(len(data), 20_000, "图标不该是巨大文件")

    def test_is_lan_ip_filters_vpn_and_public(self):
        good = ["192.168.1.4", "10.0.0.7", "172.16.0.1", "172.31.255.254"]
        bad = ["198.18.0.1", "198.19.1.1", "169.254.3.4", "127.0.0.1",
               "8.8.8.8", "172.32.0.1", "192.169.1.1", "", "abc"]
        for ip in good:
            self.assertTrue(panel._is_lan_ip(ip), ip)
        for ip in bad:
            self.assertFalse(panel._is_lan_ip(ip), ip)

    def test_token_persists_across_restarts(self):
        old_file = panel.TOKEN_FILE
        panel.TOKEN_FILE = os.path.join(self.tmp, ".panel_token")
        self.addCleanup(setattr, panel, "TOKEN_FILE", old_file)
        t1 = panel.load_or_make_token()
        self.assertTrue(t1 and len(t1) >= 8)
        t2 = panel.load_or_make_token()          # 模拟重启
        self.assertEqual(t1, t2, "令牌必须重启后保持不变")
        self.assertEqual(panel.load_or_make_token("手动"), "手动")
        self.assertIsNone(panel.load_or_make_token(disabled=True))


# ------------------------------------------------------------------ HTTP 集成
class TestHttpServer(PanelBase):
    """真起一个服务, 验证令牌/权限行为(不是纸上功能)"""

    def setUp(self):
        super().setUp()
        self.add("测试项", amount=9, next_due=(date.today() + timedelta(days=3)).isoformat())
        panel.STATE["token"] = "t0ken-test"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.port

    def get(self, path, cookie=None, opener=None):
        req = urllib.request.Request(self.base + path)
        if cookie:
            req.add_header("Cookie", "subs_token=" + cookie)
        try:
            r = (opener.open(req) if opener else urllib.request.urlopen(req))
            return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def post(self, path, payload, cookie=None, opener=None):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        if cookie:
            req.add_header("Cookie", "subs_token=" + cookie)
        try:
            r = (opener.open(req) if opener else urllib.request.urlopen(req))
            return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_requires_token(self):
        for path in ("/", "/api/data"):
            code, _, _ = self.get(path)
            self.assertEqual(code, 401, path)
        code, body = self.post("/api/action", {"action": "roll"})
        self.assertEqual(code, 401)
        self.assertIn("令牌", body["msg"])

    def test_wrong_token_rejected(self):
        self.assertEqual(self.get("/?t=nope")[0], 401)

    def test_token_in_url_then_cookie_remembers_device(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        code, body, headers = self.get("/?t=t0ken-test", opener=opener)
        self.assertEqual(code, 200)
        self.assertIn("订阅管家", body.decode())
        self.assertIn("subs_token", headers.get("Set-Cookie", ""))
        self.assertTrue(any(c.name == "subs_token" for c in jar), "应下发 cookie")
        # 第二次访问: 带 cookie, 不带 ?t=
        code2, body2, _ = self.get("/api/data", opener=opener)
        self.assertEqual(code2, 200)
        self.assertEqual(json.loads(body2.decode())["stats"]["total"], 1)

    def test_write_actions_need_token_and_work_with_cookie(self):
        code, body = self.post("/api/action", {"action": "add", "fields": {
            "name": "手机加的", "amount": "11", "cycle": "monthly", "next_due": "10/20"}},
            cookie="t0ken-test")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(len(panel.S.load()["subscriptions"]), 2)

    def test_icon_is_public_and_png(self):
        code, body, headers = self.get("/icon.png")
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")

    def test_unknown_path_404(self):
        self.assertEqual(self.get("/nope", cookie="t0ken-test")[0], 404)

    def test_no_token_mode_open(self):
        panel.STATE["token"] = None
        self.assertEqual(self.get("/")[0], 200)
        code, body = self.post("/api/action", {"action": "roll"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
