#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subs.py 的单元测试 —— 纯标准库, 不依赖网络/数据库。

跑法:
    python3 -m unittest discover -s tests -v
    python3 -m unittest tests.test_subs -v
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subs  # noqa: E402


class Base(unittest.TestCase):
    """每个用例一个临时数据文件, 互不干扰"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="subs-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "subscriptions.json")
        self._old_store = subs.STORE
        subs.STORE = self.store
        self.addCleanup(self._restore)

    def _restore(self):
        subs.STORE = self._old_store

    # --- 辅助 ---
    def cli(self, *args):
        """跑一次 subs.py 命令, 返回 stdout (stderr 丢弃)"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = subs.main(list(args))
        self.assertEqual(rc, 0, "退出码应为 0, stderr=%s" % err.getvalue())
        return out.getvalue()

    def cli_fail(self, *args):
        """预期失败的命令, 返回 stderr 文本"""
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            subs.main(list(args))
        return err.getvalue()

    def store_data(self):
        with open(self.store, encoding="utf-8") as f:
            return json.load(f)

    def add(self, name, **kw):
        due = kw.pop("next_due", (date.today() + timedelta(days=5)).isoformat())
        args = ["add", "-n", name, "--next-due", due]
        for k, v in kw.items():
            if v is True:
                args.append("--" + k.replace("_", "-"))
            elif v is False or v is None:
                continue
            else:
                args += ["--" + k.replace("_", "-"), str(v)]
        self.cli(*args)
        return self.store_data()["subscriptions"][-1]


