"""
calibrate_peel.py — Per-ticker peel threshold calibration.

Fetches historical daily bars from Alpaca, computes the TradingView
"ATR% Multiple" indicator, finds peaks of sustained positive runs,
and writes p75-based signal/warn thresholds to data/peel_calibration.json.

Formula (matches TradingView indicator):
    atr_pct_multiple = (close - sma50) * close / (sma50 * atr14)

where atr14 = Wilder smoothed ATR(14), sma50 = 50-day SMA.

Usage:
    python calibrate_peel.py --mode positions   # open positions only
    python calibrate_peel.py --mode watchlist   # watchlist only
    python calibrate_peel.py --mode all         # both
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime

import requests

# ---------------------------------------------------------------------------
# Optional dotenv support
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.json")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
CALIBRATION_FILE = os.path.join(DATA_DIR, "peel_calibration.json")

ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/{ticker}/bars"
ALPACA_START = "2022-01-01"
ALPACA_TIMEFRAME = "1Day"
ALPACA_FEED = "iex"
ALPACA_LIMIT = 1000

MIN_RUNS = 3
MIN_PEAK = 5.0
MIN_RUN_DAYS = 10

SIGNAL_FLOOR = 10.0
WARN_FLOOR = 7.5

TODAY = date.today().isoformat()


# ---------------------------------------------------------------------------
# Alpaca helpers
# ---------------------------------------------------------------------------

def get_alpaca_creds():
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        print("ERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in environment or .env")
        sys.exit(1)
    return key, secret


def fetch_bars(ticker, api_key, api_secret):
    """Fetch daily OHLCV bars from Alpaca for a single ticker.

    Returns a list of dicts with keys: t, o, h, l, c, v
    Handles pagination via next_page_token.
    """
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params = {
        "timeframe": ALPACA_TIMEFRAME,
        "start": ALPACA_START,
        "feed": ALPACA_FEED,
        "limit": ALPACA_LIMIT,
    }
    url = ALPACA_DATA_URL.format(ticker=ticker)
    bars = []
    page_token = None

    while True:
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException as exc:
            raise RuntimeError("Network error fetching {}: {}".format(ticker, exc))

        if resp.status_code == 404:
            raise RuntimeError("Ticker {} not found (404)".format(ticker))
        if resp.status_code == 429:
            time.sleep(5)
            continue
        if resp.status_code != 200:
            raise RuntimeError(
                "Alpaca returned {} for {}: {}".format(resp.status_code, ticker, resp.text[:200])
            )

        data = resp.json()
        page_bars = data.get("bars") or []
        bars.extend(page_bars)

        page_token = data.get("next_page_token")
        if not page_token:
            break

        time.sleep(0.2)  # rate-limit friendly

    return bars


# ---------------------------------------------------------------------------
# Indicator computation
# ---------------------------------------------------------------------------

def wilder_atr(bars):
    """Compute Wilder smoothed ATR(14) for each bar.

    Returns a list of floats (None for the first 13 bars).
    """
    n = len(bars)
    atr_vals = [None] * n
    if n < 14:
        return atr_vals

    # True ranges
    tr_list = []
    for i, bar in enumerate(bars):
        high = bar["h"]
        low = bar["l"]
        close = bar["c"]
        if i == 0:
            tr = high - low
        else:
            prev_close = bars[i - 1]["c"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

    # Seed: simple average of first 14 TRs
    seed = sum(tr_list[:14]) / 14.0
    atr_vals[13] = seed

    for i in range(14, n):
        atr_vals[i] = (atr_vals[i - 1] * 13.0 + tr_list[i]) / 14.0

    return atr_vals


def compute_sma(values, period):
    """Compute SMA over a list of floats. Returns list with None padding."""
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i - period + 1: i + 1]) / period
    return result


def compute_atr_pct_multiples(bars):
    """Compute ATR% Multiple series.

    Formula: (close - sma50) * close / (sma50 * atr14)

    Returns list of floats (None where indicators unavailable).
    """
    closes = [b["c"] for b in bars]
    sma50_vals = compute_sma(closes, 50)
    atr14_vals = wilder_atr(bars)

    multiples = []
    for i, bar in enumerate(bars):
        sma50 = sma50_vals[i]
        atr14 = atr14_vals[i]
        if sma50 is None or atr14 is None or sma50 == 0 or atr14 == 0:
            multiples.append(None)
        else:
            close = bar["c"]
            val = (close - sma50) * close / (sma50 * atr14)
            multiples.append(val)

    return multiples


# ---------------------------------------------------------------------------
# Run detection
# ---------------------------------------------------------------------------

def find_run_peaks(multiples):
    """Find peak ATR% Multiple for each qualifying run.

    A run = continuous period where multiple > 0 for >= MIN_RUN_DAYS days.
    Include in-progress runs (run ends at end of series).
    Only include peaks >= MIN_PEAK.

    Returns list of peak floats.
    """
    peaks = []
    in_run = False
    run_values = []

    for i, val in enumerate(multiples):
        is_last = (i == len(multiples) - 1)

        if val is None:
            # Null breaks a run
            if in_run:
                if len(run_values) >= MIN_RUN_DAYS:
                    peak = max(run_values)
                    if peak >= MIN_PEAK:
                        peaks.append(peak)
                in_run = False
                run_values = []
            continue

        if val > 0:
            if not in_run:
                in_run = True
                run_values = []
            run_values.append(val)
        else:
            # val <= 0 ends a run
            if in_run:
                if len(run_values) >= MIN_RUN_DAYS:
                    peak = max(run_values)
                    if peak >= MIN_PEAK:
                        peaks.append(peak)
                in_run = False
                run_values = []

        # Handle in-progress run at end of series
        if is_last and in_run:
            if len(run_values) >= MIN_RUN_DAYS:
                peak = max(run_values)
                if peak >= MIN_PEAK:
                    peaks.append(peak)

    return peaks


# ---------------------------------------------------------------------------
# Percentile helper (no numpy needed)
# ---------------------------------------------------------------------------

def percentile(sorted_vals, pct):
    """Linear interpolation percentile on a sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    idx = (pct / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


# ---------------------------------------------------------------------------
# Calibration logic
# ---------------------------------------------------------------------------

def calibrate_ticker(ticker, api_key, api_secret):
    """Fetch bars, compute multiples, detect runs, return calibration dict."""
    bars = fetch_bars(ticker, api_key, api_secret)

    if len(bars) < 64:
        return {
            "calibrated": False,
            "reason": "insufficient_data",
            "runs": 0,
            "updated": TODAY,
        }

    multiples = compute_atr_pct_multiples(bars)

    # Average ATR% (for context): atr14 / close * 100 where available
    atr14_vals = wilder_atr(bars)
    valid_atr_pcts = []
    for i, bar in enumerate(bars):
        if atr14_vals[i] is not None and bar["c"] > 0:
            valid_atr_pcts.append(atr14_vals[i] / bar["c"] * 100.0)
    atr_pct_avg = round(sum(valid_atr_pcts) / len(valid_atr_pcts), 2) if valid_atr_pcts else None

    peaks = find_run_peaks(multiples)
    num_runs = len(peaks)

    if num_runs < MIN_RUNS:
        return {
            "calibrated": False,
            "reason": "insufficient_runs",
            "runs": num_runs,
            "atr_pct_avg": atr_pct_avg,
            "updated": TODAY,
        }

    sorted_peaks = sorted(peaks)
    p50 = percentile(sorted_peaks, 50)
    p75 = percentile(sorted_peaks, 75)
    p90 = percentile(sorted_peaks, 90)
    max_seen = sorted_peaks[-1]

    signal = max(p75, SIGNAL_FLOOR)
    warn = max(p75 * 0.75, WARN_FLOOR)

    return {
        "signal": round(signal, 1),
        "warn": round(warn, 1),
        "p50": round(p50, 1),
        "p75": round(p75, 1),
        "p90": round(p90, 1),
        "max_seen": round(max_seen, 1),
        "runs": num_runs,
        "atr_pct_avg": atr_pct_avg,
        "calibrated": True,
        "updated": TODAY,
    }


# ---------------------------------------------------------------------------
# Ticker list builders
# ---------------------------------------------------------------------------

def load_position_tickers():
    with open(POSITIONS_FILE, "r") as f:
        data = json.load(f)
    tickers = []
    for pos in data.get("open_positions", []):
        if pos.get("status") == "active" and pos.get("ticker"):
            tickers.append(pos["ticker"].upper())
    return tickers


def load_watchlist_tickers():
    with open(WATCHLIST_FILE, "r") as f:
        data = json.load(f)
    tickers = []
    for item in data.get("watchlist", []):
        if item.get("ticker"):
            tickers.append(item["ticker"].upper())
    return tickers


def load_existing_calibration():
    if os.path.exists(CALIBRATION_FILE):
        with open(CALIBRATION_FILE, "r") as f:
            return json.load(f)
    return {}


def save_calibration(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Calibrate per-ticker peel thresholds.")
    parser.add_argument(
        "--mode",
        choices=["positions", "watchlist", "all"],
        required=True,
        help="Which ticker set to calibrate",
    )
    args = parser.parse_args()

    api_key, api_secret = get_alpaca_creds()

    tickers = []
    if args.mode in ("positions", "all"):
        pos_tickers = load_position_tickers()
        print("Positions tickers: {}".format(pos_tickers))
        tickers.extend(pos_tickers)
    if args.mode in ("watchlist", "all"):
        wl_tickers = load_watchlist_tickers()
        print("Watchlist tickers: {}".format(wl_tickers))
        tickers.extend(wl_tickers)

    # Deduplicate while preserving order
    seen = set()
    unique_tickers = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique_tickers.append(t)

    print("\nCalibrating {} ticker(s): {}\n".format(len(unique_tickers), unique_tickers))

    calibration = load_existing_calibration()

    calibrated_count = 0
    fallback_count = 0
    error_tickers = []

    for ticker in unique_tickers:
        print("  {} ... ".format(ticker), end="", flush=True)
        try:
            result = calibrate_ticker(ticker, api_key, api_secret)
            calibration[ticker] = result
            if result.get("calibrated"):
                calibrated_count += 1
                print(
                    "OK  signal={} warn={} runs={} p75={}".format(
                        result["signal"], result["warn"], result["runs"], result["p75"]
                    )
                )
            else:
                fallback_count += 1
                print(
                    "FALLBACK  reason={} runs={}".format(
                        result.get("reason", "unknown"), result.get("runs", 0)
                    )
                )
        except Exception as exc:
            error_tickers.append(ticker)
            print("ERROR  {}".format(exc))
        # Small pause between tickers to be rate-limit friendly
        time.sleep(0.3)

    save_calibration(calibration)

    print("\n--- Summary ---")
    print("Tickers processed : {}".format(len(unique_tickers)))
    print("Calibrated        : {}".format(calibrated_count))
    print("Fallback (floors) : {}".format(fallback_count))
    print("Fetch errors      : {}".format(len(error_tickers)))
    if error_tickers:
        print("  Errors on       : {}".format(error_tickers))
    print("Output written to : {}".format(CALIBRATION_FILE))


if __name__ == "__main__":
    main()
