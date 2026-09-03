"""下書きの取得と正規化。

取得元は Tessera のみ。Relayo には取得系のエンドポイントが無く（GET は /status と /v2/sent のみ）、
Tessera の /api/queue は宛先で絞り込まず全件を返す。
したがって「取得元」と「投稿先」は別物として扱う。
"""
from normalize import UnparseableDatetime, now_jst, parse_scheduled_at


class CollectResult:
    def __init__(self, delayed, not_due, already_published, unparseable, fetched, pages,
                 duplicate_ids=0):
        self.delayed = delayed
        self.not_due = not_due
        self.already_published = already_published
        self.unparseable = unparseable
        self.fetched = fetched
        self.pages = pages
        self.duplicate_ids = duplicate_ids


def collect(tessera, now=None):
    now = now or now_jst()
    raw, pages = tessera.fetch_all_drafts()

    delayed, not_due, published, unparseable = [], [], [], []
    # 同一 draft_id が複数行・複数ページに現れた場合、そのまま流すと同じ下書きを
    # 2回投稿することになる。Tessera には冪等キーが無いため取得時点で落とす。
    seen_ids, duplicate_ids = set(), 0
    for d in raw:
        did = d.get("draft_id")
        if did in seen_ids:
            duplicate_ids += 1
            continue
        seen_ids.add(did)
        if d.get("status") == "published":
            published.append(d)
            continue
        try:
            when = parse_scheduled_at(d.get("scheduled_at"))
        except UnparseableDatetime:
            # 時刻を判定できないものは遅延扱いにせず、必ず人へ回す
            unparseable.append(d)
            continue
        d["_scheduled_at"] = when.isoformat()
        (delayed if when < now else not_due).append(d)

    delayed.sort(key=lambda x: x["_scheduled_at"])
    return CollectResult(delayed, not_due, published, unparseable, len(raw), pages,
                         duplicate_ids)
