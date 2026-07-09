# Fills — real SnapTrade fills vs recorded closes

Pull actual BUY/SELL fills from SnapTrade across ALL connected accounts for the last N days (default 10; `$ARGUMENTS` may override, e.g. `/fills 30`), report the sync watermark, and flag every recently closed position whose recorded exit is still a quote **estimate** rather than a real fill. Never ask the user for fill prices — this command is the answer to that question.

Run this Python snippet via Bash (read-only, no confirmation needed):

```python
import datetime as dt, json
from dotenv import load_dotenv; load_dotenv()
import agents.trading.position_monitor as pm

DAYS = 10  # override from $ARGUMENTS if given

accts = pm.snaptrade_get("/accounts") or []
start = (dt.date.today() - dt.timedelta(days=DAYS)).isoformat()

fills, watermark = [], None
for a in accts:
    label = f"{a.get('name','?')} ••{str(a.get('number',''))[-4:]}"
    bal = (a.get("balance") or {}).get("total") or {}
    offset = 0
    while True:
        resp = pm.snaptrade_get(f"/accounts/{a['id']}/activities",
                                params={"startDate": start, "limit": 200, "offset": offset})
        data = resp.get("data") if isinstance(resp, dict) else (resp or [])
        if not data:
            break
        for act in data:
            typ = (act.get("type") or "").upper()
            sym = act.get("symbol") or {}
            inner = sym.get("symbol") if isinstance(sym, dict) else sym
            tk = inner.get("symbol") if isinstance(inner, dict) else (inner or (sym.get("local_id") if isinstance(sym, dict) else sym))
            date = (act.get("trade_date") or "")[:16]
            if date and (watermark is None or date > watermark):
                watermark = date
            if typ in ("BUY", "SELL", "BOUGHT", "SOLD") and tk:
                fills.append((date, label, "BUY" if typ in ("BUY","BOUGHT") else "SELL",
                              tk, act.get("units"), act.get("price")))
        if len(data) < 200:
            break
        offset += len(data)

print(f"=== FILLS last {DAYS}d (all accounts) — feed synced through: {watermark or 'NO ACTIVITY IN WINDOW'} ===")
for f in sorted(fills):
    print(f"  {f[0]}  {f[2]:4} {f[3]:6} {f[4]} @ {f[5]}   [{f[1]}]")

ESTIMATE_SOURCES = {"live_quote", "fallback_high", "user_reported_breakeven"}
closed = json.load(open("data/positions.json")).get("closed_positions", [])
cutoff = (dt.date.today() - dt.timedelta(days=14)).isoformat()
print("\n=== RECENT CLOSES — fill status ===")
real_sells = {f[3]: f for f in fills if f[2] == "SELL"}
for p in closed:
    if (p.get("close_date") or "") < cutoff:
        continue
    tk, src = p["ticker"], p.get("close_source", "?")
    tag = "REAL FILL" if src.startswith("snaptrade") else f"ESTIMATE ({src}) — not yet synced"
    line = f"  {tk:6} closed {p.get('close_date')} @ {p.get('close_price')} ({p.get('result_pct')}%)  [{tag}]"
    if tk in real_sells and src in ESTIMATE_SOURCES:
        line += f"  → real SELL now available: {real_sells[tk][5]} on {real_sells[tk][0]} — retro-patch will correct"
    print(line)
```

Then report, in this order:

1. **Sync watermark** — the latest activity date SnapTrade has. If the user sold after that date, say plainly: "your sells haven't synced yet (Robinhood lag, 24–48h); the recorded exits are estimates."
2. **Real fills table** — what actually filled, per account.
3. **Estimate flags** — every close in the last 14 days whose `close_source` is `live_quote` / `fallback_high` / `user_reported_breakeven`, with the caveat that its P&L is provisional. If a real SELL fill for that ticker is now in the feed, recompute the true P&L (`(fill − entry) × shares`) inline and note the retro-patch will persist it on the next monitor run.
4. If the user stated a fill price that conflicts with an estimate, trust the user's number until the feed syncs.

Do NOT hand-edit `positions.json` close prices from estimates or memory — the retro-patch in `position_monitor.py` writes real fills once SnapTrade syncs.
