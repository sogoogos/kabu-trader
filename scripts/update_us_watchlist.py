"""Update config/us.example.json to the S&P 500 constituents.

Source: datasets/s-and-p-500-companies (maintained CSV on GitHub).
https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv

Strategy:
- Normalize tickers (BRK.B -> BRK-B) for yfinance compatibility.
- Clean company names: strip common suffixes (Inc., Corp., The, etc.).
- Preserve existing watchlist_aliases from the current config.

Usage: python scripts/update_us_watchlist.py [--csv path.csv]
Default CSV path is /tmp/sp500.csv — fetch with:
    curl -fsSL https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv -o /tmp/sp500.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config" / "us.example.json"
DEFAULT_CSV = Path("/tmp/sp500.csv")


def normalize_ticker(symbol: str) -> str:
    """BRK.B -> BRK-B (yfinance convention)."""
    return symbol.strip().replace(".", "-")


def clean_name(name: str) -> str:
    """Strip common suffixes and articles for cleaner news-title matching."""
    name = name.strip()
    # Drop leading "The "
    if name.startswith("The "):
        name = name[4:]
    # Trim trailing corporate suffixes.
    suffixes = [
        ", Inc.", " Inc.", ", Inc", " Inc",
        ", Corp.", " Corp.", " Corporation",
        ", Co.", " Co.", " Company",
        ", Ltd.", " Ltd.", " Limited",
        ", LLC", " LLC",
        " Group", " Holdings",
        " plc", " PLC",
    ]
    changed = True
    while changed:
        changed = False
        for suf in suffixes:
            if name.endswith(suf):
                name = name[: -len(suf)].rstrip(",. ")
                changed = True
    # Collapse whitespace.
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def load_sp500(csv_path: Path) -> list[tuple[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        out = []
        for row in reader:
            ticker = normalize_ticker(row["Symbol"])
            name = clean_name(row["Security"])
            if ticker and name:
                out.append((ticker, name))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = ap.parse_args()

    if not args.csv.exists():
        raise SystemExit(
            f"CSV not found: {args.csv}\n"
            "Fetch it with:\n"
            "  curl -fsSL https://raw.githubusercontent.com/datasets/"
            "s-and-p-500-companies/main/data/constituents.csv "
            f"-o {args.csv}"
        )

    sp500 = load_sp500(args.csv)

    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    existing_names = cfg.get("watchlist_names", {})
    existing_aliases = cfg.get("watchlist_aliases", {})

    new_watchlist = []
    new_names = {}
    kept, added = 0, 0
    for ticker, name in sp500:
        new_watchlist.append(ticker)
        if ticker in existing_names:
            new_names[ticker] = existing_names[ticker]
            kept += 1
        else:
            new_names[ticker] = name
            added += 1

    cfg["watchlist"] = new_watchlist
    cfg["watchlist_names"] = new_names
    if existing_aliases:
        cfg["watchlist_aliases"] = existing_aliases

    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Updated {CFG}")
    print(f"  Total tickers: {len(new_watchlist)}")
    print(f"  Kept existing name: {kept}")
    print(f"  Added from CSV:     {added}")
    print(f"  Aliases preserved:  {len(existing_aliases)}")


if __name__ == "__main__":
    main()
