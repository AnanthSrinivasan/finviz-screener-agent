"""
finviz_earnings_alert.py

Runs weekdays at 21:00 CET (before the daily screener).
- Reads this week's screener CSVs from data/
- Finds tickers that appeared 2+ days this week
- Fetches earnings dates from Finviz for each ticker
- If earnings within 7 days — fires a Slack alert
- Alert: ticker, days until earnings, screener appearances, ATR%, quality score

No new secrets needed — uses SLACK_WEBHOOK_URL already in repo.
"""

import os
import re
import time
import random
import logging
import datetime
import requests
from bs4 import BeautifulSoup
from collections import defaultdict
from glob import glob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# Config
# ----------------------------
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
FINVIZ_BASE = "https://finviz.com"
EARNINGS_WINDOW_DAYS = 7       # alert if earnings within this many days
MIN_APPEARANCES = 2            # only check tickers that appeared 2+ days this week
DATA_DIR = "data"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    return s


# ----------------------------
# Step 1: Find tickers from this week's screener CSVs
# ----------------------------

def get_week_bounds() -> tuple:
    """Return (monday, sunday) of the current week as date objects."""
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday


def load_weekly_tickers() -> dict:
    """
    Scan data/finviz_screeners_YYYY-MM-DD.csv files from this week.
    Returns dict: {ticker: {"appearances": int, "atr": float, "quality": float, "sector": str}}
    """
    monday, sunday = get_week_bounds()
    log.info(f"Scanning screener CSVs for week {monday} to {sunday}")

    ticker_days = defaultdict(list)  # ticker -> list of row dicts across days

    csv_files = sorted(glob(os.path.join(DATA_DIR, "finviz_screeners_*.csv")))
    log.info(f"Found {len(csv_files)} screener CSV(s) in {DATA_DIR}/")

    for path in csv_files:
        # Extract date from filename
        m = re.search(r"finviz_screeners_(\d{4}-\d{2}-\d{2})\.csv", path)
        if not m:
            continue
        file_date = datetime.date.fromisoformat(m.group(1))
        if not (monday <= file_date <= sunday):
            continue

        log.info(f"Reading {path} (date: {file_date})")
        try:
            import csv
            with open(path, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ticker = row.get("Ticker", "").strip()
                    if not ticker:
                        continue
                    ticker_days[ticker].append({
                        "date": file_date,
                        "atr": row.get("ATR%", ""),
                        "quality": row.get("Quality Score", ""),
                        "sector": row.get("Sector", ""),
                        "screeners": row.get("Screeners", ""),
                        "market_cap": row.get("Market Cap", ""),
                    })
        except Exception as e:
            log.error(f"Failed to read {path}: {e}")

    # Collapse to summary — keep latest day's metrics, count appearances
    result = {}
    for ticker, rows in ticker_days.items():
        rows_sorted = sorted(rows, key=lambda r: r["date"])
        latest = rows_sorted[-1]

        def safe_float(v):
            try:
                return float(str(v).replace("%", "").strip())
            except:
                return None

        result[ticker] = {
            "appearances": len(rows_sorted),
            "atr": safe_float(latest["atr"]),
            "quality": safe_float(latest["quality"]),
            "sector": latest["sector"],
            "screeners": latest["screeners"],
            "market_cap": latest["market_cap"],
        }

    log.info(f"Total unique tickers this week: {len(result)}")
    qualifying = {t: v for t, v in result.items() if v["appearances"] >= MIN_APPEARANCES}
    log.info(f"Tickers with {MIN_APPEARANCES}+ appearances: {len(qualifying)}")
    return qualifying


# ----------------------------
# Step 2: Fetch earnings date from Finviz quote page
# ----------------------------

def fetch_earnings_date(ticker: str, session: requests.Session) -> datetime.date | None:
    """
    Scrape the Finviz quote page for a ticker and extract the Earnings date.
    Returns a date object or None if not found / parsing fails.
    """
    url = f"{FINVIZ_BASE}/quote.ashx"
    for attempt in range(3):
        try:
            resp = session.get(url, params={"t": ticker}, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")

            # Finviz snapshot table — find "Earnings" label and its value cell
            table = soup.find("table", class_="snapshot-table2")
            if not table:
                log.warning(f"{ticker}: snapshot table not found")
                return None

            cells = table.find_all("td")
            for i, cell in enumerate(cells):
                if cell.get_text(strip=True) == "Earnings":
                    if i + 1 < len(cells):
                        raw = cells[i + 1].get_text(strip=True)
                        return parse_earnings_date(raw)

            log.debug(f"{ticker}: Earnings field not found in snapshot table")
            return None

        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait = (2 ** attempt) + random.random()
                log.warning(f"{ticker}: rate limited, retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                log.error(f"{ticker}: HTTP {e.response.status_code}")
                return None
        except Exception as e:
            log.error(f"{ticker}: unexpected error — {e}")
            return None

    return None


def parse_earnings_date(raw: str) -> datetime.date | None:
    """
    Parse Finviz earnings strings like:
      'Feb 10 AMC', 'Mar 05 BMO', 'Apr 23', 'N/A', '--'
    Returns a date object (current or next year if month already passed), or None.
    """
    if not raw or raw in ("N/A", "--", ""):
        return None

    # Strip BMO/AMC/time suffixes
    raw_clean = re.sub(r"\s+(AMC|BMO|after|before|close|open).*", "", raw, flags=re.IGNORECASE).strip()

    today = datetime.date.today()
    for fmt in ("%b %d %Y", "%b %d"):
        try:
            if fmt == "%b %d":
                # No year — assume current year, roll to next if past
                parsed = datetime.datetime.strptime(f"{raw_clean} {today.year}", "%b %d %Y").date()
                if parsed < today - datetime.timedelta(days=7):
                    parsed = parsed.replace(year=today.year + 1)
                return parsed
            else:
                return datetime.datetime.strptime(raw_clean, fmt).date()
        except ValueError:
            continue

    log.debug(f"Could not parse earnings date: '{raw}'")
    return None


# ----------------------------
# Step 3: Find tickers with earnings within window
# ----------------------------

def find_upcoming_earnings(tickers: dict) -> list:
    """
    For each qualifying ticker, fetch earnings date.
    Returns list of dicts for tickers with earnings within EARNINGS_WINDOW_DAYS.
    Sorted by days_until ascending.
    """
    today = datetime.date.today()
    session = make_session()
    upcoming = []

    for ticker, meta in tickers.items():
        earnings_date = fetch_earnings_date(ticker, session)

        if earnings_date is None:
            log.debug(f"{ticker}: no earnings date found")
        else:
            days_until = (earnings_date - today).days
            log.info(f"{ticker}: earnings {earnings_date} ({days_until} days away)")

            if 0 <= days_until <= EARNINGS_WINDOW_DAYS:
                upcoming.append({
                    "ticker": ticker,
                    "earnings_date": earnings_date,
                    "days_until": days_until,
                    "appearances": meta["appearances"],
                    "atr": meta["atr"],
                    "quality": meta["quality"],
                    "sector": meta["sector"],
                    "screeners": meta["screeners"],
                    "market_cap": meta["market_cap"],
                })

        # Polite delay — Finviz will 429 you if you hammer it
        time.sleep(1.2 + random.uniform(0, 0.5))

    upcoming.sort(key=lambda x: x["days_until"])
    return upcoming


# ----------------------------
# Step 4: Fire Slack alert
# ----------------------------

def format_days(days: int) -> str:
    if days == 0:
        return "🔴 *TODAY*"
    elif days == 1:
        return "🔴 *TOMORROW*"
    elif days <= 3:
        return f"🟠 *{days} days*"
    else:
        return f"🟡 {days} days"


def send_slack_alert(upcoming: list, today: datetime.date):
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack alert.")
        return

    if not upcoming:
        log.info("No upcoming earnings — no alert to send.")
        return

    ticker_lines = []
    for item in upcoming:
        atr = f"{item['atr']:.1f}%" if item['atr'] is not None else "—"
        qs = f"{item['quality']:.0f}" if item['quality'] is not None else "—"
        sector = item['sector'] or "—"
        days_str = format_days(item['days_until'])
        date_str = item['earnings_date'].strftime("%b %d")

        ticker_lines.append(
            f"*{item['ticker']}* — earnings {date_str} ({days_str})\n"
            f"  Appeared {item['appearances']}x this week · Q{qs} · ATR {atr} · {sector}\n"
            f"  Screeners: {item['screeners']}"
        )

    body = "\n\n".join(ticker_lines)

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"⚠️ Earnings Alert — {len(upcoming)} screener ticker{'s' if len(upcoming) != 1 else ''} reporting soon"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"_Tickers from this week's screener with earnings within {EARNINGS_WINDOW_DAYS} days._\n"
                    f"_Watch for post-earnings setup — don't hold through the print._\n\n"
                    f"{body}"
                )
            }
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"finviz-earnings-alert · {today.strftime('%Y-%m-%d')}"
                }
            ]
        }
    ]

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info(f"Slack alert sent — {len(upcoming)} ticker(s) flagged.")
    except Exception as e:
        log.error(f"Failed to send Slack alert: {e}")


# ----------------------------
# Main
# ----------------------------

if __name__ == "__main__":
    today = datetime.date.today()
    log.info(f"=== Earnings alert starting — {today} ===")

    # Step 1: load this week's screener tickers
    weekly_tickers = load_weekly_tickers()

    if not weekly_tickers:
        log.warning("No qualifying tickers found in this week's screener CSVs — exiting.")
        log.info("(This is expected on Monday before the first screener run of the week.)")
        exit(0)

    # Step 2 + 3: fetch earnings dates, find upcoming
    log.info(f"Checking earnings dates for {len(weekly_tickers)} tickers...")
    upcoming = find_upcoming_earnings(weekly_tickers)

    log.info(f"Tickers with earnings within {EARNINGS_WINDOW_DAYS} days: {len(upcoming)}")
    for item in upcoming:
        log.info(f"  {item['ticker']} — {item['earnings_date']} ({item['days_until']} days)")

    # Step 4: fire Slack alert
    send_slack_alert(upcoming, today)

    log.info("=== Done ===")
