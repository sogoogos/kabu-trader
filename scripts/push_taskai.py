#!/usr/bin/env python3
"""Push the current paper/live trading status to a TaskAI deployment.

The companion TaskAI app shows the swing-trading status in its "投資" tab. This
script builds the same summary `report` shows, then POSTs it to TaskAI's ingest
endpoint. Run it on a cron so the app always shows a recent snapshot.

The endpoint URL and shared token are read from environment variables (never
hard-code them — this repo is public).

Usage:
    python -m scripts.push_taskai -c config/default.json --source jp  --label "日本株(ペーパー)"
    python -m scripts.push_taskai -c config/live.json    --source live --label "日本株(ライブ)"
    python -m scripts.push_taskai -c config/us.json       --source us   --label "米国株"

Env:
    TASKAI_INGEST_URL    e.g. https://<your-taskai-domain>/api/trading/ingest
    TASKAI_INGEST_TOKEN  shared secret (matches TaskAI's TRADING_INGEST_TOKEN)

To schedule, use the helper which reads the two env vars and installs a crontab:
    export TASKAI_INGEST_URL=... TASKAI_INGEST_TOKEN=...
    bash scripts/install_taskai_cron.sh
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


def _log(msg: str) -> None:
    print(f"[push] {msg}", file=sys.stderr, flush=True)


def build_strategy(config: dict, market: dict) -> dict:
    """Summarize the BUY/SELL decision logic from config so TaskAI can explain it.

    Pulls the composite-score thresholds + indicator weights from the strategy
    config, the risk/exit rules from the backtest config, and describes the BUY
    veto gates. Mirrors kabu_trader.strategy.SwingCompositeStrategy so the chat
    answer stays in sync with how trades are actually decided.
    """
    from kabu_trader.strategy import SwingCompositeStrategy

    strat = config.get("strategy", {}) or {}
    params = strat.get("params", {}) or {}
    bt = config.get("backtest", {}) or {}

    # Merge configured weights over the engine defaults (same as the strategy does).
    weights = dict(SwingCompositeStrategy.DEFAULT_WEIGHTS)
    weights.update(params.get("indicator_weights", {}) or {})
    indicators = [
        {"key": k, "weight": w}
        for k, w in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
        if w
    ]

    # Surface the parameters that actually shape the per-indicator scores.
    param_keys = (
        "sma_short", "sma_long", "rsi_period", "rsi_oversold", "rsi_overbought",
        "macd_fast", "macd_slow", "macd_signal", "bb_period", "bb_std",
        "volume_spike_threshold", "ichimoku_tenkan", "ichimoku_kijun",
        "ichimoku_senkou_b", "mfi_period", "adx_period", "rs_period",
    )
    out_params = {k: params[k] for k in param_keys if k in params}

    # BUY veto gates (see SwingCompositeStrategy._buy_vetoed).
    buy_vetoes = []
    ml_floor = params.get("buy_veto_ml_proba_below", 0.45)
    if ml_floor:
        buy_vetoes.append(
            f"ML モデルの上昇確率が {ml_floor:.0%} 未満（弱気）なら新規買いを見送る"
        )
    if params.get("buy_veto_overbought", True):
        ob = params.get("rsi_overbought", 70)
        buy_vetoes.append(
            f"RSI が {ob} 超、または終値が上部ボリンジャーバンド超（買われすぎ）なら新規買いを見送る"
        )

    exit_keys = (
        "stop_loss_pct", "take_profit_pct", "trailing_stop_enabled",
        "trailing_stop_activate_pct", "trailing_stop_distance_pct",
        "max_hold_days", "rotation_enabled", "rotation_max_pnl_pct",
        "rotation_min_hold_hours", "reentry_cooldown_days",
    )
    exit_rules = {k: bt[k] for k in exit_keys if k in bt}

    sizing = {k: bt[k] for k in ("position_size_pct", "max_positions") if k in bt}

    signal_threshold = params.get("signal_threshold", 4)
    strong_threshold = params.get("strong_signal_threshold", 7)

    return {
        "name": strat.get("name", "swing_composite"),
        "benchmark": market.get("benchmark_name"),
        "signal_threshold": signal_threshold,
        "strong_signal_threshold": strong_threshold,
        "indicators": indicators,
        "params": out_params,
        "buy_vetoes": buy_vetoes,
        "exit_rules": exit_rules,
        "position_sizing": sizing,
        "description": (
            f"{len(indicators)} 指標を各 -1〜+1 で採点し重み付けして合算。"
            f"合計の絶対値が {signal_threshold} 以上で BUY/SELL、"
            f"{strong_threshold} 以上で STRONG_BUY/SELL。"
            "買われすぎ・ML弱気の新規買いは veto で見送る。決済は損切り/利確/"
            "トレーリングストップ/最大保有日数で行う。"
        ),
    }


def _read_signals(state_dir: Path) -> tuple[list, str]:
    """Read the monitor's latest watchlist signals from state_dir/signals.json.

    The monitor (running separately) snapshots its in-memory alerts there each
    cycle. Returns (signals, generated_at). Missing/corrupt file → ([], "").
    """
    path = state_dir / "signals.json"
    try:
        if not path.exists():
            return [], ""
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("signals", []) or [], data.get("generated_at", "") or ""
    except Exception as e:  # noqa: BLE001 - best effort
        _log(f"signals.json read failed: {e}")
        return [], ""


def build_payload(config: dict) -> tuple[dict, str]:
    """Return (payload, currency_symbol) for the given config."""
    market = get_market_settings(config)
    sym = market["currency_symbol"]
    state_dir = Path(market["state_dir"]) if market["state_dir"] else None
    _log("loading paper trading state...")
    trader = PaperTrader(config["backtest"], state_dir=state_dir)
    _log(f"state loaded: {len(trader.positions)} positions")

    # Live current prices for open positions (best effort, bounded by PRICE_TIMEOUT).
    # PUSH_SKIP_PRICES=1 で価格取得を完全スキップ（取得単価で代替＝含み損益フラット）。
    price_dict: dict[str, float] = {}
    if trader.positions and not os.environ.get("PUSH_SKIP_PRICES"):
        _log(f"fetching current prices for {len(trader.positions)} tickers...")
        price_dict = _fetch_prices_with_timeout(
            market["benchmark_ticker"], list(trader.positions.keys())
        )
        _log(f"prices: got {len(price_dict)}")
    elif trader.positions:
        _log("PUSH_SKIP_PRICES set; skipping price fetch")

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

    # Current BUY/SELL signals across the watchlist (written by the monitor).
    signals, signals_at = _read_signals(trader.state_dir)
    _log(f"signals: {len(signals)} (as of {signals_at or 'n/a'})")

    payload = {
        "is_live": is_live,
        "summary": summary,
        "positions": positions,
        "trades": trades,
        "strategy": build_strategy(config, market),
        "signals": signals,
        "signals_at": signals_at,
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

    _log(f"loading config {args.config}")
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
    _log(f"posting to {url} ...")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[{args.source}] pushed: {resp.status} {resp.read().decode()[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[{args.source}] push failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
