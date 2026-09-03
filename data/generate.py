#!/usr/bin/env python3
"""Lumora SNS運用サンプルデータ生成スクリプト。

架空データ。実在の団体・人物・サービスとは無関係です。
登場するブランド Lumora、メールドメイン lumora.example.jp、
プラットフォーム tessera / ledgerly / relayo はすべて架空のものです。

標準ライブラリのみを使用し、random.Random(SEED) で決定的に生成します。
カレントディレクトリに依存せず、このスクリプトと同じディレクトリへ
accounts.csv / queue.json / history.csv の3ファイルを書き出します。

    python3 data/generate.py
"""

import csv
import json
import os
import random
from datetime import datetime, timedelta, timezone

SEED = 20260920
JST = timezone(timedelta(hours=9))

# 判定の基準時刻。scheduled_at がこれより過去 かつ status != published なら「遅延」。
NOW = datetime(2026, 9, 20, 21, 0, 0, tzinfo=JST)

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# 断定表現（contains_guarantee_claim = True の本文には必ずどれかを含める）
GUARANTEE_WORDS = ["必ず", "元本保証", "確実に", "断言", "言い切"]


# ---------------------------------------------------------------------------
# 1. 台帳（12行・固定）
# ---------------------------------------------------------------------------
# LM-102 の display_name は「Lumora」と「公式」の間が全角スペース U+3000。
# LM-106 は email 空欄、LM-107 は handle 空欄（ID でのみ解決できる例）。
ACCOUNTS_HEADER = ["account_id", "display_name", "handle", "platform",
                   "email", "status", "former_handle", "risk_review_required"]

ACCOUNTS = [
    ["LM-101", "Lumora公式", "@lumora_hq", "tessera",
     "hello@lumora.example.jp", "active", "", "no"],
    ["LM-102", "Lumora　公式", "@lumora_rl", "relayo",
     "hello@lumora.example.jp", "active", "", "no"],
    ["LM-103", "Lumora公式旧", "@lumora_hq_old", "tessera",
     "hello@lumora.example.jp", "suspended", "", "no"],
    ["LM-104", "Lumora公式", "@lumora_hq2", "tessera",
     "hello@lumora.example.jp", "active", "@lumora_hq_old", "no"],
    ["LM-105", "積立ラボ", "@tsumitate_lab", "tessera",
     "lab@lumora.example.jp", "active", "", "yes"],
    ["LM-106", "Lumora Journal", "@lumora_journal", "ledgerly",
     "", "active", "", "yes"],
    ["LM-107", "Lumoraニュース", "", "tessera",
     "news@lumora.example.jp", "active", "", "no"],
    ["LM-108", "Lumora日記", "@lumora_diary", "ledgerly",
     "diary@lumora.example.jp", "active", "", "no"],
    ["LM-109", "家計みらい", "@kakei_mirai", "relayo",
     "mirai@lumora.example.jp", "active", "", "yes"],
    ["LM-110", "まなびの窓", "@manabi_window", "relayo",
     "manabi@lumora.example.jp", "active", "", "yes"],
    ["LM-111", "雑記帳", "@zakki_lumora", "tessera",
     "misc@lumora.example.jp", "active", "", "no"],
    ["LM-112", "積立ラボ", "@tsumitate_lab2", "relayo",
     "lab2@lumora.example.jp", "active", "", "yes"],
]


# ---------------------------------------------------------------------------
# 2. 公開履歴（4行・固定）
# ---------------------------------------------------------------------------
HISTORY_HEADER = ["publish_id", "handle", "draft_id",
                  "published_at", "platform", "status"]

HISTORY = [
    # SC-1003 / SC-1010 は published → 重複としてスキップされる
    ["EV-3101", "@lumora_journal", "SC-1003",
     "2026-09-15T10:00:00+09:00", "ledgerly", "published"],
    ["EV-3102", "@lumora_hq", "SC-1010",
     "2026-09-14T09:30:00+09:00", "tessera", "published"],
    # SC-1002 は下書き側も status=published で整合している
    ["EV-3103", "@kakei_mirai", "SC-1002",
     "2026-09-13T20:00:00+09:00", "relayo", "published"],
    # SC-1017 は failed → 再投稿対象（ただし ledgerly 宛なので要手動対応）
    ["EV-3104", "@lumora_diary", "SC-1017",
     "2026-09-16T07:45:00+09:00", "ledgerly", "failed"],
]


