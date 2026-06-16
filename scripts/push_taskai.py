#!/usr/bin/env python3
"""Push the current paper/live trading status to the TaskAI app.

TaskAI (https://taskai.busystems.com) shows the swing-trading status in its
"投資" tab. This script builds the same summary `report` shows, then POSTs it to
TaskAI's ingest endpoint. Run it on a cron (see crontab example below) so the
app always shows a recent snapshot.

Usage:
    python -m scripts.push_taskai -c config/default.json --source jp  --label "日本株(ペーパー)"
    python -m scripts.push_taskai -c config/live.json    --source live --label "日本株(ライブ)"
    python -m scripts.push_taskai -c config/us.json       --source us   --label "米国株"

Env:
    TASKAI_INGEST_URL    e.g. https://taskai.busystems.com/api/trading/ingest
    TASKAI_INGEST_TOKEN  shared secret (matches TaskAI's TRADING_INGEST_TOKEN)

Cron (push every 30 min, 7-23h JST), `crontab -e`:
    */30 0-14 * * 1-5  cd /home/ec2-user/kabu-trader && /usr/bin/python3 -m scripts.push_taskai -c config/default.json --source jp   --label "日本株(ペーパー)" >> /tmp/taskai_push.log 2>&1
    */30 0-14 * * 1-5  cd /home/ec2-user/kabu-trader && /usr/bin/python3 -m scripts.push_taskai -c config/live.json    --source live --label "日本株(ライブ)"  >> /tmp/taskai_push.log 2>&1
    */30 13-23 * * 1-5 cd /home/ec2-user/kabu-trader && /usr/bin/python3 -m scripts.push_taskai -c config/us.json      --source us   --label "米国株"        >> /tmp/taskai_push.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import urllib.request
from pathlib import Path

from kabu_trader.cli import load_config, get_market_settings
from kabu_trader.paper_trader import PaperTrader

RECENT_TRADES = 15
PRICE_TIMEOUT = 30  # 現在値取得の上限秒数（超えたら取得単価で代替し、ハングを防ぐ）


def _price_worker(benchmark_ticker: str, tickers, q) -> None:
    """子プロセスで現在値を取得し、{ticker: price} を queue に入れる。"""
    try:
        from kabu_trader.data_fetcher import DataFetcher

        fetcher = DataFetcher(benchmark_ticker=benchmark_ticker)
        out = {p["ticker"]: p["price"] for p in fetcher.fetch_current_prices(list(tickers))}
        q.put(out)
    except Exception as e:  # noqa: BLE001 - best effort
        print(f"price fetch failed: {e}", file=sys.stderr)
        q.put({})


def _fetch_prices_with_timeout(benchmark_ticker: str, tickers) -> dict:
    """現在値取得を子プロセスに分離。PRICE_TIMEOUT 超過で強制終了し空 dict を返す。

    yfinance はスレッド内で通信するため SIGALRM では中断できない。子プロセスごと
    kill することで、cron が確実にハングしないようにする。
    """
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    proc = ctx.Process(target=_price_worker, args=(benchmark_ticker, tickers, q), daemon=True)
    proc.start()
    try:
        result = q.get(timeout=PRICE_TIMEOUT)
    except Exception:  # noqa: BLE001 - Empty(timeout) 含む
        result = {}
        print(f"price fetch timed out after {PRICE_TIMEOUT}s; using entry prices", file=sys.stderr)
    finally:
        proc.terminate()
        proc.join(5)
    return result


def build_payload(config: dict) -> tuple[dict, str]:
    """Return (payload, currency_symbol) for the given config."""
    market = get_market_settings(config)
    sym = market["currency_symbol"]
    state_dir = Path(market["state_dir"]) if market["state_dir"] else None
    trader = PaperTrader(config["backtest"], state_dir=state_dir)

    # Live current prices for open positions (best effort, bounded by PRICE_TIMEOUT).
    price_dict: dict[str, float] = {}
    if trader.positions:
        price_dict = _fetch_prices_with_timeout(
            market["benchmark_ticker"], list(trader.positions.keys())
        )

    summary = trader.get_summary(price_dict)

    positions = []
    for ticker, pos in trader.positions.items():
        price = price_dict.get(ticker, pos.entry_price)
        positions.append(
            {
                "ticker": ticker,
                "name": pos.name,
                "shares": pos.shares,
                "entry_price": pos.entry_price,
                "current_price": price,
                "pnl": pos.pnl(price),
                "pnl_pct": pos.pnl_pct(price),
                "entry_date": pos.entry_date,
            }
        )

    trades = [
        {
            "timestamp": t.get("timestamp", ""),
            "action": t.get("action", ""),
            "ticker": t.get("ticker", ""),
            "name": t.get("name", ""),
            "price": t.get("price", 0),
            "shares": t.get("shares", 0),
            "pnl": t.get("pnl"),
            "pnl_pct": t.get("pnl_pct"),
            "reason": t.get("reason"),
        }
        for t in trader.trade_log[-RECENT_TRADES:]
    ]

    broker_cfg = config.get("broker", {})
    is_live = broker_cfg.get("enabled", False) and not broker_cfg.get("paper", True)

    payload = {
        "is_live": is_live,
        "summary": summary,
        "positions": positions,
        "trades": trades,
    }
    return payload, sym


def main() -> int:
    ap = argparse.ArgumentParser(description="Push trading status to TaskAI")
    ap.add_argument("-c", "--config", required=True, help="config json path")
    ap.add_argument("--source", required=True, help="market id (jp/us/live)")
    ap.add_argument("--label", default=None, help="display label")
    args = ap.parse_args()

    url = os.environ.get("TASKAI_INGEST_URL")
    token = os.environ.get("TASKAI_INGEST_TOKEN")
    if not url or not token:
        print("TASKAI_INGEST_URL / TASKAI_INGEST_TOKEN must be set", file=sys.stderr)
        return 2

    config = load_config(args.config)
    payload, sym = build_payload(config)
    body = json.dumps(
        {"source": args.source, "label": args.label, "currency": sym, "payload": payload}
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[{args.source}] pushed: {resp.status} {resp.read().decode()[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[{args.source}] push failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
