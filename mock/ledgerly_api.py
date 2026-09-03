#!/usr/bin/env python3
"""Ledgerly モックAPI（ポート 8702）

読み取り専用のサービス。台帳と公開履歴をCSVで返す。投稿エンドポイントは無い。
応答は毎回ランダムに遅れる。Python 標準ライブラリのみで動作する。
"""

import json
import os
import random
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_NAME = "ledgerly"
DEFAULT_PORT = 8702

# クエリパラメータで渡す鍵
ACCESS_KEY = "ldg-4f9a1c7e2b"

# 応答遅延（秒）。delay_scale 倍して待つ
DELAY_MIN_SEC = 0.4
DELAY_MAX_SEC = 1.2

# 起動時に必要なデータファイル
REQUIRED_FILES = ("accounts.csv", "history.csv")

# 遅延も停止注入も受けない管理系のパス
EXEMPT_PATHS = ("/status", "/_admin/fault", "/_admin/reset")


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


class State:
    """サーバー全体で共有する可変状態"""

    def __init__(self):
        self.lock = threading.Lock()
        self.fault = {"halt": False, "fail_ratio": 0.0, "delay_scale": 1.0}

    def snapshot_fault(self):
        with self.lock:
            return dict(self.fault)

    def update_fault(self, changes):
        """障害注入の値を更新し、更新後の値を返す"""
        with self.lock:
            if "halt" in changes:
                self.fault["halt"] = bool(changes["halt"])
            for key in ("fail_ratio", "delay_scale"):
                if key in changes:
                    try:
                        self.fault[key] = float(changes[key])
                    except (TypeError, ValueError):
                        pass
            return dict(self.fault)

    def reset(self):
        """このサービスは送信ログ・冪等キャッシュ・レート窓を持たないので何もしない"""
        return


class Handler(BaseHTTPRequestHandler):
    """Ledgerly のリクエストハンドラ"""

    server_version = "LedgerlyMock/1.0"
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
    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def read_json_body(self):
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

        if path == "/status" and method == "GET":
            if state.snapshot_fault()["halt"]:
                self.send_json(503, {"ok": False, "service": SERVICE_NAME,
                                     "state": "halted"})
            else:
                self.send_json(200, {"ok": True, "service": SERVICE_NAME})
            return

        # 管理用エンドポイントは遅延も停止注入も受けない
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

        fault = state.snapshot_fault()

        # 停止注入中は /status 以外すべて 503
        if fault["halt"]:
            self.read_body()
            self.send_json(503, {"error": "service_halted"})
            return

        # このサービスには投稿エンドポイントが無い。POST はパスを問わず 404
        if method == "POST":
            self.read_body()
            self.send_json(404, {"error": "not_found",
                                 "note": "this service has no publish endpoint"})
            return

        # 管理系以外は応答が遅い
        self.sleep_for_response(fault)

        # 鍵の確認
        if params.get("key") != ACCESS_KEY:
            self.read_body()
            self.send_json(403, {"error": "forbidden"})
            return

        # 障害注入
        if random.random() < fault["fail_ratio"]:
            self.read_body()
            self.send_json(500, {"error": "internal"})
            return

        if method == "GET" and path == "/export/accounts.csv":
            self.send_export("accounts.csv")
            return
        if method == "GET" and path == "/export/history.csv":
            self.send_export("history.csv")
            return

        self.read_body()
        self.send_json(404, {"error": "not_found"})

    # ---- 個別処理 ----------------------------------------------------
    def sleep_for_response(self, fault):
        """0.4〜1.2秒のランダム × delay_scale だけ待つ"""
        scale = fault.get("delay_scale", 1.0)
        wait = random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC) * scale
        if wait > 0:
            time.sleep(wait)

    def send_export(self, filename):
        """data/ のCSVをそのまま返す"""
        path = os.path.join(data_dir(), filename)
        try:
            with open(path, "rb") as handle:
                self.send_csv(handle.read())
        except OSError:
            self.send_json(500, {"error": "internal",
                                 "note": "データファイルを読めません: %s" % filename})


def _to_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def build_server(port=DEFAULT_PORT):
    """HTTPサーバーを組み立てて返す（起動はしない）"""
    ensure_data_files()
    state = State()
    handler = type("LedgerlyHandler", (Handler,), {"state": state})
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
