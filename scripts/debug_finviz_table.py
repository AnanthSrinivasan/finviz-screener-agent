"""TEMP diagnostic — dump Finviz screener-table structure. Delete after use."""
import collections
from bs4 import BeautifulSoup
from agents.screener.finviz_agent import session, screener_urls

url = screener_urls["Growth"]
resp = session.get(url + "&r=1", timeout=15)
print("STATUS", resp.status_code, "LEN", len(resp.text), "URL", resp.url)
soup = BeautifulSoup(resp.text, "html.parser")
print("TITLE", soup.title.get_text(strip=True) if soup.title else None)
print("valign=top rows:", len(soup.select('tr[valign="top"]')))
cnt = collections.Counter(len(r.find_all("td")) for r in soup.find_all("tr"))
print("td-count histogram over all <tr>:", sorted(cnt.items()))
for t in soup.find_all("table"):
    cls = t.get("class"); tid = t.get("id")
    if cls or tid:
        print("TABLE", tid, cls, "rows", len(t.find_all("tr")))
tl = soup.select_one("a.tab-link") or soup.select_one("[data-boxover-ticker]")
if tl:
    row = tl.find_parent("tr")
    print("FIRST TICKER ROW HTML:\n", str(row)[:3000])
    print("HEADER ROW:\n", str(row.find_previous("tr"))[:2000] if row else None)
else:
    print("NO ticker link found. BODY SNIPPET:\n", resp.text[:4000])
