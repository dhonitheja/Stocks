#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io, os, subprocess, shutil

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def banner(text):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")

def step(n, text):
    print(f"\n[{n}] {text}")


banner("StockSage -- Setup Wizard")
print("US Markets | $100 Starting Fund | Alpaca Broker")

# Step 1: Python check
step(1, "Checking Python version...")
if sys.version_info < (3, 10):
    print(f"  FAIL: Python 3.10+ required. You have: {sys.version}")
    sys.exit(1)
print(f"  OK: Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

# Step 2: .env file
step(2, "Setting up .env file...")
if not os.path.exists(".env"):
    shutil.copy(".env.example", ".env")
    print("  Created .env from .env.example")
    print("  IMPORTANT: Edit .env and add your API keys before running the agent!")
    print()
    print("  Required keys:")
    print("    ALPACA_API_KEY    -> https://alpaca.markets (free signup)")
    print("    ALPACA_SECRET_KEY -> (from Alpaca dashboard)")
    print("    NEWS_API_KEY      -> https://newsapi.org (free tier)")
    print("    GOOGLE_API_KEY    -> https://aistudio.google.com/app/apikey (free)")
else:
    print("  OK: .env already exists")

# Step 3: Install dependencies
step(3, "Installing Python dependencies (this may take 2-3 minutes)...")
try:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"])
    print("  OK: Core dependencies installed")
except subprocess.CalledProcessError as e:
    print(f"  WARN: pip install had issues: {e}")

# Step 4: Alpaca SDK
step(4, "Installing Alpaca SDK (alpaca-py)...")
try:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "alpaca-py", "-q"])
    print("  OK: alpaca-py installed")
except Exception as e:
    print(f"  WARN: alpaca-py install issue: {e}")

# Step 5: Directories
step(5, "Creating project directories...")
dirs = ["logs/trades", "logs/reports", "backtesting/data", "backtesting/results"]
for d in dirs:
    os.makedirs(d, exist_ok=True)
print(f"  OK: {len(dirs)} directories created")

# Step 6: Verify imports
step(6, "Verifying key package imports...")
checks = [
    ("yfinance", "Yahoo Finance data"),
    ("feedparser", "RSS news parser"),
    ("ta", "Technical indicators"),
    ("loguru", "Logger"),
    ("rich", "Terminal UI"),
    ("fastapi", "API server"),
    ("yaml", "Config loader"),
]
all_ok = True
for module, desc in checks:
    try:
        __import__(module)
        print(f"  OK: {desc} ({module})")
    except ImportError:
        print(f"  MISSING: {desc} ({module})")
        all_ok = False

if not all_ok:
    print("\n  Some packages missing. Run: pip install -r requirements.txt")

# Step 7: Live data test
step(7, "Testing Yahoo Finance data feed (no API key needed)...")
try:
    import yfinance as yf
    ticker = yf.Ticker("AAPL")
    info = ticker.fast_info
    price = getattr(info, 'last_price', None) or getattr(info, 'regularMarketPrice', 0)
    if price and float(price) > 0:
        print(f"  OK: AAPL current price = ${float(price):.2f}")
    else:
        print("  WARN: Could not fetch live price (market may be closed, data still works)")
except Exception as e:
    print(f"  WARN: Yahoo Finance test: {e}")

# Done
banner("Setup Complete!")
print("""
Next steps:
  1. Edit .env and add your FREE API keys:
       Alpaca  -> https://alpaca.markets
       NewsAPI -> https://newsapi.org
       Gemini  -> https://aistudio.google.com/app/apikey

  2. Settings are pre-configured for $100 USD, US markets.
     Review: config/settings.yaml

  3. Start paper trading (no real money):
       python -m agent.main

  4. Watch it trade for 30+ days before switching to live mode.

  REMINDER: execution.mode is set to 'paper' in settings.yaml.
  Your $100 is safe until you explicitly switch to live mode.
""")
