"""出力。状態は変更しない。

承認待ちは行き止まりにせず、approved を true にして再実行すれば公開される形にする。
"""
import json
import os

import config
import gatekeeper as G


def _write(path, text):
    """一時ファイルへ書いてから置換する。

    直接書くと、中断時に切り詰められたファイルが残る。
    state.json が壊れると重複抑止が消えるため、出力全体を原子的にする。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_approvals(outdir=None):
    """前回の承認待ちファイルから approved フラグを読み込む。"""
    path = os.path.join(outdir or config.OUTPUT_DIR, "pending_approval.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    # 明示的な boolean の true のみを承認とみなす。
    # bool() 変換にすると文字列 "false" や "no" まで承認になり、承認境界を迂回できる。
    return {i["draft_id"]: i.get("approved") is True
            for i in data.get("items", []) if isinstance(i, dict) and i.get("draft_id")}


def load_state(outdir=None):
    """自前の公開実績。Tessera には冪等キーが無いため、重複抑止はこれに依存する。"""
    path = os.path.join(outdir or config.OUTPUT_DIR, "state.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("published", {})
    except (json.JSONDecodeError, OSError):
        return {}


def summarize(decisions, results, collect_result):
    """集計のみ行い、ファイルは書かない。レポート生成前の材料として使う。"""
    by = {}
    for d in decisions:
        by.setdefault(d.action, []).append(d)

    summary = {
        "fetched": collect_result.fetched,
        "fetch_pages": collect_result.pages,
        "already_published": len(collect_result.already_published),
        "not_due": len(collect_result.not_due),
        "unparseable_schedule": len(collect_result.unparseable),
        "delayed": len(decisions),
        "counts": {k: len(v) for k, v in sorted(by.items())},
        "published_ok": sum(1 for r in results.values() if r.ok),
        "published_failed": sum(1 for r in results.values() if not r.ok),
    }
    return summary, by


def write_all(decisions, results, collect_result, next_draft, outdir=None, prev_state=None):
    outdir = outdir or config.OUTPUT_DIR
    summary, by = summarize(decisions, results, collect_result)

    _write(os.path.join(outdir, "result.json"), json.dumps({
        "summary": summary,
        "decisions": [d.to_dict() for d in decisions],
        "publish_results": {k: v.to_dict() for k, v in results.items()},
    }, ensure_ascii=False, indent=2))

    pending = [{**d.to_dict(), "approved": False,
                "body_preview": d.draft.get("body_preview")}
               for d in by.get(G.APPROVAL_P1, []) + by.get(G.APPROVAL_P2, [])]
    # 承認済みで公開が完了したものは、承認履歴として残す（再承認を求めない）
    for d in by.get(G.PUBLISH, []):
        if d.reason == "承認済み":
            r = results.get(d.draft["draft_id"])
            pending.append({**d.to_dict(), "approved": True,
                            "state": "published" if (r and r.ok) else "publish_failed",
                            "body_preview": d.draft.get("body_preview")})
    _write(os.path.join(outdir, "pending_approval.json"), json.dumps({
        "note": "approved を true にして再実行すると公開されます",
        "items": pending,
    }, ensure_ascii=False, indent=2))

    manual = by.get(G.MANUAL, [])
    lines = ["# 手動投稿が必要な下書き（Ledgerly）", "",
             "投稿APIが存在しないため、以下は人手での投稿が必要です。",
             f"件数: {len(manual)}", ""]
    for d in manual:
        lines += [f"## {d.draft['draft_id']}  {d.draft.get('topic','')}",
                  f"- 投稿先: {d.account.get('handle')}（{d.account.get('account_id')}）",
                  f"- 予定時刻: {d.draft.get('_scheduled_at')}"]
        if d.flags:
            lines.append(f"- **確認事項: {' / '.join(d.flags)}**")
        lines += ["", "```", str(d.draft.get("body_preview", "")), "```", ""]
    _write(os.path.join(outdir, "manual_ledgerly.md"), "\n".join(lines))

    blocked = [d.to_dict() | {"detail": d.reason}
               for d in by.get(G.BLOCKED, []) + by.get(G.UNKNOWN_HISTORY, [])]
    # 時刻を解析できなかった下書きも、件数だけでなく実体を残す（人が直せるように）
    blocked += [{"draft_id": d.get("draft_id"), "topic": d.get("topic"),
                 "scheduled_at": d.get("scheduled_at"), "platform_ref": d.get("platform_ref"),
                 "action": "blocked", "reason": "scheduled_at を解析できない",
                 "flags": [], "detail": "既知の3フォーマットのいずれにも該当しない"}
                for d in collect_result.unparseable]
    _write(os.path.join(outdir, "unresolved.json"),
           json.dumps({"items": blocked}, ensure_ascii=False, indent=2))

    _write(os.path.join(outdir, "next_draft.json"),
           json.dumps(next_draft, ensure_ascii=False, indent=2))

    return summary


def save_state(state, outdir=None):
    """公開実績の保存。投稿ごとに呼ぶ。

    全投稿の完了後にまとめて保存すると、途中で停止したときに成功済みの実績が失われ、
    次回実行で再投稿になる。Tessera には冪等キーが無いためこれは実害になる。
    """
    _write(os.path.join(outdir or config.OUTPUT_DIR, "state.json"),
           json.dumps({"published": state}, ensure_ascii=False, indent=2))


def compose_next_draft(summary, decisions):
    """レポートから次回の下書きを生成する。

    Tessera に下書き登録用のエンドポイントは存在しない（POST は /api/publish のみ）ため、
    生成物をローカルへ出力するに留める。詳細は DESIGN.md 積み残し参照。
    """
    top = [d for d in decisions if d.action == G.PUBLISH][:3]
    body = (f"本日の自動公開 {summary['published_ok']}件。"
            f"承認待ち {summary['counts'].get(G.APPROVAL_P1, 0) + summary['counts'].get(G.APPROVAL_P2, 0)}件、"
            f"手動対応 {summary['counts'].get(G.MANUAL, 0)}件。")
    return {
        "registered": False,
        "reason": "Tessera に下書き登録エンドポイントが存在しないためローカル出力のみ",
        "draft": {
            "topic": "運用レポート",
            "body_preview": body,
            "related_draft_ids": [d.draft["draft_id"] for d in top],
        },
    }
