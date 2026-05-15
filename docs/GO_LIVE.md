# Going Live with Real Money

This guide is for the transition from **Stage 2 (paper orders via IBKR)** to **Stage 3 (live orders, real money)**. If you haven't completed Stage 2 with at least a week of clean reconciliation, do that first.

## Pre-flight checklist

Run for at least 1-2 weeks on paper and verify each of these before flipping the switch.

- [ ] **`reconcile` is clean every day** — no LINE drift alerts on broker-managed positions. Drift you can't explain is a hard block.
- [ ] **Win rate roughly matches local backtests** — paper IBKR fills drift ~¥10 from the signal price; if your edge depends on tight fills, real fills will be similar.
- [ ] **No reconnect / session issues** across the daily IBKR forced-logout cycle.
- [ ] **Position sizing is realistic for live capital** — see "Sizing for small accounts" below.
- [ ] **`commission_rate` reflects real IBKR fees** — see "Commission" below.
- [ ] **LINE alerts working end-to-end** — trade fills, breaking news, daily reconcile.
- [ ] **Live trading permissions enabled** in IBKR Account Management → Settings → Trading → Trading Permissions. New accounts often start with Japan stocks **disabled** and require you to opt in (takes ~1 business day for approval).

## Sizing for small accounts (e.g. ¥1M)

`PaperTrader` enforces a **100-share lot minimum** for JP stocks. This means: even if `position_size_pct: 0.1` says "spend ¥100K per position", a signal for a ¥5,000 stock will buy 100 shares = ¥500K, eating 50% of capital. The `position_size_pct` is best understood as a *target budget* — the lot floor takes over for higher-priced stocks.

Recommended values by capital:

| Initial capital | `position_size_pct` | `max_positions` | Notes |
|---|---|---|---|
| ¥1M | 0.45 | 2 | ¥450K target, ~¥100K cash buffer. Max 2 positions due to lot floor. |
| ¥2M | 0.30 | 3 | ¥600K target, room for 3 simultaneous positions. |
| ¥5M+ | 0.10–0.15 | 5–7 | Lot floor stops mattering; sizing becomes the real cap. |

For your local config, apply:

```bash
python3 -c "import json; p='config/default.json'; c=json.load(open(p)); c['backtest'].update({'initial_capital':1000000, 'position_size_pct':0.45, 'max_positions':2, 'commission_rate':0.0005}); json.dump(c, open(p,'w'), indent=2, ensure_ascii=False); print(c['backtest'])"
```

## Commission

IBKR Japan uses a **tiered commission**: roughly **0.05% of trade value** with a **¥80 minimum** per execution. For typical ¥300K–500K positions, the percentage rate dominates and `0.0005` is a good approximation. Tiny positions (< ¥160K) hit the floor and the effective rate climbs.

The local P&L will still diverge slightly from the broker P&L because of:
- Slippage between signal price and fill price (cents-to-yen)
- Tier-minimum effects on small trades
- Tax-related fees (paper accounts skip these)

Trust IBKR's view as the source of truth; use local for strategy tuning.

## The technical switch

### 1. Funding and permissions

- Move JPY into the **live** account (or USD that converts).
- Confirm Japan Stocks permission is enabled in Account Management.
- Approve any IBKR risk disclosures they prompt you to sign.

### 2. Update credentials

Edit `~/.ibkr.env` to use your **live** account username/password (not paper). Chmod 600 if it isn't already.

### 3. Relaunch Gateway in live mode

```bash
docker rm -f ib-gateway && docker run -d --name ib-gateway --restart unless-stopped --network host \
  --env-file ~/.ibkr.env \
  -e TRADING_MODE=live \
  -e READ_ONLY_API=no \
  -e EXISTING_SESSION_DETECTED_ACTION=primary \
  -e TWS_SETTINGS_PATH=/home/ibgateway/settings \
  -v ~/ibkr-data-live:/home/ibgateway/settings \
  gnzsnz/ib-gateway:stable
```

Changes from the paper command:
- `TRADING_MODE=live` (was `paper`)
- Separate bind-mount `~/ibkr-data-live` so the autorestart file is per-environment

Approve 2FA on your phone (IBKR will **not** let you disable 2FA on a live account).

Verify login:

```bash
docker logs ib-gateway 2>&1 | grep 'Login has completed'
```

### 4. Switch the trader config

```bash
python3 -c "import json; p='config/default.json'; c=json.load(open(p)); c['broker'].update({'paper':False,'port':4001,'readonly':False}); json.dump(c, open(p,'w'), indent=2, ensure_ascii=False); print(c['broker'])"
```

You should see `"paper": false` and `"port": 4001`. **Both** are required for live.

### 5. Reset local PaperTrader state

Live shouldn't inherit phantom paper positions:

```bash
docker compose exec kabu-trader-jp python -m kabu_trader.cli report --reset
```

### 6. Live smoke-test (one share, far below market, then cancel)

Before letting the monitor place real orders, verify the path with the smallest possible order:

```bash
docker compose exec kabu-trader-jp python -c "from kabu_trader.brokers.ibkr import IBKRBroker as B; b=B('127.0.0.1',4001,client_id=42,paper=False,readonly=False); b.connect(); print(b.place_order('7203.T','BUY',1,order_type='LMT',limit_price=1000.0)); b.disconnect()"
```

Expected output: `'status': 'PreSubmitted'`. Then **cancel the order immediately** via the IBKR web UI (live account, not paper). If it filled before you canceled, sell the 1 share with another order.

### 7. Restart the trader

```bash
docker compose restart kabu-trader-jp && docker logs kabu-trader-jp 2>&1 | grep -i broker
```

You want to see `IBKR live broker ENABLED (LIVE)`. The word `LIVE` (not `PAPER`) is the confirmation.

## First-day live monitoring

- Watch the first signal closely. Don't go AFK on day 1.
- Set LINE alerts to less aggressive muting (e.g. notify on every fill) for the first week.
- Run `kabu_trader.cli reconcile` after each session, in addition to the daily cron.
- Keep `kabu_trader.cli broker` open in a terminal during market hours to watch fills land.

## Rollback

If anything looks wrong, immediately flip back to paper:

```bash
python3 -c "import json; p='config/default.json'; c=json.load(open(p)); c['broker'].update({'paper':True,'port':4002}); json.dump(c, open(p,'w'), indent=2, ensure_ascii=False)" && docker compose restart kabu-trader-jp
```

Then `docker rm -f ib-gateway` and relaunch with `TRADING_MODE=paper` per the README.

Any live positions you opened will stay at the broker until you close them manually (or re-enable live and let the strategy trigger exits). **The trader cannot manage positions it doesn't know about** — so reset local state only when you have no live positions, or accept that legacy live positions need manual cleanup.

## Known limitations of the current adapter (live trading)

These were tolerable for paper but matter more for live. See `kabu_trader/brokers/ibkr.py` for the implementation.

- **Fill price is approximate.** Local books store the signal price, not the actual broker fill price. P&L will drift a few yen per trade. Fix on the roadmap: read `trade.fills` and override `entry_price`.
- **No order-status reconciliation.** Once submitted, the trader doesn't poll for partial fills or eventual fills of orders that go from PreSubmitted → Filled async.
- **No retry on transient broker errors.** If `place_order()` raises for a reason that should be retried (e.g. brief network blip), the trade is just skipped. Acceptable for swing trading; not for HFT.
- **Position sizing is computed locally**, ignoring broker buying power. If the broker rejects a trade for margin reasons, local state is correctly not updated (Stage 2 verified), but you may see "Live BUY rejected" messages.
