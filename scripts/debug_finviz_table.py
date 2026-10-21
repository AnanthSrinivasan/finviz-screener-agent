"""TEMP diagnostic — dump Finviz screener page structure. Delete after use."""
import collections, re
from bs4 import BeautifulSoup
from agents.screener.finviz_agent import session, screener_urls

url = screener_urls["Growth"]
resp = session.get(url + "&r=1", timeout=15)
html = resp.text
print("STATUS", resp.status_code, "LEN", len(html), "URL", resp.url)
print("HISTORY", [(h.status_code, h.headers.get("Location")) for h in resp.history])
soup = BeautifulSoup(html, "html.parser")
for kw in ["data-boxover-ticker", "tab-link", "stock?t=", "quote.ashx?t=", "screener-body",
           "styled-row", "__NEXT_DATA__", "window.__", "\"ticker\"", "\"Ticker\"", "role=\"row\"",
           "<table", "<tr", "Total:", "#1 /"]:
    print(f"COUNT {kw!r}: {html.count(kw)}")
tags = collections.Counter(t.name for t in soup.find_all(True))
print("TAGS", tags.most_common(25))
for s in soup.find_all("script"):
    txt = s.string or ""
    if len(txt) > 2000:
        print("BIG SCRIPT", s.get("id"), s.get("type"), len(txt), repr(txt[:600]))
i = html.find("stock?t=")
if i < 0: i = html.find("quote.ashx?t=")
print("CONTEXT AROUND FIRST TICKER LINK:\n", html[max(0, i-2500):i+2500] if i >= 0 else "none")
for m in list(re.finditer(r'"ticker"\s*:\s*"[A-Z.\-]+"', html))[:3]:
    print("JSON CTX:", html[max(0, m.start()-400):m.start()+1200])
print("TEXT SNIPPET:", soup.get_text(" ", strip=True)[:3000])
