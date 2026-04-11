"""
Peel Calibration Research Script
Run-based peak detection: finds the peak ATR% Multiple during each distinct
run above the 50MA (atr_pct_multiple > 0 for 20+ consecutive days), separated
by a confirmed cross below 50MA (atr_pct_multiple < 0).

Formula (matches TradingView "ATR% Multiple" indicator):
  atr_pct_multiple = (close - SMA50) * close / (SMA50 * ATR14)
                   = ((close - SMA50) / SMA50) / (ATR14 / close)

Verification: AAOI Jun 16 2023: close=4.85, SMA50=2.25, ATR14=0.259
  = (4.85 - 2.25) * 4.85 / (2.25 * 0.259) = 21.6x  ✓ (matches ~22x chart reading)
"""

import os
import math
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
DATA_URL = "https://data.alpaca.markets/v2/stocks/{ticker}/bars"

TICKERS = [
    "AAOI", "MRVL", "TSM", "ANET", "COHR", "LITE", "AMD", "ARWR",
    "GEV", "MTSI", "SNDK", "LRCX", "MU", "VRT",
]

START_DATE = "2022-01-01"
MIN_RUN_DAYS = 10       # minimum consecutive days above 50MA to count as a run
MIN_PEAK_MULTIPLE = 5.0  # ignore runs whose peak atr_multiple < this


def fetch_bars(ticker):
    """Fetch daily OHLCV bars from Alpaca IEX, following pagination."""
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    params = {
        "timeframe": "1Day",
        "start": START_DATE,
        "feed": "iex",
        "limit": 1000,
    }
    url = DATA_URL.format(ticker=ticker)
    bars = []
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        bars.extend(data.get("bars", []))
        next_token = data.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
    return bars


def compute_atr14(highs, lows, closes):
    """Compute ATR(14) using Wilder's smoothing. Returns list with None for warmup bars."""
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    atrs = [None] * len(closes)
    if len(trs) < 14:
        return atrs

    seed = sum(trs[:14]) / 14.0
    atrs[14] = seed
    for i in range(15, len(closes)):
        atrs[i] = (atrs[i - 1] * 13 + trs[i - 1]) / 14.0
    return atrs


def compute_sma(values, period):
    """Compute simple moving average. Returns list with None for warmup bars."""
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i - period + 1: i + 1]) / period
    return result


def find_run_peaks(atr_multiples, dates):
    """
    Identify distinct runs above 50MA and return the peak (date, value) from each run.

    A run is a continuous sequence where atr_multiple > 0 for at least MIN_RUN_DAYS.
    A new run can only start after atr_multiple drops below 0 (cross below 50MA).
    Only runs with a peak atr_multiple >= MIN_PEAK_MULTIPLE are counted.

    Returns list of (peak_date, peak_atr_multiple) for qualifying runs.
    """
    n = len(atr_multiples)
    runs = []
    in_run = False
    run_start = None

    for i in range(n):
        val = atr_multiples[i]
        if val is None:
            if in_run:
                # treat missing data as below 50MA — end the run
                run_len = i - run_start
                if run_len >= MIN_RUN_DAYS:
                    runs.append((run_start, i - 1))
                in_run = False
            continue

        if val > 0:
            if not in_run:
                in_run = True
                run_start = i
        else:
            # val <= 0: price at or below 50MA
            if in_run:
                run_len = i - run_start
                if run_len >= MIN_RUN_DAYS:
                    runs.append((run_start, i - 1))
                in_run = False

    # Handle run still open at end of data
    if in_run:
        run_len = n - run_start
        if run_len >= MIN_RUN_DAYS:
            runs.append((run_start, n - 1))

    # Extract peak of each qualifying run
    peaks = []
    for start, end in runs:
        segment = [(i, atr_multiples[i]) for i in range(start, end + 1)
                   if atr_multiples[i] is not None]
        if not segment:
            continue
        peak_idx, peak_val = max(segment, key=lambda x: x[1])
        if peak_val >= MIN_PEAK_MULTIPLE:
            peaks.append((dates[peak_idx], peak_val, end - start + 1))

    return peaks


def percentile(data, p):
    """Interpolating percentile."""
    if not data:
        return None
    sorted_data = sorted(data)
    idx = (len(sorted_data) - 1) * p / 100.0
    lo = int(idx)
    hi = lo + 1
    if hi >= len(sorted_data):
        return sorted_data[-1]
    frac = idx - lo
    return sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac


