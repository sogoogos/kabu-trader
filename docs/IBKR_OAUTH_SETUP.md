# IBKR ヘッドレス接続を OAuth 1.0a (Web API) へ移行する手順

## 背景 / なぜ必要か

- **2026-07-01 から IBKR Japan の取引では Passkey 認証が必須化**された。
- Passkey は認証器（Face ID / 指紋 / FIDOキー）が「その場の端末」に必要で、EC2 上の**ヘッドレス IB Gateway では物理的に通せない**（"Use your Passkey device" → "Authentication failed"）。IB Key プッシュも Passkey が必須になった時点で提供されなくなる。
- Passkey は Client Portal から削除不可（最後の1つは削除不可・JPでは必須）。
- 結論：**IB Gateway の対話ログイン方式を捨て、OAuth 1.0a トークンで Web API を直接叩くヘッドレス方式へ移行**する。CP Gateway 不要・2FA 不要・完全無人。

### 根拠 / 一次情報（Passkey必須化）

「2026年6月末（実効7/1）から IBKR Japan でパスキー必須」の裏付け：

1. **IBC 公式リリースノート（最も直接的）** — IbcAlpha/IBC v3.24.0：
   > "IBKR Japan have given notice that **passkey authentication will be mandatory for all users from the end of June 2026**."
   https://github.com/IbcAlpha/IBC/releases
2. **IBKR Japan 公式** — セキュアログイン案内に「パスキー設定は2026年6月30日までに必須」：
   https://www.interactivebrokers.co.jp/jp/general/secure-login.php
3. **規制・業界背景** — 2025年の証券口座乗っ取り多発 → 金融庁(FSA)＋日本証券業協会(JSDA)が
   フィッシング耐性MFAを義務化。日本各社が6〜7月に一斉必須化：
   - 野村證券 6/27必須化: https://www.nomura.co.jp/introduc/news/2026/20260513_1.html
   - 松井証券 6月〜順次: https://www.matsui.co.jp/news/2026/detail_0526_01.html
   - 業界動向: https://finance.biggo.com/news/a2d5491a-f606-49eb-8f1b-d9a812140734
4. **実測** — 6/30深夜→7/1の定例再起動で突然フルログイン(Passkey)要求。
   Gateway が `Required PassKey is not supported` / `Use your Passkey device → Authentication failed`。
   上記「6/30必須化」と時系列が一致。

関連メモ: `memory/project_ibkr_passkey_lockout.md`

## 前提

- 口座は **IBKR Pro**（Lite では不可）。
- OAuth 1.0a の First Party（自己利用）は**個人でも基本的に承認不要**で自己発行できる。
- ⚠️ **登録直後は "invalid consumer" (401) が返る**。有効化に**最大24時間**、週末サーバ再起動後に有効化されるとの報告あり。署名が通って正規の JSON エラーが返るなら設定は正しく、あとは待つだけ。

## 1. OAuth 自己発行（ブラウザ・一回きり）

Client Portal の通常メニュー/検索には出てこない。**専用URL**から入る：

```
https://ndcdyn.interactivebrokers.com/sso/Login?action=OAUTH&RL=1&ip2loc=US
```

1. 口座でログイン
2. **Consumer key** を自分で決める（9文字英数字）。本番で使用中の値: `KABUTRADE`
3. 公開鍵3点をアップロード（生成は下記2で）:
   - Signature public key ← `public_signature.pem`
   - Encryption public key ← `public_encryption.pem`
   - DH param ← `dhparam.pem`
4. **Access Token** と **Access Token Secret** を生成 → 控える（**再取得不可**）

## 2. 鍵生成（EC2 上で。秘密鍵は EC2 から出さない）

保存先: `/home/ec2-user/ibkr-oauth/`（`chmod 700`）

