# Migrating IBKR headless access to OAuth 1.0a (Web API)

## Background / why this is needed

- **From 2026-07-01 IBKR Japan made passkey 2FA mandatory** for trading.
- A passkey needs an authenticator (Face ID / fingerprint / FIDO key) present on
  the machine, which a **headless IB Gateway on EC2 cannot provide**
  ("Use your Passkey device" → "Authentication failed"). IB Key push also stops
  being offered once passkey is required.
- The passkey cannot be removed (last/only passkey is non-deletable; mandatory in
  JP).
- Conclusion: **drop IB Gateway's interactive-login model and hit the Web API
  directly with OAuth 1.0a tokens** — no CP Gateway, no interactive login, no
  2FA, fully unattended.

Related memory: `memory/project_ibkr_passkey_lockout.md`

### Evidence for the passkey mandate

1. **IBC release notes (most direct)** — IbcAlpha/IBC v3.24.0:
   > "IBKR Japan have given notice that **passkey authentication will be
   > mandatory for all users from the end of June 2026**."
   https://github.com/IbcAlpha/IBC/releases
2. **IBKR Japan** secure-login page: passkey activation mandatory by 2026-06-30:
   https://www.interactivebrokers.co.jp/jp/general/secure-login.php
3. **Regulatory background** — 2025 brokerage account-takeover incidents led the
   FSA + JSDA to mandate phishing-resistant MFA; Japanese brokers rolled out
   mandatory passkey in June–July 2026 (e.g. Nomura 6/27, Matsui from June).
4. **Observed** — overnight 6/30→7/1 the scheduled Gateway restart demanded a
   full passkey login; it returned `Required PassKey is not supported` /
   `Use your Passkey device → Authentication failed`, matching the mandate date.

## Prerequisites

- Account must be **IBKR Pro** (not Lite).
- First-party OAuth 1.0a is self-service for individuals (no IBKR approval).
- ⚠️ Right after registration you will get `invalid consumer` (401). Uploaded
  keys/params only activate during **overnight server maintenance** — expect up
  to a day. If the signature is accepted and you get a normal JSON error, the
  config is correct; just wait.

## 1. Self-service OAuth registration (browser, one-time)

Not reachable from the normal Client Portal menu/search. Use the dedicated URL:

```
https://ndcdyn.interactivebrokers.com/sso/Login?action=OAUTH&RL=1&ip2loc=US
```

1. Log in with the account.
2. Choose a **consumer key** (9 alphanumeric chars). In use: `KABUTRADE`.
3. Upload the three public artifacts (generated in step 2):
   - Signature public key ← `public_signature.pem`
   - Encryption public key ← `public_encryption.pem`
   - DH param ← `dhparam.pem`
4. Generate the **Access Token** and **Access Token Secret** → record them
   (**non-recoverable**).

## 2. Key generation (on EC2; private keys never leave the box)

Location: `/home/ec2-user/ibkr-oauth/` (`chmod 700`)

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

Upload `public_*.pem` and `dhparam.pem` to the portal. Private keys stay on EC2.
**Use exactly this `dhparam.pem`** — do not generate a separate one via the
command shown on the portal page (mismatch causes LST validation to fail).

## 3. Store credentials (on EC2, 600-perm)

`~/ibkr-oauth/oauth.env` (**git-ignored; never paste into chat/logs**):

```
IBIND_USE_OAUTH=True
IBIND_OAUTH1A_CONSUMER_KEY=KABUTRADE
IBIND_OAUTH1A_ACCESS_TOKEN=<from portal>
IBIND_OAUTH1A_ACCESS_TOKEN_SECRET=<from portal>
IBIND_OAUTH1A_SIGNATURE_KEY_FP=/home/ec2-user/ibkr-oauth/private_signature.pem
IBIND_OAUTH1A_ENCRYPTION_KEY_FP=/home/ec2-user/ibkr-oauth/private_encryption.pem
IBIND_OAUTH1A_DH_PRIME=<extracted below>
```

Extract the DH prime (p) as hex — use openssl, not `cryptography` (which rejects
the params):

```
openssl asn1parse -in ~/ibkr-oauth/dhparam.pem | grep -m1 INTEGER | sed 's/.*://' | tr 'A-Z' 'a-z'
```

The 512-hex (2048-bit) output goes into `IBIND_OAUTH1A_DH_PRIME`.

## 4. Dependencies

```
python3 -m venv ~/ibkr-oauth/venv
~/ibkr-oauth/venv/bin/pip install ibind cryptography requests pycryptodome
```

`pycryptodome` (the `Crypto` module) is required by ibind's OAuth1a code.

## 5. Connection test

```
cd ~/ibkr-oauth
set -a; . ./oauth.env; set +a
./venv/bin/python -c "from ibind import IbkrClient; c=IbkrClient(use_oauth=True); print(c.tickle().data)"
```

- `invalid consumer` (401) → **not activated yet; wait and retry**.
- Auth data returned → activated. Proceed.

## 6. Application side

- `kabu_trader/brokers/ibkr_webapi.py` provides `IBKRWebAPIBroker`, a drop-in
  replacement for `IBKRBroker` (`ibkr.py`) built on `ibind.IbkrClient`. Same
  interface: `connect` / `disconnect` / `is_healthy` / `place_order` /
  `cancel_order` / `get_positions` / `get_orders` / `get_quote` /
  `get_account_summary`.
