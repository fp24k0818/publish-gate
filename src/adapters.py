"""各システムへのアクセスを抽象化する層。

認証方式・レート制限・リトライはここに閉じ込め、上位層に漏らさない。
Ledgerly は投稿不可を「戻り値」で返す。例外にしない（仕様であり異常系ではない）。
"""
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque

import config


class RateLimiter:
    """スライディングウィンドウ。制限を回避せず、必要なだけ待つ。"""

    def __init__(self, max_calls, window, label=""):
        self.max_calls, self.window, self.label = max_calls, window, label
        self._calls = deque()
        self._lock = threading.Lock()
        self.total_waited = 0.0

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self.window:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.window - (now - self._calls[0]) + 0.05
            self.total_waited += sleep_for
            # 無言で止まるとデモ中に停止と区別がつかないため必ず表示する
            print(f"      [待機] {self.label} レート制限のため {sleep_for:.0f}秒待機します",
                  flush=True)
            time.sleep(sleep_for)


class PublishResult:
    """投稿の結果。投稿不可(supported=False)も正常な結果として表現する。"""

    def __init__(self, ok, supported=True, post_id=None, reason=None, attempts=0):
        self.ok, self.supported = ok, supported
        self.post_id, self.reason, self.attempts = post_id, reason, attempts

    def to_dict(self):
        return {"ok": self.ok, "supported": self.supported,
                "post_id": self.post_id, "reason": self.reason, "attempts": self.attempts}