# ---------------------------------------------------------------------------
# 3. 下書き40件の設計
# ---------------------------------------------------------------------------
# 各要素: (区分, platform_ref, status, contains_guarantee_claim, topic, body_preview)
#
# 「区分」は基準時刻 2026-09-20T21:00:00+09:00 で本体を通したときに期待される分類。
# 区分ラベルは設計を追うためのものでコメント相当。生成される queue.json には
# draft_id / topic / body_preview / platform_ref / status /
# contains_guarantee_claim / estimated_reach / scheduled_at / created_at
# の9キーのみを出力し、区分そのものは書き出さない。
#
#   PUBLISHED  : 公開済（status=published。取得段階で公開済みに仕分けられる）      7件
#   NOT_DUE    : 未到来（予定が 2026-09-21 以降）                                   3件
#   UNRESOLVED : 遅延 → 未解決（台帳に無い参照）                                     3件
#   AMBIGUOUS  : 遅延 → 要確認（同一メールに複数アカウントが該当）                   2件
#   SUSPENDED  : 遅延 → 凍結アカウント宛（LM-103）                                   3件
#   DUPLICATE  : 遅延 → 重複（公開履歴に published あり）                            2件
#   MANUAL     : 遅延 → 要手動対応（ledgerly は投稿API無し）                         7件
#   P1         : 遅延 → 承認待ちP1（断定表現あり・解決可・投稿可）                    5件
#   P2         : 遅延 → 承認待ちP2（断定表現なし・要確認アカウント宛）                5件
#   AUTO       : 遅延 → 自動公開（断定表現なし・要確認でない・tessera）               3件
#
# 合計40 / 遅延30 / contains_guarantee_claim=True は10件
# （P1 5 + MANUAL 1 + UNRESOLVED 2 + SUSPENDED 1 + AMBIGUOUS 1）。
# relayo への自動公開は0件（relayo 宛で断定表現なしのものは
# すべて risk_review_required=yes のアカウント宛にしてある）。
# LM-102 @lumora_rl を直接参照する下書きは作らない。

# --- 公開済 7件（status=published / 断定表現なし）---
SPEC_PUBLISHED = [
    ("PUBLISHED", "@kakei_mirai", "published", False,
     "家計簿の続け方", "家計簿は費目を5つに絞ると続けやすくなります。"),
    ("PUBLISHED", "@lumora_hq", "published", False,
     "固定費の見直し", "通信費と保険料から見直すと効果が出やすい傾向があります。"),
    ("PUBLISHED", "LM-105", "published", False,
     "積立の基本", "積立は金額よりも継続できる仕組みづくりが要点です。"),
    ("PUBLISHED", "manabi@lumora.example.jp", "published", False,
     "NISAの基本", "NISAの非課税枠は年単位で管理する考え方が基本です。"),
    ("PUBLISHED", "@zakki_lumora", "published", False,
     "先取り貯蓄", "給料日に自動で別口座へ移す方法を紹介します。"),
    ("PUBLISHED", "LM-111", "published", False,
     "家計の年間予算", "年払いの支出を先に洗い出すと予算が立てやすくなります。"),
    ("PUBLISHED", "@tsumitate_lab2", "published", False,
     "ふるさと納税の基礎", "控除の上限額は年収と家族構成で変わります。"),
]

# --- 未到来 3件（予定が 2026-09-21 以降）---
SPEC_NOT_DUE = [
    ("NOT_DUE", "@lumora_hq", "scheduled", False,
     "秋の家計点検", "季節の変わり目は光熱費の契約を見直す機会になります。"),
    ("NOT_DUE", "LM-111", "draft", False,
     "家計簿アプリの選び方", "連携できる口座数と入力の手間で選ぶ観点を整理します。"),
    ("NOT_DUE", "@kakei_mirai", "scheduled", False,
     "積立の見直し時期", "収入が変わったタイミングは積立額を点検する目安です。"),
]

