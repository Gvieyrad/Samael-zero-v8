#!/usr/bin/env python3
"""Samael Zero v4 — Mean Reversion on Volume Spikes.

Strategy: mean reversion on volume spikes.
  Pairs: DOGE/XRP/SUI (retail meme coins with reversion tendency)
  1. Volume spike >= 3x rolling median on last CLOSED 1m candle
  2. Prior 1h move >= 2% (pre_trend filter)
  3. Fade the move (mean reversion): SHORT if up, LONG if down
  4. SL=0.3%, TP=0.7%, max hold=30min, cooldown=30min/pair
  Fees: 0.14% RT deducted from every trade PnL

No ML. No indicators. No order book. Just volume + mean reversion.
"""
import sys
import os
import time
import logging
import sqlite3
import math
import requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/noc/samaelalpha')
os.chdir('/home/noc/samaelalpha')

# ── Config ────────────────────────────────────────────────────────────────────
CAPITAL = 500
LONG_RISK = 0.35           # 35% per LONG trade (~$175)
SHORT_RISK = 0.15          # 15% per SHORT trade (~$75)
MAX_POSITIONS = 7
PAIRS = {
    # v5 config (2026-05-05): vol_mult=10 (solo spikes extremos, 48%WR), pre_trend=3.0%
    # Removidos: ZECUSDT/DASHUSDT/LUNCUSDT/ETHUSDT (0-20%WR en live), FILUSDT, CHZUSDT
    'DOGEUSDT':   {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'TRUMPUSDT':  {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'NEARUSDT':   {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'WLFIUSDT':   {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'XRPUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'BNBUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'DOGSUSDT':   {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 30},
    'TSTUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'PENGUUSDT':  {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'CHIPUSDT':   {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'ADAUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'TONUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'SUIUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'NOTUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'LTCUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'SOLUSDT':    {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
    'OPUSDT':     {'exit_min': 15, 'pre_trend_pct': 3.0, 'vol_mult': 10},
}
SL_PCT = 0.2                # live-validated: 0.1% liquidado por spread, 0.2% ok
TP_PCT = 1.2                # live-validated: 43-48% WR con esto vs 9% WR con 2.0%
VOL_WINDOW = 100            # Rolling median window (candles)
CHECK_SECONDS = 15          # Check every 15s (need to catch 1m spikes)
COOLDOWN_MINUTES = 20       # optimizado: 20min > 30min (APR +3.5%, MaxDD -0.2%)       # Min time between trades on same pair
SKIP_HOURS = {1, 2, 6, 7, 10, 12, 18, 20, 21, 23}  # Optimizado 90d backtest: bloquea horas perdedoras, libera 09h UTC
DAILY_LOSS_LIMIT = 0.03     # Circuit breaker: halt entries if daily loss > 3% capital
LONG_TREND_MIN  = 4.0           # LONG: necesita caida >= 4% (dumps pequeños continúan)
LONG_TREND_MAX  = 6.0           # LONG: cap en 6%
SHORT_TREND_MIN = 3.0           # SHORT: cualquier pump >= 3% revierte rápido
SHORT_TREND_MAX = 6.0           # SHORT: cap en 6%

DB_PATH = '/home/noc/samaelalpha/data/samael_zero_v4.db'
LOG_PATH = '/home/noc/samaelalpha/logs/samael_zero_v4.log'

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    handlers=[logging.FileHandler(LOG_PATH)]  # no StreamHandler: nohup redirects stdout
)
log = logging.getLogger('zero_v4')

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, side TEXT, entry_price REAL, quantity REAL,
        entry_time TEXT, stop_loss REAL, take_profit REAL,
        max_hold_min INTEGER, pre_trend_pct REAL, vol_ratio REAL,
        status TEXT DEFAULT 'open')""")
    db.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, side TEXT, entry_price REAL, exit_price REAL,
        quantity REAL, pnl REAL, pnl_pct REAL,
        pre_trend_pct REAL, vol_ratio REAL,
        entry_time TEXT, exit_time TEXT, exit_reason TEXT,
        hold_minutes REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, balance REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS signals_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, side TEXT,
        price REAL, pre_trend_pct REAL, vol_ratio REAL,
        traded INTEGER DEFAULT 0, skip_reason TEXT)""")
    db.commit()
    if db.execute('SELECT COUNT(*) FROM equity').fetchone()[0] == 0:
        db.execute('INSERT INTO equity (timestamp, balance) VALUES (?, ?)',
                   (datetime.now(timezone.utc).isoformat(), CAPITAL))
        db.commit()
    return db


def get_balance(db):
    row = db.execute('SELECT balance FROM equity ORDER BY id DESC LIMIT 1').fetchone()
    return row[0] if row else CAPITAL


def get_open_positions(db):
    cur = db.execute("SELECT * FROM positions WHERE status=\"open\""); rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


# ── Market Data ───────────────────────────────────────────────────────────────
def fetch_recent_klines(symbol, limit=250):
    """Fetch recent 1m klines from Binance."""
    url = 'https://api.binance.com/api/v3/klines'
    r = requests.get(url, params={'symbol': symbol, 'interval': '1m', 'limit': limit}, timeout=10)
    data = r.json()
    return data


def get_current_price(symbol):
    """Get current price from Binance."""
    r = requests.get('https://api.binance.com/api/v3/ticker/price',
                     params={'symbol': symbol}, timeout=5)
    return float(r.json()['price'])


# ── Signal Detection ──────────────────────────────────────────────────────────
def detect_signal(klines, config):
    """Detect volume spike + pre-trend for mean reversion signal.

    Returns ('LONG', vol_ratio, pre_trend) or ('SHORT', vol_ratio, pre_trend) or None.
    """
    if len(klines) < config['vol_mult'] + 70:
        return None

    # Use only CLOSED candles — klines[-1] is the current forming candle (partial volume)
    closed = klines[:-1]
    if len(closed) < 70:
        return None
    volumes = [float(k[5]) for k in closed]
    closes = [float(k[4]) for k in closed]

    # Last CLOSED candle volume vs rolling median of prior candles
    current_vol = volumes[-1]
    window = min(VOL_WINDOW, len(volumes) - 1)
    recent_vols = volumes[-(window + 1):-1]
    if not recent_vols:
        return None
    n = len(recent_vols)
    sv = sorted(recent_vols)
    median_vol = (sv[n // 2 - 1] + sv[n // 2]) / 2 if n % 2 == 0 else sv[n // 2]

    if median_vol <= 0:
        return None

    vol_ratio = current_vol / median_vol
    if vol_ratio < config['vol_mult']:
        return None

    # Hour filter: skip NY open hours (mean-reversion fails in directional sessions)
    candle_hour = datetime.fromtimestamp(closed[-1][0] / 1000, tz=timezone.utc).hour
    if candle_hour in SKIP_HOURS:
        return None

    # Pre-trend: 1h change (60 candles back) using closed candles
    if len(closes) < 61:
        return None
    pre_trend = (closes[-1] - closes[-61]) / closes[-61] * 100

    # 4h filter removed — not needed with 3% pre-trend threshold (sweep validated)

    # Mean reversion: LONG y SHORT con rangos optimizados por separado
    if pre_trend < -LONG_TREND_MIN and abs(pre_trend) <= LONG_TREND_MAX:
        return 'LONG', vol_ratio, pre_trend
    elif pre_trend > SHORT_TREND_MIN and abs(pre_trend) <= SHORT_TREND_MAX:
        return 'SHORT', vol_ratio, pre_trend

    return None


# ── Position Management ───────────────────────────────────────────────────────
def open_position(db, symbol, side, price, vol_ratio, pre_trend, config):
    bal = get_balance(db)
    # Dynamic sizing: spikes >= 30x → +50% size, >= 20x → +25% size (backtest v9: +7.56/7y)
    base_risk = LONG_RISK if side == "LONG" else SHORT_RISK
    if vol_ratio >= 30:
        risk = min(base_risk * 1.5, 0.60)
    elif vol_ratio >= 20:
        risk = min(base_risk * 1.25, 0.50)
    else:
        risk = base_risk
    size_usd = bal * risk
    qty = size_usd / price

    if side == 'LONG':
        sl = price * (1 - SL_PCT / 100)
        tp = price * (1 + TP_PCT / 100)
    else:
        sl = price * (1 + SL_PCT / 100)
        tp = price * (1 - TP_PCT / 100)

    db.execute(
        'INSERT INTO positions (symbol,side,entry_price,quantity,entry_time,stop_loss,take_profit,max_hold_min,pre_trend_pct,vol_ratio) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (symbol, side, price, qty, datetime.now(timezone.utc).isoformat(),
         sl, tp, config['exit_min'], pre_trend, vol_ratio))
    db.commit()

    log.info('OPEN %s %s @ %.2f | SL=%.2f TP=%.2f | vol=%.1fx trend=%+.2f%% | hold<=%dm',
             side, symbol, price, sl, tp, vol_ratio, pre_trend, config['exit_min'])


def close_position(db, pos, exit_price, reason):
    entry = pos['entry_price']
    qty = pos['quantity']

    if pos['side'] == 'LONG':
        pnl = (exit_price - entry) * qty
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl = (entry - exit_price) * qty
        pnl_pct = (entry - exit_price) / entry * 100
    fee = entry * qty * 0.0014  # 0.07% per side x2
    pnl -= fee
    pnl_pct = pnl / (entry * qty) * 100  # net pnl_pct after fees

    entry_dt = datetime.fromisoformat(pos['entry_time'])
    hold_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60

    db.execute('UPDATE positions SET status="closed" WHERE id=?', (pos['id'],))
    db.execute(
        'INSERT INTO trades (symbol,side,entry_price,exit_price,quantity,pnl,pnl_pct,pre_trend_pct,vol_ratio,entry_time,exit_time,exit_reason,hold_minutes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (pos['symbol'], pos['side'], entry, exit_price, qty,
         round(pnl, 4), round(pnl_pct, 4), pos['pre_trend_pct'], pos['vol_ratio'],
         pos['entry_time'], datetime.now(timezone.utc).isoformat(), reason, round(hold_min, 1)))

    bal = get_balance(db) + pnl
    db.execute('INSERT INTO equity (timestamp, balance) VALUES (?, ?)',
               (datetime.now(timezone.utc).isoformat(), bal))
    db.commit()

    icon = '+' if pnl > 0 else ''
    log.info('CLOSE %s %s %.2f->%.2f | %s%.2f (%s%.2f%%) | %s | %.0fm | Bal=$%.2f',
             pos['side'], pos['symbol'], entry, exit_price,
             icon, pnl, icon, pnl_pct, reason, hold_min, bal)
    return pnl


def check_exits(db, positions):
    """Check SL, TP, and time exits for all positions. Returns total PnL closed."""
    now = datetime.now(timezone.utc)
    total_pnl = 0.0
    for pos in positions:
        try:
            price = get_current_price(pos['symbol'])
            entry_dt = datetime.fromisoformat(pos['entry_time'])
            age_min = (now - entry_dt).total_seconds() / 60

            # Stop loss
            if pos['side'] == 'LONG' and price <= pos['stop_loss']:
                total_pnl += close_position(db, pos, price, 'stop_loss')
                continue
            elif pos['side'] == 'SHORT' and price >= pos['stop_loss']:
                total_pnl += close_position(db, pos, price, 'stop_loss')
                continue

            # Take profit
            if pos['side'] == 'LONG' and price >= pos['take_profit']:
                total_pnl += close_position(db, pos, price, 'take_profit')
                continue
            elif pos['side'] == 'SHORT' and price <= pos['take_profit']:
                total_pnl += close_position(db, pos, price, 'take_profit')
                continue

            # Time exit
            if age_min >= pos['max_hold_min']:
                total_pnl += close_position(db, pos, price, 'time_exit(%.0fm)' % age_min)
                continue

        except Exception as e:
            log.error('Exit check error %s: %s', pos['symbol'], e)
    return total_pnl


# ── Report ────────────────────────────────────────────────────────────────────
def report(db):
    trades = db.execute('SELECT pnl, pnl_pct, exit_reason FROM trades').fetchall()
    if not trades:
        log.info('REPORT: No completed trades yet')
        return

    pnls = [t[0] for t in trades]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    wr = len(wins) / n if n else 0
    total_pnl = sum(pnls)
    bal = get_balance(db)

    # Sharpe annualizado por frecuencia real (trades/dia * 252)
    if n > 1:
        mean_r = sum(pnls) / n
        std_r = math.sqrt(sum((p - mean_r) ** 2 for p in pnls) / (n - 1))
        times = db.execute('SELECT entry_time FROM trades ORDER BY entry_time').fetchall()
        if len(times) >= 2:
            span_days = max((datetime.fromisoformat(times[-1][0]) - datetime.fromisoformat(times[0][0])).total_seconds() / 86400, 1)
            freq = len(times) / span_days
        else:
            freq = 2.5
        sharpe = (mean_r / std_r) * math.sqrt(freq * 252) if std_r > 0 else 0
    else:
        sharpe = 0

    # Max DD (from actual starting balance)
    equity = [get_balance(db) - total_pnl]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak = equity[0]
    max_dd = 0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Exit reasons
    sl_count = sum(1 for t in trades if 'stop_loss' in t[2])
    tp_count = sum(1 for t in trades if 'take_profit' in t[2])
    time_count = sum(1 for t in trades if 'time_exit' in t[2])

    log.info('=' * 60)
    log.info('ZERO v4 REPORT: %d trades | WR=%.0f%% | Sharpe=%.2f | DD=%.1f%%',
             n, wr * 100, sharpe, max_dd)
    log.info('Balance: $%.2f | PnL: $%.2f (%.2f%%)', bal, total_pnl, total_pnl / CAPITAL * 100)
    log.info('Exits: SL=%d TP=%d TIME=%d', sl_count, tp_count, time_count)
    log.info('Pairs: %d | SL=%.1f%% TP=%.1f%% L-Risk=%.0f%% S-Risk=%.0f%% vol=10x hold<=15m', len(PAIRS), SL_PCT, TP_PCT, LONG_RISK*100, SHORT_RISK*100)
    log.info('=' * 60)


# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    db = init_db()
    bal = get_balance(db)
    last_trade_time = {}  # symbol -> datetime (cooldown)
    # Load today PnL from DB so circuit breaker survives restarts
    _today_prefix = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    _row = db.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE exit_time LIKE ?",
        (_today_prefix + '%',)).fetchone()
    daily_pnl = _row[0]
    circuit_day = datetime.now(timezone.utc).date()

    import signal as _signal
    def _shutdown(sig, frame):
        log.info('Shutdown signal %d received', sig)
        report(db)
        try:
            from alerts.telegram import notify_bot_event
            notify_bot_event('Samael Zero v4 PARADO', 'Bal=$%.2f | PnL hoy=$%.2f' % (get_balance(db), daily_pnl))
        except Exception:
            pass
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _shutdown)
    _signal.signal(_signal.SIGINT, _shutdown)

    log.info('Samael Zero v4 — Mean Reversion on Volume Spikes')
    log.info('Capital: $%.2f | L-Risk: %.0f%% S-Risk: %.0f%% | SL: %.1f%% | TP: %.1f%%',
             bal, LONG_RISK * 100, SHORT_RISK * 100, SL_PCT, TP_PCT)
    log.info('Pairs: %s', list(PAIRS.keys()))
    log.info('Config: LONG=%.0f-%.0f%% SHORT=%.0f-%.0f%% vol>=10x cooldown=%dmin skip=%s', LONG_TREND_MIN, LONG_TREND_MAX, SHORT_TREND_MIN, SHORT_TREND_MAX, COOLDOWN_MINUTES, sorted(SKIP_HOURS))

    try:
        from alerts.telegram import notify_bot_event
        notify_bot_event('Samael Zero v4 started',
                         'Mean Reversion, $%.2f, SL=%.1f%% TP=%.1f%%' % (bal, SL_PCT, TP_PCT))
    except:
        pass

    # Restore cooldown state from DB (survives restarts)
    recent = db.execute(
        "SELECT symbol, MAX(exit_time) FROM trades GROUP BY symbol").fetchall()
    for sym, exit_t in recent:
        try:
            last_trade_time[sym] = datetime.fromisoformat(exit_t)
        except Exception:
            pass

    cycle = 0
    while True:
        try:
            cycle += 1
            now = datetime.now(timezone.utc)

            # Reset circuit breaker at UTC midnight (before accumulating)
            today = now.date()
            if circuit_day != today:
                circuit_day = today
                daily_pnl = 0.0

            # ── Check exits ──
            positions = get_open_positions(db)
            daily_pnl += check_exits(db, positions)

            # ── Look for new entries ──
            positions = get_open_positions(db)
            open_symbols = {p['symbol'] for p in positions}
            bal = get_balance(db)

            # BTC regime & SHORT filter pre-compute
            btc_1h_up = False
            atr_regime_skip = False
            try:
                btc_kl = fetch_recent_klines('BTCUSDT', limit=70)
                if btc_kl and len(btc_kl) >= 62:
                    btc_c = [float(k[4]) for k in btc_kl[:-1]]
                    btc_1h_up = (btc_c[-1] - btc_c[-61]) / btc_c[-61] * 100 > 0
                # ATR ratio regime filter — backtest 1yr showed +2.5% APR (+0.72 Sharpe)
                # Skips signals when BTC volatility is EXPANDING (trending market, not mean-reverting)
                if btc_kl and len(btc_kl) >= 52:
                    def _atr(kl, period):
                        trs = [max(float(kl[i][2])-float(kl[i][3]),
                                   abs(float(kl[i][2])-float(kl[i-1][4])),
                                   abs(float(kl[i][3])-float(kl[i-1][4])))
                               for i in range(1, len(kl))]
                        return sum(trs[-period:]) / period if len(trs) >= period else 0
                    fast_atr = _atr(btc_kl[-7:], 5)
                    slow_atr = _atr(btc_kl[-52:], 50)
                    if slow_atr > 0 and fast_atr / slow_atr > 1.5:
                        atr_regime_skip = True
                        log.debug('REGIME ATR skip — ratio=%.2f', fast_atr / slow_atr)
            except Exception:
                pass

            if daily_pnl < -(bal * DAILY_LOSS_LIMIT):
                if cycle % 20 == 1:
                    log.warning('CIRCUIT BREAKER -- daily PnL $%.2f (%.1f%%) -- entries paused',
                                daily_pnl, abs(daily_pnl / bal * 100))
            elif len(positions) < MAX_POSITIONS:
                open_count = len(positions)
                for symbol, config in PAIRS.items():
                    if open_count >= MAX_POSITIONS:
                        break
                    if symbol in open_symbols:
                        continue

                    # Cooldown
                    if symbol in last_trade_time:
                        elapsed = (now - last_trade_time[symbol]).total_seconds() / 60
                        if elapsed < COOLDOWN_MINUTES:
                            continue

                    try:
                        klines = fetch_recent_klines(symbol, limit=250)
                        if not klines or len(klines) < 70:
                            continue

                        result = detect_signal(klines, config)
                        if result is None:
                            continue

                        side, vol_ratio, pre_trend = result

                        # ATR regime filter — skip ALL signals in trending/expanding-vol market
                        if atr_regime_skip:
                            log.debug('SKIP %s — ATR regime vol expansion', symbol)
                            continue

                        # SHORT filter: Combo BTC1h + 3 velas verdes consecutivas
                        # Backtest 2024+2025: SHORTs pierden cuando BTC sube o hay momentum alcista
                        if side == 'SHORT':
                            kl_c = [float(k[4]) for k in klines[:-1]]
                            consec3 = len(kl_c) >= 4 and kl_c[-1] > kl_c[-2] > kl_c[-3] > kl_c[-4]
                            if btc_1h_up or consec3:
                                log.debug('SKIP SHORT %s — BTC_1h_up=%s consec3=%s', symbol, btc_1h_up, consec3)
                                continue

                        price = get_current_price(symbol)

                        # Log signal
                        db.execute(
                            'INSERT INTO signals_log (timestamp,symbol,side,price,pre_trend_pct,vol_ratio,traded) VALUES (?,?,?,?,?,?,1)',
                            (now.isoformat(), symbol, side, price, pre_trend, vol_ratio))
                        db.commit()

                        # Re-query DB to block duplicate/opposing positions on same symbol
                        existing = db.execute(
                            "SELECT COUNT(*) FROM positions WHERE symbol=? AND status='open'",
                            (symbol,)).fetchone()[0]
                        if existing > 0:
                            log.warning("SKIP %s - already has open position", symbol)
                            continue

                        open_position(db, symbol, side, price, vol_ratio, pre_trend, config)
                        last_trade_time[symbol] = now
                        open_count += 1
                        open_symbols.add(symbol)

                    except Exception as e:
                        log.error('Entry error %s: %s', symbol, e)

            # ── Periodic status ──
            if cycle % 20 == 1:  # Every ~5 min
                positions = get_open_positions(db)
                bal = get_balance(db)
                open_pnl = 0
                for p in positions:
                    try:
                        px = get_current_price(p['symbol'])
                        if p['side'] == 'LONG':
                            open_pnl += (px - p['entry_price']) * p['quantity']
                        else:
                            open_pnl += (p['entry_price'] - px) * p['quantity']
                    except:
                        pass
                log.info('Cycle #%d | Bal: $%.2f | Pos: %d | Unreal: $%.2f | Total: $%.2f',
                         cycle, bal, len(positions), open_pnl, bal + open_pnl)

            # Report every 2h
            if cycle % 480 == 1:
                report(db)

            time.sleep(CHECK_SECONDS)

        except KeyboardInterrupt:
            log.info('Shutting down...')
            report(db)
            break
        except Exception as e:
            log.error('Cycle error: %s', e)
            time.sleep(60)


if __name__ == '__main__':
    main()
