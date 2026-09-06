"""Run the EU criteria over a TradingView screener CSV export.

    python3 run.py export.csv
    python3 run.py export.csv --all      # show rejects and why

No network. No writes outside this folder. Feed it whatever market you like —
the gate is price-based, so it works on any exchange.

Getting the CSV: TradingView → Screener → pick your market → add the columns
Price, SMA20, SMA50, SMA200, ATR, Relative Volume, 52-week High → Export.
Column names vary by locale/version; if a column can't be matched the script
prints what it did find so the mapping can be fixed.
"""
import csv, sys
from criteria import evaluate

# candidate header spellings → our normalized key
ALIASES = {
    "ticker":   ["ticker", "symbol", "name"],
    "close":    ["close", "price", "last"],
    "sma20":    ["sma20", "sma 20", "simple moving average (20)", "ma20"],
    "sma50":    ["sma50", "sma 50", "simple moving average (50)", "ma50"],
    "sma200":   ["sma200", "sma 200", "simple moving average (200)", "ma200"],
    "atr_pct":  ["atr%", "atr percent", "atr_pct"],
    "atr_abs":  ["atr", "average true range (14)"],
    "high_52w": ["52 week high", "52w high", "high 52w", "52-week high"],
    "rvol":     ["relative volume", "rvol", "rel volume"],
}


def _map_headers(headers):
    low = {h.strip().lower(): h for h in headers}
    out = {}
    for key, names in ALIASES.items():
        for n in names:
            if n in low:
                out[key] = low[n]
                break
    return out


def _num(v):
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except Exception:
        return None


def load(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        m = _map_headers(rdr.fieldnames or [])
        need = ["ticker", "close", "sma20", "sma50", "sma200", "high_52w"]
        missing = [k for k in need if k not in m]
        if missing:
            sys.exit(f"Could not map columns {missing}.\nFound headers: {rdr.fieldnames}\n"
                     f"Add the spelling to ALIASES in run.py.")
        rows, skipped = [], 0
        for r in rdr:
            row = {"ticker": r[m["ticker"]]}
            ok = True
            for k in ("close", "sma20", "sma50", "sma200", "high_52w"):
                row[k] = _num(r[m[k]])
                if row[k] in (None, 0):
                    ok = False
            if not ok:
                skipped += 1
                continue
            row["rvol"] = _num(r.get(m.get("rvol", ""), "")) or 0.0
            atr = _num(r.get(m.get("atr_pct", ""), ""))
            if atr is None and "atr_abs" in m:          # ATR given absolute
                a = _num(r.get(m["atr_abs"], ""))
                atr = (a / row["close"] * 100.0) if a else None
            row["atr_pct"] = atr if atr is not None else 0.0
            rows.append(row)
    return rows, skipped


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rows, skipped = load(sys.argv[1])
    show_all = "--all" in sys.argv
    res = [evaluate(r) for r in rows]
    ok = [r for r in res if r["passes"]]
    # least extended first — closest to the 50 MA is the best entry
    ok.sort(key=lambda r: r["atr_multiple"])

    print(f"\nrows {len(rows)}  (skipped {skipped} incomplete)   PASSED {len(ok)}\n")
    hdr = f"{'TICKER':<12}{'close':>10}{'dist52':>9}{'ATR%':>7}{'mult':>7}{'warn':>6}{'S20%':>7}{'RVol':>6}"
    print(hdr); print("-" * len(hdr))
    for r in ok:
        print(f"{r['ticker'][:11]:<12}{r['close']:>10.2f}{r['dist_high_pct']:>8.1f}%"
              f"{r['atr_pct']:>6.1f}%{r['atr_multiple']:>7.2f}{r['peel_warn']:>6.1f}"
              f"{r['sma20_pct']:>6.1f}%{r['rvol']:>6.2f}")
    if not ok:
        print("  none passed — that is a valid result, not an error")
    if show_all:
        print("\nrejected:")
        for r in res:
            if not r["passes"]:
                print(f"  {r['ticker'][:11]:<12}{r['stage']['label']:<22}{', '.join(r['fails'])}")


if __name__ == "__main__":
    main()
