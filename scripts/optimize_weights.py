"""Random-search weight optimizer for SwingCompositeStrategy.

Holds structural weights (sentiment, relative_strength, ml) fixed at their
configured values; samples the technical-indicator weights from plausible
ranges; runs a backtest per sample; prints the top-N sets ranked by
risk-adjusted return.

Usage:
    python scripts/optimize_weights.py -c config/default.json -n 100
    python scripts/optimize_weights.py -c config/us.json -n 50 -t AAPL MSFT TSLA -d 365

Data fetch happens once up-front and is reused across all iterations.
Use a ticker subset (-t) and/or shorter history (-d) to keep runtime manageable.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kabu_trader.backtester import Backtester
from kabu_trader.data_fetcher import DataFetcher
from kabu_trader.ml_model import MLPredictor
from kabu_trader.strategy import SwingCompositeStrategy


TUNABLE = ["sma", "rsi", "macd", "bollinger", "volume", "ichimoku", "mfi", "adx", "earnings"]
FIXED = ["sentiment", "relative_strength", "ml"]

RANGES = {
    "sma":       (0.5, 2.5),
    "rsi":       (0.5, 2.5),
    "macd":      (1.0, 3.0),
    "bollinger": (0.5, 2.0),
    "volume":    (0.5, 2.0),
    "ichimoku":  (1.0, 3.0),
    "mfi":       (1.0, 3.0),
    "adx":       (1.0, 3.0),
    "earnings":  (0.5, 3.0),
}


def objective(avg_return: float, avg_dd: float, avg_sharpe: float) -> float:
    """Risk-adjusted score. Higher is better. Negative returns penalize linearly."""
    if avg_return <= 0:
        return avg_return
    dd_penalty = max(0.0, 1.0 - avg_dd / 100)
    sharpe_bonus = max(0.3, min(2.0, avg_sharpe))
    return avg_return * dd_penalty * sharpe_bonus


def run_once(data, benchmark_df, strategy_params, weights, backtest_cfg, benchmark_name, ml):
    params = deepcopy(strategy_params)
    params["indicator_weights"] = weights
    strategy = SwingCompositeStrategy(params, benchmark_name)
    strategy.set_benchmark_data(benchmark_df)
    if ml is not None:
        strategy.set_ml_model(ml)
    bt = Backtester(backtest_cfg)
    results = bt.run_multiple(data, strategy)
    if not results:
        return 0.0, 100.0, 0.0, 0
    rets = [r.total_return_pct for r in results.values()]
    dds = [r.max_drawdown_pct for r in results.values()]
    sharpes = [r.sharpe_ratio for r in results.values() if r.sharpe_ratio]
    total_trades = sum(len(r.trades) for r in results.values() if hasattr(r, "trades"))
    return (
        sum(rets) / len(rets),
        sum(dds) / len(dds) if dds else 0.0,
        sum(sharpes) / len(sharpes) if sharpes else 0.0,
        total_trades,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("-n", "--iterations", type=int, default=100)
    ap.add_argument("-d", "--days", type=int, default=365)
    ap.add_argument("-t", "--tickers", nargs="+")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    random.seed(args.seed)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    market = cfg.get("market", {})
    tickers = args.tickers or cfg["watchlist"]
    benchmark_ticker = market.get("benchmark_ticker", "^N225")
    benchmark_name = market.get("benchmark_name", "Nikkei")

    configured = cfg["strategy"]["params"].get("indicator_weights", {})
    fixed_weights = {k: configured.get(k, SwingCompositeStrategy.DEFAULT_WEIGHTS[k]) for k in FIXED}

    print(f"Fetching {args.days} days of data for {len(tickers)} tickers...")
    fetcher = DataFetcher(benchmark_ticker=benchmark_ticker)
    benchmark_df = fetcher.fetch_benchmark(days=args.days)
    data = fetcher.fetch_multiple(tickers, days=args.days)
    if not data:
        raise SystemExit("No data fetched.")
    print(f"Fetched {len(data)} tickers. Running {args.iterations} random samples...\n")

    ml_name = cfg.get("ml", {}).get("model_name", "default")
    ml = MLPredictor()
    if not ml.load(ml_name):
        ml = None
        print("(no ML model loaded — ML feature will contribute 0 regardless of weight)\n")

    results = []
    for i in range(args.iterations):
        weights = dict(fixed_weights)
        for k in TUNABLE:
            lo, hi = RANGES[k]
            weights[k] = round(random.uniform(lo, hi), 2)
        ret, dd, sharpe, trades = run_once(
            data, benchmark_df, cfg["strategy"]["params"], weights, cfg["backtest"], benchmark_name, ml
        )
        score = objective(ret, dd, sharpe)
        results.append((score, ret, dd, sharpe, trades, weights))
        print(f"  [{i+1:3d}/{args.iterations}] score={score:+7.2f} "
              f"return={ret:+6.2f}% dd={dd:5.1f}% sharpe={sharpe:+.2f} trades={trades}")

    results.sort(key=lambda x: x[0], reverse=True)
    print(f"\n=== Top {args.top} weight sets ===")
    for rank, (score, ret, dd, sharpe, trades, weights) in enumerate(results[: args.top], 1):
        print(f"\n#{rank}  score={score:+.2f}  return={ret:+.2f}%  dd={dd:.1f}%  sharpe={sharpe:+.2f}  trades={trades}")
        print(json.dumps(weights, indent=2))

    best = results[0][5]
    best_path = Path(args.config).with_suffix(".best_weights.json")
    best_path.write_text(json.dumps(best, indent=2), encoding="utf-8")
    print(f"\n--- Best weights written to {best_path} ---")
    print(f"Apply with:")
    print(f"  python3 -c \"import json; p='{args.config}'; d=json.load(open(p));"
          f" d['strategy']['params']['indicator_weights']=json.load(open('{best_path}'));"
          f" json.dump(d,open(p,'w'),ensure_ascii=False,indent=2)\"")


if __name__ == "__main__":
    main()