# --- 遅延 → 未解決 3件（台帳に存在しないハンドル。うち2件は断定表現あり）---
SPEC_UNRESOLVED = [
    ("UNRESOLVED", "@nobody_here", "scheduled", True,
     "積立の効果", "この積立プランなら資産は必ず増えると言い切れます。"),
    ("UNRESOLVED", "@nobody_here", "draft", True,
     "老後資金の準備", "元本保証つきなので、老後資金は確実に用意できます。"),
    ("UNRESOLVED", "@phantom_writer", "scheduled", False,
     "家計の記録", "レシートは週1回まとめて入力する方法もあります。"),
]

# --- 遅延 → 要確認 2件（hello@ は LM-101/102/103/104 の4件に該当し一意に決まらない）---
SPEC_AMBIGUOUS = [
    ("AMBIGUOUS", "hello@lumora.example.jp", "scheduled", True,
     "積立シミュレーション", "毎月1万円の積立で、10年後の資産は必ず増えると断言します。"),
    ("AMBIGUOUS", "hello@lumora.example.jp", "draft", False,
     "家計簿の費目分け", "固定費と変動費を分けるだけでも把握しやすくなります。"),
]

# --- 遅延 → 凍結アカウント宛 3件（LM-103 suspended。後継 LM-104 へ自動転送はしない）---
SPEC_SUSPENDED = [
    ("SUSPENDED", "@lumora_hq_old", "scheduled", True,
     "固定費の見直し", "固定費の見直しで、家計は確実に黒字になります。"),
    ("SUSPENDED", "@lumora_hq_old", "draft", False,
     "光熱費の節約", "契約アンペアの見直しで基本料金が変わる場合があります。"),
    ("SUSPENDED", "@lumora_hq_old", "scheduled", False,
     "通信費の見直し", "料金プランは使用データ量に合わせて選ぶ考え方です。"),
]

# --- 遅延 → 重複 2件（draft_id 固定。公開履歴に published がある）---
SPEC_DUP_1003 = ("DUPLICATE", "@lumora_journal", "draft", False,
                 "NISAの基本", "NISAの口座は1人1つという原則をおさらいします。")
SPEC_DUP_1010 = ("DUPLICATE", "@lumora_hq", "scheduled", False,
                 "積立の始め方", "毎月の積立額は手取りの1割から始める考え方があります。")

# --- 遅延 → 要手動対応 7件（ledgerly の LM-106 / LM-108 宛。投稿APIが無い）---
# SC-1017 は draft_id 固定（履歴が failed のため再投稿対象になるが、
# それでも ledgerly 宛なので自動公開はされない）。
SPEC_MANUAL_1017 = ("MANUAL", "@lumora_diary", "scheduled", False,
                    "家計簿の振り返り", "月末に3分だけ振り返る習慣づくりの話です。")
SPEC_MANUAL = [
    ("MANUAL", "@lumora_journal", "scheduled", True,
     "NISAの活用", "NISAを使えば元本保証のまま資産が必ず増える仕組みです。"),
    ("MANUAL", "LM-106", "draft", False,
     "積立の記録", "積立の履歴は年1回まとめて確認する方法もあります。"),
    ("MANUAL", "@lumora_journal", "scheduled", False,
     "家計の棚卸し", "半年に一度、口座とサブスクを棚卸しする観点です。"),
    ("MANUAL", "@lumora_diary", "draft", False,
     "固定費メモ", "見直した固定費は金額と日付を残しておくと便利です。"),
    ("MANUAL", "diary@lumora.example.jp", "scheduled", False,
     "積立ノート", "積立の目的を1行で書き残す習慣を紹介します。"),
    ("MANUAL", "LM-108", "draft", False,
     "家計簿の続け方", "入力を1日1分に収める工夫をまとめました。"),
]

# --- 遅延 → 承認待ちP1 5件（断定表現あり・解決可・投稿可。tessera3 / relayo2）---
SPEC_P1 = [
    ("P1", "@lumora_hq", "scheduled", True,
     "先取り貯蓄", "先取り貯蓄を始めれば、貯金は確実に増えます。"),
    ("P1", "@zakki_lumora", "draft", True,
     "家計の赤字対策", "この方法なら家計の赤字は必ず解消できます。"),
    ("P1", "@tsumitate_lab", "scheduled", True,
     "積立投資の心構え", "積立投資は元本保証と同じ安心感で続けられます。"),
    ("P1", "@kakei_mirai", "scheduled", True,
     "保険の見直し", "保険を乗り換えれば、支出は確実に下がると断言できます。"),
    ("P1", "@manabi_window", "draft", True,
     "NISAの基本", "新NISAの活用で、将来の資産は必ず増えると言えます。"),
]

