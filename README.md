# 🤖 StockSage — Personal AI Trading Agent

**US Markets | $100 Starting Fund | Alpaca Broker | Paper-First**

An autonomous AI trading agent that acts like a 30-year Wall Street veteran — analyzing every signal before risking a cent of your money.

---

## 🚀 Quick Start (5 minutes)

### 1. Run the setup wizard
```bash
cd d:\Ideas\Stock
python setup.py
```

### 2. Get your FREE API keys
| Service | Cost | Link |
|---|---|---|
| **Alpaca** (broker) | Free | [alpaca.markets](https://alpaca.markets) |
| **NewsAPI** (news) | Free tier | [newsapi.org](https://newsapi.org) |
| **Google Gemini** (AI) | Free tier | [aistudio.google.com](https://aistudio.google.com/app/apikey) |

### 3. Add your keys to `.env`
```bash
# Edit .env (copy from .env.example)
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_PAPER=true          ← Keep this TRUE until ready for real money
NEWS_API_KEY=your_key
GOOGLE_API_KEY=your_key
```

### 4. Start paper trading
```bash
python -m agent.main
```

---

## 📁 Project Structure

```
StockSage/
├── agent/
│   ├── main.py                 ← Entry point — run this!
│   ├── config.py               ← Config loader + logger
│   ├── core/
│   │   ├── decision_engine.py  ← Master AI brain (fuses all signals)
│   │   ├── risk_manager.py     ← Position sizing + stop-losses
│   │   └── execution_engine.py ← Alpaca order placement
│   ├── data/
│   │   ├── price_feed.py       ← Real-time prices (Yahoo Finance / Alpaca)
│   │   └── news_feed.py        ← News from 6+ US sources + gov RSS
│   └── models/
│       ├── technical_indicators.py ← EMA, RSI, MACD, VWAP, patterns
│       └── sentiment_model.py      ← FinBERT financial NLP
├── config/
│   └── settings.yaml           ← All your settings (edit this!)
├── database/
│   └── schema.sql              ← PostgreSQL schema
├── logs/                       ← All trade logs saved here
├── .env.example                ← Copy to .env, add your keys
├── setup.py                    ← Run once for first-time setup
└── requirements.txt
```

---

## ⚙️ Key Settings (`config/settings.yaml`)

```yaml
risk:
  total_fund: 100.0          # Your $100
  max_position_pct: 0.15     # Max $15 per trade
  max_daily_loss_pct: 0.05   # Stop if daily loss > $5

execution:
  mode: "paper"              # ← ALWAYS start here!
  # Change to "live" ONLY after 30+ days of profitable paper trading
```

---

## 🧠 How the Agent Thinks

Every 5 minutes during market hours (9:30 AM – 4:00 PM ET):

```
For each stock in watchlist:
  1. Get real-time price
  2. Run technical analysis (EMA, RSI, MACD, VWAP, patterns)
  3. Scan news sentiment (FinBERT NLP on 3 hours of headlines)
  4. Fuse signals: 45% technical + 35% sentiment + 20% fundamental
  5. Check risk limits (position size, daily loss, confidence threshold)
  6. Place fractional share order via Alpaca (e.g., $13 of AAPL)
  7. Log plain-English reasoning for every decision
```

---

## 💰 Why Fractional Shares Matter for $100

With $100, you can't buy a whole share of NVDA (~$130). Alpaca's fractional shares let you buy **$13 worth of NVDA** — same returns, proportionally.

```
$100 Fund Example:
  $20 cash reserve (always)
  $15 AAPL  (fractional)
  $13 NVDA  (fractional)
  $12 MSFT  (fractional)
  $12 GOOGL (fractional)
  $8  AMD   (fractional)  ← free cash for next signal
```

---

## 🛡️ Risk Management

| Rule | Value |
|---|---|
| Max per trade | $15 (15% of fund) |
| Always-on cash reserve | $20 (20%) |
| Auto stop-loss | 2% below entry |
| Trailing stop | 1% (locks in profits) |
| Daily loss limit | $5 (5%) — agent pauses |
| Max open positions | 5 |
| Min confidence to trade | 68% |
| News blackout events | Fed Rate Decision, CPI, Jobs Report |

**Kill switch**: Press `CTRL+C` — agent stops immediately, positions stay open in Alpaca.

---

## 📈 Going Live (After Paper Trading)

Only switch when ALL of these are true for 30+ days:
- ✅ Win rate > 55%
- ✅ Daily loss never hit the circuit breaker
- ✅ Portfolio grew (even small gains count)
- ✅ You understand every decision the agent made

Then in `config/settings.yaml`:
```yaml
execution:
  mode: "live"      # ← Real money now
```
And in `.env`:
```
ALPACA_PAPER=false
```

---

## ⚠️ Important Disclaimer

> This agent is for educational and personal research purposes.
> No AI can guarantee profitable trades. Markets are unpredictable.
> Always paper-trade first. Never risk money you cannot afford to lose.
> Not financial advice.
