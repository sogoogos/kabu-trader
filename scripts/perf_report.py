#!/usr/bin/env python3
"""Forward-observation performance report for the paper/live trading ledgers.

Reads one or more trades.csv files and prints a compact scorecard per
environment, plus a "since" slice so a change deployed on a given date can be
judged against the trades that happened after it. Pure stdlib so it runs with
the system python3 on EC2 (no venv, no deps).

Headline metrics per environment: closed-trade count, win rate, profit factor
(PF = gross win / gross loss), expectancy (avg P&L), avg win / avg loss, and a
by-reason breakdown. The slice additionally surfaces the three things we are
watching after the 2026-06-29 changes:
  - take_profit hit rate     (US TP lowered 15%->10%: should rise)
  - stop_loss exit pnl_pct   (ATR stops: realized stops should widen past -5%)
  - rotated_out count + avg  (rotation score-margin guard: churn should drop)

Usage:
  perf_report.py                      # default EC2 host layout, since=deploy date
  perf_report.py --since 2026-06-29
  perf_report.py JP=path/to/jp.csv US=path/to/us.csv --since 2026-06-29
"""
import csv
import sys
import statistics as st

DEPLOY_DATE = "2026-06-29"  # rotation guard + ATR stops (JP) + US TP/stop refit

# label -> default host path (mounted volumes under ~/kabu-trader on EC2)
DEFAULT_SOURCES = [
    ("JP_paper", "paper_trading/trades.csv"),
    ("JP_live", "paper_trading_live/trades.csv"),
    ("US_paper", "paper_trading_us/trades.csv"),
]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def load_sells(path):
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return None
    return [r for r in rows if r.get("action") == "SELL"]


def metrics(sells):
    """Headline metrics for a list of SELL rows."""
    if not sells:
        return None
    pnls = [num(r["pnl"]) for r in sells]
    pcts = [num(r["pnl_pct"]) for r in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss else float("inf")
    win_pcts = [p for p in pcts if p > 0]
    loss_pcts = [p for p in pcts if p <= 0]
    return {
        "n": len(sells),
        "win_rate": 100.0 * len(wins) / len(sells),
        "pf": pf,
        "total_pnl": sum(pnls),
        "expectancy": st.mean(pnls),
        "avg_win_pct": st.mean(win_pcts) if win_pcts else 0.0,
        "avg_loss_pct": st.mean(loss_pcts) if loss_pcts else 0.0,
    }


def by_reason(sells):
    agg = {}
    for r in sells:
        g = agg.setdefault(r.get("reason", "?"), [0, 0.0])
        g[0] += 1
        g[1] += num(r["pnl"])
    return agg


def fmt_metrics(m):
    if not m:
        return "  (no closed trades)"
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    return (
        f"  trades {m['n']:<3}  win {m['win_rate']:4.0f}%  PF {pf:>4}  "
        f"expectancy {m['expectancy']:+,.0f}  "
        f"avgW {m['avg_win_pct']:+.1f}% / avgL {m['avg_loss_pct']:+.1f}%  "
        f"total {m['total_pnl']:+,.0f}"
    )


def report_env(label, path, since):
    sells = load_sells(path)
    print(f"\n===== {label} =====")
    if sells is None:
        print(f"  FILE NOT FOUND: {path}")
        return
    print(f"  source: {path}")
    print(" ALL-TIME:")
    print(fmt_metrics(metrics(sells)))

    recent = [r for r in sells if r.get("timestamp", "") >= since]
    print(f" SINCE {since}:")
    print(fmt_metrics(metrics(recent)))
    if recent:
        agg = by_reason(recent)
        parts = [f"{k} {v[0]}({v[1]:+,.0f})" for k, v in
                 sorted(agg.items(), key=lambda x: x[1][1])]
        print("   by reason: " + ", ".join(parts))
        # Watch items for the 2026-06-29 changes.
        tp = [r for r in recent if r.get("reason") == "take_profit"]
        print(f"   take_profit hits: {len(tp)}/{len(recent)} "
              f"({100*len(tp)/len(recent):.0f}%)")
        sl_pcts = sorted(num(r["pnl_pct"]) for r in recent
                         if r.get("reason") == "stop_loss")
        if sl_pcts:
            print(f"   stop_loss exits ({len(sl_pcts)}): "
                  f"{', '.join(f'{p:.1f}%' for p in sl_pcts)}")
        rot = [num(r["pnl"]) for r in recent if r.get("reason") == "rotated_out"]
        if rot:
            print(f"   rotated_out: {len(rot)} trades, "
                  f"avg {st.mean(rot):+,.0f}, total {sum(rot):+,.0f}")


def main(argv):
    since = DEPLOY_DATE
    sources = []
    for a in argv:
        if a.startswith("--since="):
            since = a.split("=", 1)[1]
        elif a == "--since":
            continue  # handled by next token below
        elif "=" in a and not a.startswith("--"):
            label, path = a.split("=", 1)
            sources.append((label, path))
    # support "--since DATE" (space-separated)
    if "--since" in argv:
        i = argv.index("--since")
        if i + 1 < len(argv):
            since = argv[i + 1]
    if not sources:
        sources = DEFAULT_SOURCES

    print(f"# Trading performance report  (since-slice from {since})")
    for label, path in sources:
        report_env(label, path, since)
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
