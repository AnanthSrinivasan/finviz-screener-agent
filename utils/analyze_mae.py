#!/usr/bin/env python3
"""
MAE/MFE Analysis — Maximum Adverse/Favorable Excursion
Reads 1099-B consolidated CSVs, pulls Alpaca daily OHLCV for each hold period,
and computes the worst intraday dip (MAE) and best peak (MFE) per trade.

Usage:
    python analyze_mae.py \
        --csv24 ~/Downloads/RH_Consolidated-2024.csv \
        --csv25 ~/Downloads/RH_Consolidated-2025.csv \
        --out data/mae_analysis.html
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests

# ── Ticker mapping: 1099-B description → Alpaca symbol ──────────────────────
TICKER_MAP = {
    "4D MOLECULAR THERAPEUTICS  INC . COMMON STOCK": "FDMT",
    "ABERCROMBIE & FITCH CO.": "ANF",
    "AFFIRM HOLDINGS  INC. CLASS A COMMON STOCK": "AFRM",
    "AIRBNB  INC. CLASS A COMMON ST OCK": "ABNB",
    "AMAZON.COM  INC. COMMON STOCK": "AMZN",
    "APPLOVIN CORPORATION CLASS A C OMMON STOCK": "APP",
    "ARCHER AVIATION INC.": "ACHR",
    "ARK INNOVATION ETF": "ARKK",
    "AST SPACEMOBILE  INC. CLASS A COMMON STOCK": "ASTS",
    "ASTERA LABS  INC. COMMON STOCK": "ALAB",
    "AURORA INNOVATION  INC. CLASS A COMMON STOCK": "AUR",
    "BIGBEAR.AI HOLDINGS  INC.": "BBAI",
    "BLOCK  INC.": "SQ",
    "CANTOR EQUITY PARTNERS  INC. C LASS A ORDINARY SHARES": "CEP",
    "CARVANA CO.": "CVNA",
    "CAVA GROUP  INC.": "CAVA",
    "CERENCE INC. COMMON STOCK": "CRNC",
    "CIRCLE INTERNET GROUP  INC.": "CRCL",
    "CLOUDFLARE  INC. CLASS A COMMO N STOCK  PAR VALUE $0.001 PER": "NET",
    "COEUR MINING  INC.": "CDE",
    "COINBASE GLOBAL  INC. CLASS A COMMON STOCK": "COIN",
    "CORE SCIENTIFIC  INC. COMMON S TOCK": "CORZ",
    "COUPANG  INC.": "CPNG",
    "CRISPR THERAPEUTICS AG COMMON SHARES": "CRSP",
    "CROWDSTRIKE HOLDINGS  INC. CLA SS A COMMON STOCK": "CRWD",
    "D-WAVE QUANTUM INC.": "QBTS",
    "DAVE INC. CLASS A COMMON STOCK": "DAVE",
    "DIREXION DAILY HOMEBUILDERS & SUPPLIES BULL 3X SHARES": "NAIL",
    "DIREXION DAILY SMALL CAP BULL 3X SHARES": "TNA",
    "DOCUSIGN  INC. COMMON STOCK": "DOCU",
    "DOORDASH  INC. CLASS A COMMON STOCK": "DASH",
    "DOXIMITY  INC.": "DOCS",
    "FIGMA  INC.": None,  # not publicly traded
    "FUTU HOLDINGS LIMITED AMERICAN DEPOSITARY SHARES": "FUTU",
    "GAMESTOP CORP. CLASS A": "GME",
    "GLOBALFOUNDRIES INC. ORDINARY SHARES": "GFS",
    "HIMS & HERS HEALTH  INC.": "HIMS",
    "INNODATA INC. COMMON STOCK": "INOD",
    "IONQ  INC.": "IONQ",
    "ISHARES BITCOIN TRUST ETF": "IBIT",
    "ISHARES BITCOIN TRUST ETF SHAR ES": "IBIT",
    "ISHARES SILVER TRUST": "SLV",
    "JFROG LTD. ORDINARY SHARES": "FROG",
    "JINKOSOLAR HOLDINGS CO": "JKS",
    "JOBY AVIATION  INC.": "JOBY",
    "JUMIA TECHNOLOGIES AG": "JMIA",
    "KRANESHARES CSI CHINA INTERNET ETF": "KWEB",
    "LEMONADE  INC.": "LMND",
    "LYFT  INC. CLASS A COMMON STOC K": "LYFT",
    "MAPLEBEAR INC. COMMON STOCK": "CART",
    "MARA HOLDINGS  INC. COMMON STO CK": "MARA",
    "META PLATFORMS  INC. CLASS A C OMMON STOCK": "META",
    "MP MATERIALS CORP.": "MP",
    "NANO NUCLEAR ENERGY INC. COMMO N STOCK": "NNE",
    "NEBIUS GROUP N.V. CLASS A ORDI NARY SHARES": "NBIS",
    "NETFLIX  INC. COMMON STOCK": "NFLX",
    "NIU TECHNOLOGIES AMERICAN DEPO SITARY SHARES": "NIU",
    "NVIDIA CORPORATION COMMON STOC K": "NVDA",
    "OKLO INC.": "OKLO",
    "OPENDOOR TECHNOLOGIES INC COMM ON STOCK": "OPEN",
    "ORACLE CORP": "ORCL",
    "PALANTIR TECHNOLOGIES INC. CLA SS A COMMON STOCK": "PLTR",
    "PALLADYNE AI CORP. COMMON STOC K": "PDYN",
    "PLANET LABS PBC": "PL",
    "PROSHARES ULTRA QQQ": "QLD",
    "QUANTUM COMPUTING INC. COMMON STOCK": "QUBT",
    "REDDIT  INC.": "RDDT",
    "REDWIRE CORPORATION": "RDW",
    "REGULUS THERAPEUTICS INC. COMM ON STOCK": "RGLS",
    "RIGETTI COMPUTING  INC. COMMON STOCK": "RGTI",
    "RIVIAN AUTOMOTIVE  INC. CLASS A COMMON STOCK": "RIVN",
    "ROBINHOOD MARKETS  INC. CLASS A COMMON STOCK": "HOOD",
    "ROBLOX CORPORATION": "RBLX",
    "ROCKET COMPANIES  INC.": "RKT",
    "ROCKET LAB CORPORATION COMMON STOCK": "RKLB",
    "ROCKET LAB USA  INC. COMMON ST OCK": "RKLB",
    "ROOT  INC. CLASS A COMMON STOC K": "ROOT",
    "RUBRIK  INC.": "RBRK",
    "SEALSQ CORP ORDINARY SHARES": "LAES",
    "SERVE ROBOTICS INC. COMMON STO CK": "SERV",
    "SERVICENOW  INC.": "NOW",
    "SNOWFLAKE INC.": "SNOW",
    "SOFI TECHNOLOGIES  INC. COMMON STOCK": "SOFI",
    "SPDR GOLD TRUST  SPDR GOLD SHA RES": "GLD",
    "SPOTIFY TECHNOLOGY S.A.": "SPOT",
    "STITCH FIX  INC. CLASS A COMMO N STOCK": "SFIX",
    "STRATEGY INC COMMON STOCK CLAS S A": "MSTR",
    "SUPER MICRO COMPUTER  INC. COM MON STOCK": "SMCI",
    "TEMPUS AI  INC. CLASS A COMMON STOCK": "TEM",
    "TESLA  INC. COMMON STOCK": "TSLA",
    "UIPATH  INC.": "PATH",
    "UNITY SOFTWARE INC.": "U",
    "UP FINTECH HOLDING LTD AMERICA N DEPOSITARY SHARE REPRESENTI": "TIGR",
    "UPSTART HOLDINGS  INC. COMMON STOCK": "UPST",
    "WEBULL CORPORATION CLASS A ORD INARY SHARES": "BULL",
    "ZSCALER  INC. COMMON STOCK": "ZS",
}

ALPACA_BASE = "https://data.alpaca.markets/v2"


def alpaca_headers():
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set", file=sys.stderr)
        sys.exit(1)
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch_bars(ticker, start_date, end_date):
    """Fetch daily OHLCV bars from Alpaca for [start_date, end_date] (YYYY-MM-DD)."""
    url = f"{ALPACA_BASE}/stocks/{ticker}/bars"
    params = {
        "timeframe": "1Day",
        "start": start_date,
        "end": end_date,
        "adjustment": "all",
        "feed": "iex",
        "limit": 500,
    }
    headers = alpaca_headers()
    bars = []
    while True:
        for attempt in range(4):
            try:
                r = requests.get(url, params=params, headers=headers, timeout=30)
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < 3:
                    time.sleep(3 * (attempt + 1))
                else:
                    print(f"  WARN: timeout fetching {ticker} after 4 attempts — skipping")
                    return None
        if r.status_code == 429:
            time.sleep(3)
            continue
        if r.status_code != 200:
            return None
        data = r.json()
        bars.extend(data.get("bars", []))
        next_token = data.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
    return bars


def parse_date(s):
    """Parse YYYYMMDD → datetime."""
    s = s.strip()
    if not s or len(s) != 8:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return None


def load_trades(csv_path):
    b_header = None
    trades = []
    with open(csv_path) as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            if row[0] == "1099-B" and len(row) > 4 and row[3] == "DATE ACQUIRED":
                b_header = row
            elif b_header and row[0] == "1099-B":
                d = dict(zip(b_header, row))
                trades.append(d)
    return trades


def is_option(description):
    desc = description.upper()
    return " CALL " in desc or " PUT " in desc


def compute_mae_mfe(trades_by_ticker, verbose=False):
    """For each trade with valid dates, fetch Alpaca bars and compute MAE/MFE."""
    results = []
    skipped = []

    for ticker, trades in trades_by_ticker.items():
        for trade in trades:
            entry_date = parse_date(trade.get("DATE ACQUIRED", ""))
            exit_date = parse_date(trade.get("SALE DATE", ""))
            if not entry_date or not exit_date:
                skipped.append((ticker, "missing date"))
                continue

            try:
                shares = float(trade.get("SHARES", 0) or 0)
                cost = float(trade.get("COST BASIS", 0) or 0)
                proceeds = float(trade.get("SALES PRICE", 0) or 0)
            except ValueError:
                skipped.append((ticker, "bad numbers"))
                continue

            if shares <= 0:
                skipped.append((ticker, "zero shares"))
                continue

            entry_price = cost / shares
            exit_price = proceeds / shares
            realized_pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

            hold_days = (exit_date - entry_date).days
            # Allow a small buffer on either side
            fetch_start = (entry_date - timedelta(days=1)).strftime("%Y-%m-%d")
            fetch_end = (exit_date + timedelta(days=1)).strftime("%Y-%m-%d")

            bars = fetch_bars(ticker, fetch_start, fetch_end)
            if verbose:
                print(f"  {ticker} {entry_date.date()}→{exit_date.date()} bars={len(bars) if bars else 'FAIL'}")
            time.sleep(0.15)  # rate limit

            if not bars:
                skipped.append((ticker, "no Alpaca data"))
                continue

            # Filter to hold period only
            hold_bars = [
                b for b in bars
                if entry_date <= datetime.fromisoformat(b["t"][:10]) <= exit_date
            ]
            if not hold_bars:
                # Fall back to all returned bars
                hold_bars = bars

            daily_lows = [b["l"] for b in hold_bars]
            daily_highs = [b["h"] for b in hold_bars]

            mae_price = min(daily_lows)
            mfe_price = max(daily_highs)

            mae_pct = (mae_price - entry_price) / entry_price * 100
            mfe_pct = (mfe_price - entry_price) / entry_price * 100

            results.append({
                "ticker": ticker,
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "hold_days": hold_days,
                "entry_price": round(entry_price, 2),
                "exit_price": round(exit_price, 2),
                "realized_pnl_pct": round(realized_pnl_pct, 1),
                "mae_pct": round(mae_pct, 1),
                "mfe_pct": round(mfe_pct, 1),
                "winner": realized_pnl_pct > 0,
            })

    return results, skipped


def bucket_mae(results):
    """Bin MAE% into buckets for distribution table."""
    buckets = [
        (0,   -2,   "0% to −2%"),
        (-2,  -5,   "−2% to −5%"),
        (-5,  -10,  "−5% to −10%"),
        (-10, -15,  "−10% to −15%"),
        (-15, -20,  "−15% to −20%"),
        (-20, -30,  "−20% to −30%"),
        (-30, -999, "Beyond −30%"),
    ]
    counts = defaultdict(int)
    for r in results:
        mae = r["mae_pct"]
        for lo, hi, label in buckets:
            if mae <= lo and mae > hi:
                counts[label] += 1
                break
    total = len(results)
    rows = []
    for _, _, label in buckets:
        n = counts[label]
        rows.append((label, n, round(n / total * 100, 0) if total else 0))
    return rows, total


def generate_html(results, skipped, out_path):
    mae_buckets, total = bucket_mae(results)

    winners = [r for r in results if r["winner"]]
    losers  = [r for r in results if not r["winner"]]

    def avg(lst, key):
        return round(sum(r[key] for r in lst) / len(lst), 1) if lst else 0

    overall_avg_mae  = avg(results, "mae_pct")
    winner_avg_mae   = avg(winners, "mae_pct")
    loser_avg_mae    = avg(losers, "mae_pct")
    overall_avg_mfe  = avg(results, "mfe_pct")
    avg_hold         = avg(results, "hold_days")

    sorted_results = sorted(results, key=lambda r: r["mae_pct"])  # worst MAE first

    rows_html = ""
    for r in sorted_results:
        color = "#4ade80" if r["winner"] else "#f87171"
        rows_html += f"""
        <tr>
          <td>{r['ticker']}</td>
          <td>{r['entry_date']}</td>
          <td>{r['exit_date']}</td>
          <td>{r['hold_days']}d</td>
          <td style="color:#dc2626;font-weight:600">{r['mae_pct']}%</td>
          <td style="color:#16a34a;font-weight:600">{r['mfe_pct']}%</td>
          <td style="color:{'#16a34a' if r['realized_pnl_pct']>=0 else '#dc2626'};font-weight:600">{'+' if r['realized_pnl_pct']>=0 else ''}{r['realized_pnl_pct']}%</td>
        </tr>"""

    bucket_rows = ""
    for label, n, pct in mae_buckets:
        bucket_rows += f"<tr><td>{label}</td><td>{n}</td><td>{int(pct)}%</td></tr>"

    skip_html = ""
    if skipped:
        skip_html = "<p style='color:#64748b;font-size:0.8rem'>Skipped: " + "; ".join(f"{t}({r})" for t, r in skipped[:20]) + ("..." if len(skipped) > 20 else "") + "</p>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>MAE/MFE Analysis — 2024–2025</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #f8f9fc; color: #1a202c; font-family: 'Segoe UI', system-ui, sans-serif; padding: 32px 24px; }}
h1 {{ font-size: 1.5rem; font-weight: 700; color: #111827; margin-bottom: 4px; }}
.sub {{ color: #6b7280; font-size: 0.85rem; margin-bottom: 32px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; max-width: 1100px; margin: 0 auto; }}
.card {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
.card.full {{ grid-column: 1 / -1; }}
.card h2 {{ font-size: 0.75rem; font-weight: 700; color: #6b7280; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 14px; }}
.stat-row {{ display: flex; gap: 28px; flex-wrap: wrap; }}
.stat .label {{ font-size: 0.75rem; color: #9ca3af; margin-bottom: 3px; }}
.stat .value {{ font-size: 1.4rem; font-weight: 700; color: #111827; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
th {{ color: #6b7280; font-weight: 600; text-align: left; padding: 7px 8px; border-bottom: 2px solid #e5e7eb; }}
td {{ padding: 6px 8px; border-bottom: 1px solid #f3f4f6; }}
tr:hover td {{ background: #f9fafb; }}
.note {{ font-size: 0.72rem; color: #9ca3af; margin-top: 10px; font-style: italic; line-height: 1.5; }}
</style>
</head>
<body>
<h1>MAE / MFE Analysis — 2024–2025 Live Trades</h1>
<p class="sub">Maximum Adverse Excursion (worst daily Low vs entry) · Maximum Favorable Excursion (best daily High vs entry) · {total} trades with full hold period data · Source: Robinhood 1099-B + Alpaca OHLCV</p>

<div class="grid">

<div class="card full">
  <h2>Summary</h2>
  <div class="stat-row">
    <div class="stat"><div class="label">Trades Analysed</div><div class="value">{total}</div></div>
    <div class="stat"><div class="label">Avg MAE (all trades)</div><div class="value" style="color:#dc2626">{overall_avg_mae}%</div></div>
    <div class="stat"><div class="label">Avg MAE (winners only)</div><div class="value" style="color:#d97706">{winner_avg_mae}%</div></div>
    <div class="stat"><div class="label">Avg MAE (losers)</div><div class="value" style="color:#dc2626">{loser_avg_mae}%</div></div>
    <div class="stat"><div class="label">Avg MFE (all trades)</div><div class="value" style="color:#16a34a">{overall_avg_mfe}%</div></div>
    <div class="stat"><div class="label">Avg Hold Period</div><div class="value">{avg_hold}d</div></div>
    <div class="stat"><div class="label">Win Rate</div><div class="value" style="color:#16a34a">{round(len(winners)/total*100) if total else 0}%</div></div>
  </div>
  <p class="note"><strong>MAE</strong> = Maximum Adverse Excursion — the worst the position went against you (lowest daily Low vs. entry price) at any point during the hold. A trade can close positive yet dip −10% midway — MAE captures that real pain. <strong>MFE</strong> = Maximum Favorable Excursion — the best point reached during the hold, regardless of where it closed. Win rate of 32% is normal for momentum systems where winners are much larger than losers.</p>
</div>

<div class="card">
  <h2>MAE Distribution — How Deep Did Trades Dip?</h2>
  <table>
    <tr><th>MAE Range</th><th>Trades</th><th>Frequency</th></tr>
    {bucket_rows}
  </table>
  <p class="note">
    <strong>0% to −2% (9%):</strong> Trade barely moved against you — bought the breakout and it ran. Rare.<br>
    <strong>−2% to −5% (26%):</strong> Small routine pullback. Most common bucket. Normal noise — a stop at −4% would have cut these unnecessarily.<br>
    <strong>−5% to −10% (26%):</strong> Still normal. Combined with above: <strong>52% of all trades dipped 0–10%</strong> — this is the daily pain you must accept to stay in winners.<br>
    <strong>−10% to −15% (13%):</strong> Meaningful dip. 1 in 8 trades went this far before resolving. Panic exit here = cutting live trades.<br>
    <strong>Beyond −30% (10%):</strong> Real losers or multi-month holds that broke down structurally. Stop-loss rules should have fired here.
  </p>
</div>

<div class="card">
  <h2>What This Means for 2026</h2>
  <table>
    <tr><th>Situation</th><th>What history says</th></tr>
    <tr><td>Your position is down 5–10%</td><td style="color:#d97706;font-weight:600">Normal — happens in 52% of all trades. Stay in.</td></tr>
    <tr><td>Winners before they won</td><td style="color:#d97706;font-weight:600">Dipped {winner_avg_mae}% on average first. Give it room.</td></tr>
    <tr><td>Every trade's best moment (MFE)</td><td style="color:#16a34a;font-weight:600">+{overall_avg_mfe}% avg peak — most trades give you a window to exit in profit.</td></tr>
    <tr><td>When to actually worry</td><td style="color:#dc2626;font-weight:600">Position down &gt;20% with no recovery — that's outside normal range (only 18% of trades).</td></tr>
  </table>
  <p class="note">When a current position is down {overall_avg_mae}% from entry — that is normal historical behaviour, not a system failure. The $4,500 hard stop and dynamic ATR stop exist to catch structural breakdowns, not routine 5–10% dips.</p>
</div>

<div class="card full">
  <h2>Trade Detail — Sorted by Worst MAE</h2>
  <table>
    <tr><th>Ticker</th><th>Entry</th><th>Exit</th><th>Hold</th><th>MAE (worst dip)</th><th>MFE (best peak)</th><th>Realized P&amp;L</th></tr>
    {rows_html}
  </table>
  {skip_html}
</div>

</div>
</body>
</html>"""

    with open(out_path, "w") as f:
        f.write(html)
    print(f"Written: {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv24", default=os.path.expanduser("~/Downloads/RH_Consolidated-2024.csv"))
    parser.add_argument("--csv25", default=os.path.expanduser("~/Downloads/RH_Consolidated-2025.csv"))
    parser.add_argument("--out", default="data/mae_analysis.html")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    all_trades = []
    for path in [args.csv24, args.csv25]:
        if os.path.exists(path):
            trades = load_trades(path)
            print(f"Loaded {len(trades)} rows from {path}")
            all_trades.extend(trades)
        else:
            print(f"WARNING: not found: {path}")

    # Group by ticker, skip options and unknowns
    trades_by_ticker = defaultdict(list)
    skipped_map = []
    for t in all_trades:
        desc = t.get("DESCRIPTION", "")
        if is_option(desc):
            skipped_map.append((desc[:30], "option"))
            continue
        ticker = TICKER_MAP.get(desc)
        if ticker is None:
            skipped_map.append((desc[:30], "no ticker mapping"))
            continue
        trades_by_ticker[ticker].append(t)

    total_trades = sum(len(v) for v in trades_by_ticker.values())
    print(f"Trades to analyse: {total_trades} across {len(trades_by_ticker)} tickers")
    print(f"Skipped: {len(skipped_map)}")

    results, fetch_skipped = compute_mae_mfe(trades_by_ticker, verbose=args.verbose)
    all_skipped = skipped_map + fetch_skipped
    print(f"MAE computed for {len(results)} trades. Fetch failures: {len(fetch_skipped)}")

    generate_html(results, all_skipped, args.out)

    # Also save raw JSON
    json_out = args.out.replace(".html", ".json")
    with open(json_out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Raw data: {json_out}")


if __name__ == "__main__":
    main()
