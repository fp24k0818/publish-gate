#!/usr/bin/env python3
"""3つのモックAPIをまとめて起動する

同一プロセス内のスレッドで Tessera / Ledgerly / Relayo を起動し、
一覧を表示して待機する。Ctrl-C ですべて停止する。
"""

import os
import sys
import threading
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ledgerly_api  # noqa: E402
import relayo_api  # noqa: E402
import tessera_api  # noqa: E402

# 表示順とポート
SERVICES = (
    (tessera_api, tessera_api.DEFAULT_PORT,
     "12回 / 90秒", "ヘッダ X-Api-Token", "投稿可"),
    (ledgerly_api, ledgerly_api.DEFAULT_PORT,
     "なし", "クエリ ?key=", "読み取り専用"),
    (relayo_api, relayo_api.DEFAULT_PORT,
     "4回 / 75秒", "ヘッダ X-Body-Signature（本文のHMAC）", "投稿可"),
)

HEADERS = ("サービス", "URL", "レート制限", "認証方式", "投稿")


def display_width(text):
    """全角文字を2文字分として文字列の表示幅を数える"""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text, width):
    return text + " " * max(0, width - display_width(text))


def print_table(rows):
    """整形した表を標準出力に書く"""
    widths = [max(display_width(row[i]) for row in rows) for i in range(len(rows[0]))]
    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(border)
    for index, row in enumerate(rows):
        print("| " + " | ".join(pad(cell, widths[i]) for i, cell in enumerate(row)) + " |")
        if index == 0:
            print(border)
    print(border)


def check_data():
    """3サービス分の必須データファイルをまとめて確認する"""
    missing = []
    for module, _, _, _, _ in SERVICES:
        missing.extend(module.missing_data_files())
    if missing:
        sys.stderr.write("起動できません。次のデータファイルが見つかりません:\n")
        for path in sorted(set(missing)):
            sys.stderr.write("  - %s\n" % path)
        sys.stderr.write(
            "  別の場所を使う場合は環境変数 MOCK_DATA_DIR で指定してください。\n")
        sys.exit(1)


def main():
    check_data()

    servers = []
    for module, port, _, _, _ in SERVICES:
        servers.append((module, module.build_server(port)))

    threads = []
    for module, httpd in servers:
        thread = threading.Thread(target=httpd.serve_forever,
                                  name=module.SERVICE_NAME, daemon=True)
        thread.start()
        threads.append(thread)

    rows = [HEADERS]
    for module, port, rate, auth, publish in SERVICES:
        rows.append((module.SERVICE_NAME, "http://127.0.0.1:%d" % port,
                     rate, auth, publish))
    print_table(rows)
    print("データディレクトリ: %s" % tessera_api.data_dir())
    print("Ctrl-C で全サービスを停止します。")
    sys.stdout.flush()

    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(0.5)
    except KeyboardInterrupt:
        print("\n停止します。")
    finally:
        for _, httpd in servers:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