# --- 遅延 → 承認待ちP2 5件（断定表現なし・risk_review_required=yes 宛）---
# relayo 4件（うち1件は mirai@ のメール参照）＋ tessera の LM-105 宛 1件。
SPEC_P2 = [
    ("P2", "@kakei_mirai", "scheduled", False,
     "家計簿の始め方", "まずは1か月だけ支出を記録してみる方法です。"),
    ("P2", "@manabi_window", "draft", False,
     "固定費の見直し", "サブスクの契約日を一覧にすると見直しやすくなります。"),
    ("P2", "@tsumitate_lab2", "scheduled", False,
     "積立の考え方", "相場を読むより積立の期間を長く取る考え方です。"),
    ("P2", "mirai@lumora.example.jp", "scheduled", False,
     "NISAの基本", "非課税枠の使い方は年間の入金計画から考えます。"),
    ("P2", "@tsumitate_lab", "draft", False,
     "積立の記録方法", "積立の設定内容は1枚の表にまとめると管理しやすくなります。"),
]

# --- 遅延 → 自動公開 3件（断定表現なし・要確認でない・tessera）---
# LM-107 は handle 空欄なので ID 参照で解決させる。
SPEC_AUTO = [
    ("AUTO", "@zakki_lumora", "scheduled", False,
     "家計の小ワザ", "買い物メモを作ると衝動買いが減るという話です。"),
    ("AUTO", "@lumora_hq", "draft", False,
     "固定費の見直し", "見直しの優先順位は金額の大きい順が基本です。"),
    ("AUTO", "LM-107", "scheduled", False,
     "積立の基本", "積立は自動化して意識しない状態を作るのが要点です。"),
]

# draft_id を固定する4件（履歴と対応させるため）
PINNED = {
    "SC-1002": SPEC_PUBLISHED[0],   # 公開済 かつ 履歴 EV-3103 published
    "SC-1003": SPEC_DUP_1003,       # 履歴 EV-3101 published → 重複
    "SC-1010": SPEC_DUP_1010,       # 履歴 EV-3102 published → 重複
    "SC-1017": SPEC_MANUAL_1017,    # 履歴 EV-3104 failed → 再投稿対象だが手動
}

# 固定4件を除いた36件。この順序のまま出すと区分が draft_id 順に固まるので、
# 後段で SEED 固定のシャッフルを掛ける。
FLOATING = (
    SPEC_PUBLISHED[1:]
    + SPEC_NOT_DUE
    + SPEC_UNRESOLVED
    + SPEC_AMBIGUOUS
    + SPEC_SUSPENDED
    + SPEC_MANUAL
    + SPEC_P1
    + SPEC_P2
    + SPEC_AUTO
)

# 固定4件の予定時刻。公開履歴の時刻と前後関係が矛盾しないようにする。
PINNED_WHEN = {
    "SC-1002": (2026, 9, 13, 20, 0),   # 履歴 20:00 と同時刻
    "SC-1003": (2026, 9, 15, 9, 0),    # 履歴 10:00 の1時間前
    "SC-1010": (2026, 9, 14, 9, 0),    # 履歴 09:30 の30分前
    "SC-1017": (2026, 9, 16, 7, 0),    # 履歴 07:45 の45分前
}

# 未到来にする区分（予定を 2026-09-21 以降にする）
FUTURE_CATEGORIES = {"NOT_DUE"}


def fmt_scheduled(parts, style):
    """3フォーマットを輪番で使い分ける。

    style 0: ISO 8601      2026-09-12T08:30:00+09:00
    style 1: スラッシュ     2026/09/13 19:00
    style 2: 英語月名       14-Sep-2026 12:00
    """
    y, mo, d, h, mi = parts
    if style == 0:
        return "%04d-%02d-%02dT%02d:%02d:00+09:00" % (y, mo, d, h, mi)
    if style == 1:
        return "%04d/%02d/%02d %02d:%02d" % (y, mo, d, h, mi)
    return "%02d-%s-%04d %02d:%02d" % (d, MONTH_ABBR[mo - 1], y, h, mi)


