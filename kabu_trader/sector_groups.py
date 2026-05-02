"""Static sector / peer-group map for Japanese equities.

Used by the strategy's `sector_spillover` scorer: when several peers in a group
have just reported earnings with a strong directional gap, anticipate a similar
move in the unreported peers.

Curated rather than fetched (yfinance sector data is noisy + costs API calls).
Only includes groups with documented high earnings/price correlation. Adding
loosely-related names dilutes the signal.
"""

from __future__ import annotations


# sector_id -> list of TSE tickers (with .T suffix for yfinance compatibility)
SECTOR_GROUPS: dict[str, list[str]] = {
    # 5大商社 — driven by 資源価格, 為替, overlapping investments
    "trading_companies": ["8058.T", "8031.T", "8001.T", "8053.T", "8002.T"],

    # 3大メガバンク — driven by 金利, 国債利回り, 不良債権 trends
    "megabanks": ["8306.T", "8316.T", "8411.T"],

    # 海運大手3社 — extremely high correlation via container & bulk shipping rates
    "shipping": ["9101.T", "9104.T", "9107.T"],

    # 3大損保 — 自然災害, 保険料率, 政策保有株
    "non_life_insurance": ["8766.T", "8725.T", "8630.T"],

    # 鉄鋼大手 — 鋼材価格, 原料炭, 中国需要
    "steel": ["5401.T", "5411.T", "5406.T"],

    # 自動車大手 — correlated via USD/JPY, 北米販売, EV戦略
    "autos": ["7203.T", "7267.T", "7201.T", "7269.T", "7261.T", "7270.T"],

    # 電力大手 — 燃料価格, 原発再稼働, 規制
    "electric_utilities": ["9501.T", "9503.T", "9502.T", "9508.T"],

    # 通信3大キャリア — 通信料金規制, 5G投資
    "telecom": ["9432.T", "9433.T", "9434.T"],

    # 石油元売 — highly correlated to 原油価格 + 為替
    "oil": ["5020.T", "5019.T", "5021.T"],

    # 半導体製造装置 — global semiconductor capex cycle
    "semiconductor_equipment": ["8035.T", "6857.T", "7735.T", "6146.T"],

    # ゲーム大手 — lower correlation (hit-driven) but shared console/platform cycle
    "gaming": ["7974.T", "6758.T", "7832.T", "9697.T", "9684.T"],

    # 不動産大手 — 金利, オフィス需要, 都心地価
    "real_estate": ["8801.T", "8802.T", "8830.T", "3289.T"],

    # 重工大手3社 — 防衛予算, 原発再稼働, 航空需要; move as a monolith on geopolitics
    "heavy_machinery_defense": ["7011.T", "7012.T", "7013.T"],

    # 電子部品 (MLCC / Apple supply chain) — global smartphone cycle + USD/JPY
    "electronic_components": ["6981.T", "6762.T", "6976.T"],

    # JR大手 — domestic travel volume, inbound tourism, regional stability
    # NOTE: 9020 (JR東日本) was dropped from JPX-Nikkei 400 — kept here so the
    # sector group is correct if/when it returns to the watchlist.
    "railways": ["9020.T", "9022.T", "9021.T"],

    # コスメ・インバウンド — Chinese macro, duty-free, inbound tourism
    # NOTE: 4922 (コーセー) and 4927 (ポーラオルビス) are not in the current
    # JPX-Nikkei 400 watchlist; keeping them so 4911 (資生堂) gets correct peers
    # if those tickers are later added.
    "cosmetics_inbound": ["4911.T", "4922.T", "4927.T"],

    # 高成長SaaS — long-rate sensitivity (JGB 10y), growth-multiple compression
    # NOTE: 3959, 4478, 4443 are smaller-cap names not in JPX-Nikkei 400.
    # Group exists for future watchlist expansion.
    "saas_growth": ["3959.T", "4478.T", "4443.T"],
}


# Reverse lookup: ticker -> sector_id
TICKER_TO_SECTOR: dict[str, str] = {
    ticker: sector
    for sector, tickers in SECTOR_GROUPS.items()
    for ticker in tickers
}


def get_peers(ticker: str) -> list[str]:
    """Return peer tickers in the same sector as `ticker` (excluding itself).

    Returns [] if the ticker isn't in any tracked sector.
    """
    sector = TICKER_TO_SECTOR.get(ticker)
    if not sector:
        return []
    return [t for t in SECTOR_GROUPS[sector] if t != ticker]
