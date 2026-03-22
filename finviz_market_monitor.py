#!/usr/bin/env python3
# ----------------------------
# Market Monitor Agent
# ----------------------------
# Standalone daily agent — runs after market close.
# Fetches breadth data from Finviz, calculates up/down ratios,
# classifies market state (THRUST/GREEN/CAUTION/DANGER/RED/BLACKOUT),
# stores rolling 30-day history, and sends Slack alerts on state changes.
# ----------------------------

import os
import re
import json
import time
import random
import logging
import datetime
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# Config
# ----------------------------
FINVIZ_BASE        = "https://finviz.com"
CNN_FNG_URL        = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
DATA_DIR           = os.environ.get("DATA_DIR", "data")
HISTORY_FILE       = os.path.join(DATA_DIR, "market_monitor_history.json")
SLACK_WEBHOOK_ALERTS = os.environ.get("SLACK_WEBHOOK_MARKET_ALERTS", "")
SLACK_WEBHOOK_DAILY  = os.environ.get("SLACK_WEBHOOK_MARKET_DAILY", "")
FETCH_DELAY        = int(os.environ.get("MONITOR_FETCH_DELAY", "7"))

# Scaled thresholds (from ~1500-ticker liquid universe)
THRUST_THRESHOLD   = 500   # stocks up 4% in one day = breadth thrust
DANGER_DOWN_THRESHOLD = 175  # stocks down 4% in one day = major deterioration

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    return s


# ----------------------------
# Data Storage
# ----------------------------
def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load history: {e}")
    return []


