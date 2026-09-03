"""アカウントの名寄せ。一意に決まらないものは決めない。

優先順位: account_id > handle > email
（一意性が保証される順。handle は再発行され得る。email は共有され得る）
display_name は同名の別アカウントが実在するため識別子に使わない。
"""
import csv
import io

from normalize import normalize_display_name

OK = "ok"
AMBIGUOUS = "ambiguous"
UNRESOLVED = "unresolved"
SUSPENDED = "suspended"


class Resolution:
    def __init__(self, state, account=None, candidates=None, note=None):
        self.state, self.account = state, account
        self.candidates = candidates or []
        self.note = note


class AccountDirectory:
    def __init__(self, csv_text):
        self.accounts = list(csv.DictReader(io.StringIO(csv_text)))
        for a in self.accounts:
            a["_display_norm"] = normalize_display_name(a.get("display_name"))

        self.by_id = {a["account_id"]: a for a in self.accounts if a.get("account_id")}
        self.by_handle = {}
        self.by_email = {}
        for a in self.accounts:
            if a.get("handle"):
                self.by_handle.setdefault(a["handle"], []).append(a)
            if a.get("email"):
                self.by_email.setdefault(a["email"], []).append(a)

    def successor_of(self, account):
        """凍結アカウントの後継候補（former_handle が一致し active なもの）。"""
        h = account.get("handle")
        if not h:
            return None
        for a in self.accounts:
            if a.get("former_handle") == h and \
                    (a.get("status") or "").strip().lower() == "active":
                return a
        return None

    def resolve(self, ref):
        if not ref:
            return Resolution(UNRESOLVED, note="platform_ref が空")

        hit = None
        if ref in self.by_id:
            hit = self.by_id[ref]
        elif ref in self.by_handle:
            cands = self.by_handle[ref]
            if len(cands) > 1:
                return Resolution(AMBIGUOUS, candidates=cands, note="同一handleが複数")
            hit = cands[0]
        elif ref in self.by_email:
            cands = self.by_email[ref]
            if len(cands) > 1:
                # 正当な複数媒体運用と凍結後の再発行が混在しており、
                # status/former_handle で絞ってもアクティブな候補が複数残る場合は決めない
                actives = [a for a in cands
                           if (a.get("status") or "").strip().lower() == "active"]
                if len(actives) != 1:
                    return Resolution(AMBIGUOUS, candidates=cands,
                                      note="同一emailに複数アカウント。台帳内に区別する情報が無い")
                hit = actives[0]
            else:
                hit = cands[0]

        if hit is None:
            return Resolution(UNRESOLVED, note="台帳に該当なし")

        # active 以外はすべて投稿対象外とする。未知の status を「active でないなら安全」
        # ではなく「suspended でないなら公開可」と解釈すると、値が増えたときに公開side へ倒れる。
        status = (hit.get("status") or "").strip().lower()
        if status != "active":
            succ = self.successor_of(hit)
            note = "凍結アカウント宛" if status == "suspended" else f"status が active でない（{status or '空欄'}）"
            if succ:
                note += f"。後継候補 {succ['account_id']} {succ.get('handle')}（自動転送はしない）"
            return Resolution(SUSPENDED, account=hit, note=note)

        return Resolution(OK, account=hit)
