"""SB (Setup Bar) backtest — exploration script.

Tests two candidate predicates on watchlist tickers over the last 90 trading days:
- Pattern A (high-tight): RVol<=1, range_contract<=0.95, dist_5d_hi>=-6%, chg in [-2,+5]
- Pattern B (pullback-reversal): RVol<=1, range_contract<=0.80, prior 3d cum<=-8%, chg>=+3

Pre-filter (cheap structural gate):
  SMA50% >= +10 (price 10% above 50d SMA)
  perf60d >= +15% (3-month RS proxy)
  no expansion (RVol>=3 OR chg>=+10) in last 7 trading days

For each fire: measure next 1/3/5/10d max gain (high vs fire-day close).
Hit = next 5d max gain >= +15%.

Output: per-ticker summary + aggregate stats + sample fires.
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
print(f"backtest universe: {len(tickers)} active watchlist tickers")

END = datetime(2026, 5, 22, tzinfo=timezone.utc)
START = END - timedelta(days=200)  # 200 cal days for ~90 trading days + lookback


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
    df['cum3_prev'] = (df['close'].shift(1) / df['close'].shift(4) - 1) * 100  # 3-day return up to yesterday
    # expansion flag (today's bar)
    df['is_expansion'] = ((df['rvol'] >= 3) | (df['chg_pct'] >= 10)).fillna(False)
    df['expansion_in_last_7'] = df['is_expansion'].shift(1).rolling(7).max().fillna(0).astype(bool)
    # forward returns
    for k in (1, 3, 5, 10):
        df[f'fwd{k}_max'] = (df['high'].shift(-1).rolling(k, min_periods=1).max().shift(-(k-1)) / df['close'] - 1) * 100
    return df


def pre_filter(row):
    return (
        row['sma50_pct'] >= 10
        and row['perf60'] >= 15
        and not row['expansion_in_last_7']
        and pd.notna(row['rvol'])
        and pd.notna(row['range_contract'])
    )


def pattern_A(row):
    return (
        row['rvol'] <= 1.0
        and row['range_contract'] <= 0.95
        and row['dist_hi5'] >= -6
        and -2 <= row['chg_pct'] <= 5
    )


def pattern_B(row):
    return (
        row['rvol'] <= 1.0
        and row['range_contract'] <= 0.80
        and row['cum3_prev'] <= -8
        and row['chg_pct'] >= 3
    )


fires = []
processed = 0
errors = 0
for t in tickers:
    try:
        req = StockBarsRequest(symbol_or_symbols=t, timeframe=TimeFrame.Day, start=START, end=END)
        df = client.get_stock_bars(req).df
        if df.empty:
            continue
        df = df.reset_index()
        df = compute_metrics(df)
        # restrict to last 90 trading days
        cutoff = df.iloc[-90:] if len(df) > 90 else df
        for _, r in cutoff.iterrows():
            if not pre_filter(r):
                continue
            pa = pattern_A(r)
            pb = pattern_B(r)
            if not (pa or pb):
                continue
            fires.append({
                'ticker': t, 'date': r['date'], 'pattern': 'A' if pa else 'B',
                'pa': pa, 'pb': pb,
                'close': r['close'], 'rvol': r['rvol'], 'chg_pct': r['chg_pct'],
                'range_contract': r['range_contract'], 'dist_hi5': r['dist_hi5'],
                'sma50_pct': r['sma50_pct'], 'perf60': r['perf60'],
                'fwd1_max': r['fwd1_max'], 'fwd3_max': r['fwd3_max'],
                'fwd5_max': r['fwd5_max'], 'fwd10_max': r['fwd10_max'],
            })
        processed += 1
    except Exception as e:
        errors += 1
        if errors <= 3:
            print(f"  err {t}: {e}")

print(f"\nprocessed {processed}/{len(tickers)} tickers, {errors} errors")
print(f"total fires: {len(fires)}")

if not fires:
    sys.exit(0)

f = pd.DataFrame(fires)
pd.set_option('display.width', 240)
pd.set_option('display.max_rows', 60)

print("\n=== fires by pattern ===")
print(f['pattern'].value_counts())

for k in (1, 3, 5, 10):
    col = f'fwd{k}_max'
    print(f"\n=== forward {k}d max gain ===")
    print(f.groupby('pattern')[col].describe()[['count','mean','50%','75%','max']].round(1))

# hit-rate at +15% in next 5d
f['hit_5d_15'] = f['fwd5_max'] >= 15
f['hit_5d_10'] = f['fwd5_max'] >= 10
f['hit_3d_10'] = f['fwd3_max'] >= 10
print("\n=== hit rates ===")
print(f.groupby('pattern')[['hit_5d_15', 'hit_5d_10', 'hit_3d_10']].mean().round(2))
print("overall:")
print(f[['hit_5d_15', 'hit_5d_10', 'hit_3d_10']].mean().round(2))

# top hits
print("\n=== top 15 fires by fwd5_max ===")
print(f.nlargest(15, 'fwd5_max')[['ticker','date','pattern','close','rvol','chg_pct','range_contract','dist_hi5','fwd1_max','fwd5_max']].round(2).to_string(index=False))

# misses (false positives at +10/5d threshold)
print("\n=== bottom 15 fires (false positives) ===")
print(f.nsmallest(15, 'fwd5_max')[['ticker','date','pattern','close','rvol','chg_pct','range_contract','dist_hi5','fwd1_max','fwd5_max']].round(2).to_string(index=False))

# fires per ticker
print("\n=== tickers with most fires ===")
print(f['ticker'].value_counts().head(10))

# save
f.to_csv('data/sb_backtest_results.csv', index=False)
print("\nsaved -> data/sb_backtest_results.csv")
EOF