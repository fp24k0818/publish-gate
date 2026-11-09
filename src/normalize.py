"""正規化。時刻フォーマットの差異と表記ゆれをここで吸収し、下流に漏らさない。"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

_SLASH = re.compile(r"^(\d{4})/(\d{2})/(\d{2})[ T](\d{2}):(\d{2})$")
_ENG = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})[ T](\d{2}):(\d{2})$")


class UnparseableDatetime(ValueError):
    """既知の3形式のいずれにも該当しない値。握りつぶさず呼び出し側へ返す。"""


def parse_scheduled_at(value):
    """3フォーマット混在の scheduled_at を aware datetime に正規化する。

    タイムゾーン情報を持たない形式は JST とみなす（積み残し#9 参照）。
    """
    if not value or not str(value).strip():
        raise UnparseableDatetime("empty")
    s = str(value).strip()

    # 日付と時刻の区切りが "T" であることを必須とする。
    # 標準パーサに丸投げすると、日付のみ（2026-08-10）や任意の区切り文字まで
    # 受理してしまい、Pythonのバージョンによって受理範囲も変わる。
    if "T" in s:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=JST)
        except ValueError:
            pass

    # 形式に合致しても暦として不正な値（2026/02/30 等）がありうる。
    # 生の ValueError を上位へ漏らすとバッチ全体が停止するため、専用例外に変換する。
    m = _SLASH.match(s)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, h, mi, tzinfo=JST)
        except ValueError as e:
            raise UnparseableDatetime(f"{s}（暦として不正: {e}）") from None

    m = _ENG.match(s)
    if m:
        d, mon, y, h, mi = m.groups()
        key = mon[:1].upper() + mon[1:].lower()
        if key in _MONTHS:
            try:
                return datetime(int(y), _MONTHS[key], int(d), int(h), int(mi), tzinfo=JST)
            except ValueError as e:
                raise UnparseableDatetime(f"{s}（暦として不正: {e}）") from None

    raise UnparseableDatetime(s)


def normalize_display_name(value):
    """全角/半角・空白ゆれを吸収する。表示・照合の補助であり、識別子には使わない。"""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\s+", "", s)


def now_jst():
    return datetime.now(JST)