def fmt_created(dt):
    """created_at は常に ISO 8601。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def build_drafts():
    rng = random.Random(SEED)

    ids = ["SC-%04d" % n for n in range(1001, 1041)]
    floating_ids = [i for i in ids if i not in PINNED]

    shuffled = list(FLOATING)
    rng.shuffle(shuffled)

    assigned = dict(PINNED)
    for did, spec in zip(floating_ids, shuffled):
        assigned[did] = spec

    drafts = []
    for index, did in enumerate(ids):
        category, ref, status, guarantee, topic, body = assigned[did]

        if did in PINNED_WHEN:
            parts = PINNED_WHEN[did]
        elif category in FUTURE_CATEGORIES:
            # 未到来: 基準時刻より後
            parts = (2026, 9, rng.choice([21, 22, 23, 25, 27, 28]),
                     rng.choice([8, 10, 12, 19]), rng.choice([0, 15, 30, 45]))
        else:
            # 遅延・公開済: 基準時刻より前
            parts = (2026, 9, rng.choice([5, 6, 8, 9, 10, 11, 12, 14, 15, 17, 18, 19]),
                     rng.choice([7, 8, 9, 12, 18, 19, 20]),
                     rng.choice([0, 15, 30, 45]))

        scheduled_at = fmt_scheduled(parts, index % 3)

        y, mo, d, h, mi = parts
        scheduled_dt = datetime(y, mo, d, h, mi, tzinfo=JST)
        created = scheduled_dt - timedelta(days=rng.randint(3, 20),
                                           hours=rng.randint(0, 6))
        assert created < scheduled_dt

        drafts.append({
            "draft_id": did,
            "topic": topic,
            "body_preview": body,
            "platform_ref": ref,
            "status": status,
            "contains_guarantee_claim": guarantee,
            "estimated_reach": rng.randrange(300, 12000, 50),
            "scheduled_at": scheduled_at,
            "created_at": fmt_created(created),
        })

    _self_check(drafts, assigned)
    return drafts, assigned


def _self_check(drafts, assigned):
    """設計どおりに出来ているかの軽い自己点検（本体の判定ロジックは使わない）。"""
    assert len(drafts) == 40, len(drafts)
    assert len({d["draft_id"] for d in drafts}) == 40

    for d in drafts:
        has_word = any(w in d["body_preview"] for w in GUARANTEE_WORDS)
        if d["contains_guarantee_claim"]:
            assert has_word, "断定表現が本文に無い: %s" % d["draft_id"]
        else:
            assert not has_word, "断定表現が混入している: %s" % d["draft_id"]

    guarantees = sum(1 for d in drafts if d["contains_guarantee_claim"])
    assert guarantees == 10, guarantees

    counts = {}
    for did, spec in assigned.items():
        counts[spec[0]] = counts.get(spec[0], 0) + 1
    expected = {"PUBLISHED": 7, "NOT_DUE": 3, "UNRESOLVED": 3, "AMBIGUOUS": 2,
                "SUSPENDED": 3, "DUPLICATE": 2, "MANUAL": 7, "P1": 5,
                "P2": 5, "AUTO": 3}
    assert counts == expected, counts


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main():
    write_csv(os.path.join(OUT_DIR, "accounts.csv"), ACCOUNTS_HEADER, ACCOUNTS)
    write_csv(os.path.join(OUT_DIR, "history.csv"), HISTORY_HEADER, HISTORY)

    drafts, assigned = build_drafts()
    with open(os.path.join(OUT_DIR, "queue.json"), "w", encoding="utf-8") as fh:
        json.dump({"drafts": drafts}, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print("wrote 3 files to %s" % OUT_DIR)
    print("accounts: %d / drafts: %d / history: %d"
          % (len(ACCOUNTS), len(drafts), len(HISTORY)))
    print("-- draft_id と設計上の区分 --")
    for did in sorted(assigned):
        print("  %s  %s" % (did, assigned[did][0]))


if __name__ == "__main__":
    main()
