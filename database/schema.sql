-- ============================================================
-- StockSage Database Schema
-- Run: psql -U stocksage_user -d stocksage -f schema.sql
-- ============================================================

-- Trades log
CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    order_id        VARCHAR(100) UNIQUE,
    symbol          VARCHAR(20)  NOT NULL,
    action          VARCHAR(10)  NOT NULL,   -- buy | sell
    quantity        DECIMAL(12,4),
    notional        DECIMAL(12,2),           -- dollar amount
    filled_price    DECIMAL(12,4),
    status          VARCHAR(20),
    reason          TEXT,
    -- Decision context
    confidence      DECIMAL(5,4),
    tech_score      DECIMAL(6,4),
    sent_score      DECIMAL(6,4),
    final_score     DECIMAL(6,4),
    reasoning       TEXT,
    -- Timestamps
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    filled_at       TIMESTAMPTZ
);

-- P&L per trade (computed after close)
CREATE TABLE IF NOT EXISTS trade_pnl (
    id              SERIAL PRIMARY KEY,
    buy_trade_id    INT REFERENCES trades(id),
    sell_trade_id   INT REFERENCES trades(id),
    symbol          VARCHAR(20)  NOT NULL,
    buy_price       DECIMAL(12,4),
    sell_price      DECIMAL(12,4),
    quantity        DECIMAL(12,4),
    pnl_usd         DECIMAL(12,4),
    pnl_pct         DECIMAL(8,4),
    hold_duration   INTERVAL,
    closed_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Daily portfolio snapshots
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              SERIAL PRIMARY KEY,
    snapshot_date   DATE NOT NULL,
    total_equity    DECIMAL(12,2),
    cash            DECIMAL(12,2),
    invested        DECIMAL(12,2),
    daily_pnl       DECIMAL(12,2),
    daily_pnl_pct   DECIMAL(8,4),
    open_positions  INT,
    risk_level      VARCHAR(20),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- News articles (for audit trail)
CREATE TABLE IF NOT EXISTS news_articles (
    id              SERIAL PRIMARY KEY,
    article_hash    VARCHAR(64) UNIQUE,
    title           TEXT,
    source          VARCHAR(100),
    url             TEXT,
    published_at    TIMESTAMPTZ,
    sentiment_score DECIMAL(6,4),
    sentiment_label VARCHAR(20),
    category        VARCHAR(30),
    importance      VARCHAR(20),
    affected_symbols TEXT[],
    ingested_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Signal log (every analysis cycle output)
CREATE TABLE IF NOT EXISTS signals (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    action          VARCHAR(10),
    confidence      DECIMAL(5,4),
    price           DECIMAL(12,4),
    tech_action     VARCHAR(10),
    sent_label      VARCHAR(20),
    final_score     DECIMAL(6,4),
    reasoning       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at);
