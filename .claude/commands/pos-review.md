# Position Review

Pull live SnapTrade positions, fetch live quotes + Finviz technicals, show sizing, buy-point quality, and peel/hold/cut verdicts. Read-only — run without confirmation.

Run this Python snippet via Bash:

```python
import sys, urllib.request, re
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('.env')
from agents.trading.position_monitor import fetch_positions, snaptrade_get
from agents.screener.finviz_agent import get_snapshot_metrics, compute_stage

def live_price(t):
    try:
        req = urllib.request.Request(f'https://finviz.com/quote.ashx?t={t}',
                                     headers={'User-Agent':'Mozilla/5.0'})
        html = urllib.request.urlopen(req, timeout=10).read().decode()
        m = re.search(r'class="quote-price[^"]*"[^>]*>([\d,.]+)', html)
        return float(m.group(1).replace(',','')) if m else None
    except: return None

# Account balance
accounts = snaptrade_get('/accounts') or []
total_equity = cash = bp = 0
for a in accounts:
    bal_obj = a.get('balance') or {}
    total_equity += (bal_obj.get('total') or bal_obj).get('amount', 0) or 0
    bals = snaptrade_get(f"/accounts/{a['id']}/balances") or []
    for b in bals:
        cash += b.get('cash', 0) or 0
        bp += b.get('buying_power', 0) or 0

positions = fetch_positions()
rows = []
total_mv = total_pl = 0
for p in positions:
    t = p['ticker']
    sh = p['shares']; ac = p['avg_cost']
    live = live_price(t) or p.get('current_price', 0)
    if not live: continue
    gain = (live - ac) / ac * 100 if ac else 0
    pl = (live - ac) * sh
    mv = live * sh
    total_mv += mv; total_pl += pl
    # technicals
    m = get_snapshot_metrics(t)
    if m:
        atr, eps, sales, dist52, rvol, av, s20, s50, s200, eq, io, it, pm, pq, ph, py = m
        si = compute_stage({'SMA20%': s20, 'SMA50%': s50, 'SMA200%': s200})
        stage = f"{si['stage']}{'P' if si['perfect'] else ''}"
    else:
        atr=s20=s50=dist52=rvol=0; stage='?'
    rows.append({'t':t,'sh':sh,'ac':ac,'live':live,'gain':gain,'pl':pl,'mv':mv,
                 'atr':atr,'s20':s20,'s50':s50,'dist52':dist52,'rvol':rvol,'stage':stage})

rows.sort(key=lambda r: -r['mv'])

def verdict(r):
    notes = []
    if r['gain'] < -5: notes.append('🚨 CUT — past stop zone')
    elif r['gain'] >= 20: notes.append('💰 PEEL ½ (T1 rule)')
    elif r['gain'] >= 10: notes.append('🟢 trail tighter')
    elif r['gain'] >= 7 and r['atr'] > 7: notes.append('⚠ peel ⅓ — high vol extended')
    elif r['gain'] >= 5: notes.append('✅ working, hold')
    elif r['gain'] >= 0: notes.append('hold')
    else: notes.append('watch — give a day')
    if r['s20'] > 20: notes.append(f"ext +{r['s20']:.0f}% S20")
    if r['stage'] not in ('2P','2'): notes.append(f"⚠ {r['stage']}")
    return ' · '.join(notes)

print(f"\n=== ACCOUNT ===")
print(f"Total equity: ${total_equity:>12,.0f}")
print(f"Cash:         ${cash:>12,.0f}   (negative = margin debt)")
print(f"Buying power: ${bp:>12,.0f}")
print(f"Position MV:  ${total_mv:>12,.0f}")
print(f"Unrealized:   ${total_pl:>+12,.0f}")
margin_pct = ((-cash) / total_equity * 100) if (total_equity and cash < 0) else 0
print(f"Leverage:     {margin_pct:>12.0f}%")

print(f"\n=== {len(rows)} POSITIONS — sorted by MV ===")
print(f"{'TKR':6s} {'Sh':>4s} {'Avg':>9s} {'Live':>9s} {'Δ%':>6s} {'$P/L':>8s} {'MV':>9s} {'%Bk':>5s} {'ATR':>4s} {'S20':>5s} {'St':>3s}  Verdict")
print('-'*145)
for r in rows:
    pct = r['mv']/total_mv*100 if total_mv else 0
    print(f"{r['t']:6s} {r['sh']:>4.0f} {r['ac']:>9.2f} {r['live']:>9.2f} {r['gain']:>+6.1f} {r['pl']:>+8.0f} {r['mv']:>9.0f} {pct:>4.1f}% {r['atr']:>4.1f} {r['s20']:>+5.1f} {r['stage']:>3s}  {verdict(r)}")

# Summary actions
peels = [r for r in rows if r['gain'] >= 7 and r['atr'] > 7 or r['gain'] >= 10]
cuts  = [r for r in rows if r['gain'] <= -5]
if peels:
    print(f"\n💰 PEEL CANDIDATES ({len(peels)}):  " + ', '.join(f"{r['t']}+{r['gain']:.1f}%" for r in peels))
if cuts:
    print(f"🚨 CUT TODAY ({len(cuts)}):  " + ', '.join(f"{r['t']}{r['gain']:.1f}%" for r in cuts))
```

Execute directly — no questions, just print the tables.
