"""TEMP diagnostic — live check of the patched screener parser. Delete after use."""
from agents.screener.finviz_agent import screener_urls, fetch_all_tickers

total = set()
for name, url in screener_urls.items():
    df, meta = fetch_all_tickers(url)
    total |= set(df["Ticker"])
    t0 = df["Ticker"].iloc[0] if len(df) else None
    print(f"LIVE {name}: {len(df)} tickers; sample {t0} -> {meta.get(t0)}")
print("LIVE TOTAL UNIQUE:", len(total))
assert total, "still 0 tickers"