# ------------------------------------------------------------------ 日期/周期
class TestDateMath(Base):
    def test_parse_date_formats(self):
        for raw in ("2026-09-25", "2026/9/25", "2026.09.25", "20260925"):
            self.assertEqual(subs.parse_date(raw), date(2026, 9, 25), raw)
        self.assertEqual(subs.parse_date(date(2026, 1, 2)), date(2026, 1, 2))

    def test_parse_date_short_form_rolls_to_future(self):
        """9/25 这种没年份的: 已过去就理解成明年, 免得一填就过期"""
        today = date.today()
        for raw, md in (("01-05", (1, 5)), ("12/31", (12, 31)), ("6/15", (6, 15))):
            got = subs.parse_date(raw)
            candidate = date(today.year, md[0], md[1])
            expect = candidate if candidate >= today - timedelta(days=1) \
                else date(today.year + 1, md[0], md[1])
            self.assertEqual(got, expect, "%s -> %s" % (raw, got))
            self.assertGreaterEqual(got, today - timedelta(days=1))

    def test_parse_date_garbage_dies(self):
        self.assertIn("无法解析日期", self.cli_fail("add", "-n", "x", "--next-due", "13月45日"))

    def test_add_months_clamps_month_end(self):
        self.assertEqual(subs.add_months(date(2026, 1, 31), 1, 31), date(2026, 2, 28))
        self.assertEqual(subs.add_months(date(2024, 1, 31), 1, 31), date(2024, 2, 29))  # 闰年
        self.assertEqual(subs.add_months(date(2026, 3, 31), 1, 31), date(2026, 4, 30))
        self.assertEqual(subs.add_months(date(2026, 1, 15), 3), date(2026, 4, 15))
        self.assertEqual(subs.add_months(date(2026, 11, 30), 3, 30), date(2027, 2, 28))
        self.assertEqual(subs.add_months(date(2026, 1, 31), -1, 31), date(2025, 12, 31))

    def test_anchor_day_keeps_monthly_on_the_31st(self):
        """每月 31 号扣费: 2 月落到 28/29, 但 3 月要回到 31"""
        sub = {"cycle": "monthly", "anchor_day": 31}
        d = date(2026, 1, 31)
        seq = []
        for _ in range(4):
            d = subs.advance(d, sub)
            seq.append(d.isoformat())
        self.assertEqual(seq, ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"])

    def test_advance_all_cycles(self):
        base = date(2026, 1, 15)
        cases = {
            "weekly": ({"cycle": "weekly"}, date(2026, 1, 22)),
            "biweekly": ({"cycle": "biweekly"}, date(2026, 1, 29)),
            "monthly": ({"cycle": "monthly"}, date(2026, 2, 15)),
            "quarterly": ({"cycle": "quarterly"}, date(2026, 4, 15)),
            "semiannual": ({"cycle": "semiannual"}, date(2026, 7, 15)),
            "yearly": ({"cycle": "yearly"}, date(2027, 1, 15)),
            "custom_days": ({"cycle": "custom_days", "cycle_days": 45}, date(2026, 3, 1)),
        }
        for name, (sub, expect) in cases.items():
            self.assertEqual(subs.advance(base, sub), expect, name)


# ------------------------------------------------------------------ 显示
class TestFormatting(Base):
    def test_money_symbols_and_unknown_currency(self):
        self.assertEqual(subs.money({"amount": 68, "currency": "CNY"}), "¥68")
        self.assertEqual(subs.money({"amount": 15, "currency": "USD"}), "$15")
        self.assertEqual(subs.money({"amount": 88, "currency": "HKD"}), "HK$88")
        self.assertEqual(subs.money({"amount": 19.9}), "$19.9")          # 默认 USD
        self.assertEqual(subs.money({"amount": 1.5, "currency": "BTC"}), "BTC 1.5")
        self.assertEqual(subs.money({"amount": None}), "—")

    def test_status_labels(self):
        today = date.today()
        cases = [
            (today + timedelta(days=5), "还有 5 天"),
            (today + timedelta(days=1), "明天到期"),
            (today, "今天到期"),
            (today - timedelta(days=1), "已过期 1 天"),
            (today - timedelta(days=30), "已过期 30 天"),
        ]
        for when, label in cases:
            sub = {"next_due": when.isoformat()}
            self.assertEqual(subs.status_of(sub, today)[2], label)

    def test_total_by_currency(self):
        subs_list = [
            {"amount": 68, "currency": "CNY"}, {"amount": 21, "currency": "CNY"},
            {"amount": 12, "currency": "USD"}, {"amount": None},
        ]
        self.assertEqual(subs.total_by_currency(subs_list), "¥89 + $12")

    def test_cycle_labels(self):
        self.assertEqual(subs.cycle_label({"cycle": "monthly"}), "每月")
        self.assertEqual(subs.cycle_label({"cycle": "yearly"}), "每年")
        self.assertEqual(subs.cycle_label({"cycle": "custom_days", "cycle_days": 45}),
                         "每45天")


# ------------------------------------------------------------------ CRUD
class TestAddEditRemove(Base):
    def test_add_writes_default_currency_usd(self):
        sub = self.add("Figma 专业版", amount=15, cycle="monthly")
        self.assertEqual(sub["currency"], "USD")
        self.assertEqual(sub["amount"], 15.0)
        self.assertTrue(sub["active"])
        self.assertTrue(sub["auto_renew"])
        self.assertEqual(sub["anchor_day"], subs.parse_date(sub["next_due"]).day)
        self.assertIn("updated_at", self.store_data())

    def test_add_explicit_currency(self):
        self.assertEqual(self.add("微信读书", amount=19, currency="CNY")["currency"], "CNY")

    def test_default_currency_constant_is_used_everywhere(self):
        self.assertEqual(subs.DEFAULT_CURRENCY, "USD")
        self.assertEqual(self.add("没写币种", amount=3)["currency"], subs.DEFAULT_CURRENCY)

    def test_ids_are_unique_and_slugged(self):
        a = self.add("Netflix", amount=1)
        b = self.add("Netflix", amount=2)
        c = self.add("中文名字", amount=3)
        self.assertEqual(a["id"], "netflix")
        self.assertEqual(b["id"], "netflix-2")
        self.assertTrue(c["id"].startswith("sub"))
        ids = [s["id"] for s in self.store_data()["subscriptions"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_custom_days_requires_cycle_days(self):
        self.assertIn("custom_days", self.cli_fail(
            "add", "-n", "x", "-c", "custom_days", "--next-due", "2026-10-01"))

    def test_list_shows_all_and_sorted_by_due(self):
        self.add("远的", next_due=(date.today() + timedelta(days=40)).isoformat())
        self.add("近的", next_due=(date.today() + timedelta(days=2)).isoformat())
        out = self.cli("list")
        self.assertLess(out.index("近的"), out.index("远的"))

    def test_list_empty_state(self):
        self.assertIn("还没有订阅", self.cli("list"))

    def test_edit_changes_fields(self):
        self.add("Spotify", amount=15, notes="拼车")
        self.cli("edit", "spotify", "-a", "18", "--notes", "涨价了")
        sub = self.store_data()["subscriptions"][0]
        self.assertEqual(sub["amount"], 18.0)
        self.assertEqual(sub["notes"], "涨价了")

    def test_edit_without_fields_dies(self):
        self.add("Spotify", amount=15)
        self.assertIn("没给任何要改的字段", self.cli_fail("edit", "spotify"))

    def test_inactive_hides_from_reminders_but_stays_in_store(self):
        self.add("停用的", next_due=(date.today() - timedelta(days=1)).isoformat())
        self.cli("edit", "停用的", "--inactive")
        self.assertIn("没有需要续费", self.cli("due"))
        self.assertIn("已停用", self.cli("list", "--all"))

    def test_remove(self):
        self.add("要删的", amount=9)
        self.cli("remove", "要删的")
        self.assertEqual(self.store_data()["subscriptions"], [])

    def test_unknown_key_dies(self):
        self.assertIn("找不到订阅", self.cli_fail("renew", "不存在的订阅"))

    def test_reset_needs_yes(self):
        self.add("x", amount=1)
        self.assertIn("--yes", self.cli_fail("reset"))
        self.cli("reset", "--yes")
        self.assertEqual(self.store_data()["subscriptions"], [])

    def test_ids_command(self):
        self.add("Netflix", amount=1)
        self.add("Spotify", amount=2)
        self.assertEqual(self.cli("ids").split(), ["netflix", "spotify"])


# ------------------------------------------------------------------ 续费/提醒
class TestRenewAndRemind(Base):
    def test_renew_advances_one_cycle_even_if_not_due_yet(self):
        """提前续费也要往后推一个周期, 不能原地不动"""
        start = date.today() + timedelta(days=10)
        self.add("Spotify", amount=15, cycle="monthly", next_due=start.isoformat())
        self.cli("renew", "spotify")
        got = subs.parse_date(self.store_data()["subscriptions"][0]["next_due"])
        self.assertEqual(got, subs.add_months(start, 1, start.day))

    def test_renew_overdue_catches_up_to_future(self):
        start = date.today() - timedelta(days=70)
        self.add("老会员", amount=30, cycle="monthly", next_due=start.isoformat())
        self.cli("renew", "老会员")
        sub = self.store_data()["subscriptions"][0]
        got = subs.parse_date(sub["next_due"])
        self.assertGreater(got, date.today())
        self.assertLessEqual(got, date.today() + timedelta(days=31))

    def test_renew_records_history(self):
        self.add("Spotify", amount=15, cycle="monthly")
        self.cli("renew", "spotify")
        hist = self.store_data()["subscriptions"][0]["history"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["renewed_on"], date.today().isoformat())

    def test_renew_explicit_date(self):
        self.add("Spotify", amount=15, cycle="monthly")
        self.cli("renew", "spotify", "--date", "2027-01-31")
        sub = self.store_data()["subscriptions"][0]
        self.assertEqual(sub["next_due"], "2027-01-31")
        self.assertEqual(sub["anchor_day"], 31)

    def test_roll_only_touches_overdue_auto_renew(self):
        today = date.today()
        a = self.add("过期自动", cycle="monthly",
                     next_due=(today - timedelta(days=3)).isoformat())
        b = self.add("过期手动", cycle="monthly", no_auto_renew=True,
                     next_due=(today - timedelta(days=3)).isoformat())
        c = self.add("未来自动", cycle="monthly",
                     next_due=(today + timedelta(days=20)).isoformat())
        out = self.cli("roll")
        rows = {s["id"]: s for s in self.store_data()["subscriptions"]}
        self.assertGreater(subs.parse_date(rows[a["id"]]["next_due"]), today)
        self.assertEqual(rows[b["id"]]["next_due"], b["next_due"])   # 手动的不动
        self.assertEqual(rows[c["id"]]["next_due"], c["next_due"])   # 没到期不动
        self.assertIn("过期自动", out)

    def test_roll_says_nothing_to_do(self):
        self.add("未来自动", cycle="monthly",
                 next_due=(date.today() + timedelta(days=10)).isoformat())
        self.assertIn("没有需要顺延", self.cli("roll"))

    def test_check_is_silent_when_nothing_due(self):
        self.add("很久以后", cycle="yearly",
                 next_due=(date.today() + timedelta(days=200)).isoformat())
        self.assertEqual(self.cli("check").strip(), "")

    def test_check_prints_when_due(self):
        self.add("快到期", cycle="monthly",
                 next_due=(date.today() + timedelta(days=3)).isoformat())
        out = self.cli("check")
        self.assertIn("快到期", out)
        self.assertIn("还有 3 天", out)

    def test_overdue_always_reported_regardless_of_window(self):
        self.add("早就过期", cycle="monthly",
                 next_due=(date.today() - timedelta(days=90)).isoformat())
        out = self.cli("check", "-d", "1")
        self.assertIn("早就过期", out)
        self.assertIn("已过期 90 天", out)
        self.assertIn("已到期", out)

    def test_window_boundary(self):
        self.add("第7天", cycle="monthly",
                 next_due=(date.today() + timedelta(days=7)).isoformat())
        self.add("第8天", cycle="monthly",
                 next_due=(date.today() + timedelta(days=8)).isoformat())
        out = self.cli("check", "-d", "7")
        self.assertIn("第7天", out)
        self.assertNotIn("第8天", out)

    def test_due_shows_total_by_currency(self):
        self.add("美元项", amount=20, currency="USD", cycle="monthly",
                 next_due=(date.today() + timedelta(days=2)).isoformat())
        self.add("人民币项", amount=68, currency="CNY", cycle="monthly",
                 next_due=(date.today() + timedelta(days=1)).isoformat())
        out = self.cli("due")
        self.assertIn("合计待扣: ¥68 + $20", out)

    def test_corrupt_store_dies_cleanly(self):
        with open(self.store, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIn("数据文件格式错误", self.cli_fail("list"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