def save_history(history: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    log.info(f"History saved — {len(history)} days.")


def save_daily(record: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"market_monitor_{record['date']}.json")
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
    log.info(f"Daily record saved: {path}")


# ----------------------------
# Finviz Screener Count Fetcher
# ----------------------------
def fetch_screener_count(session: requests.Session, url: str, label: str = "") -> int:
    """
    Fetch a Finviz screener page and return the total result count.
    Parses the 'Total:' indicator from the screener page header.
    Falls back to counting visible rows if Total not found.
    """
    try:
        resp = session.get(url, timeout=15)
        if not resp.ok:
            log.warning(f"HTTP {resp.status_code} fetching {label or url}")
            return 0
        soup = BeautifulSoup(resp.text, "html.parser")

        # Finviz shows total in a cell like "1 - 20 / Total: 43" or "Total: 43"
        total_cell = soup.find("td", class_="count-text")
        if total_cell:
            match = re.search(r"Total:\s*(\d+)", total_cell.get_text())
            if match:
                count = int(match.group(1))
                log.info(f"{label}: {count} (from Total header)")
                return count

        # Fallback: search all text for "Total: N" pattern
        page_text = soup.get_text()
        match = re.search(r"Total[:\s]+(\d+)", page_text)
        if match:
            count = int(match.group(1))
            log.info(f"{label}: {count} (from page text)")
            return count

        # Last fallback: count screener rows on this page
        rows = soup.select('tr[valign="top"]')
        row_count = 0
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 2:
                row_count += 1
        log.info(f"{label}: {row_count} (from row count — may undercount if paginated)")
        return row_count

    except Exception as e:
        log.error(f"Failed to fetch {label}: {e}")
        return 0


# ----------------------------
# Data Fetchers
# ----------------------------
def fetch_breadth_data(session: requests.Session) -> dict:
    """Fetch all 5 Finviz screener counts + SPY data + F&G."""

    base_filters = "geo_usa,sh_avgvol_o500,sh_price_o5,exch_nysenasd"

    # Fetch 1 — Stocks up 4%+ today
    url_up4 = (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f={base_filters},ta_change_u4"
        f"&o=-change"
    )
    up_4 = fetch_screener_count(session, url_up4, "Up 4%+ today")
    time.sleep(FETCH_DELAY)

    # Fetch 2 — Stocks down 4%+ today
    url_down4 = (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f={base_filters},ta_change_d4"
        f"&o=change"
    )
    down_4 = fetch_screener_count(session, url_down4, "Down 4%+ today")
    time.sleep(FETCH_DELAY)

    # Fetch 3 — Stocks up 25%+ in a quarter
    url_up25q = (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f={base_filters},ta_perf_13w30o"
    )
    up_25_quarter = fetch_screener_count(session, url_up25q, "Up 25%+ quarter")
    time.sleep(FETCH_DELAY)

    # Fetch 4 — Stocks down 25%+ in a quarter
    url_down25q = (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f={base_filters},ta_perf_13w30u"
    )
    down_25_quarter = fetch_screener_count(session, url_down25q, "Down 25%+ quarter")
    time.sleep(FETCH_DELAY)

    # Fetch 5 — Stocks above 40-day SMA (T2108 equivalent)
    url_above40 = (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f={base_filters},ta_sma40_pa"
    )
    above_40ma = fetch_screener_count(session, url_above40, "Above 40d SMA")
    time.sleep(FETCH_DELAY)

    # Total universe — no performance filter
    url_total = (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f={base_filters}"
    )
    total_universe = fetch_screener_count(session, url_total, "Total universe")
    time.sleep(FETCH_DELAY)

    # SPY snapshot for price + SMA data
    spy_data = fetch_spy_data(session)

    # Fear & Greed
    fg = fetch_fng()

    return {
        "up_4_today": up_4,
        "down_4_today": down_4,
        "up_25_quarter": up_25_quarter,
        "down_25_quarter": down_25_quarter,
        "above_40ma_count": above_40ma,
        "total_universe": total_universe,
        "spy_price": spy_data.get("price"),
        "spy_sma200_pct": spy_data.get("sma200_pct"),
        "fg": fg,
    }


def fetch_spy_data(session: requests.Session) -> dict:
    """Fetch SPY price and SMA data from Finviz quote page."""
    try:
        resp = session.get(f"{FINVIZ_BASE}/quote.ashx", params={"t": "SPY"}, timeout=10)
        if not resp.ok:
            log.warning(f"SPY fetch failed: HTTP {resp.status_code}")
            return {}
        soup = BeautifulSoup(resp.content, "html.parser")
        table = soup.find("table", class_="snapshot-table2")
        if not table:
            return {}
        data = {}
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            for k, v in zip(cells[0::2], cells[1::2]):
                data[k.get_text(strip=True).rstrip(".")] = v.get_text(strip=True)

        price_raw = data.get("Price", "").replace(",", "")
        sma200_raw = data.get("SMA200", "").replace("%", "")

        result = {}
        try:
            result["price"] = round(float(price_raw), 2)
        except (ValueError, TypeError):
            result["price"] = None
        try:
            result["sma200_pct"] = round(float(sma200_raw), 2)
        except (ValueError, TypeError):
            result["sma200_pct"] = None

        log.info(f"SPY: ${result.get('price')} | SMA200: {result.get('sma200_pct')}%")
        return result
    except Exception as e:
        log.error(f"SPY snapshot failed: {e}")
        return {}


def fetch_fng() -> float | None:
    """Fetch current CNN Fear & Greed score."""
    try:
        resp = make_session().get(CNN_FNG_URL, timeout=10)
        if resp.ok:
            score = resp.json()["fear_and_greed"]["score"]
            log.info(f"F&G: {score:.1f}")
            return round(float(score), 1)
    except Exception as e:
        log.error(f"F&G fetch failed: {e}")
    return None


# ----------------------------
# Calculations
# ----------------------------
def calculate_metrics(history: list, today_data: dict) -> dict:
    """Calculate breadth ratios, thrust detection, and T2108 equivalent."""
    up_4 = today_data["up_4_today"]
    down_4 = today_data["down_4_today"]

    # Daily ratio
    ratio_today = up_4 / max(down_4, 1)

    # 5-day ratio — include today in the window
    recent = history[-(5 - 1):] + [today_data]  # last 4 from history + today
    last_5 = recent[-5:]
    ratio_5day = (
        sum(d["up_4_today"] for d in last_5) /
        max(sum(d["down_4_today"] for d in last_5), 1)
    )

    # 10-day ratio
    recent_10 = history[-(10 - 1):] + [today_data]
    last_10 = recent_10[-10:]
    ratio_10day = (
        sum(d["up_4_today"] for d in last_10) /
        max(sum(d["down_4_today"] for d in last_10), 1)
    )

    # Thrust detection
    thrust = up_4 >= THRUST_THRESHOLD

    # T2108 equivalent — % of universe above 40-day SMA
    total_universe = today_data.get("total_universe", 0)
    above_40ma = today_data.get("above_40ma_count", 0)
    t2108 = (above_40ma / total_universe * 100) if total_universe > 0 else 0

    # SPY above 200-day SMA (conservative proxy for 20-week MA)
    spy_sma200_pct = today_data.get("spy_sma200_pct")
    spy_above_200d = spy_sma200_pct is not None and spy_sma200_pct > 0

    return {
        "ratio_today": round(ratio_today, 2),
        "ratio_5day": round(ratio_5day, 2),
        "ratio_10day": round(ratio_10day, 2),
        "thrust": thrust,
        "t2108": round(t2108, 2),
        "spy_above_200d": spy_above_200d,
    }


# ----------------------------
# Market State Classification
# ----------------------------
def is_blackout(date: datetime.date) -> bool:
    """Check if date falls in seasonal no-trade blackout period."""
    month = date.month
    day = date.day
    # Sep 1 — Oct 15
    if month == 9:
        return True
    if month == 10 and day <= 15:
        return True
    # Feb 1 — Mar 15
    if month == 2:
        return True
    if month == 3 and day <= 15:
        return True
    return False


def classify_market_state(metrics: dict, fg: float | None,
                          spy_price: float | None,
                          spy_above_200d: bool,
                          today_data: dict,
                          date: datetime.date) -> tuple[str, str]:
    """
    Classify market into one of: THRUST, GREEN, CAUTION, DANGER, RED, BLACKOUT.
    Returns (state, message).
    """
    # Check seasonal blackout first
    if is_blackout(date):
        return "BLACKOUT", "Seasonal no-trade period active"

    # THRUST — most important single signal
    if metrics["thrust"]:
        return "THRUST", f"Breadth thrust — {today_data['up_4_today']} stocks up 4%"

    # GREEN — full size entries
    fg_val = fg if fg is not None else 0
    if (metrics["ratio_5day"] >= 2.0
            and metrics["ratio_10day"] >= 1.5
            and fg_val >= 35
            and spy_above_200d
            and metrics["t2108"] >= 40):
        return "GREEN", "Full conditions met"

    # CAUTION — half size
    if (metrics["ratio_5day"] >= 1.5
            and fg_val >= 25
            and spy_above_200d):
        return "CAUTION", "Recovering — reduce size"

    # DANGER — deteriorating fast
    if (today_data["down_4_today"] >= DANGER_DOWN_THRESHOLD
            and metrics["ratio_5day"] < 0.5):
        return "DANGER", "Major breadth deterioration"

    # RED — default when nothing confirms
    return "RED", "No new trades"


# ----------------------------
# Record Builder
# ----------------------------
def build_daily_record(date: datetime.date, today_data: dict, metrics: dict,
                       state: str, message: str) -> dict:
    """Build the complete daily record for storage."""
    return {
        "date": date.isoformat(),
        "up_4_today": today_data["up_4_today"],
        "down_4_today": today_data["down_4_today"],
        "ratio_today": metrics["ratio_today"],
        "ratio_5day": metrics["ratio_5day"],
        "ratio_10day": metrics["ratio_10day"],
        "up_25_quarter": today_data.get("up_25_quarter", 0),
        "down_25_quarter": today_data.get("down_25_quarter", 0),
        "above_40ma_count": today_data.get("above_40ma_count", 0),
        "total_universe": today_data.get("total_universe", 0),
        "t2108_equiv": metrics["t2108"],
        "thrust_detected": metrics["thrust"],
        "fg": today_data.get("fg"),
        "spy_price": today_data.get("spy_price"),
        "spy_sma200_pct": today_data.get("spy_sma200_pct"),
        "spy_above_200d": metrics["spy_above_200d"],
        "market_state": state,
        "state_message": message,
        "blackout": is_blackout(date),
    }


# ----------------------------
# Slack Alerts
# ----------------------------
def send_state_change_alert(record: dict, prev_state: str | None):
    """Send state change alert to #market-alerts."""
    if not SLACK_WEBHOOK_ALERTS:
        log.info("SLACK_WEBHOOK_MARKET_ALERTS not set — skipping state change alert.")
        return

    state = record["market_state"]
    state_emoji = {
        "THRUST": "🚨", "GREEN": "✅", "CAUTION": "🟡",
        "DANGER": "⚠️", "RED": "🔴", "BLACKOUT": "⛔",
    }
    emoji = state_emoji.get(state, "📊")

    prev_str = prev_state or "UNKNOWN"
    fg_str = f"{record['fg']:.1f}" if record["fg"] is not None else "n/a"
    spy_str = f"${record['spy_price']:.2f}" if record["spy_price"] is not None else "n/a"

    # Build action guidance based on state
    if state == "THRUST":
        action = (
            "ACTION: Start building watchlist.\n"
            "Watch for 5-day ratio > 1.5 to confirm entry.\n"
            "Do NOT size full yet."
        )
    elif state == "GREEN":
        action = (
            "ACTION: Full conditions met.\n"
            f"Size at 10-15% for high conviction.\n"
            "Current watchlist candidates: check weekly report."
        )
    elif state == "CAUTION":
        action = (
            "ACTION: Half size only.\n"
            "Be selective — only highest conviction setups.\n"
            "Tighten stops on existing positions."
        )
    elif state == "DANGER":
        action = (
            "ACTION: No new entries.\n"
            "Raise stops on all open positions.\n"
            "Consider peeling weak names."
        )
    elif state == "BLACKOUT":
        action = (
            "ACTION: Seasonal no-trade period.\n"
            "No new entries until blackout ends.\n"
            "Existing positions: trail stops only."
        )
    else:
        action = (
            "ACTION: No new trades.\n"
            "Wait for breadth confirmation.\n"
            "Monitor daily for state change."
        )

    text = (
        f"{emoji} *MARKET MONITOR — STATE CHANGE*\n"
        f"{record['date']}\n\n"
        f"Previous: {prev_str} → Now: *{state}*\n\n"
        f"Stocks up 4% today: {record['up_4_today']}\n"
        f"Stocks down 4% today: {record['down_4_today']}\n"
        f"5-day ratio: {record['ratio_5day']}\n"
        f"10-day ratio: {record['ratio_10day']}\n"
        f"F&G: {fg_str} | SPY: {spy_str}\n\n"
        f"{action}"
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]

    try:
        resp = requests.post(SLACK_WEBHOOK_ALERTS, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info(f"State change alert sent: {prev_str} → {state}")
    except Exception as e:
        log.error(f"State change alert failed: {e}")


def send_confirmation_alert(record: dict):
    """Send confirmation alert when transitioning to GREEN from THRUST/CAUTION."""
    if not SLACK_WEBHOOK_ALERTS:
        return

    fg_str = f"{record['fg']:.1f}" if record["fg"] is not None else "n/a"

    text = (
        f"✅ *MARKET MONITOR — CONFIRMED RECOVERY*\n"
        f"{record['date']}\n\n"
        f"5-day ratio: {record['ratio_5day']} ✅\n"
        f"10-day ratio: {record['ratio_10day']} ✅\n"
        f"F&G: {fg_str} ✅\n"
        f"SPY above 200d MA: {'✅' if record['spy_above_200d'] else '❌'}\n"
        f"T2108: {record['t2108_equiv']:.0f}% ✅\n\n"
        f"ACTION: Full conditions met.\n"
        f"Size at 10-15% for high conviction.\n"
        f"Current watchlist candidates: check weekly report."
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]

    try:
        resp = requests.post(SLACK_WEBHOOK_ALERTS, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("Confirmation alert sent.")
    except Exception as e:
        log.error(f"Confirmation alert failed: {e}")


def send_daily_summary(record: dict):
    """Send daily summary to #market-daily."""
    if not SLACK_WEBHOOK_DAILY:
        log.info("SLACK_WEBHOOK_MARKET_DAILY not set — skipping daily summary.")
        return

    state = record["market_state"]
    state_emoji = {
        "THRUST": "🚨", "GREEN": "✅", "CAUTION": "🟡",
        "DANGER": "⚠️", "RED": "🔴", "BLACKOUT": "⛔",
    }
    emoji = state_emoji.get(state, "📊")

    fg_str = f"{record['fg']:.1f}" if record["fg"] is not None else "n/a"
    spy_str = f"${record['spy_price']:.0f}" if record["spy_price"] is not None else "n/a"
    sma_str = ""
    if record.get("spy_above_200d"):
        sma_str = " (above 200d MA)"
    elif record.get("spy_sma200_pct") is not None:
        sma_str = " (below 200d MA)"

    text = (
        f"📊 Market Monitor — {record['date']}\n"
        f"State: {emoji} {state}\n"
        f"Up 4%: {record['up_4_today']} | Down 4%: {record['down_4_today']}\n"
        f"5d ratio: {record['ratio_5day']} | 10d ratio: {record['ratio_10day']}\n"
        f"F&G: {fg_str} | T2108: {record['t2108_equiv']:.0f}%\n"
        f"SPY: {spy_str}{sma_str}"
    )

    try:
        resp = requests.post(SLACK_WEBHOOK_DAILY, json={"text": text}, timeout=10)
        resp.raise_for_status()
        log.info("Daily summary sent.")
    except Exception as e:
        log.error(f"Daily summary failed: {e}")


# ----------------------------
# Main
# ----------------------------
def run_market_monitor(date: datetime.date | None = None):
    """Main entry point for the market monitor."""
    if date is None:
        date = datetime.date.today()

    log.info(f"=== Market Monitor starting — {date.isoformat()} ===")

    # Load history
    history = load_history()
    log.info(f"Loaded {len(history)} days of history")

    # Determine previous state
    prev_state = history[-1]["market_state"] if history else None
    log.info(f"Previous market state: {prev_state or 'UNKNOWN'}")

    # Fetch all breadth data
    session = make_session()
    today_data = fetch_breadth_data(session)

    log.info(f"Up 4%: {today_data['up_4_today']} | Down 4%: {today_data['down_4_today']}")
    log.info(f"Up 25% qtr: {today_data['up_25_quarter']} | Down 25% qtr: {today_data['down_25_quarter']}")
    log.info(f"Above 40d SMA: {today_data['above_40ma_count']} / {today_data['total_universe']}")

    # Calculate metrics
    metrics = calculate_metrics(history, today_data)
    log.info(f"Ratios — today: {metrics['ratio_today']} | 5d: {metrics['ratio_5day']} | 10d: {metrics['ratio_10day']}")
    log.info(f"T2108: {metrics['t2108']:.1f}% | Thrust: {metrics['thrust']} | SPY above 200d: {metrics['spy_above_200d']}")

    # Classify market state
    state, message = classify_market_state(
        metrics, today_data.get("fg"), today_data.get("spy_price"),
        metrics["spy_above_200d"], today_data, date
    )
    log.info(f"Market state: {state} — {message}")

    # Build and save daily record
    record = build_daily_record(date, today_data, metrics, state, message)
    save_daily(record)

    # Update rolling history (keep last 30 trading days)
    history.append(record)
    history = history[-30:]
    save_history(history)

    # Send Slack alerts
    state_changed = prev_state is not None and state != prev_state

    if state_changed:
        send_state_change_alert(record, prev_state)
        # Send confirmation alert when moving to GREEN from THRUST or CAUTION
        if state == "GREEN" and prev_state in ("THRUST", "CAUTION"):
            send_confirmation_alert(record)

    # Always send daily summary
    send_daily_summary(record)

    log.info(f"=== Market Monitor complete — {state} ===")
    return record


if __name__ == "__main__":
    run_market_monitor()
