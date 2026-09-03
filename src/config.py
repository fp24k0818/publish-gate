"""設定と接続情報。認証情報はソースに直書きせず環境変数から読む（既定値はローカルモック用のダミー）。"""
import os

TESSERA_BASE = os.getenv("TESSERA_BASE", "http://localhost:8701")
LEDGERLY_BASE = os.getenv("LEDGERLY_BASE", "http://localhost:8702")
RELAYO_BASE = os.getenv("RELAYO_BASE", "http://localhost:8703")

TESSERA_TOKEN = os.getenv("TESSERA_TOKEN", "tsr_key_R7vK2mQ9pLx4NbW8")
LEDGERLY_KEY = os.getenv("LEDGERLY_KEY", "ldg-4f9a1c7e2b")
RELAYO_SECRET = os.getenv("RELAYO_SECRET", "rly_sig_Zq83NvT1Mx6Kp2Ry").encode()

# レート制限（サーバ側の実値より1件少なく設定し、境界での429を避ける）
# サーバ側: Tessera 12req/90s, Relayo 4req/75s
RATE_LIMITS = {"tessera": (11, 90.0), "relayo": (3, 75.0)}

TESSERA_PAGE_SIZE = 15   # 下書き取得の1ページ件数（サーバ側の上限も15）

LEDGERLY_TIMEOUT = 8.0   # 応答0.4〜1.2秒。障害注入で最大3倍まで遅くなる
DEFAULT_TIMEOUT = 10.0
MAX_RETRIES = 3
MAX_FETCH_CALLS = 30   # 下書き取得の総呼び出し上限（リトライ含む）。無限ループの防止

# 承認境界。既定は安全側。運用判断でP2を自動公開に含めることは可能にしてある。
AUTO_PUBLISH_P1 = False   # contains_guarantee_claim（内容起因のリスク）
AUTO_PUBLISH_P2 = False   # risk_review_required（アカウント起因のリスク）

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")

# 検証用。任意時点の判定を再現するための基準時刻（未設定なら現在時刻）
SIMULATED_NOW = os.getenv("SIMULATED_NOW")
