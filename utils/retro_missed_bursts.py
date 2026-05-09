"""Retro: find tickers that appeared in screeners and subsequently ran +30% within 30 days,
then classify why we didn't surface them (which gate excluded each)."""
from __future__ import annotations
import os
import glob
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"


def load_first_appearance() -> pd.DataFrame:
    """Earliest screener row per ticker in last 60 days."""
    files = sorted(glob.glob(str(DATA / "finviz_screeners_2026-0[3-5]*.csv")))
    rows = []
    for f in files:
        date = Path(f).stem.split("_")[-1]
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        df["date"] = date
        rows.append(df)
    if not rows:
        return pd.DataFrame()
    full = pd.concat(rows, ignore_index=True)
    full["date"] = pd.to_datetime(full["date"])
    full = full.sort_values("date")
    first = full.drop_duplicates(subset=["Ticker"], keep="first").copy()
    return first


def fetch_max_after(tickers: list[str], start_date: str) -> dict:
    """For each ticker fetch daily bars start_date..today, return max high."""
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    out = {}
    # Alpaca multi-symbol bars
    BATCH = 100
    for i in range(0, len(tickers), BATCH):
        batch = [t for t in tickers[i : i + BATCH] if "-" not in t and "." not in t]
        if not batch:
            continue
        params = {
            "symbols": ",".join(batch),
            "timeframe": "1Day",
            "start": start_date,
            "limit": 10000,
            "adjustment": "split",
            "feed": "iex",
        }
        r = requests.get(BARS_URL, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            print(f"  err {r.status_code}: {r.text[:200]}", file=sys.stderr)
            continue
        bars = r.json().get("bars", {})
        for tk, rows in bars.items():
            if not rows:
                continue
            entry_close = rows[0]["c"]
            future_max_high = max((b["h"] for b in rows[1:]), default=entry_close)
            future_max_close = max((b["c"] for b in rows[1:]), default=entry_close)
            # find date of max close
            max_close_date = None
            for b in rows[1:]:
                if b["c"] == future_max_close:
                    max_close_date = b["t"][:10]
                    break
            out[tk] = {
                "entry_close": entry_close,
                "future_max_high": future_max_high,
                "future_max_close": future_max_close,
                "max_close_date": max_close_date,
                "days": len(rows) - 1,
            }
    return out


def classify_exclusion(row) -> list[str]:
    """Why didn't this name hit Ready / Fresh Breakout / Hidden Growth?"""
    reasons = []
    dist = float(row.get("Dist From High%", 0) or 0)
    rvol = float(row.get("Rel Volume", 0) or 0)
    atr = float(row.get("ATR%", 0) or 0)
    q = float(row.get("Quality Score", 0) or 0)
    sma50 = float(row.get("SMA50%", 0) or 0)
    sma200 = float(row.get("SMA200%", 0) or 0)
    sma20 = float(row.get("SMA20%", 0) or 0)
    apps = int(row.get("Appearances", 0) or 0)
    eps_ttm = float(row.get("EPS Y/Y TTM", 0) or 0)
    eps_qq = float(row.get("EPS Q/Q", 0) or 0)
    inst = float(row.get("Inst Trans", 0) or 0)
    stage_str = str(row.get("Stage", ""))
    perfect = "'perfect': True" in stage_str

    # Ready to Enter: stage2 perfect, VCP≥70, Q≥80, dist -1..-12, ATR≤7, RVol≤1.2
    # (gate softened from -10 to -12 May 2026 — MTSI/RMBS class)
    rte_pass = perfect and q >= 80 and -12 <= dist <= -1 and atr <= 7 and rvol <= 1.2
    # Fresh Breakout: stage2, sma20>0, 0<sma50≤25, sma200>0, ATR≤8, Q≥70, dist 0..-12
    # RVol default ≥1.2 OR Q≥80+ATR≤6+RVol≥1.0 (tight-quality exception May 2026)
    fb_rvol_ok = rvol >= 1.2 or (q >= 80 and atr <= 6 and rvol >= 1.0)
    fb_pass = (
        ("Stage 2" in stage_str or perfect)
        and sma20 > 0
        and 0 < sma50 <= 25
        and sma200 > 0
        and fb_rvol_ok
        and atr <= 8
        and q >= 70
        and -12 <= dist <= 0
    )
    # Fresh Breakout WIDER (proposed): dist -20..0 with RVol≥1.2
    fb_wide_pass = (
        ("Stage 2" in stage_str or perfect)
        and sma20 > 0
        and 0 < sma50 <= 25
        and sma200 > 0
        and rvol >= 1.2
        and atr <= 8
        and q >= 70
        and -20 <= dist <= 0
    )
    # Base Building: stage2, Q≥75, dist -25..-12, ATR≤7
    bb_pass = (
        ("Stage 2" in stage_str or perfect)
        and q >= 75
        and -25 <= dist <= -12
        and atr <= 7
    )
    # HTF Base Reclaim (proxy — actual signal also gates on swing-pivot dist):
    # stage2 perfect, Q≥75, dist<-12, rising MA stack, ATR≤7, RVol≥1.0.
    # Without per-day Alpaca bars we can't compute swing dist here; this proxy
    # marks names that *could* qualify if their swing pivot is tight.
    htf_proxy = (
        perfect
        and q >= 75
        and dist < -12
        and atr <= 7
        and sma20 > 0
        and sma50 > 0
        and sma200 > 0
        and rvol >= 1.0
    )
    # Hidden Growth: count criteria
    hg_count = sum(
        [
            apps >= 3,
            eps_ttm > 50,
            (eps_qq > 50 or (eps_ttm < 0 and eps_qq > 20)),
            inst >= 3,
            perfect,
        ]
    )
    hg_pass = hg_count >= 3

    surfaced = rte_pass or fb_pass or bb_pass or hg_pass or htf_proxy
    surfaced_wide = rte_pass or fb_wide_pass or bb_pass or hg_pass or htf_proxy

    return {
        "surfaced": surfaced,
        "surfaced_wide": surfaced_wide,
        "rte": rte_pass,
        "fb": fb_pass,
        "fb_wide": fb_wide_pass,
        "bb": bb_pass,
        "hg": hg_pass,
        "hg_count": hg_count,
        "htf_base_reclaim": htf_proxy,
    }


def main():
    first = load_first_appearance()
    if first.empty:
        print("no screener data")
        return
    print(f"loaded {len(first)} unique tickers across {first['date'].nunique()} days")

    tickers = first["Ticker"].tolist()
    earliest = first["date"].min().date().isoformat()
    print(f"fetching Alpaca bars {earliest}..today for {len(tickers)} tickers")
    bars = fetch_max_after(tickers, earliest)
    print(f"got bars for {len(bars)} tickers")

    rows = []
    for _, r in first.iterrows():
        tk = r["Ticker"]
        b = bars.get(tk)
        if not b or b["entry_close"] <= 0:
            continue
        # use the row's future bars relative to row's date — we need per-ticker bars from row date
        # but we used earliest_date for all; max_close still anchored to its own date
        # so recompute pct from this row's date by re-fetching slice — for simplicity use row date close
        # Actually entry_close is the close on earliest day, not on row.date. Skip & refetch per-ticker if mismatch.
        rows.append({"ticker": tk, "first_date": r["date"].date().isoformat(), "row": r, "bars": b})

    # to be honest about returns we need per-row entry, but since most tickers first-appeared on different dates,
    # we'll refetch using individual ticker dates. To minimize calls, batch by date.
    by_date = {}
    for x in rows:
        by_date.setdefault(x["first_date"], []).append(x["ticker"])

    print(f"refetching by first-appearance date for accurate returns ({len(by_date)} dates)")
    accurate = {}
    for d, tks in by_date.items():
        b = fetch_max_after(tks, d)
        for tk, info in b.items():
            accurate[tk] = info

    results = []
    for x in rows:
        tk = x["ticker"]
        b = accurate.get(tk)
        if not b:
            continue
        ec = b["entry_close"]
        if ec <= 0:
            continue
        gain = (b["future_max_close"] - ec) / ec * 100
        cls = classify_exclusion(x["row"])
        results.append(
            {
                "ticker": tk,
                "first_date": x["first_date"],
                "entry_close": round(ec, 2),
                "max_close": round(b["future_max_close"], 2),
                "max_close_date": b["max_close_date"],
                "gain_pct": round(gain, 1),
                "dist": float(x["row"].get("Dist From High%", 0) or 0),
                "rvol": float(x["row"].get("Rel Volume", 0) or 0),
                "atr": float(x["row"].get("ATR%", 0) or 0),
                "q": float(x["row"].get("Quality Score", 0) or 0),
                "apps": int(x["row"].get("Appearances", 0) or 0),
                "stage_perfect": "'perfect': True" in str(x["row"].get("Stage", "")),
                **cls,
            }
        )

    df = pd.DataFrame(results)
    df = df.sort_values("gain_pct", ascending=False)
    print(f"\n=== top 25 gainers since first appearance ===")
    print(
        df.head(25)[
            [
                "ticker",
                "first_date",
                "gain_pct",
                "dist",
                "rvol",
                "atr",
                "q",
                "apps",
                "stage_perfect",
                "surfaced",
                "surfaced_wide",
                "rte",
                "fb",
                "fb_wide",
                "bb",
                "hg",
            ]
        ].to_string(index=False)
    )

    bursts = df[df["gain_pct"] >= 30].copy()
    print(f"\n=== {len(bursts)} tickers ran +30% from first appearance ===")
    not_surfaced = bursts[~bursts["surfaced"]]
    saved_by_wide = bursts[~bursts["surfaced"] & bursts["surfaced_wide"]]
    print(f"of those: {len(not_surfaced)} were NOT surfaced by current rules")
    print(f"of those: {len(saved_by_wide)} would be saved by Fresh Breakout dist→-20%")

    print(f"\n=== top 30 missed bursts (current rules excluded, gain ≥ 30%) ===")
    print(
        not_surfaced.head(30)[
            ["ticker", "first_date", "gain_pct", "dist", "rvol", "atr", "q", "apps", "stage_perfect"]
        ].to_string(index=False)
    )

    out = ROOT / "data" / "retro_missed_bursts.json"
    df.to_json(out, orient="records", indent=2)
    print(f"\nsaved → {out}")


if __name__ == "__main__":
    main()
