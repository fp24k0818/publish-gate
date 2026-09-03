#!/usr/bin/env python3
"""正規化と名寄せの単体テスト。

この2つに限定しているのは、外部システムに依存せず、かつ誤りが静かに通過する箇所だから。
（誤った時刻解析は「遅延ではない」と判定され、誤った名寄せは別アカウントへの公開になる）
実行: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import resolver as R  # noqa: E402
from normalize import (UnparseableDatetime, normalize_display_name,  # noqa: E402
                       parse_scheduled_at)

LEDGER = """account_id,display_name,handle,platform,email,status,former_handle,risk_review_required
LM-101,Lumora公式,@lumora_hq,tessera,hello@lumora.example.jp,active,,no
LM-102,Lumora　公式,@lumora_rl,relayo,hello@lumora.example.jp,active,,no
LM-103,Lumora公式旧,@lumora_hq_old,tessera,hello@lumora.example.jp,suspended,,no
LM-104,Lumora公式,@lumora_hq2,tessera,hello@lumora.example.jp,active,@lumora_hq_old,no
LM-107,Lumoraニュース,,tessera,news@lumora.example.jp,active,,no
LM-106,Lumora Journal,@lumora_journal,ledgerly,,active,,yes
"""


class TestScheduledAt(unittest.TestCase):
    def test_three_formats_resolve_to_same_instant(self):
        a = parse_scheduled_at("2026-08-10T09:00:00+09:00")
        b = parse_scheduled_at("2026/08/10 09:00")
        c = parse_scheduled_at("10-Aug-2026 09:00")
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_iso_variants_without_seconds_or_offset(self):
        """形の推測で分岐すると、正当なISO 8601を取りこぼす。"""
        for iso in ["2026-08-10T09:30", "2026-08-10T09:30:00", "2026-08-10T00:30:00Z"]:
            self.assertIsNotNone(parse_scheduled_at(iso).tzinfo, iso)
        self.assertEqual(parse_scheduled_at("2026-08-10T09:30"),
                         parse_scheduled_at("2026-08-10T09:30:00"))
        self.assertEqual(parse_scheduled_at("2026-08-10T00:30:00Z"),
                         parse_scheduled_at("2026-08-10T09:30:00+09:00"))

    def test_naive_formats_are_treated_as_jst(self):
        self.assertEqual(parse_scheduled_at("2026/08/10 09:00").utcoffset().seconds, 9 * 3600)

    def test_unknown_format_is_not_silently_accepted(self):
        for bad in ["", "   ", None, "2026年8月10日 9時", "10/08/2026 09:00:00 PM"]:
            with self.assertRaises(UnparseableDatetime):
                parse_scheduled_at(bad)

    def test_out_of_spec_iso_like_values_are_rejected(self):
        """標準パーサに丸投げすると受理される形式を、仕様の3形式に絞り込む。"""
        for bad in ["2026-08-10", "2026-08-10X09:30", "2026-W33-1"]:
            with self.assertRaises(UnparseableDatetime, msg=bad):
                parse_scheduled_at(bad)

    def test_invalid_calendar_dates_do_not_crash(self):
        """形式には合うが暦として存在しない値。生のValueErrorを漏らさない。"""
        for bad in ["2026/02/30 09:00", "31-Feb-2026 09:00", "2026/13/01 09:00"]:
            with self.assertRaises(UnparseableDatetime, msg=bad):
                parse_scheduled_at(bad)


class TestDisplayName(unittest.TestCase):
    def test_width_and_space_variants_collapse(self):
        self.assertEqual(normalize_display_name("Lumora　公式"),
                         normalize_display_name("Lumora 公式"))

    def test_normalization_increases_collisions(self):
        """正規化は衝突を減らさず、むしろ増やす。ゆえに識別子に使わない。

        素の表示名で一致するのは LM-101 と LM-104 の2件だが、
        全角スペースを正規化すると LM-102「Lumora　公式」も同じ文字列になり3件に増える。
        """
        d = R.AccountDirectory(LEDGER)
        raw = [a for a in d.accounts if a["display_name"] == "Lumora公式"]
        norm = [a for a in d.accounts if a["_display_norm"] == "Lumora公式"]
        self.assertEqual(len(raw), 2)
        self.assertEqual(len(norm), 3)
        self.assertEqual(len({a["account_id"] for a in norm}), 3)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.d = R.AccountDirectory(LEDGER)

    def test_account_id_takes_priority(self):
        r = self.d.resolve("LM-101")
        self.assertEqual(r.state, R.OK)
        self.assertEqual(r.account["account_id"], "LM-101")

    def test_handle_resolves(self):
        self.assertEqual(self.d.resolve("@lumora_journal").account["account_id"], "LM-106")

    def test_unique_email_resolves(self):
        self.assertEqual(self.d.resolve("news@lumora.example.jp").account["account_id"], "LM-107")

    def test_shared_email_is_not_guessed(self):
        """activeが複数残る場合は推測しない。誤公開は取り消せない。"""
        r = self.d.resolve("hello@lumora.example.jp")
        self.assertEqual(r.state, R.AMBIGUOUS)
        self.assertGreater(len(r.candidates), 1)

    def test_missing_reference_is_unresolved(self):
        self.assertEqual(self.d.resolve("@nobody_here").state, R.UNRESOLVED)
        self.assertEqual(self.d.resolve("").state, R.UNRESOLVED)

    def test_unknown_status_is_treated_as_not_publishable(self):
        """active 以外はすべて投稿対象外。値が増えたときに公開側へ倒れないこと。"""
        ledger = LEDGER + "LM-199,新設,@new_acct,tessera,new@lumora.example.jp,disabled,,no\n"
        ledger += "LM-198,状態空,@blank_status,tessera,blank@lumora.example.jp,,,no\n"
        d = R.AccountDirectory(ledger)
        self.assertEqual(d.resolve("@new_acct").state, R.SUSPENDED)
        self.assertEqual(d.resolve("@blank_status").state, R.SUSPENDED)

    def test_suspended_is_blocked_but_successor_is_reported(self):
        r = self.d.resolve("@lumora_hq_old")
        self.assertEqual(r.state, R.SUSPENDED)
        self.assertIn("LM-104", r.note)

    def test_empty_handle_account_is_reachable_by_id_only(self):
        self.assertEqual(self.d.resolve("LM-107").state, R.OK)
        self.assertNotIn("", self.d.by_handle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