- ibind mapping: `stock_conid_by_symbol` (conid) / `place_order` + `reply`
  (confirmations) / `cancel_order` / `positions` / `live_orders` /
  `live_marketdata_snapshot` / `portfolio_summary` / `check_auth_status`. Session
  kept alive with `start_tickler(60)`.
- To use from a container: mount `~/ibkr-oauth` read-only and pass `oauth.env`
  as an env_file.
- ⚠️ Set Client Portal → Settings → Trading Platform → **Read-Only Access =
  Disabled** or API orders are blocked (change takes effect after overnight
  maintenance).

## 7. Retiring the old stack (after cutover)

- The `ib-gateway` container (gnzsnz/ib-gateway) is no longer needed — stop and
  remove it.
- Remove the `TWS_*` settings and Gateway dependencies from `docker-compose` /
  env.

## Lessons learned / battle-tested troubleshooting (2026-07, in the order hit)

This migration took a full 5 days. The core lesson: **portal changes only take
effect during IBKR's overnight maintenance.** Changing keys repeatedly resets the
propagation clock and never converges. **Change one thing, then wait a night.**

### Propagation (most important)
- Consumer key, public keys, dhparam, Read-Only, etc. **do not apply
  immediately** — they activate after **overnight maintenance (≥1 day)**. After a
  change, **leave it alone and wait**.

### Username / account
- The OAuth registration page shows username **`ypzdkx114`** even when you log in
  as the live user `sogoogos123` (**even in an incognito window**). It
  nonetheless authenticates to the **LIVE account `U25706175`** (JPY, IB-JP) — no
  separate paper registration is needed. Confirm the account via
  `portfolio_accounts()`.

### Error progression (this is the order you hit)
1. `401 invalid consumer` → consumer not activated yet. **Wait overnight**
   (regenerating the token did not help).
2. `401 LST failed, Invalid signature` (on `/oauth/live_session_token`) →
   **signature key mismatch** (portal public signature key ≠ EC2 private
   signature key), or the signature-key change hasn't propagated yet.
3. `RuntimeError: Live session token validation failed` (with `/logout` returning
   Invalid signature) → **DH prime mismatch**: the portal's dhparam was a
   different file (a `dhparams.pem` generated via the on-page openssl command).
   Fix: re-upload EC2's `dhparam.pem`, then **wait overnight**.

### Isolation technique
- **Secret decryption test**: if
  `calculate_live_session_token_prepend(secret, private_encryption_key)`
  succeeds, the encryption side is correct → the problem is the signature key or
  DH.
- **If the RSA signature is accepted (the LST request returns a response), IBKR
  rebuilt the same base string using the same prepend (= decrypted secret), so
  the secret is correct.** If it still fails validation, the only remaining
  difference is the DH shared secret → **prime mismatch**, provable by
  elimination.

### Keys / parameters
- The access token is fixed. **Regenerating only changes the secret.**
- Extract the DH prime with `openssl asn1parse` (`cryptography` rejects the
  params with `Invalid DH parameters`). **Strip leading zeros** (a 2048-bit prime
  starting non-zero needs none). Generator = 2 (matches ibind's default).
- Self-check a key pair: `diff <(openssl rsa -in private_X.pem -pubout) public_X.pem`.
- ⚠️ Watch for **swapped signature/encryption slots** (a swap also yields Invalid
  signature).

### Session establishment (adapter requirement)
- Right after connecting, `authentication_status().established == False` and
  `/iserver/accounts` is empty. During this window, orders/whatif are rejected
  with `accountId is not valid: U25706175`. → **retry
  `initialize_brokerage_session()` until established=True**, then prime with
  `receive_brokerage_accounts()`. Disabling Read-Only Access was NOT the fix for
  this (but is still required to place real orders).

### conid resolution (Japanese-stock trap)
- `/trsrv/stocks` (`stock_conid_by_symbol`) returns instruments for the same
  numeric ticker across **TSE (Japan) / TWSE (Taiwan) / SEHK (Hong Kong)**. The
  default `isUS=True` filter drops all JP listings (0 results).
- Correct: `default_filtering=False` + **JP =
  `contract_conditions={"exchange": "TSEJ"}` / US = `{"isUS": True}`**. (A
  `currency` filter returns 0 — that field isn't present on this endpoint.)
  Verified: 2371.T→44060588, 2802.T→13905336, AAPL→265598.

### Market data
- `live_marketdata_snapshot` needs `/iserver/accounts` priming + an established
  session (otherwise 500 `Please query /accounts first`), plus a TSE real-time
  data subscription. → For now **prices stay on yfinance** (the switchable
  provider in `market_data.py`).

### Misc
- `No module named 'Crypto'` → `pip install pycryptodome`.
- Real orders require Client Portal → Trading Platform → **Read-Only Access =
  Disabled** (applies after overnight maintenance).

### Validation status (as of 2026-07-06)
- ✅ OAuth connect / account summary / positions / open orders / conid
  resolution / whatif order preview (live class, no real orders placed)
- 🔸 `place_order` (real order) and `get_quote` (market-data subscription) not
  yet validated
- ⏳ Wire the app to the adapter → resume live → retire the `ib-gateway` container
