#!/usr/bin/env python3
"""Tessera モックAPI（ポート 8701）

投稿可能なサービス。トークン認証・カーソル型ページング・レート制限を持つ。
Python 標準ライブラリのみで動作する。
"""

import collections
import json
import os
import random
import secrets
import sys
import threading
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_NAME = "tessera"
DEFAULT_PORT = 8701

# 認証トークン
API_TOKEN = "tsr_key_R7vK2mQ9pLx4NbW8"

# レート制限（スライディングウィンドウ）
RATE_LIMIT = 12
RATE_WINDOW_SEC = 90
RETRY_AFTER_SEC = 15

# ページングの上限
MAX_PAGE_SIZE = 15

# 起動時に必要なデータファイル
REQUIRED_FILES = ("queue.json",)

JST = datetime.timezone(datetime.timedelta(hours=9))


def data_dir():
    """データディレクトリのパスを返す（MOCK_DATA_DIR があればそちらを使う）"""
    override = os.environ.get("MOCK_DATA_DIR")
    if override:
        return os.path.abspath(override)
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "data"))


def missing_data_files():
    """存在しない必須データファイルの絶対パス一覧を返す"""
    base = data_dir()
    return [os.path.join(base, name)
            for name in REQUIRED_FILES
            if not os.path.isfile(os.path.join(base, name))]


def ensure_data_files():
    """必須データファイルが無ければ理由を表示して終了する"""
    missing = missing_data_files()
    if missing:
        sys.stderr.write(
            "[%s] 起動できません。次のデータファイルが見つかりません:\n" % SERVICE_NAME)
        for path in missing:
            sys.stderr.write("  - %s\n" % path)
        sys.stderr.write(
            "  データディレクトリ: %s\n"
            "  別の場所を使う場合は環境変数 MOCK_DATA_DIR で指定してください。\n"
            % data_dir())
        sys.exit(1)


def load_drafts():
    """queue.json を読み、drafts の配列を返す"""
    path = os.path.join(data_dir(), "queue.json")
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    drafts = payload.get("drafts")
    if not isinstance(drafts, list):
        raise ValueError("queue.json の drafts が配列ではありません: %s" % path)
    return drafts


class State:
    """サーバー全体で共有する可変状態"""

    def __init__(self, drafts):
        self.lock = threading.Lock()
        self.drafts = drafts
        self.posted = []                       # 投稿記録（冪等キーの仕組みは無い）
        self.window = collections.deque()      # レート制限のスライディングウィンドウ
        self.fault = {"halt": False, "fail_ratio": 0.0}

    def rate_allow(self, now):
        """レート枠を1つ消費できれば True。超過していれば False"""
        with self.lock:
            limit = now - RATE_WINDOW_SEC
            while self.window and self.window[0] <= limit:
                self.window.popleft()
            if len(self.window) >= RATE_LIMIT:
                return False
            self.window.append(now)
            return True

    def reset(self):
        """投稿記録とレート窓をクリアする（冪等キャッシュはこのサービスには無い）"""
        with self.lock:
            self.posted.clear()
            self.window.clear()

    def snapshot_fault(self):
        with self.lock:
            return dict(self.fault)

    def update_fault(self, changes):
        """障害注入の値を更新し、更新後の値を返す"""
        with self.lock:
            if "halt" in changes:
                self.fault["halt"] = bool(changes["halt"])
            if "fail_ratio" in changes:
                try:
                    self.fault["fail_ratio"] = float(changes["fail_ratio"])
                except (TypeError, ValueError):
                    pass
            return dict(self.fault)


