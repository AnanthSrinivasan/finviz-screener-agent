"""SB backtest v2 — tightened gates + consecutive bars + per-ticker cooldown.

Compares four variants:
  A1: original loose Pattern A
  A2: tightened Pattern A (range_contract<=0.75, rvol<=0.85, chg in [-1,+2])
  A3: tightened + consecutive (2+ A2 days in a row, fire on the 2nd day)
  A4: tightened + consecutive + 5-day per-ticker cooldown
  B:  Pattern B unchanged (pullback-reversal)

Hit definition: max high in next N days >= +X% above fire-day close.
"""
import os, sys, json
from dotenv import dotenv_values
for k, v in dotenv_values('.env').items():
    if v: os.environ.setdefault(k, v)

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

client = StockHistoricalDataClient(os.environ['ALPACA_API_KEY'], os.environ['ALPACA_SECRET_KEY'])

W = json.load(open('data/watchlist.json'))
tickers = sorted({r['ticker'] for r in W['watchlist'] if r.get('status') == 'watching'})
print(f"universe: {len(tickers)} tickers, ~90 trading day window\n")

END = datetime(2026, 5, 22, tzinfo=timezone.utc)
START = END - timedelta(days=200)


def compute_metrics(df):
    df = df.copy().sort_values('timestamp').reset_index(drop=True)
    df['date'] = df['timestamp'].dt.strftime('%Y-%m-%d')
    df['chg_pct'] = df['close'].pct_change() * 100
    df['range_pct'] = (df['high'] - df['low']) / df['close'] * 100
    df['rvol'] = df['volume'] / df['volume'].rolling(20).mean()
    df['range_avg10'] = df['range_pct'].shift(1).rolling(10).mean()
    df['range_contract'] = df['range_pct'] / df['range_avg10']
    df['sma50'] = df['close'].rolling(50).mean()
    df['sma50_pct'] = (df['close'] / df['sma50'] - 1) * 100
    df['perf60'] = (df['close'] / df['close'].shift(60) - 1) * 100
    df['hi5_prev'] = df['high'].shift(1).rolling(5).max()
    df['dist_hi5'] = (df['close'] / df['hi5_prev'] - 1) * 100
    df['cum3_prev'] = (df['close'].shift(1) / df['close'].shift(4) - 1) * 100
    df['is_expansion'] = ((df['rvol'] >= 3) | (df['chg_pct'] >= 10)).fillna(False)
    df['expansion_in_last_7'] = df['is_expansion'].shift(1).rolling(7).max().fillna(0).astype(bool)
    for k in (1, 3, 5, 10):
        df[f'fwd{k}_max'] = (df['high'].shift(-1).rolling(k, min_periods=1).max().shift(-(k-1)) / df['close'] - 1) * 100
    return df


def pre_filter(r):
    return (
        pd.notna(r['rvol']) and pd.notna(r['range_contract'])
        and r['sma50_pct'] >= 10
        and r['perf60'] >= 15
        and not r['expansion_in_last_7']
    )


def pat_A1(r):  # loose
    return r['rvol'] <= 1.0 and r['range_contract'] <= 0.95 and r['dist_hi5'] >= -6 and -2 <= r['chg_pct'] <= 5


def pat_A2(r):  # tightened
    return r['rvol'] <= 0.85 and r['range_contract'] <= 0.75 and r['dist_hi5'] >= -6 and -1 <= r['chg_pct'] <= 2


def pat_B(r):
    return r['rvol'] <= 1.0 and r['range_contract'] <= 0.80 and r['cum3_prev'] <= -8 and r['chg_pct'] >= 3


def scan(ticker, df):
    """Return list of fires per variant dict."""
    out = {'A1': [], 'A2': [], 'A3': [], 'A4': [], 'B': []}
    cutoff = df.iloc[-90:] if len(df) > 90 else df

    # build day-by-day flags (need A2 for prev-day consecutive check)
    flags = []
    for _, r in cutoff.iterrows():
        if not pre_filter(r):
            flags.append({'A1': False, 'A2': False, 'B': False, 'row': r})
            continue
        flags.append({'A1': pat_A1(r), 'A2': pat_A2(r), 'B': pat_B(r), 'row': r})

    last_a4_idx = -999
    for i, f in enumerate(flags):
        r = f['row']
        if f['A1']:
            out['A1'].append(r)
        if f['A2']:
            out['A2'].append(r)
        prev_a2 = i > 0 and flags[i-1]['A2']
        is_a3 = f['A2'] and prev_a2
        if is_a3:
            out['A3'].append(r)
            if i - last_a4_idx >= 5:
                out['A4'].append(r)
                last_a4_idx = i
        if f['B']:
            out['B'].append(r)
    return out


def fwd_cols(r):
    return {f'fwd{k}_max': r[f'fwd{k}_max'] for k in (1, 3, 5, 10)}


variants = {'A1': [], 'A2': [], 'A3': [], 'A4': [], 'B': []}
for t in tickers:
    try:
        req = StockBarsRequest(symbol_or_symbols=t, timeframe=TimeFrame.Day, start=START, end=END)
        df = client.get_stock_bars(req).df
        if df.empty:
            continue
        df = df.reset_index()
        df = compute_metrics(df)
        fires = scan(t, df)
        for k, rows in fires.items():
            for r in rows:
                rec = {'ticker': t, 'date': r['date'], 'close': r['close'],
                       'rvol': r['rvol'], 'chg_pct': r['chg_pct'],
                       'range_contract': r['range_contract'], 'dist_hi5': r['dist_hi5'],
                       'cum3_prev': r['cum3_prev'], **fwd_cols(r)}
                variants[k].append(rec)
    except Exception as e:
        pass

pd.set_option('display.width', 240)

print(f"{'variant':<6} {'fires':>6} {'per_day':>8} {'hit_5d_15':>10} {'hit_5d_10':>10} {'hit_3d_10':>10} {'med_fwd5':>9} {'p75_fwd5':>9} {'worst':>8}")
print('-' * 90)
for v in ('A1', 'A2', 'A3', 'A4', 'B'):
    rows = variants[v]
    if not rows:
        print(f"{v:<6} 0")
        continue
    f = pd.DataFrame(rows)
    n = len(f)
    fpd = n / 90
    h515 = (f['fwd5_max'] >= 15).mean()
    h510 = (f['fwd5_max'] >= 10).mean()
    h310 = (f['fwd3_max'] >= 10).mean()
    med = f['fwd5_max'].median()
    p75 = f['fwd5_max'].quantile(0.75)
    worst = f['fwd5_max'].min()
    print(f"{v:<6} {n:>6} {fpd:>8.2f} {h515:>10.2f} {h510:>10.2f} {h310:>10.2f} {med:>9.1f} {p75:>9.1f} {worst:>8.1f}")

# detail on A4 + B (the candidates we'd actually ship)
for v in ('A4', 'B'):
    rows = variants[v]
    if not rows:
        continue
    f = pd.DataFrame(rows)
    print(f"\n=== {v} — all fires ({len(f)}) ===")
    print(f[['ticker','date','close','rvol','chg_pct','range_contract','dist_hi5','cum3_prev','fwd1_max','fwd3_max','fwd5_max','fwd10_max']].round(2).to_string(index=False))

    print(f"\n=== {v} — hit/miss split ===")
    f['outcome'] = np.where(f['fwd5_max'] >= 10, 'HIT', np.where(f['fwd5_max'] >= 0, 'flat', 'MISS'))
    print(f['outcome'].value_counts())
