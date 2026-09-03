#!/usr/bin/env python3
"""エントリポイント。Router（宛先への振り分け）の責務のみを持ち、可否の再判定はしない。"""
import sys

import collector
import config
import gatekeeper as G
import reporter
from adapters import build_adapters
from normalize import parse_scheduled_at
from resolver import AccountDirectory


def run(outdir=None):
    outdir = outdir or config.OUTPUT_DIR
    adapters = build_adapters(outdir)

    print("[1/5] 下書きを取得中（Tessera）...")
    now = parse_scheduled_at(config.SIMULATED_NOW) if config.SIMULATED_NOW else None
    if now:
        print(f"      [検証モード] 基準時刻を {now.isoformat()} に固定")
    cr = collector.collect(adapters["tessera"], now=now)
    print(f"      取得 {cr.fetched}件 / {cr.pages}ページ  → 遅延 {len(cr.delayed)}件"
          f"（公開済 {len(cr.already_published)} / 未到来 {len(cr.not_due)}"
          f" / 時刻解析不能 {len(cr.unparseable)}"
          f" / ID重複 {cr.duplicate_ids}）")

    print("[2/5] アカウント台帳を取得中（Ledgerly・応答が遅いので1回のみ）...")
    directory = AccountDirectory(adapters["ledgerly"].fetch_accounts_csv())
    publish_log = G.parse_publish_log(adapters["ledgerly"].fetch_publish_log_csv())
    approvals = reporter.load_approvals(outdir)
    state = reporter.load_state(outdir)
    for msg in adapters["ledgerly"].degraded:
        print(f"      [縮退] {msg}")
    print(f"      台帳 {len(directory.accounts)}件 / 公開履歴 {len(publish_log)}件"
          f" / 承認済 {sum(1 for v in approvals.values() if v)}件"
          f" / 公開実績 {len(state)}件")
    if state:
        print(f"      ※ 公開実績にある下書きは重複として除外されます。"
              f"初回相当の実行をする場合は別の出力先を指定してください")

    print("[3/5] 判定中...")
    gate = G.Gatekeeper(directory, publish_log, approvals, adapters, state)
    decisions = [gate.decide(d) for d in cr.delayed]

    print("[4/5] 公開実行中...")
    results = {}
    for d in decisions:
        if d.action != G.PUBLISH:
            continue
        adapter = adapters[d.account["platform"]]
        res = adapter.publish(d.draft, d.account)
        results[d.draft["draft_id"]] = res
        if res.ok:
            # 全件終わってからまとめて保存すると、途中停止で実績が消えて再投稿になる
            state[d.draft["draft_id"]] = {"post_id": res.post_id}
            reporter.save_state(state, outdir)
        mark = "OK " if res.ok else "NG "
        print(f"      {mark}{d.draft['draft_id']} -> {d.account['platform']}"
              f" {d.account.get('handle')}"
              f"{'  ' + res.reason if res.reason else ''}")

    print("[5/5] レポート出力中...")
    summary, _ = reporter.summarize(decisions, results, cr)
    next_draft = reporter.compose_next_draft(summary, decisions)
    summary_final = reporter.write_all(decisions, results, cr, next_draft, outdir)
    reporter.save_state(state, outdir)

    print("\n===== 実行結果 =====")
    print(f"遅延下書き: {summary_final['delayed']}件")
    labels = {G.PUBLISH: "自動公開", G.APPROVAL_P1: "承認待ちP1(断定表現)",
              G.APPROVAL_P2: "承認待ちP2(要確認アカウント)", G.MANUAL: "要手動対応(Ledgerly)",
              G.SKIP_DUPLICATE: "重複のため除外", G.BLOCKED: "未解決・凍結等",
              G.UNKNOWN_HISTORY: "公開履歴の状態が不明"}
    for k, v in summary_final["counts"].items():
        print(f"  {labels.get(k, k):<28} {v:>3}件")
    print(f"  公開成功 {summary_final['published_ok']} / 失敗 {summary_final['published_failed']}")
    print(f"\n出力: {outdir}/ (result.json, pending_approval.json, "
          f"manual_ledgerly.md, unresolved.json, next_draft.json)")
    return summary_final


if __name__ == "__main__":
    try:
        run(sys.argv[1] if len(sys.argv) > 1 else None)
    except (RuntimeError, OSError) as e:
        # 外部システム由来の停止（接続不可・タイムアウト含む）は、
        # スタックトレースではなく対処可能な形で返す。OSError は URLError の親。
        print(f"\n[中止] {e}", file=sys.stderr)
        print("       モックAPIが起動しているか確認してください "
              "(cd mock && python3 run_all.py)", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)
