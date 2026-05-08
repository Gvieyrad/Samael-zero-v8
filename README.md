# Samael Zero v8

Mean reversion trading bot on volume spikes — Binance paper trading.

## Strategy

1. Detects 1m candles with volume spike ≥ 10x rolling median
2. Checks 62-candle (1h) price change in [4–6%] range  
3. Enters mean reversion: LONG on drops, SHORT on pumps
4. Exits at SL=0.2%, TP=1.2%, or after 15 min (TIME exit)

## Filters

- **ATR Regime Filter (v8)**: Skips all signals when BTC fast_ATR(5) / slow_ATR(50) > 1.5 — detects trending/expanding volatility markets where mean reversion fails
- **v7 SHORT Filter**: Skips SHORT if BTC 1h is up OR 3 consecutive green candles
- **Hour Filter**: Skips UTC hours {1, 2, 6, 7, 10, 12, 21, 23}
- **Cooldown**: 20 min per pair after any trade

## Config

| Parameter | Value |
|---|---|
| Capital | $500 (paper) |
| Pairs | 17 (DOGE/TRUMP/NEAR/WLFI/XRP/BNB/DOGS/TST/PENGU/CHIP/ADA/TON/SUI/AVAX/NOT/LTC/BTC) |
| LONG risk | 35% (~$175/trade) |
| SHORT risk | 15% (~$75/trade) |
| SL | 0.2% |
| TP | 1.2% |
| Max positions | 7 |

## Backtest (2 years: 2024 + 2025-26)

| Metric | Value |
|---|---|
| APR | +7.1% |
| WR | ~18% |
| Sharpe | ~2.3 |
| Max Drawdown | ~3% |
| Break-even WR | 14.3% |

## Setup

```bash
cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
pip install requests python-dotenv
mkdir -p logs data
nohup python3 samael_zero_v4.py >> logs/samael_zero_v4.log 2>&1 &
```

## Files

- `samael_zero_v4.py` — main bot
- `data/samael_zero_v4.db` — SQLite DB (trades, equity, positions, signals_log)
- `logs/samael_zero_v4.log` — runtime log

## Stop Criteria (pre-committed)

- Stop if in any 100-trade window: WR ≤ 14% OR SL rate ≥ 88% OR MaxDD ≥ 8%
- Scale to real capital if after 150+ trades: WR ≥ 16% sustained, MaxDD < 5%, PF ≥ 1.10
