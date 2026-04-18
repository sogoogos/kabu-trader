# Kabu Trader

Japanese stock swing trading system with real-time monitoring, signal scanning, and backtesting.

## Features

- **Backtest** - Test trading strategies against historical data with detailed performance metrics
- **Scan** - Scan stocks for current buy/sell signals
- **Monitor** - Real-time price monitoring with alerts during Tokyo market hours (9:00-15:00 JST)
- **LINE Alerts** - Get buy/sell signals sent to your phone via LINE (free)

## Setup

### Option A: Local (Python)

```bash
# Create a virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate    # macOS / Linux
# venv\Scripts\activate     # Windows

# Install dependencies
pip install yfinance pandas numpy rich scikit-learn openai
```

### Option B: Docker (recommended for cloud / 24/7 operation)

```bash
# Copy the example config and fill in your credentials
cp config/default.example.json config/default.json

# Build and start (runs paper trading monitor by default)
docker compose up -d

# Check logs
docker compose logs -f

# Check paper trading report
docker compose exec kabu-trader python -m kabu_trader.cli report

# Run a scan
docker compose exec kabu-trader python -m kabu_trader.cli scan

# Train ML model
docker compose exec kabu-trader python -m kabu_trader.cli train

# Stop
docker compose down
```

Config, paper trading state, and trained models are mounted as volumes and persist across container restarts. The container auto-restarts after crashes or server reboots.

## Usage

```bash
# Analyze news sentiment for all stocks (requires OpenAI API key)
python3 -m kabu_trader.cli sentiment

# Analyze specific stocks
python3 -m kabu_trader.cli sentiment -t 7203.T 9984.T

# Train the ML model (do this first for best results)
python3 -m kabu_trader.cli train

# Train with more history / specific stocks
python3 -m kabu_trader.cli train -d 1000
python3 -m kabu_trader.cli train -t 7203.T 6758.T 9984.T

# Backtest all watchlist stocks (1 year)
python3 -m kabu_trader.cli backtest

# Backtest specific stocks
python3 -m kabu_trader.cli backtest -t 7203.T 6758.T

# Backtest with longer history and trade details
python3 -m kabu_trader.cli backtest -d 730 -v

# Scan watchlist for current signals
python3 -m kabu_trader.cli scan

# Scan specific stocks
python3 -m kabu_trader.cli scan -t 7203.T 9984.T

# Start real-time monitor (loops during market hours)
python3 -m kabu_trader.cli monitor

# Run a single monitoring cycle
python3 -m kabu_trader.cli monitor --once

# Use a custom config file
python3 -m kabu_trader.cli -c config/my_config.json backtest
```

## Strategy: Composite Swing Trading

The system combines 9 technical indicators into a composite score. Each indicator contributes a partial score, and the total determines the signal.

### Traditional Indicators

| Indicator | Buy Signal | Sell Signal | Max Score |
|---|---|---|---|
| SMA Crossover (5/25) | Golden cross / uptrend | Death cross / downtrend | +/-2 |
| RSI (14) | Oversold (< 30) | Overbought (> 70) | +/-2 |
| MACD | Bullish crossover | Bearish crossover | +/-2 |
| Bollinger Bands | Price near/below lower band | Price near/above upper band | +/-2 |
| Volume | Spike on up day | Spike on down day | +/-1 |

### Advanced Indicators

| Indicator | What it measures | Why it matters | Max Score |
|---|---|---|---|
| Ichimoku Cloud (一目均衡表) | Trend, support/resistance, momentum | Designed for Japanese stocks; used by institutional traders in Japan | +/-2 |
| Money Flow Index (MFI) | Volume-weighted buying/selling pressure | Detects institutional money flowing in/out — more reliable than RSI alone | +/-2 |
| ADX | Trend strength (not direction) | Filters out choppy sideways markets where swing trading loses money | +/-1 |
| Relative Strength vs Nikkei 225 | Stock performance vs the market | A stock rising 2% in a 5% market is actually weak — this catches that | +/-2 |

### ML Model (10th indicator)

A Gradient Boosting classifier trained on historical data adds a powerful prediction layer:

- **35 engineered features**: multi-timeframe returns, momentum, volatility regime, volume patterns, candlestick structure, mean reversion z-scores, and all technical indicator values
- **Target**: Predicts probability of +3% price increase within 5 trading days
- **Validation**: Walk-forward evaluation (train on past, test on future) — never sees future data
- **Score contribution**: Up to +/-3 points based on prediction confidence

Top features by importance: ATR%, volatility, relative strength vs Nikkei, Ichimoku cloud thickness, momentum.

