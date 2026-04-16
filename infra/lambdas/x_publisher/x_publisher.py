"""
X (Twitter) publisher Lambda.
Handles SetupOfDay and PersistencePick — PreMarketPulse is skipped.
Charts: Finviz daily chart attached as media to tweets with a ticker.
"""
import os
import boto3
import requests
from requests_oauthlib import OAuth1

_SSM_PREFIX = "/anva-trade"
_creds_cache: dict = {}

FINVIZ_CHART = "https://finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d"
FINVIZ_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AnvaTrade/1.0)"}

REGIME_EMOJI = {
    "GREEN":    "🟢",
    "THRUST":   "🚀",
    "CAUTION":  "🟡",
    "RED":      "🔴",
    "DANGER":   "🚨",
    "BLACKOUT": "⛔",
}

SILENT_STATES = {"RED", "BLACKOUT", "DANGER"}


def _load_creds():
    """Fetch X credentials from SSM SecureString at runtime. Cached per container."""
    global _creds_cache
    if _creds_cache:
        return _creds_cache
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "eu-central-1"))
    names = [f"{_SSM_PREFIX}/{k}" for k in
             ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"]]
    resp = ssm.get_parameters(Names=names, WithDecryption=True)
    _creds_cache = {p["Name"].split("/")[-1]: p["Value"] for p in resp["Parameters"]}
    return _creds_cache


def _auth():
    creds = _load_creds()
    return OAuth1(
        creds["X_API_KEY"],
        creds["X_API_SECRET"],
        creds["X_ACCESS_TOKEN"],
        creds["X_ACCESS_SECRET"],
    )


def _upload_chart(ticker: str) -> str | None:
    """Download Finviz chart and upload to X media API. Returns media_id or None."""
    try:
        img_resp = requests.get(
            FINVIZ_CHART.format(ticker=ticker),
            headers=FINVIZ_HEADERS,
            timeout=10,
        )
        img_resp.raise_for_status()
        media_resp = requests.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            auth=_auth(),
            files={"media": img_resp.content},
            timeout=20,
        )
        media_resp.raise_for_status()
        media_id = media_resp.json()["media_id_string"]
        print(f"Chart uploaded for {ticker}: media_id={media_id}")
        return media_id
    except Exception as e:
        print(f"Chart upload failed for {ticker} (non-fatal): {e}")
        return None


def post_tweet(text: str, media_id: str | None = None) -> int:
    if len(text) > 280:
        text = text[:277] + "..."
    body: dict = {"text": text}
    if media_id:
        body["media"] = {"media_ids": [media_id]}
    resp = requests.post(
        "https://api.twitter.com/2/tweets",
        auth=_auth(),
        json=body,
        timeout=15,
    )
    print(f"X [{resp.status_code}]: {text[:80]}...")
    resp.raise_for_status()
    return resp.status_code


def handle_market_daily_summary(detail: dict) -> tuple[str | None, str | None]:
    """
    MarketDailySummary — no-op for X. Fires from market_monitor at 5pm ET.

    TODO: PreMarketPulse (morning tweet) should come from premarket_alert.py
    at 8am ET when that agent is wired to the bus. Add a new handler then.
    """
    print("MarketDailySummary received — no X tweet (future: SlackPublisher/Discord)")
    return None, None


def handle_screener(detail: dict) -> tuple[str | None, str | None]:
    state = detail["market_state"]
    if state in SILENT_STATES:
        print(f"Skipping SetupOfDay — market is {state}")
        return None, None

    p     = detail["top_pick"]
    total = detail["total_tickers"]
    vcp_line = "VCP pattern ✓\n" if p.get("vcp") else ""
    qs = p.get("quality_score", 0)
    conviction = "A+ setup" if qs >= 90 else ("A setup" if qs >= 80 else ("B+ setup" if qs >= 70 else "B setup"))

    text = (
        f"Setup of the Day: ${p['ticker']}\n\n"
        f"Stage 2 confirmed ✓\n"
        f"{vcp_line}"
        f"Relative volume: {p['rel_vol']}x ✓\n"
        f"Conviction: {conviction} (Q:{qs})\n\n"
        f"{total} tickers in yesterday's screen.\n"
        f"Reply for the full PDF report.\n\n"
        f"Rules-based. Not advice."
    )
    media_id = _upload_chart(p["ticker"])
    return text, media_id


def handle_persistence(detail: dict) -> tuple[str | None, str | None]:
    ticker  = detail["ticker"]
    days    = detail["persistence_days"]
    state   = detail.get("market_state", "")
    fg      = detail.get("fear_greed", "")
    spy_pos = detail.get("spy_above_200ma")

    # Market state header line if available
    if state and fg != "":
        emoji   = REGIME_EMOJI.get(state, "⚪")
        spy_str = "above 200MA" if spy_pos else "below 200MA"
        state_line = f"{emoji} {state} | F&G: {fg} | SPY {spy_str}\n\n"
    else:
        state_line = ""

    text = (
        f"{state_line}"
        f"${ticker} has appeared in the screener\n"
        f"{days} days in a row this week.\n\n"
        f"Not a one-day spike.\n"
        f"Sustained presence = institutional interest building.\n\n"
        f"This is the pattern that preceded $FLY and $PL\n"
        f"before they made their moves.\n\n"
        f"Watching closely."
    )
    media_id = _upload_chart(ticker)
    return text, media_id


HANDLERS = {
    "MarketDailySummary": handle_market_daily_summary,  # no-op — future Slack/Discord
    "ScreenerCompleted":  handle_screener,               # 4:30pm ET — SetupOfDay tweet
    "PersistencePick":    handle_persistence,            # 8:00pm ET — PersistencePick tweet
}


def handler(event, context):
    detail_type = event.get("detail-type", "")
    detail      = event.get("detail", {})
    fn = HANDLERS.get(detail_type)
    if not fn:
        print(f"Unknown detail-type: {detail_type} — skipping")
        return {"statusCode": 200, "body": "unknown type"}
    text, media_id = fn(detail)
    if text is None:
        return {"statusCode": 200, "body": "skipped"}
    status = post_tweet(text, media_id)
    return {"statusCode": status}
