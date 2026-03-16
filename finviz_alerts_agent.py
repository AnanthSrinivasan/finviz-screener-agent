#!/usr/bin/env python3
# ----------------------------
# Finviz Special Alerts Agent
# ----------------------------
# Runs daily. Fires into #finviz-alerts ONLY when a real signal triggers.
# Tracks:
#   - CNN Fear & Greed threshold events (buy zone / greed topping)
#   - NYSE + Nasdaq Net New Highs 10-day SMA flip (breadth reversal)
#   - ATR% compression signal (momentum drying up — peel alert)
#   - Commodity breakout in fear (SLV/GLD running while F&G < 30)
# ----------------------------

import os
import csv
import json
import random
import logging
import datetime
import requests
import pandas as pd
from pathlib import Path
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# Config
# ----------------------------
SLACK_WEBHOOK_URL  = os.environ.get("SLACK_WEBHOOK_URL", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL  = "https://api.anthropic.com/v1/messages"
CNN_FNG_URL        = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FINVIZ_BASE        = "https://finviz.com"
DATA_DIR           = os.environ.get("DATA_DIR", "data")
STATE_FILE         = os.path.join(DATA_DIR, "alerts_state.json")

# Thresholds — tune these over time
FNG_BUY_THRESHOLD        = 15.0   # extreme fear buy zone
FNG_BUY_RECOVERY_MIN     = 20.0   # was below this, now rising = recovery signal
FNG_GREED_WARNING        = 75.0   # start peeling
FNG_EXTREME_GREED        = 80.0   # aggressive peel
NET_HIGHS_EXTREME_LOW    = -300   # NYSE net new lows extreme (finviz scale)
ATR_COMPRESSION_WARN     = 3.5    # avg ATR% across top tickers approaching filter threshold
COMMODITY_FEAR_FNG_MAX   = 30.0   # F&G must be below this for commodity breakout alert
COMMODITY_WEEK_MIN_PCT   = 3.0    # commodity ETF weekly gain to trigger

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
# State Management
# Persists signal history to repo so SMA can be computed across days
# ----------------------------
def load_state() -> dict:
    """Load persisted signal state from JSON file in data/."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load state: {e}")
    return {
        "fng_history":       [],   # list of {date, score} — last 10 days
        "nyse_hl_history":   [],   # list of {date, value} — last 10 days
        "ndaq_hl_history":   [],   # list of {date, value} — last 10 days
        "slv_week_history":  [],   # list of {date, perf_week_pct}
        "gld_week_history":  [],   # list of {date, perf_week_pct}
        "last_alerts_sent":  {},   # alert_key -> date last sent (prevent spam)
    }


def save_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info("State saved.")


def append_history(history: list, entry: dict, max_len: int = 15) -> list:
    """Append new entry and keep last max_len entries."""
    history.append(entry)
    return history[-max_len:]


def sma(values: list, period: int = 10) -> float | None:
    """Simple moving average of the last `period` values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def cooldown_ok(state: dict, alert_key: str, cooldown_days: int = 7) -> bool:
    """Return True if we haven't sent this alert in the last cooldown_days."""
    last_sent = state["last_alerts_sent"].get(alert_key)
    if not last_sent:
        return True
    delta = (datetime.date.today() - datetime.date.fromisoformat(last_sent)).days
    return delta >= cooldown_days


def mark_sent(state: dict, alert_key: str):
    state["last_alerts_sent"][alert_key] = datetime.date.today().isoformat()


# ----------------------------
# Data Fetchers
# ----------------------------
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


def fetch_finviz_value(symbol: str) -> dict:
    """
    Fetch snapshot metrics for any Finviz symbol including $NYHL, $NAHL.
    Returns dict with price, change, perf_week, perf_month etc.
    """
    session = make_session()
    try:
        resp = session.get(f"{FINVIZ_BASE}/quote.ashx", params={"t": symbol}, timeout=10)
        if not resp.ok:
            log.warning(f"Finviz fetch failed for {symbol}: {resp.status_code}")
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
        return data
    except Exception as e:
        log.error(f"Finviz snapshot failed for {symbol}: {e}")
        return {}


def parse_hl_value(data: dict) -> float | None:
    """
    Parse net new highs/lows value from Finviz snapshot.
    $NYHL and $NAHL show as the 'Price' field — positive = net new highs, negative = net new lows.
    """
    try:
        raw = data.get("Price", "").replace(",", "").strip()
        return float(raw) if raw else None
    except:
        return None


def parse_perf_week(data: dict) -> float | None:
    """Parse Perf Week % value from Finviz snapshot."""
    try:
        raw = data.get("Perf Week", "").replace("%", "").strip()
        return float(raw) if raw else None
    except:
        return None


def fetch_latest_atr_avg() -> float | None:
    """
    Read the most recent daily CSV and compute average ATR% of top 10 tickers.
    Used to detect ATR compression — momentum drying up.
    """
    try:
        today = datetime.date.today()
        for i in range(7):
            date_str = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            path = os.path.join(DATA_DIR, f"finviz_screeners_{date_str}.csv")
            if os.path.exists(path):
                df = pd.read_csv(path)
                if "ATR%" in df.columns:
                    top = df.dropna(subset=["ATR%"]).head(10)
                    avg = top["ATR%"].mean()
                    log.info(f"Avg ATR% top 10: {avg:.1f}% from {date_str}")
                    return round(avg, 1)
    except Exception as e:
        log.error(f"ATR avg fetch failed: {e}")
    return None


# ----------------------------
# Signal Checkers
# ----------------------------
def check_fng_signals(state: dict, fng_score: float) -> list:
    """Check Fear & Greed threshold signals. Returns list of alert dicts."""
    alerts = []
    today = datetime.date.today().isoformat()
    history = state["fng_history"]

    # Get yesterday's score
    prev_score = history[-1]["score"] if history else None

    # --- BUY SIGNALS ---

    # 1. F&G drops below 15 — extreme capitulation
    if fng_score <= FNG_BUY_THRESHOLD:
        key = "fng_extreme_fear"
        if cooldown_ok(state, key, cooldown_days=14):
            alerts.append({
                "key":   key,
                "type":  "BUY",
                "emoji": "🟢",
                "title": f"Extreme Fear — F&G at {fng_score}",
                "body":  (
                    f"Fear & Greed has hit *{fng_score}* — below the extreme threshold of {FNG_BUY_THRESHOLD}. "
                    f"Historically one of the best buying zones. Do not chase immediately — "
                    f"wait for a 1-2 day recovery signal (F&G rising + net new highs turning positive) "
                    f"before sizing in. This is the zone, not necessarily the day."
                ),
            })

    # 2. F&G was below 20, now rising — recovery confirmation
    if prev_score is not None and prev_score <= FNG_BUY_RECOVERY_MIN and fng_score > prev_score:
        # Check it was in recovery zone for at least 2 days
        low_days = sum(1 for h in history[-5:] if h["score"] <= FNG_BUY_RECOVERY_MIN)
        if low_days >= 2:
            key = "fng_recovery_rising"
            if cooldown_ok(state, key, cooldown_days=10):
                alerts.append({
                    "key":   key,
                    "type":  "BUY",
                    "emoji": "🟢",
                    "title": f"Fear Recovery — F&G rising from {prev_score} to {fng_score}",
                    "body":  (
                        f"Fear & Greed spent {low_days} days below {FNG_BUY_RECOVERY_MIN} and is now rising "
                        f"({prev_score} → {fng_score}). This is the recovery signal — "
                        f"not the bottom, but the turn. Combine with net new highs SMA for confirmation. "
                        f"This is when quality momentum setups from your screener become high-conviction entries."
                    ),
                })

    # --- EXIT SIGNALS ---

    # 3. F&G crosses 75 — greed warning
    if fng_score >= FNG_GREED_WARNING and (prev_score is None or prev_score < FNG_GREED_WARNING):
        key = "fng_greed_warning"
        if cooldown_ok(state, key, cooldown_days=21):
            alerts.append({
                "key":   key,
                "type":  "PEEL",
                "emoji": "🟡",
                "title": f"Greed Warning — F&G at {fng_score}",
                "body":  (
                    f"Fear & Greed has crossed into Greed territory at *{fng_score}*. "
                    f"Start reviewing your open positions. "
                    f"Tighten stops on anything up 30%+. Don't add new positions unless the setup is exceptional. "
                    f"Watch ATR% — if it starts compressing below 4%, momentum is fading."
                ),
            })

    # 4. F&G above 80 for 3 consecutive days — aggressive peel
    if fng_score >= FNG_EXTREME_GREED:
        extreme_days = sum(1 for h in history[-3:] if h["score"] >= FNG_EXTREME_GREED)
        if extreme_days >= 2:
            key = "fng_extreme_greed_peel"
            if cooldown_ok(state, key, cooldown_days=14):
                alerts.append({
                    "key":   key,
                    "type":  "PEEL",
                    "emoji": "🔴",
                    "title": f"Extreme Greed — Aggressive Peel Signal",
                    "body":  (
                        f"Fear & Greed has been above {FNG_EXTREME_GREED} for {extreme_days+1} consecutive days (now {fng_score}). "
                        f"This is a late-stage momentum environment. "
                        f"Reduce position sizes, raise stops aggressively, and do not chase new breakouts. "
                        f"The best trades from this point forward are exits, not entries."
                    ),
                })

    return alerts


def check_net_highs_signals(state: dict, nyse_val: float, ndaq_val: float) -> list:
    """Check NYSE and Nasdaq net new highs SMA flip signals."""
    alerts = []

    nyse_hist = [h["value"] for h in state["nyse_hl_history"]]
    ndaq_hist = [h["value"] for h in state["ndaq_hl_history"]]

    # Compute SMAs with new values appended
    nyse_sma_prev = sma(nyse_hist, 10)
    ndaq_sma_prev = sma(ndaq_hist, 10)

    nyse_sma_new = sma(nyse_hist + [nyse_val], 10)
    ndaq_sma_new = sma(ndaq_hist + [ndaq_val], 10)

    log.info(f"NYSE $NYHL: {nyse_val} | SMA10: {nyse_sma_new}")
    log.info(f"Nasdaq $NAHL: {ndaq_val} | SMA10: {ndaq_sma_new}")

    # --- BREADTH FLIP BUY ---

    # NYSE SMA crosses from negative to positive
    if (nyse_sma_prev is not None and nyse_sma_new is not None
            and nyse_sma_prev < 0 and nyse_sma_new >= 0):
        key = "nyse_hl_flip_positive"
        if cooldown_ok(state, key, cooldown_days=30):
            alerts.append({
                "key":   key,
                "type":  "BUY",
                "emoji": "🟢",
                "title": "NYSE Breadth Flip — Net New Highs SMA crossed positive",
                "body":  (
                    f"NYSE Net New Highs 10-day SMA has crossed from negative to positive "
                    f"({nyse_sma_prev:.0f} → {nyse_sma_new:.0f}). "
                    f"This is the breadth flip signal — broad market participation is recovering. "
                    f"Historically one of the most reliable signals that a sustained rally is beginning. "
                    f"Combine with Fear & Greed recovery and your screener conviction scores for sizing decisions."
                ),
            })

    # Nasdaq SMA crosses from negative to positive
    if (ndaq_sma_prev is not None and ndaq_sma_new is not None
            and ndaq_sma_prev < 0 and ndaq_sma_new >= 0):
        key = "ndaq_hl_flip_positive"
        if cooldown_ok(state, key, cooldown_days=30):
            alerts.append({
                "key":   key,
                "type":  "BUY",
                "emoji": "🟢",
                "title": "Nasdaq Breadth Flip — Net New Highs SMA crossed positive",
                "body":  (
                    f"Nasdaq Net New Highs 10-day SMA has crossed from negative to positive "
                    f"({ndaq_sma_prev:.0f} → {ndaq_sma_new:.0f}). "
                    f"Nasdaq flipping first = tech-led recovery. "
                    f"Your Growth + IPO screener setups are most relevant here — "
                    f"this is when high-ATR tech names from the persistence leaderboard are highest conviction."
                ),
            })

    # Both flip together — strongest signal
    if (nyse_sma_prev is not None and ndaq_sma_prev is not None
            and nyse_sma_prev < 0 and ndaq_sma_prev < 0
            and nyse_sma_new is not None and ndaq_sma_new is not None
            and nyse_sma_new >= 0 and ndaq_sma_new >= 0):
        key = "both_hl_flip_positive"
        if cooldown_ok(state, key, cooldown_days=30):
            # Replace individual alerts with the combined one
            alerts = [a for a in alerts if a["key"] not in ("nyse_hl_flip_positive", "ndaq_hl_flip_positive")]
            alerts.append({
                "key":   key,
                "type":  "BUY",
                "emoji": "🚀",
                "title": "BOTH NYSE + Nasdaq Breadth Flip — Strongest Buy Signal",
                "body":  (
                    f"Both NYSE and Nasdaq Net New Highs 10-day SMAs have simultaneously crossed from "
                    f"negative to positive (NYSE: {nyse_sma_prev:.0f}→{nyse_sma_new:.0f}, "
                    f"Nasdaq: {ndaq_sma_prev:.0f}→{ndaq_sma_new:.0f}). "
                    f"This is the highest-conviction market-wide buy signal in this setup. "
                    f"Full size on your highest-persistence screener setups. "
                    f"The tickers that appeared most consistently in the weekly leaderboard are your targets."
                ),
            })

    # --- BREADTH FLIP SELL ---

    # NYSE SMA crosses from positive to negative
    if (nyse_sma_prev is not None and nyse_sma_new is not None
            and nyse_sma_prev >= 0 and nyse_sma_new < 0):
        key = "nyse_hl_flip_negative"
        if cooldown_ok(state, key, cooldown_days=30):
            alerts.append({
                "key":   key,
                "type":  "PEEL",
                "emoji": "🔴",
                "title": "NYSE Breadth Deteriorating — Net New Highs SMA crossed negative",
                "body":  (
                    f"NYSE Net New Highs 10-day SMA has crossed from positive to negative "
                    f"({nyse_sma_prev:.0f} → {nyse_sma_new:.0f}). "
                    f"Broad market participation is narrowing. "
                    f"Raise stops on all open positions. Be more selective on new entries from screener. "
                    f"If Nasdaq follows, that is a full exit signal."
                ),
            })

    # Extreme net new lows — capitulation spike
    if nyse_val <= NET_HIGHS_EXTREME_LOW:
        key = "nyse_extreme_lows_spike"
        if cooldown_ok(state, key, cooldown_days=14):
            alerts.append({
                "key":   key,
                "type":  "WATCH",
                "emoji": "👀",
                "title": f"NYSE Extreme Net New Lows — {nyse_val:.0f}",
                "body":  (
                    f"NYSE Net New Lows has hit an extreme reading of {nyse_val:.0f}. "
                    f"This is a capitulation signal — not a buy yet, but mark this date. "
                    f"When the 10-day SMA starts turning up from here, that is your entry window. "
                    f"Watch the next 5-7 days for the turn."
                ),
            })

    return alerts


def check_atr_compression(state: dict, avg_atr: float) -> list:
    """Check if ATR% is compressing — momentum drying up signal."""
    alerts = []

    if avg_atr <= ATR_COMPRESSION_WARN:
        key = "atr_compression"
        if cooldown_ok(state, key, cooldown_days=7):
            alerts.append({
                "key":   key,
                "type":  "PEEL",
                "emoji": "🟡",
                "title": f"ATR Compression — avg ATR% at {avg_atr}%",
                "body":  (
                    f"Average ATR% across your top screener tickers has compressed to *{avg_atr}%* "
                    f"— approaching your filter threshold of 3.0%. "
                    f"Momentum is drying up. This is your signal to review open positions and "
                    f"start peeling anything that has achieved its target move. "
                    f"Do not add new positions until ATR% expands again above 5%."
                ),
            })

    return alerts


def check_commodity_breakout(state: dict, fng_score: float,
                               slv_perf_week: float, gld_perf_week: float) -> list:
    """Check for commodity breakout in fear environment — the SLV/GLD setup you missed."""
    alerts = []

    if fng_score > COMMODITY_FEAR_FNG_MAX:
        return alerts   # only valid in fear environment

    slv_hist = [h["pct"] for h in state["slv_week_history"]]
    gld_hist = [h["pct"] for h in state["gld_week_history"]]

    # SLV running 3%+ for 2 consecutive weeks while F&G < 30
    slv_recent = slv_hist[-1:] + [slv_perf_week]
    if (len(slv_recent) >= 2
            and all(p >= COMMODITY_WEEK_MIN_PCT for p in slv_recent[-2:])):
        key = "slv_breakout_in_fear"
        if cooldown_ok(state, key, cooldown_days=21):
            alerts.append({
                "key":   key,
                "type":  "BUY",
                "emoji": "🥈",
                "title": f"Silver Breakout in Fear — SLV +{slv_perf_week:.1f}% this week",
                "body":  (
                    f"SLV has gained {slv_perf_week:.1f}% this week and was up "
                    f"{slv_hist[-1]:.1f}% last week — two consecutive weeks of 3%+ gains "
                    f"while Fear & Greed sits at {fng_score} (fear environment). "
                    f"This is the exact setup that preceded the big silver move you referenced. "
                    f"Commodity breakouts in fear environments often run 30-50%+. "
                    f"Consider SILJ (junior silver miners) or SLV directly for exposure."
                ),
            })

    # GLD running 2%+ for 2 consecutive weeks while F&G < 30
    gld_recent = gld_hist[-1:] + [gld_perf_week]
    if (len(gld_recent) >= 2
            and all(p >= 2.0 for p in gld_recent[-2:])):
        key = "gld_breakout_in_fear"
        if cooldown_ok(state, key, cooldown_days=21):
            alerts.append({
                "key":   key,
                "type":  "BUY",
                "emoji": "🥇",
                "title": f"Gold Breakout in Fear — GLD +{gld_perf_week:.1f}% this week",
                "body":  (
                    f"GLD has gained {gld_perf_week:.1f}% this week and was up "
                    f"{gld_hist[-1]:.1f}% last week — sustained breakout while "
                    f"Fear & Greed sits at {fng_score}. "
                    f"Dollar weakness + fear environment = gold running. "
                    f"GDX and GDXJ (miners) typically amplify the move 2-3x."
                ),
            })

    return alerts


# ----------------------------
# AI Context for Alerts
# ----------------------------
def enrich_alert_with_ai(alert: dict, fng_score: float,
                          nyse_sma: float, ndaq_sma: float) -> str:
    """Optional: add a one-sentence AI context to high-priority alerts."""
    if not ANTHROPIC_API_KEY or alert["type"] not in ("BUY",):
        return alert["body"]

    try:
        prompt = (
            f"You are a momentum trader. A signal just fired: {alert['title']}. "
            f"Context: Fear & Greed {fng_score}, NYSE NH/NL SMA {nyse_sma}, Nasdaq NH/NL SMA {ndaq_sma}. "
            f"Add ONE sentence of specific actionable context to this alert body — "
            f"what to watch for in the next 24-48 hours to confirm the signal. "
            f"Be direct. No disclaimers. Plain text only.\n\n"
            f"Alert body: {alert['body']}"
        )
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        if resp.ok:
            extra = resp.json()["content"][0]["text"].strip()
            return alert["body"] + f"\n\n_{extra}_"
    except Exception as e:
        log.warning(f"AI enrichment failed: {e}")

    return alert["body"]


# ----------------------------
# Slack Alert Sender
# ----------------------------
def send_alert(alert: dict, fng_score: float = None,
               nyse_sma: float = None, ndaq_sma: float = None):
    """Send a single alert to the #finviz-alerts Slack channel."""
    if not SLACK_WEBHOOK_URL:
        log.info(f"No webhook — would have sent: {alert['title']}")
        return

    type_colors = {"BUY": "#4ade80", "PEEL": "#f87171", "WATCH": "#facc15"}
    type_labels = {"BUY": "BUY SIGNAL", "PEEL": "PEEL / EXIT", "WATCH": "WATCH"}

    body = enrich_alert_with_ai(alert, fng_score or 0, nyse_sma or 0, ndaq_sma or 0)

    payload = {
        "attachments": [
            {
                "color": type_colors.get(alert["type"], "#64748b"),
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"{alert['emoji']} {alert['title']}"
                        }
                    },
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f"*{type_labels.get(alert['type'], alert['type'])}*  ·  {datetime.date.today().isoformat()}"}]
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": body}
                    },
                ]
            }
        ]
    }

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"Alert sent: {alert['title']}")
    except Exception as e:
        log.error(f"Failed to send alert: {e}")


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    today = datetime.date.today().isoformat()
    log.info(f"=== Finviz alerts agent starting — {today} ===")

    os.makedirs(DATA_DIR, exist_ok=True)
    state = load_state()
    all_alerts = []

    # --- Fetch all data ---

    # Fear & Greed
    fng_score = fetch_fng()
    if fng_score is not None:
        state["fng_history"] = append_history(
            state["fng_history"], {"date": today, "score": fng_score}
        )

    # NYSE Net New Highs ($NYHL)
    nyse_data = fetch_finviz_value("$NYHL")
    nyse_val  = parse_hl_value(nyse_data)
    if nyse_val is not None:
        state["nyse_hl_history"] = append_history(
            state["nyse_hl_history"], {"date": today, "value": nyse_val}
        )
    log.info(f"NYSE $NYHL raw: {nyse_val}")

    # Nasdaq Net New Highs ($NAHL)
    ndaq_data = fetch_finviz_value("$NAHL")
    ndaq_val  = parse_hl_value(ndaq_data)
    if ndaq_val is not None:
        state["ndaq_hl_history"] = append_history(
            state["ndaq_hl_history"], {"date": today, "value": ndaq_val}
        )
    log.info(f"Nasdaq $NAHL raw: {ndaq_val}")

    # SLV and GLD weekly performance
    slv_data      = fetch_finviz_value("SLV")
    slv_perf_week = parse_perf_week(slv_data)
    if slv_perf_week is not None:
        state["slv_week_history"] = append_history(
            state["slv_week_history"], {"date": today, "pct": slv_perf_week}
        )

    gld_data      = fetch_finviz_value("GLD")
    gld_perf_week = parse_perf_week(gld_data)
    if gld_perf_week is not None:
        state["gld_week_history"] = append_history(
            state["gld_week_history"], {"date": today, "pct": gld_perf_week}
        )

    # Average ATR% from latest daily screener CSV
    avg_atr = fetch_latest_atr_avg()

    # --- Compute SMAs for logging ---
    nyse_sma_val = sma([h["value"] for h in state["nyse_hl_history"]], 10)
    ndaq_sma_val = sma([h["value"] for h in state["ndaq_hl_history"]], 10)
    log.info(f"NYSE SMA10: {nyse_sma_val}  |  Nasdaq SMA10: {ndaq_sma_val}")

    # --- Run signal checks ---

    if fng_score is not None:
        all_alerts += check_fng_signals(state, fng_score)

    if nyse_val is not None and ndaq_val is not None:
        all_alerts += check_net_highs_signals(state, nyse_val, ndaq_val)

    if avg_atr is not None:
        all_alerts += check_atr_compression(state, avg_atr)

    if (fng_score is not None
            and slv_perf_week is not None
            and gld_perf_week is not None):
        all_alerts += check_commodity_breakout(
            state, fng_score, slv_perf_week, gld_perf_week
        )

    # --- Send alerts & update state ---

    if all_alerts:
        log.info(f"{len(all_alerts)} alert(s) firing today")
        for alert in all_alerts:
            send_alert(alert, fng_score, nyse_sma_val, ndaq_sma_val)
            mark_sent(state, alert["key"])
    else:
        log.info("No alerts today — all signals within normal range.")

    save_state(state)
    log.info("=== Alerts agent done ===")
