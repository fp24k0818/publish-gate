# モックAPI（3サービス）

外部SNS側の動きを再現するモックサーバーです。Python 標準ライブラリだけで動きます。

## 3サービスの違い

表中のトークン・キーは、ローカルモック用のダミー値です。

| サービス | ポート | 認証 | レート制限 | 投稿 | 遅延・障害 |
|---|---|---|---|---|---|
| tessera | 8701 | ヘッダ `X-Api-Token: tsr_key_R7vK2mQ9pLx4NbW8` | 12回 / 90秒（`/api/queue` と `/api/publish` で共有） | できる（冪等キーなし） | 既定では無し。`fail_ratio` を上げると 500 |
| ledgerly | 8702 | クエリ `?key=ldg-4f9a1c7e2b` | なし | **できない**（POST は常に 404） | 毎回 0.4〜1.2秒の遅延 |
| relayo | 8703 | ヘッダ `X-Body-Signature`（本文のHMAC-SHA256） | 4回 / 75秒（`/v2/messages` のみ） | できる（冪等キー対応） | 既定で 15% の確率で 503 |

共通の決まり

- 応答は `application/json; charset=utf-8`。未知のパスは 404 `{"error": "not_found"}`
- レート超過は 429 ＋ ヘッダ `Retry-After: 15`、本文 `{"error": "rate_limited", "retry_after": 15}`
- `GET /status` は 200 `{"ok": true, "service": "..."}`。停止注入中は 503
- アクセスログは出しません。`MOCK_VERBOSE=1` を付けたときだけ1行（メソッド・パス・ステータス）を出します

### 主なエンドポイント

- tessera: `GET /api/queue?after=<開始位置>&size=<最大15>` / `POST /api/publish` / `GET /api/posted`
- ledgerly: `GET /export/accounts.csv?key=` / `GET /export/history.csv?key=`
- relayo: `POST /v2/messages` / `GET /v2/sent`

`/api/queue` は宛先で絞り込まず全件を返します（`platform_ref` が空のものだけ除外）。
応答の `next` は次の開始位置で、最終ページでは `null` です。

relayo の署名は、**受信した本文のバイト列そのもの**に対して計算します。
本文を組み立て直してから署名すると一致しません。

```bash
BODY='{"account":"@example","text":"本文"}'
SIG=$(printf '%s' "$BODY" | python3 -c \
  "import sys,hmac,hashlib; print(hmac.new(b'rly_sig_Zq83NvT1Mx6Kp2Ry', sys.stdin.buffer.read(), hashlib.sha256).hexdigest())")
curl -X POST -H "X-Body-Signature: $SIG" -H "Idempotency-Token: t-001" \
  --data-binary "$BODY" http://127.0.0.1:8703/v2/messages
```

同じ `Idempotency-Token` で送り直すと、同じ結果が `"idempotent_replay": true` 付きで返り、
レート枠も消費しません。

## 起動

データは `mock/` の1つ上の `data/`（`queue.json` / `accounts.csv` / `history.csv`）から読みます。
ファイルが無いときは、足りないパスを表示して起動しません。

```bash
# 3つまとめて（Ctrl-C で全停止）
python3 mock/run_all.py

# 1つだけ
python3 mock/tessera_api.py
python3 mock/ledgerly_api.py
python3 mock/relayo_api.py

# データの置き場所を変える / ログを出す
MOCK_DATA_DIR=/path/to/data MOCK_VERBOSE=1 python3 mock/run_all.py
```

## 障害注入

`POST /_admin/fault` に JSON を送ります（認証は不要）。空の `{}` を送ると現在値を返すだけです。
`POST /_admin/reset` で投稿記録・冪等キャッシュ・レート窓をクリアします。

```bash
# 現在値を見る
curl -X POST -d '{}' http://127.0.0.1:8701/_admin/fault

# 停止させる → 元に戻す
curl -X POST -d '{"halt":true}'  http://127.0.0.1:8702/_admin/fault
curl -X POST -d '{"halt":false}' http://127.0.0.1:8702/_admin/fault

# エラー率を上げる（tessera は 500、relayo は 503）
curl -X POST -d '{"fail_ratio":1.0}' http://127.0.0.1:8701/_admin/fault
curl -X POST -d '{"fail_ratio":0.5}' http://127.0.0.1:8703/_admin/fault

# ledgerly の遅延を伸ばす / 縮める
curl -X POST -d '{"delay_scale":3.0}' http://127.0.0.1:8702/_admin/fault
curl -X POST -d '{"delay_scale":0.1}' http://127.0.0.1:8702/_admin/fault

# 記録をクリアする
curl -X POST http://127.0.0.1:8703/_admin/reset
```

受け付けるキー

| サービス | キー | 既定値 |
|---|---|---|
| tessera | `halt` / `fail_ratio` | `false` / `0.0` |
| ledgerly | `halt` / `fail_ratio` / `delay_scale` | `false` / `0.0` / `1.0` |
| relayo | `halt` / `fail_ratio` | `false` / `0.15` |

### 注意

障害注入は縮退動作の確認のためだけに使い、**確認が終わったら必ず既定値に戻してください**
（`halt: false` / `fail_ratio` は各サービスの既定値 / `delay_scale: 1.0`）。
戻し忘れると、次のテストが理由の分からない失敗をします。

```bash
curl -X POST -d '{"halt":false,"fail_ratio":0}'                    http://127.0.0.1:8701/_admin/fault
curl -X POST -d '{"halt":false,"fail_ratio":0,"delay_scale":1.0}'  http://127.0.0.1:8702/_admin/fault
curl -X POST -d '{"halt":false,"fail_ratio":0.15}'                 http://127.0.0.1:8703/_admin/fault
```