class Handler(BaseHTTPRequestHandler):
    """Tessera のリクエストハンドラ"""

    server_version = "TesseraMock/1.0"
    state = None  # サーバー生成時に差し込む

    # ---- ログ --------------------------------------------------------
    def log_message(self, fmt, *args):
        """アクセスログを抑制する。MOCK_VERBOSE=1 のときだけ1行だけ出す"""
        if os.environ.get("MOCK_VERBOSE") != "1":
            return
        status = args[1] if len(args) >= 2 else "-"
        sys.stderr.write("[%s] %s %s %s\n" % (
            SERVICE_NAME,
            getattr(self, "command", "-"),
            getattr(self, "path", "-"),
            status,
        ))

    # ---- 応答の組み立て ----------------------------------------------
    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        """リクエスト本文を生バイト列で読む"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def read_json_body(self):
        """本文をJSONとして読む。空なら空dict。壊れていれば None"""
        raw = self.read_body()
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # ---- 経路 --------------------------------------------------------
    def split_path(self):
        """パスとクエリを分解して返す"""
        raw = self.path or "/"
        if "?" in raw:
            path, query = raw.split("?", 1)
        else:
            path, query = raw, ""
        params = {}
        for chunk in query.split("&"):
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            params[key] = value
        return path, params

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        path, params = self.split_path()
        state = self.state

        # /status は常に応答する
        if path == "/status" and method == "GET":
            if state.snapshot_fault()["halt"]:
                self.send_json(503, {"ok": False, "service": SERVICE_NAME,
                                     "state": "halted"})
            else:
                self.send_json(200, {"ok": True, "service": SERVICE_NAME})
            return

        # 管理用エンドポイントは認証もレート制限も停止注入も受けない
        if path == "/_admin/fault" and method == "POST":
            changes = self.read_json_body()
            if changes is None:
                self.send_json(400, {"error": "bad_request"})
                return
            self.send_json(200, {"ok": True, "fault": state.update_fault(changes)})
            return
        if path == "/_admin/reset" and method == "POST":
            self.read_body()
            state.reset()
            self.send_json(200, {"ok": True})
            return

        # 停止注入中は残り全部を 503 にする
        if state.snapshot_fault()["halt"]:
            self.read_body()
            self.send_json(503, {"error": "service_halted"})
            return

        # トークン認証
        if self.headers.get("X-Api-Token") != API_TOKEN:
            self.read_body()
            self.send_json(401, {"error": "invalid_token"})
            return

        # レート制限（/api/queue と /api/publish で同じ枠を共有する）
        if path in ("/api/queue", "/api/publish"):
            if not self.state.rate_allow(_now()):
                self.read_body()
                self.send_json(429,
                               {"error": "rate_limited",
                                "retry_after": RETRY_AFTER_SEC},
                               {"Retry-After": RETRY_AFTER_SEC})
                return
            # 障害注入はレート判定の後、処理の前に行う
            if random.random() < state.snapshot_fault()["fail_ratio"]:
                self.read_body()
                self.send_json(500, {"error": "internal"})
                return

        if path == "/api/queue" and method == "GET":
            self.handle_queue(params)
            return
        if path == "/api/publish" and method == "POST":
            self.handle_publish()
            return
        if path == "/api/posted" and method == "GET":
            with state.lock:
                posted = list(state.posted)
            self.send_json(200, {"posted": posted})
            return

        self.read_body()
        self.send_json(404, {"error": "not_found"})

    # ---- 個別処理 ----------------------------------------------------
    def handle_queue(self, params):
        """全件をカーソル型ページングで返す（宛先での絞り込みはしない）"""
        after = _to_int(params.get("after"), 0)
        if after < 0:
            after = 0
        size = _to_int(params.get("size"), MAX_PAGE_SIZE)
        if size > MAX_PAGE_SIZE or size <= 0:
            size = MAX_PAGE_SIZE

        entries = [d for d in self.state.drafts if str(d.get("platform_ref") or "").strip()]
        total = len(entries)
        page = entries[after:after + size]
        nxt = after + size if after + size < total else None
        self.send_json(200, {"entries": page, "next": nxt, "total_hint": None})

    def handle_publish(self):
        """投稿を記録する。冪等キーの仕組みは無いので同じ下書きでも毎回記録される"""
        body = self.read_json_body()
        if body is None:
            self.send_json(400, {"error": "bad_request"})
            return
        post_id = "TS-" + secrets.token_hex(4)
        posted_at = datetime.datetime.now(JST).isoformat(timespec="seconds")
        record = {
            "post_id": post_id,
            "draft_id": body.get("draft_id"),
            "text": body.get("text"),
            "posted_at": posted_at,
        }
        with self.state.lock:
            self.state.posted.append(record)
        self.send_json(200, {"ok": True, "post_id": post_id, "posted_at": posted_at})


def _now():
    import time
    return time.monotonic()


def _to_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def build_server(port=DEFAULT_PORT):
    """HTTPサーバーを組み立てて返す（起動はしない）"""
    ensure_data_files()
    state = State(load_drafts())
    handler = type("TesseraHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(port=DEFAULT_PORT):
    """単独でサーバーを起動する"""
    httpd = build_server(port)
    sys.stderr.write("[%s] http://127.0.0.1:%d で待機中\n" % (SERVICE_NAME, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    serve(_to_int(os.environ.get("PORT"), DEFAULT_PORT))
