# Peel Status

For each open position in `data/positions.json`, compute the current ATR% multiple from SMA50, show where it stands against calibrated peel thresholds, and end with a **TODAY'S ORDERS** block — one plain, non-emotional instruction per position (stop check first, peel check second).

Run this Python snippet via Bash (no confirmation needed — read-only):

```python
import json, math
import numpy as np
import yfinance as yf

positions = json.load(open("data/positions.json")).get("open_positions", [])
calib = json.load(open("data/peel_calibration.json"))

TIER_FALLBACK = [(4, 3.0, 4.0), (7, 5.0, 6.0), (10, 6.5, 8.0), (999, 8.5, 10.0)]

def wilder_atr(h, n=14):
    tr = np.maximum(h["High"]-h["Low"],
         np.maximum(abs(h["High"]-h["Close"].shift(1)),
                    abs(h["Low"]-h["Close"].shift(1)))).dropna()
    atr = [tr.iloc[:n].mean()]
    for v in tr.iloc[n:]:
        atr.append((atr[-1]*(n-1) + v) / n)
    return atr[-1]

orders = []
print(f"{'Ticker':<6} {'Price':>7} {'Gain%':>6} {'ATR%':>5} {'Mult':>5} {'Warn':>5} {'Sig':>5} {'P90':>5} {'Max':>5}  Status")
print("-"*80)
for p in positions:
    ticker = p["ticker"]
    entry  = p["entry_price"]
    stop   = p.get("stop_price")
    shares = p.get("shares", 0)
    gain   = p.get("current_gain_pct", 0)
    h = yf.Ticker(ticker).history(period="100d", interval="1d")
    if len(h) < 60:
        print(f"{ticker:<6} DATA ERR — only {len(h)} bars from yfinance, check manually (Finviz)")
        orders.append(f"{ticker}: DATA ERR — verify price vs stop ${stop} manually before the open.")
        continue
    price  = float(h["Close"].iloc[-1])
    sma50  = h["Close"].rolling(50).mean().iloc[-1]
    atr14  = wilder_atr(h)
    atr_pct = atr14 / price * 100
    mult   = (price - sma50) * price / (sma50 * atr14) if sma50 and atr14 else 0
    if math.isnan(mult) or math.isnan(atr_pct):
        print(f"{ticker:<6} DATA ERR — NaN metrics, check manually (Finviz)")
        orders.append(f"{ticker}: DATA ERR — verify price vs stop ${stop} manually before the open.")
        continue

    c = calib.get(ticker, {})
    if c.get("calibrated"):
        warn, sig, p90, mx = c["warn"], c["signal"], c.get("p90", 0), c.get("max_seen", 0)
        src = "calibrated"
    else:
        warn, sig, p90, mx = TIER_FALLBACK[-1][1], TIER_FALLBACK[-1][2], 0, 0
        for threshold, w, s in TIER_FALLBACK:
            if atr_pct <= threshold:
                warn, sig, p90, mx = w, s, 0, 0
                break
        src = "fallback"

    if mult >= mx and mx > 0:       status = "MAX ZONE 🔴"
    elif mult >= p90 and p90 > 0:   status = "P90+ 🟠"
    elif mult >= sig:                status = "SIGNAL 🟡"
    elif mult >= warn:               status = "WARN ⚠️"
    elif mult >= warn * 0.75:        status = "APPROACHING"
    else:                            status = "OK ✅"

    p90_str = f"{p90:.1f}" if p90 else "—"
    mx_str  = f"{mx:.1f}"  if mx  else "—"
    print(f"{ticker:<6} ${price:>6.2f} {gain:>+5.1f}% {atr_pct:>4.1f}% {mult:>5.1f}x {warn:>5.1f}x {sig:>5.1f}x {p90_str:>5} {mx_str:>5}  {status} ({src})")

    # ---- order logic: stop first, peel second, else hold ----
    if stop and price < stop:
        orders.append(f"{ticker}: SELL all {shares} at open — price ${price:.2f} is below stop ${stop:.2f}.")
    elif stop and (price - stop) / price * 100 < 0.5:
        orders.append(f"{ticker}: SELL all {shares} today only if it CLOSES below ${stop:.2f} — it is within 0.5% of the stop.")
    elif status.startswith(("MAX", "P90", "SIGNAL")):
        orders.append(f"{ticker}: PEEL half ({shares//2} shares) today — extension {mult:.1f}x is at/past the {sig:.1f}x signal.")
    elif status.startswith("WARN"):
        orders.append(f"{ticker}: NO ADD. Prepare to peel half if extension reaches {sig:.1f}x (now {mult:.1f}x).")
    else:
        orders.append(f"{ticker}: HOLD — no action. Stop ${stop:.2f} stands." if stop else f"{ticker}: HOLD — no action (no stop on file — set one).")

print()
print("TODAY'S ORDERS")
print("-"*80)
for o in orders:
    print(f"  {o}")
```

Execute it directly with Bash — no questions, just run and print the table.

After printing, relay the TODAY'S ORDERS block to the user verbatim as the summary — one line per position, no hedging, no added commentary beyond flagging any DATA ERR rows.