Run `python3 -m kabu_trader.cli train` to train the model. Once trained, it is automatically loaded by `scan`, `backtest`, and `monitor`.

### LLM News Sentiment (11th indicator)

Uses OpenAI (GPT-4o-mini) to analyze recent news headlines for each stock:

- Fetches news from Yahoo Finance for each stock
- Sends headlines to GPT with a structured prompt
- Returns a sentiment score (-5 to +5) with confidence and reasoning
- Score contribution: up to +/-3 points based on sentiment strength
- Results are cached for 1 hour to avoid redundant API calls
- Cost: ~$0.10-0.30/day for 109 stocks

Configure in `config/default.json`:
```json
"llm_sentiment": {
  "enabled": true,
  "api_key": "sk-...",
  "model": "gpt-4o-mini"
}
```

Or use environment variable: `export OPENAI_API_KEY="sk-..."`

### Signal Thresholds

- **BUY** when composite score >= 4
- **STRONG BUY** when composite score >= 7
- **SELL** when composite score <= -4
- **STRONG SELL** when composite score <= -7

## Risk Management

| Parameter | Default | Description |
|---|---|---|
| `initial_capital` | 1,000,000 | Starting capital in JPY |
| `position_size_pct` | 0.10 | 10% of capital per trade |
| `max_positions` | 5 | Maximum concurrent open positions |
| `stop_loss_pct` | 0.05 | -5% stop loss |
| `take_profit_pct` | 0.15 | +15% take profit |
| `commission_rate` | 0.001 | 0.1% commission per trade |

## Default Watchlist

109 major Japanese stocks across 15 sectors including automotive, electronics/semiconductors, banking/finance, pharma, trading companies, heavy industry, food/beverage, retail, energy, gaming, and more.

See `config/default.json` for the full list. Edit it to customize the watchlist, strategy parameters, and risk management settings.

## Configuration

All settings live in `config/default.json`. Key sections:

- **watchlist** - Array of ticker symbols (use `.T` suffix for Tokyo Stock Exchange)
- **strategy.params** - Indicator periods and thresholds
- **backtest** - Capital, position sizing, stop loss / take profit
- **monitor** - Polling interval and trading hours
- **line** - LINE Messaging API alert settings

## LINE Alerts Setup

Alerts are sent via LINE Messaging API (free tier: 200 messages/month). No extra packages needed.

### 1. Create a LINE Official Account & Messaging API Channel

1. Go to [LINE Developers Console](https://developers.line.biz/console/)
2. Log in with your LINE account
3. Create a **Provider** (any name, e.g., "Kabu Trader")
4. Create a **Messaging API Channel** under that provider
5. On the channel page, go to the **Messaging API** tab
6. Issue a **Channel Access Token** (long-lived) at the bottom of the page

### 2. Get your User ID

On the same channel page, go to the **Basic settings** tab. Your **User ID** is listed there (starts with `U`).

### 3. Add the bot as a friend

Scan the QR code shown on the **Messaging API** tab with your LINE app. You must be friends with the bot to receive messages.

### 4. Configure

**Option A: Edit `config/default.json`**

```json
"line": {
  "enabled": true,
  "channel_access_token": "your_channel_access_token",
  "user_id": "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

**Option B: Use environment variables** (recommended for security)

Set `"enabled": true` in config, then:

```bash
export LINE_CHANNEL_ACCESS_TOKEN="your_channel_access_token"
export LINE_USER_ID="Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### How it works

- LINE message is sent automatically when the monitor detects a BUY or SELL signal
- Each signal is sent only once per day per stock (no duplicate messages)
- Alerts work in both `monitor` and `monitor --once` modes

## Project Structure

```
kabu-trader/
  config/
    default.json          # Default configuration
  kabu_trader/
    __init__.py
    cli.py                # CLI entry point
    data_fetcher.py       # Stock data fetching via yfinance
    indicators.py         # Technical indicators (SMA, RSI, MACD, BB, Ichimoku, MFI, ADX, RS)
    ml_features.py        # ML feature engineering (35 features)
    ml_model.py           # Gradient Boosting model training and prediction
    llm_sentiment.py      # LLM-based news sentiment analysis (OpenAI)
    news_fetcher.py       # News headline fetcher via yfinance
    strategy.py           # Swing trading strategy engine
    backtester.py         # Backtesting engine with performance metrics
    monitor.py            # Real-time price monitor with alerts
    notifier.py           # LINE notifications via Messaging API
  models/
    default.pkl           # Trained ML model (generated by train command)
```

## Disclaimer

This software is for educational and research purposes only. It does not constitute financial advice. Trading stocks involves risk, and past performance does not guarantee future results. Always do your own research before making investment decisions.
