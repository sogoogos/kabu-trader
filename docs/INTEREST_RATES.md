# Interest rates in the strategy

**Status:** shipped 2026-09-09 as a low-weight sector tilt, off by default
(`indicator_weights.rates = 0`). Enable at 1.5 on JP paper first; live picks it
up only when its config sets the weight.

## What is used

| Input | Source | Cadence | Notes |
|---|---|---|---|
| JGB 2y, 10y | MOF `jgbcm.csv` (current month) + `data/jgbcm_all.csv` (history since 1974) | Daily after the TSE close | Era dates (`R8.9.1`) converted in `kabu_trader/rates.py`. History file (~1 MB) cached in `state_dir/jgb_history.csv` for 7 days. |
| US 10y | Yahoo `^TNX` via `DataFetcher` | Daily on the US close | Respects the yfinance rate-limit breaker. |

`RatesTracker.refresh()` returns `{jgb2y, jgb10y, us10y}` → `{level, date,
chg_5d_bp, chg_20d_bp}`. Every source is fail-open: a failure drops that key
and the scorer stays silent.

Where it shows up:

- `report` → Market panel, `Rates: JGB 2y 1.85% / 10y 2.90% (+8bp/20d) | US 10y 4.81% (+11bp/20d)`
- monthly notification → `Market now:` line
- monitor log once a day, and the `rates` scorer's reason string on signals

## The scorer (`_score_rates`)

Fires only when **JGB 10y 20-session change ≥ `rates_tilt_rise_bp`** (15bp).
Magnitude is 0.5 at the threshold, 1.0 at twice it. Sign comes from
`sector_groups.RATE_SENSITIVITY`: megabanks +1, real estate −1. Nothing else
is scored: not falling rates, not the US 10y, not the index.

## Why so narrow — the evidence

The obvious design ("rates spike → suppress buys") was tested first and
rejected. All tests: N225 or sector-basket 5-day forward return (the bot's
median hold), conditioned on the yield change observed the prior day. Yield
changes in bp; MOF and Yahoo data.

### Index-level gates: sign is not stable across periods

JGB 10y 20-day change ≥ +20bp, N225 5-day forward return, mean / win rate:

| Period | Gate fires | Fwd 5d when firing | Fwd 5d otherwise |
|---|---|---|---|
| 2000–2007 | 6.6% | +0.10% / 53% | −0.05% / 52% |
| 2008–2012 | 3.3% | +0.44% / 62% | −0.09% / 51% |
| 2013–2019 | 1.3% | **−2.28% / 35%** | +0.30% / 58% |
| 2020–2022 | 0.7% | **−1.25% / 20%** | +0.13% / 53% |
| 2023–2026 | 9.8% | **+1.40% / 67%** | +0.38% / 57% |
| 2000–2026 | 4.4% | +0.33% / 56% | +0.11% / 54% |

US 10y 20-day change ≥ +30bp:

| Period | Gate fires | Fwd 5d when firing | Fwd 5d otherwise |
|---|---|---|---|
| 2000–2007 | 10.9% | −0.53% / 43% | +0.09% / 54% |
| 2008–2012 | 12.0% | +0.28% / 63% | −0.13% / 49% |
| 2013–2019 | 5.4% | +0.57% / 71% | +0.23% / 57% |
| 2020–2022 | 16.3% | −0.60% / 41% | +0.37% / 56% |
| 2023–2026 | 12.3% | −0.15% / 47% | +0.67% / 60% |
| 2000–2026 | 10.5% | −0.16% / 51% | +0.20% / 55% |

A rate-trend variant (10y above its own 50-day MA) was indistinguishable from
noise in every period (fwd 5d +0.15% vs +0.15% over 2000–2026 for JGB).

Reading: the same rate move means different things depending on *why* rates
moved (reflation vs. inflation shock vs. BOJ tightening), and a level/change
rule cannot tell them apart. In 2023–2026 specifically, a JGB gate would have
suppressed buys in the best weeks. Fitting a threshold to the last three
years, which include the COVID and post-YCC regimes, would be curve-fitting.

### Sector tilt: the one relationship that held

Sector basket 5-day excess return vs N225 when the JGB 10y rose ≥ 15bp over 20
days (baskets: 8306/8316/8411; 8801/8802/8830):

| Period | Megabanks (up / neutral) | Real estate (up / neutral) |
|---|---|---|
| 2000–2007 | +1.65% / +0.19% | −0.31% / +0.59% |
| 2008–2012 | +1.16% / −0.12% | −0.33% / +0.23% |
| 2013–2019 | +0.51% / −0.10% | −1.21% / −0.11% |
| 2020–2022 | +2.64% / +0.22% | −0.29% / −0.11% |
| 2023–2026 | +0.14% / +0.52% | +0.35% / −0.07% |
| 2000–2026 | **+0.97% / +0.09%** | **−0.16% / +0.16%** |

Banks outperformed on rising yields in every period but the latest (where the
whole market rallied with yields); real estate lagged in four of five.
Correlations are small (+0.04 to +0.16) — this is an event-conditional tilt,
not a linear factor — hence the low suggested weight.

Tested and **not** included because the sign flipped between periods:
utilities, telecom, non-life insurers; the response to *falling* yields for
every sector; the US 10y as the driver of the tilt (only 2023–2026 showed it);
the JGB 2y.

## Config

```json
"rates_us_ticker": "^TNX",
"rates_tilt_rise_bp": 15,
"indicator_weights": { "rates": 1.5 }
```

Set `rates_tilt_rise_bp` to `null` or the weight to `0` to disable. The yield
display in `report` stays on regardless.

## Rollout

1. JP paper: `"rates": 1.5` in the EC2 paper config; watch bank/real-estate
   entries for a few rate moves (a ≥15bp/20d JGB rise happens ~15% of days
   since 2023, ~5% since 2000).
2. Live only after paper shows the tilt is not increasing churn.
3. US config: leave at 0 — `sector_groups` covers TSE names only, so the
   scorer is silent there anyway.

Reproduce the tables: the calibration scripts are not committed; they are a
40-line pandas join of the MOF CSV, `^TNX` and `^N225` from yfinance.
