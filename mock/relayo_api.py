#!/usr/bin/env python3
"""Relayo モックAPI（ポート 8703）

投稿可能なサービス。リクエスト本文への署名で認証し、冪等キーに対応する。
3サービスの中でレート制限が最も厳しく、一定確率で一時障害を返す。
Python 標準ライブラリのみで動作する。
"""

import collections
import hmac
import hashlib
import json
import os
import random
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_NAME = "relayo"
DEFAULT_PORT = 8703

# 署名に使うシークレット
SIGNING_SECRET = b"rly_sig_Zq83NvT1Mx6Kp2Ry"

# レート制限（スライディングウィンドウ）
RATE_LIMIT = 4
RATE_WINDOW_SEC = 75
RETRY_AFTER_SEC = 15

# 既定の一時障害率。障害注入をしていなくてもこの確率で 503 になる
DEFAULT_FAIL_RATIO = 0.15

# このサービスは起動時にデータファイルを必要としない
REQUIRED_FILES = ()


def data_dir():
    """データディレクトリのパスを返す（MOCK_DATA_DIR があればそちらを使う）"""
    override = os.environ.get("MOCK_DATA_DIR")
    if override:
        return os.path.abspath(override)
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "data"))


def missing_data_files():
    """存在しない必須データファイルの絶対パス一覧を返す（このサービスは常に空）"""
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
        sys.stderr.write("  データディレクトリ: %s\n" % data_dir())
        sys.exit(1)


def sign(raw_body):
    """受信した生のリクエスト本文バイト列に対する署名を計算する"""
    return hmac.new(SIGNING_SECRET, raw_body, hashlib.sha256).hexdigest()


class State:
    """サーバー全体で共有する可変状態"""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent = []                         # 送信済み一覧
        self.idempotency = {}                  # 冪等トークン -> 応答内容
        self.window = collections.deque()      # レート制限のスライディングウィンドウ
        self.fault = {"halt": False, "fail_ratio": DEFAULT_FAIL_RATIO}

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

    def cached(self, token):
        if not token:
            return None
        with self.lock:
            payload = self.idempotency.get(token)
            return dict(payload) if payload else None

    def remember(self, token, payload):
        if not token:
            return
        with self.lock:
            self.idempotency[token] = dict(payload)

    def reset(self):
        """送信済みログ・冪等キャッシュ・レート窓をクリアする"""
        with self.lock:
            self.sent.clear()
            self.idempotency.clear()
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
    """Relayo のリクエストハンドラ"""

    server_version = "RelayoMock/1.0"
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
        """リクエスト本文を生バイト列で読む。署名はこのバイト列に対して計算する"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    # ---- 経路 --------------------------------------------------------
    def split_path(self):
        raw = self.path or "/"
        path = raw.split("?", 1)[0]
        return path

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        path = self.split_path()
        state = self.state

        if path == "/status" and method == "GET":
            if state.snapshot_fault()["halt"]:
                self.send_json(503, {"ok": False, "service": SERVICE_NAME,
                                     "state": "halted"})
            else:
                self.send_json(200, {"ok": True, "service": SERVICE_NAME})
            return

        # 管理用エンドポイントは署名もレート制限も停止注入も受けない
        if path == "/_admin/fault" and method == "POST":
            raw = self.read_body()
            changes = _parse_json_object(raw)
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

        if path == "/v2/messages" and method == "POST":
            self.handle_messages()
            return
        if path == "/v2/sent" and method == "GET":
            with state.lock:
                sent = [dict(item) for item in state.sent]
            self.send_json(200, {"sent": sent})
            return

        self.read_body()
        self.send_json(404, {"error": "not_found"})

    # ---- 個別処理 ----------------------------------------------------
    def handle_messages(self):
        state = self.state
        raw = self.read_body()

        # 1. 署名検証（受信したバイト列そのものに対して計算する。再シリアライズしない）
        given = self.headers.get("X-Body-Signature") or ""
        if not hmac.compare_digest(given, sign(raw)):
            self.send_json(401, {"error": "bad_signature"})
            return

        # 2. 冪等キャッシュの確認（レート枠を消費しない）
        token = self.headers.get("Idempotency-Token")
        cached = state.cached(token)
        if cached is not None:
            cached["idempotent_replay"] = True
            self.send_json(200, cached)
            return

        # 3. レート制限
        if not state.rate_allow(_now()):
            self.send_json(429,
                           {"error": "rate_limited", "retry_after": RETRY_AFTER_SEC},
                           {"Retry-After": RETRY_AFTER_SEC})
            return

        # 4. 一時障害（レート判定の後、処理の前）
        if random.random() < state.snapshot_fault()["fail_ratio"]:
            self.send_json(503, {"error": "temporary_outage"})
            return

        # 5. 本処理
        body = _parse_json_object(raw)
        if body is None:
            self.send_json(400, {"error": "bad_request"})
            return
        account = body.get("account")
        if not account:
            self.send_json(400, {"error": "account_required"})
            return

        post_id = "RL-" + secrets.token_hex(5)
        text = body.get("text")
        with state.lock:
            state.sent.append({"account": account, "text": text, "post_id": post_id})
        payload = {"ok": True, "post_id": post_id, "account": account,
                   "idempotent_replay": False}
        state.remember(token, payload)
        self.send_json(200, payload)


def _parse_json_object(raw):
    """バイト列をJSONオブジェクトとして読む。空なら空dict。壊れていれば None"""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


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
    state = State()
    handler = type("RelayoHandler", (Handler,), {"state": state})
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
