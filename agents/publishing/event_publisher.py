"""
Fires events to EventBridge finviz-events bus.
All functions are non-fatal — if publishing fails, calling agent continues.
Never crash a screener or monitor run on publish failure.
"""
import json
import logging
import boto3

log = logging.getLogger(__name__)

REGION = "eu-central-1"
EVENT_BUS = "finviz-events"
SOURCE = "finviz.screener"


def _put(detail_type: str, detail: dict):
    """Internal: fire one event. Non-fatal."""
    try:
        client = boto3.client("events", region_name=REGION)
        resp = client.put_events(Entries=[{
            "Source": SOURCE,
            "DetailType": detail_type,
            "Detail": json.dumps(detail),
            "EventBusName": EVENT_BUS,
        }])
        failed = resp.get("FailedEntryCount", 0)
        if failed:
            log.error(f"EventBridge {detail_type} failure: {resp['Entries']}")
        else:
            log.info(f"EventBridge: {detail_type} fired")
    except Exception as e:
        log.warning(f"EventBridge {detail_type} skipped (non-fatal): {e}")


def publish_market_daily_summary(
    date: str,
    market_state: str,
    fear_greed: int,
    spy_above_200ma: bool,
):
    """
    Called from market_monitor at end of nightly run (~21:00 UTC / 5pm ET).

    Fires MarketDailySummary to the finviz-events bus.
    Currently a no-op on the subscriber side — XPublisher skips this event.

    TODO: When premarket_alert.py is wired to the bus, move the morning
    tweet trigger there and fire PreMarketPulse at 8am ET instead.
    At that point, wire a new rule on finviz-events:
      MarketDailySummary → SlackPublisher Lambda (replaces direct webhook calls)
    """
    _put("MarketDailySummary", {
        "date": date,
        "market_state": market_state,
        "fear_greed": fear_greed,
        "spy_above_200ma": spy_above_200ma,
    })


def publish_screener_completed(
    date: str,
    market_state: str,
    fear_greed: int,
    top_pick: dict,
    total_tickers: int,
    preview_report_url: str,
    full_report_url: str,
):
    """Called from finviz_agent.py at end of main(). Single best pick only."""
    _put("ScreenerCompleted", {
        "date": date,
        "market_state": market_state,
        "fear_greed": fear_greed,
        "top_pick": top_pick,
        "total_tickers": total_tickers,
        "preview_report_url": preview_report_url,
        "full_report_url": full_report_url,
    })


def publish_persistence_pick(
    date: str,
    ticker: str,
    persistence_days: int,
    quality_score: int,
    section: str,
    market_state: str = "",
    fear_greed: int = 0,
    spy_above_200ma: bool = False,
):
    """
    Called from finviz_agent.py only if ticker has 3+ days this week.
    If nothing qualifies, do NOT call this — silence is better than noise.
    """
    if persistence_days < 3:
        log.info(f"PersistencePick skipped — {ticker} only {persistence_days} days")
        return
    _put("PersistencePick", {
        "date": date,
        "ticker": ticker,
        "persistence_days": persistence_days,
        "quality_score": quality_score,
        "section": section,
        "market_state": market_state,
        "fear_greed": fear_greed,
        "spy_above_200ma": spy_above_200ma,
    })