def analyze_ticker(ticker):
    bars = fetch_bars(ticker)
    if len(bars) < 60:
        return ticker, [], None, None, None, None, None, "insufficient data"

    dates = [b["t"][:10] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]

    atrs = compute_atr14(highs, lows, closes)
    sma50s = compute_sma(closes, 50)

    # Compute ATR% and atr_multiple for each bar
    atr_multiples = []
    atr_pcts = []
    for i in range(len(closes)):
        a = atrs[i]
        s = sma50s[i]
        c = closes[i]
        if a is None or s is None or a == 0:
            atr_multiples.append(None)
            atr_pcts.append(None)
        else:
            # TradingView "ATR% Multiple": ((close-SMA50)/SMA50) / (ATR14/close)
            atr_multiples.append((c - s) * c / (s * a))
            atr_pcts.append((a / c) * 100.0)

    run_peaks = find_run_peaks(atr_multiples, dates)
    peak_vals = [v for _, v, _ in run_peaks]

    valid_atr_pcts = [x for x in atr_pcts if x is not None]
    avg_atr_pct = sum(valid_atr_pcts) / len(valid_atr_pcts) if valid_atr_pcts else None

    n_runs = len(peak_vals)
    p50 = percentile(peak_vals, 50) if peak_vals else None
    p75 = percentile(peak_vals, 75) if peak_vals else None
    p90 = percentile(peak_vals, 90) if peak_vals else None
    mx = max(peak_vals) if peak_vals else None

    note = f"<3 runs — unreliable" if n_runs < 3 else ""

    return ticker, run_peaks, p50, p75, p90, mx, avg_atr_pct, note


def fmt(val, decimals=1, width=8):
    if val is None:
        return " " * (width - 3) + "N/A"
    return f"{val:{width}.{decimals}f}"


def main():
    print(f"\nPeel Calibration — ATR% Multiple (TradingView formula) — {datetime.today().strftime('%Y-%m-%d')}")
    print(f"Formula: (close - SMA50) * close / (SMA50 * ATR14)")
    print(f"Data: {START_DATE} to today | Min run: {MIN_RUN_DAYS}d above 50MA | Min peak: {MIN_PEAK_MULTIPLE}x\n")

    header = f"{'Ticker':<8} {'Runs':>5} {'P50':>8} {'P75':>8} {'P90':>8} {'Max':>8} {'AvgATR%':>9}  Note"
    print(header)
    print("-" * (len(header) + 10))

    results = []
    aaoi_run_peaks = None

    for ticker in TICKERS:
        try:
            ticker, run_peaks, p50, p75, p90, mx, avg_atr_pct, note = analyze_ticker(ticker)
            results.append((ticker, run_peaks, p50, p75, p90, mx, avg_atr_pct, note))
            n_runs = len(run_peaks)
            print(
                f"{ticker:<8} {n_runs:>5} {fmt(p50)} {fmt(p75)} {fmt(p90)} {fmt(mx)}"
                f" {fmt(avg_atr_pct, 2, 9)}  {note}"
            )
            if ticker == "AAOI":
                aaoi_run_peaks = run_peaks
        except Exception as e:
            print(f"{ticker:<8}  ERROR: {e}")

    # AAOI run detail
    print()
    print("=" * 60)
    print("AAOI — every qualifying run peak (cross-check vs TradingView):")
    print(f"  {'Peak Date':<12} {'ATR Multiple':>14} {'Run Days':>10}")
    print(f"  {'-'*12} {'-'*14} {'-'*10}")
    if aaoi_run_peaks:
        for peak_date, peak_val, run_days in sorted(aaoi_run_peaks, key=lambda x: x[0]):
            print(f"  {peak_date:<12} {peak_val:>14.2f}x {run_days:>9}d")
    else:
        print("  (no qualifying runs found)")
    print("=" * 60)

    # Cross-ticker summary
    all_p50 = [r[2] for r in results if r[2] is not None]
    all_p75 = [r[3] for r in results if r[3] is not None]
    all_p90 = [r[4] for r in results if r[4] is not None]
    all_mx = [r[5] for r in results if r[5] is not None]

    print()
    print("Cross-ticker medians (percentile of all per-ticker Pxx values):")
    if all_p50:
        print(f"  Median P50: {percentile(all_p50, 50):.1f}x")
    if all_p75:
        print(f"  Median P75: {percentile(all_p75, 50):.1f}x")
    if all_p90:
        print(f"  Median P90: {percentile(all_p90, 50):.1f}x")
    print()
    print("Cross-ticker averages (mean of per-ticker values):")
    if all_p50:
        print(f"  P50: {sum(all_p50)/len(all_p50):.1f}x")
    if all_p75:
        print(f"  P75: {sum(all_p75)/len(all_p75):.1f}x")
    if all_p90:
        print(f"  P90: {sum(all_p90)/len(all_p90):.1f}x")
    if all_mx:
        print(f"  Max: {sum(all_mx)/len(all_mx):.1f}x (avg of per-ticker maxes)")

    print()
    print("Interpretation:")
    print("  P50 = typical run peak  → warn threshold candidate")
    print("  P75 = elevated run peak → signal threshold candidate")
    print("  P90 = extreme run peak  → hard-exit threshold candidate")
    print(f"  Current system: warn ~6.5x / signal ~8x (high ATR% tier)")
    print()


if __name__ == "__main__":
    main()