```
mkdir -p ~/ibkr-oauth && chmod 700 ~/ibkr-oauth && cd ~/ibkr-oauth
umask 077
openssl genrsa -out private_signature.pem 2048
openssl rsa -in private_signature.pem -outform PEM -pubout -out public_signature.pem
openssl genrsa -out private_encryption.pem 2048
openssl rsa -in private_encryption.pem -outform PEM -pubout -out public_encryption.pem
openssl dhparam -out dhparam.pem 2048
chmod 600 private_*.pem
```

`public_*.pem` と `dhparam.pem` をポータルにアップロード。秘密鍵(`private_*.pem`)は EC2 内のみ。

## 3. 資格情報の保存（EC2, 600 権限）

`~/ibkr-oauth/oauth.env`（**Git管理外・チャット/ログに残さない**）:

```
IBIND_USE_OAUTH=True
IBIND_OAUTH1A_CONSUMER_KEY=KABUTRADE
IBIND_OAUTH1A_ACCESS_TOKEN=<portalで生成>
IBIND_OAUTH1A_ACCESS_TOKEN_SECRET=<portalで生成>
IBIND_OAUTH1A_SIGNATURE_KEY_FP=/home/ec2-user/ibkr-oauth/private_signature.pem
IBIND_OAUTH1A_ENCRYPTION_KEY_FP=/home/ec2-user/ibkr-oauth/private_encryption.pem
IBIND_OAUTH1A_DH_PRIME=<下記で抽出>
```

DH prime (P) の hex 抽出（cryptography では弾かれるので openssl で）:

```
openssl asn1parse -in ~/ibkr-oauth/dhparam.pem | grep -m1 INTEGER | sed 's/.*://' | tr 'A-Z' 'a-z'
```

出力512桁(2048bit)を `IBIND_OAUTH1A_DH_PRIME` に設定。

## 4. 依存パッケージ

```
python3 -m venv ~/ibkr-oauth/venv
~/ibkr-oauth/venv/bin/pip install ibind cryptography requests pycryptodome
```

`pycryptodome`（`Crypto` モジュール）は ibind の OAuth1a に必須。

## 5. 接続テスト

```
cd ~/ibkr-oauth
set -a; . ./oauth.env; set +a
./venv/bin/python -c "from ibind import IbkrClient; c=IbkrClient(use_oauth=True); print(c.tickle().data)"
```

- `invalid consumer` (401) → **未有効化。待って再試行**。
- 認証情報が返る → 有効化済み。次へ。

## 6. アプリ側（今後の実装）

- 新アダプタ `kabu_trader/brokers/ibkr_webapi.py` を `ibind.IbkrClient` ベースで作成し、
  既存 `IBKRBroker`（`ibkr.py`）と**同じI/Fのドロップイン**にする。
  対応メソッド: `place_order` / `cancel_order` / `get_positions` / `get_orders` /
  `get_quote` / `get_account_summary` / `is_healthy`。
- ibind マッピング: `stock_conid_by_symbol`(conid解決) / `place_order`+`reply`(確認応答) /
  `cancel_order` / `positions` / `live_orders` / `live_marketdata_snapshot` /
  `portfolio_summary` / `check_auth_status`。セッション維持は `start_tickler(60)`。
- コンテナから使う場合: `~/ibkr-oauth` を read-only マウントし、`oauth.env` を env_file 指定。
- ⚠️ Client Portal → Settings → Trading Platform の **Read-Only Access を Disabled** にしないと
  API 発注がブロックされる（現状 Enabled）。

## 7. 旧構成の撤去（移行完了後）

- `ib-gateway` コンテナ（gnzsnz/ib-gateway）は不要になるので停止・削除。
- `docker-compose` / env の TWS_* 設定と Gateway 依存を除去。

## トラブルシュート

- `invalid consumer` → 未有効化 or consumer key 不一致。まず24h待つ。
- `No module named 'Crypto'` → `pip install pycryptodome`。
- `Invalid DH parameters`(cryptography) → 上記 openssl 方式で prime を抽出。
- 発注が通らない → Read-Only Access が Enabled でないか確認。
