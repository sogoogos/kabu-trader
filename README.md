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

### Cloud Server Setup (Oracle Cloud / EC2 / etc.)

```bash
# SSH into your server
ssh -i your-key.key opc@your-server-ip

# Clone the repo
git clone https://github.com/YOUR_USERNAME/kabu-trader.git
cd kabu-trader

# Run the setup script (installs Docker, adds swap, sets timezone)
bash scripts/setup-server.sh

# Log out and back in (for Docker group)
exit
ssh -i your-key.key opc@your-server-ip
cd kabu-trader

# Edit config with your credentials
nano config/default.json

# Train ML model
docker compose run --rm kabu-trader train

# Start paper trading (runs 24/7)
docker compose up -d

# Check it's running
docker compose logs -f
```

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

# Start monitor with paper trading (simulated money, no real trades)
python3 -m kabu_trader.cli monitor --paper

# Run a single paper trading cycle
python3 -m kabu_trader.cli monitor --paper --once

# Check paper trading results (works even when monitor is stopped)
python3 -m kabu_trader.cli report

# Reset paper trading state and start fresh
python3 -m kabu_trader.cli report --reset

# Use a custom config file
python3 -m kabu_trader.cli -c config/my_config.json backtest
```

## Paper Trading (Dry Run)

Test the system with virtual money before risking real capital.

```bash
# Start paper trading
python3 -m kabu_trader.cli monitor --paper
```

- Starts with **¥1,000,000** virtual capital
- Simulates trades at real market prices when signals trigger
- Tracks stop loss (-5%) and take profit (+15%) automatically
- Max 5 concurrent positions, 10% of capital per trade
- State saved to `paper_trading/state.json` — survives restarts
- LINE notifications tagged with `🧪 PAPER TEST` to distinguish from live
- Check results any time with `python3 -m kabu_trader.cli report`

Recommended: run for 1-2 weeks before committing real money.

## Strategy: Composite Swing Trading

The system combines 11 indicators into a weighted composite score. Each indicator returns a normalized signal (-1.0 to +1.0), which is multiplied by its **weight** to determine how much influence it has.

### Indicators & Weights

| # | Indicator | What it detects | Default Weight |
|---|---|---|---|
| 1 | SMA Crossover (5/25) | Golden/death cross, trend direction | 1.5 |
| 2 | RSI (14) | Oversold / overbought | 1.5 |
| 3 | MACD | Momentum crossover | 2.0 |
| 4 | Bollinger Bands | Price extremes | 1.0 |
| 5 | Volume Spike | Unusual trading activity | 1.0 |
| 6 | Ichimoku Cloud (一目均衡表) | Trend + support/resistance | 2.5 |
| 7 | Money Flow Index (MFI) | Institutional buying/selling pressure | 2.0 |
| 8 | ADX | Trend strength confirmation | 2.0 |
| 9 | Relative Strength vs Nikkei | Outperformance vs market | 2.5 |
| 10 | ML Model (Gradient Boosting) | Predicted probability of +3% in 5 days | 3.0 |
| 11 | LLM News Sentiment (GPT) | News headline analysis | 2.5 |

Default weights are based on ML feature importance analysis. Higher weight = more influence on the final score.

### Customizing Weights

Edit `indicator_weights` in `config/default.json` to tune. Set a weight to `0` to disable an indicator entirely:

```json
"indicator_weights": {
  "sma": 1.5,
  "rsi": 1.5,
  "macd": 2.0,
  "bollinger": 1.0,
  "volume": 1.0,
  "ichimoku": 2.5,
  "mfi": 2.0,
  "adx": 2.0,
  "relative_strength": 2.5,
  "ml": 3.0,
  "sentiment": 2.5
}
```

### ML Model (auto-retrains weekly)

- **35 engineered features**: multi-timeframe returns, momentum, volatility regime, volume patterns, candlestick structure, mean reversion z-scores
- **Walk-forward validation**: trains on past, tests on future — never sees future data
- **Auto-retrains** every Sunday night at 20:00 JST during `monitor`
- Or manually: `python3 -m kabu_trader.cli train`

### LLM News Sentiment

Uses OpenAI (GPT-4o-mini) to analyze news headlines:

- Refreshes hourly + checks for breaking news every 60 seconds
- Breaking news triggers instant LINE notification if sentiment score >= 4
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
- **STRONG BUY** when composite score >= 7 (LINE notification sent)
- **SELL** when composite score <= -4
- **STRONG SELL** when composite score <= -7 (LINE notification sent)

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