def _request(url, data=None, headers=None, timeout=config.DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _retry_after(headers, default=20):
    """サーバが提示した待ち時間を尊重する。自前の推測で上書きしない。"""
    try:
        return max(1, int(headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


class TesseraAdapter:
    """トークン認証 / 12req 90s / カーソルページング。冪等キーの仕組みは無い。"""

    name = "tessera"
    can_publish = True

    def __init__(self):
        self.limiter = RateLimiter(*config.RATE_LIMITS["tessera"], label="Tessera")
        self._headers = {"X-Api-Token": config.TESSERA_TOKEN}

    def fetch_all_drafts(self):
        """全ページを取得する。

        リトライとページングが同じループにあるため、上限とカーソルの前進確認を必ず置く。
        置かないと、429が続く場合やサーバが同じ next_cursor を返し続ける場合に終わらない。
        """
        items, cursor, pages, calls = [], 0, 0, 0
        seen_cursors = {0}
        while True:
            calls += 1
            if calls > config.MAX_FETCH_CALLS:
                raise RuntimeError(
                    f"下書き取得が {config.MAX_FETCH_CALLS} 回を超えました。"
                    f"取得済み {len(items)}件で中止します")
            self.limiter.acquire()
            status, body, hdrs = _request(
                f"{config.TESSERA_BASE}/api/queue?after={cursor}&size={config.TESSERA_PAGE_SIZE}",
                headers=self._headers)
            if status in (429, 500, 502, 503):
                wait = _retry_after(hdrs) if status == 429 else 2
                print(f"      [待機] Tessera {status}。{wait}秒後に再試行します", flush=True)
                time.sleep(wait)
                continue
            if status != 200:
                raise RuntimeError(f"tessera queue failed: {status} {body[:120]}")
            payload = json.loads(body)
            items.extend(payload["entries"])
            pages += 1
            nxt = payload.get("next")
            if nxt is None:
                return items, pages
            if not isinstance(nxt, int) or nxt in seen_cursors:
                raise RuntimeError(f"next が前進しません（{nxt!r}）。取得を中止します")
            seen_cursors.add(nxt)
            cursor = nxt

    def publish(self, draft, account):
        payload = json.dumps({"draft_id": draft["draft_id"], "text": draft["body_preview"]},
                             ensure_ascii=False).encode()
        for attempt in range(1, config.MAX_RETRIES + 1):
            self.limiter.acquire()
            status, body, hdrs = _request(f"{config.TESSERA_BASE}/api/publish", data=payload,
                                          headers={**self._headers,
                                                   "Content-Type": "application/json"})
            if status == 200:
                return PublishResult(True, post_id=json.loads(body).get("post_id"),
                                     attempts=attempt)
            if status in (429, 500, 502, 503):
                if attempt == config.MAX_RETRIES:
                    break  # これ以上送らないので待たない
                wait = _retry_after(hdrs, min(2 ** attempt, 20)) if status == 429 \
                    else min(2 ** attempt, 20)
                print(f"      [待機] Tessera {status}。{wait}秒後に再試行します", flush=True)
                time.sleep(wait)
                continue
            return PublishResult(False, reason=f"http_{status}", attempts=attempt)
        return PublishResult(False, reason="retries_exhausted", attempts=config.MAX_RETRIES)


class LedgerlyAdapter:
    """読み取り専用。投稿エンドポイントは存在しない（探索しない）。

    応答が0.4〜1.2秒と遅く、停止すると台帳が引けず全件が処理不能になる。
    そのため取得成功時にローカルへキャッシュし、停止時は鮮度を明示して継続する。
    """

    name = "ledgerly"
    can_publish = False

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir or config.OUTPUT_DIR
        self.degraded = []

    def _cache_path(self, key):
        return os.path.join(self.cache_dir, f"cache_{key}.csv")

    def _get_csv(self, path, key):
        try:
            status, body, _ = _request(
                f"{config.LEDGERLY_BASE}{path}?key={config.LEDGERLY_KEY}",
                timeout=config.LEDGERLY_TIMEOUT)
            if status == 200:
                text = body.decode("utf-8-sig")
                if text.strip():
                    # 空・部分応答で正常なキャッシュを壊さないよう、内容を確認してから置換する
                    os.makedirs(self.cache_dir, exist_ok=True)
                    tmp = self._cache_path(key) + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(text)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self._cache_path(key))
                    return text
                err = "空の応答"
            else:
                err = f"HTTP {status}"
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            err = str(e)

        cache = self._cache_path(key)
        if os.path.exists(cache):
            age = int(time.time() - os.path.getmtime(cache))
            self.degraded.append(f"{key}: Ledgerly応答なし({err})。{age}秒前のキャッシュで継続")
            with open(cache, encoding="utf-8") as f:
                return f.read()
        raise RuntimeError(f"Ledgerly {path} 取得失敗({err})かつキャッシュ無し。処理を中止します")

    def fetch_accounts_csv(self):
        return self._get_csv("/export/accounts.csv", "accounts")

    def fetch_publish_log_csv(self):
        return self._get_csv("/export/history.csv", "publish_log")

    def publish(self, draft, account):
        # 投稿APIが存在しない。代替手段(ブラウザ自動操作等)は方針として採らない。
        return PublishResult(False, supported=False, reason="no_publish_endpoint")


class RelayoAdapter:
    """HMAC署名 / 4req 75s / 既定15%の503。冪等キーでリトライを安全にする。"""

    name = "relayo"
    can_publish = True

    def __init__(self):
        self.limiter = RateLimiter(*config.RATE_LIMITS["relayo"], label="Relayo")

    def publish(self, draft, account):
        handle = (account or {}).get("handle") or ""
        if not handle:
            return PublishResult(False, reason="handle_missing")
        raw = json.dumps({"account": handle, "text": draft["body_preview"]},
                         ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            # 署名対象は「送信する生バイト列そのもの」
            "X-Body-Signature": hmac.new(config.RELAYO_SECRET, raw, hashlib.sha256).hexdigest(),
            # 冪等キーは重複対策であると同時にレート制限対策でもある
            "Idempotency-Token": draft["draft_id"],
        }
        for attempt in range(1, config.MAX_RETRIES + 1):
            self.limiter.acquire()
            status, body, hdrs = _request(f"{config.RELAYO_BASE}/v2/messages",
                                          data=raw, headers=headers)
            if status == 200:
                data = json.loads(body)
                return PublishResult(True, post_id=data.get("post_id"), attempts=attempt,
                                     reason="idempotent_replay" if data.get("idempotent_replay") else None)
            if status in (429, 500, 502, 503):
                if attempt == config.MAX_RETRIES:
                    break  # これ以上送らないので待たない
                wait = _retry_after(hdrs, min(2 ** attempt, 20)) if status == 429 \
                    else min(2 ** attempt, 20)
                print(f"      [待機] Relayo {status}。{wait}秒後に再試行します", flush=True)
                time.sleep(wait)
                continue
            return PublishResult(False, reason=f"http_{status}", attempts=attempt)
        return PublishResult(False, reason="retries_exhausted", attempts=config.MAX_RETRIES)


def build_adapters(cache_dir=None):
    """cache_dir は実行時の出力先。既定値に固定すると、出力先を変えたときに
    キャッシュだけ別の場所に残り、縮退時の挙動が実行ごとに変わってしまう。"""
    return {a.name: a for a in (TesseraAdapter(),
                                LedgerlyAdapter(cache_dir),
                                RelayoAdapter())}
