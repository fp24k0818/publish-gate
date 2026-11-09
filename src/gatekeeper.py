"""投稿しない理由をすべてここで確定させる。以降の層は判定をやり直さない。

通信を一切行わずに「なぜ公開されなかったか」を再現できることを優先している。
"""
import csv
import io

import config
import resolver as R

UNKNOWN_HISTORY = "unknown_history"
PUBLISH = "publish"
SKIP_DUPLICATE = "skip_duplicate"
MANUAL = "manual"
APPROVAL_P1 = "approval_p1"
APPROVAL_P2 = "approval_p2"
BLOCKED = "blocked"


class Decision:
    def __init__(self, action, reason, draft, account=None, resolution=None, flags=None):
        self.action, self.reason, self.draft = action, reason, draft
        self.account, self.resolution = account, resolution
        self.flags = flags or []

    def to_dict(self):
        return {
            "draft_id": self.draft["draft_id"],
            "topic": self.draft.get("topic"),
            "scheduled_at": self.draft.get("_scheduled_at") or self.draft.get("scheduled_at"),
            "platform_ref": self.draft.get("platform_ref"),
            "account_id": (self.account or {}).get("account_id"),
            "handle": (self.account or {}).get("handle"),
            "platform": (self.account or {}).get("platform"),
            "action": self.action,
            "reason": self.reason,
            "flags": self.flags,
        }


def parse_publish_log(csv_text):
    """公開履歴を正とする。下書き側の status は更新漏れがありうるため信用しない。"""
    latest = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        did = row.get("draft_id")
        if not did:
            continue
        prev = latest.get(did)
        if prev is None or (row.get("published_at") or "") >= (prev.get("published_at") or ""):
            latest[did] = row
    return latest


class Gatekeeper:
    def __init__(self, directory, publish_log, approvals=None, adapters=None, state=None):
        self.directory = directory
        self.publish_log = publish_log
        self.approvals = approvals or {}
        self.adapters = adapters or {}
        self.state = state or {}

    def decide(self, draft):
        # 内容起因のリスクは投稿先の解決可否と独立に成立する。
        # 名寄せに失敗した下書きにも断定表現は含まれ得るため、先に判定して全経路で持ち回る。
        # 判定は非対称にする。
        #   リスク信号は「不明なら危険」側（フィールド欠落＝判定不能＝人が見る）
        #   承認は「明示的な boolean の true のみ」（文字列 "false" 等で迂回させない）
        # どちらも「公開しない」方向に倒れる。
        guarantee = draft.get("contains_guarantee_claim", True) is not False
        base_flags = ["断定的表現を含む"] if guarantee else []

        res = self.directory.resolve(draft.get("platform_ref"))
        if res.state != R.OK:
            return Decision(BLOCKED, f"{res.state}: {res.note}", draft,
                            account=res.account, resolution=res, flags=base_flags)

        account = res.account

        acct_flags = base_flags + (
            ["要確認アカウント"]
            if (account.get("risk_review_required") or "").strip().lower() != "no" else [])

        if draft["draft_id"] in self.state:
            return Decision(SKIP_DUPLICATE, "前回実行で公開済み", draft, account,
                            flags=acct_flags)

        prev = self.publish_log.get(draft["draft_id"])
        if prev:
            if prev.get("status") == "published":
                return Decision(SKIP_DUPLICATE,
                                f"公開履歴あり（{prev.get('publish_id')}）", draft, account,
                                flags=acct_flags)
            # failed のみ再投稿対象。ここを一律スキップにすると公開漏れを作り込む。
            # 逆に「published でなければ再投稿」とすると、状態不明のものまで再送してしまう。
            prev_status = (prev.get("status") or "").strip().lower()
            if prev_status != "failed":
                return Decision(UNKNOWN_HISTORY,
                                f"公開履歴の状態が不明（{prev_status or '空欄'}）", draft, account,
                                flags=acct_flags)
            flags_retry = [f"前回失敗（{prev.get('publish_id')}）のため再投稿対象"]
        else:
            flags_retry = []

        flags = base_flags + flags_retry
        risky_account = (account.get("risk_review_required") or "").strip().lower() != "no"
        if risky_account:
            flags.append("要確認アカウント")

        platform = account.get("platform")
        adapter = self.adapters.get(platform)
        if adapter is None:
            # 台帳に未知のプラットフォームが現れた場合。落とさず、処理対象から外す。
            return Decision(BLOCKED, f"unsupported_platform: 未対応のプラットフォーム「{platform}」",
                            draft, account, resolution=res, flags=flags)

        # 投稿手段が無いプラットフォームは承認以前に自動公開できない。
        # ただしリスク判定の結果は人手作業者が参照できるよう flags に残す。
        if not adapter.can_publish:
            return Decision(MANUAL, "投稿APIが存在しないプラットフォーム宛", draft, account,
                            resolution=res, flags=flags)

        if self.approvals.get(draft["draft_id"]) is True:  # 明示的な true のみ
            return Decision(PUBLISH, "承認済み", draft, account, resolution=res, flags=flags)

        if guarantee and not config.AUTO_PUBLISH_P1:
            return Decision(APPROVAL_P1, "内容起因のリスク（法令抵触の可能性）", draft, account,
                            resolution=res, flags=flags)
        if risky_account and not config.AUTO_PUBLISH_P2:
            return Decision(APPROVAL_P2, "アカウント起因のリスク", draft, account,
                            resolution=res, flags=flags)

        return Decision(PUBLISH, "リスク信号なし", draft, account, resolution=res, flags=flags)
