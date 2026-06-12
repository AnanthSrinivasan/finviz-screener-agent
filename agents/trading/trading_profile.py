"""
Trading profile resolution + live-account guard rails.

TRADING_PROFILE=paper (default) | live selects which Alpaca account the
executor/monitor pair operates on. Spec: docs/specs/live-alpaca-executor.md.

The live profile is the ONLY code path authorized to place real-money orders,
and only against the dedicated Alpaca live account (~$5k). SnapTrade/Robinhood
remain alert-only forever — nothing in this module or its callers touches them.

Live deltas vs paper (all enforced by the pure functions below):
  - position cap 3, base size = equity / cap × size_mul
  - notional buys / fractional sells, $10 order floor
  - marketable-limit buys (last × 1.005, day TIF)
  - circuit breakers: −3% intraday halt · equity < 85% of high-water suspend
  - order sanity: ≤ 60% of equity, symbol must be on today's qualified list
  - idempotent client_order_id = "live-{YYYYMMDD}-{ticker}"
  - full exits only: no T1/T2 peels, hard full take-profit at +30%
"""

import os

LIVE_MAX_POSITIONS = 3
LIVE_MIN_ORDER_NOTIONAL = 10.0
LIVE_MAX_ORDER_EQUITY_FRAC = 0.60
LIVE_DAILY_HALT_PCT = -3.0
LIVE_DRAWDOWN_SUSPEND_FRAC = 0.85
LIVE_TAKE_PROFIT_PCT = 30.0
LIVE_LIMIT_MARKUP = 1.005


def resolve_profile(env=None) -> dict:
    """Resolve the trading profile from the environment.

    Unknown / unset TRADING_PROFILE values resolve to paper — live must be
    requested explicitly.
    """
    env = os.environ if env is None else env
    name = (env.get("TRADING_PROFILE") or "paper").strip().lower()
    if name == "live":
        return {
            "name": "live",
            "is_live": True,
            "api_key": env.get("ALPACA_LIVE_API_KEY", ""),
            "secret_key": env.get("ALPACA_LIVE_SECRET_KEY", ""),
            "base_url": env.get("ALPACA_LIVE_BASE_URL", "https://api.alpaca.markets/v2"),
            "stops_filename": "live_alpaca_stops.json",
            "state_filename": "live_alpaca_trading_state.json",
            "slack_tag": "[LIVE \U0001F534]",
            "label_prefix": "LIVE \U0001F534",
            "dry_run": (env.get("LIVE_DRY_RUN") or "").strip().lower() in ("1", "true", "yes"),
        }
    return {
        "name": "paper",
        "is_live": False,
        "api_key": env.get("ALPACA_API_KEY", ""),
        "secret_key": env.get("ALPACA_SECRET_KEY", ""),
        "base_url": env.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2"),
        "stops_filename": "paper_stops.json",
        "state_filename": "paper_trading_state.json",
        "slack_tag": "[PAPER]",
        "label_prefix": "PAPER",
        "dry_run": False,
    }


def make_client_order_id(today: str, ticker: str, side: str = "buy") -> str:
    """Idempotent order id — a retried workflow can never double-buy.
    today is ISO YYYY-MM-DD; buys get "live-YYYYMMDD-TICKER", sells a
    "live-sell-" prefix so a same-day full exit doesn't collide with the entry.
    """
    d = today.replace("-", "")
    prefix = "live-" if side == "buy" else "live-sell-"
    return prefix + d + "-" + ticker.upper()


def compute_live_allocation(equity: float, size_mul: float, quality_score: float,
                            cap: int = LIVE_MAX_POSITIONS) -> float:
    """Live base size = equity / cap × size_mul. Q<60 is not a trade (same
    floor as the paper Q-tier table — eligibility, not size)."""
    if quality_score < 60 or equity <= 0 or cap <= 0 or size_mul <= 0:
        return 0.0
    return equity / cap * size_mul


def live_order_sanity(notional: float, equity: float, ticker: str,
                      qualified_tickers: set) -> tuple:
    """Return (ok, reason). Last line of defense before a real-money order."""
    if notional < LIVE_MIN_ORDER_NOTIONAL:
        return False, "notional $" + str(round(notional, 2)) + " below $" + str(int(LIVE_MIN_ORDER_NOTIONAL)) + " minimum"
    if equity <= 0:
        return False, "equity unknown"
    if notional > LIVE_MAX_ORDER_EQUITY_FRAC * equity:
        return False, "order exceeds " + str(int(LIVE_MAX_ORDER_EQUITY_FRAC * 100)) + "% of equity"
    if ticker not in qualified_tickers:
        return False, "not on today's qualified list"
    return True, ""


def intraday_change_pct(equity: float, last_equity: float) -> float:
    if last_equity <= 0:
        return 0.0
    return (equity - last_equity) / last_equity * 100


def daily_halt_triggered(equity: float, last_equity: float) -> bool:
    """−3% intraday vs prior close equity → no further new entries today."""
    return intraday_change_pct(equity, last_equity) <= LIVE_DAILY_HALT_PCT


def drawdown_suspend_triggered(equity: float, high_water: float) -> bool:
    """Equity < 85% of high-water mark → live profile self-suspends."""
    if high_water <= 0:
        return False
    return equity < LIVE_DRAWDOWN_SUSPEND_FRAC * high_water


def update_high_water(state: dict, equity: float) -> bool:
    """Ratchet high_water_equity in the live trading state. Returns True when raised."""
    hw = float(state.get("high_water_equity", 0) or 0)
    if equity > hw:
        state["high_water_equity"] = round(equity, 2)
        return True
    return False


def should_full_take_profit(entry_price: float, current_price: float) -> bool:
    """Hard full take-profit at +30% — the whole position exits in one order."""
    if entry_price <= 0 or current_price <= 0:
        return False
    return (current_price - entry_price) / entry_price * 100 >= LIVE_TAKE_PROFIT_PCT


def marketable_limit(last_price: float) -> float:
    """Marketable-limit price: last × 1.005, protects against thin-open slippage."""
    return round(last_price * LIVE_LIMIT_MARKUP, 2)


def filter_expired_unfilled(orders: list, since_iso: str = "") -> list:
    """Agent-placed live BUY orders that expired/cancelled with zero fill —
    these get a Slack "no chase" log line. since_iso (ISO timestamp) dedups
    across monitor runs; partial fills are positions, not unfilled orders."""
    out = []
    for o in orders:
        if o.get("side") != "buy":
            continue
        if not str(o.get("client_order_id") or "").startswith("live-"):
            continue
        if o.get("status") not in ("expired", "canceled"):
            continue
        if float(o.get("filled_qty") or 0) > 0:
            continue
        ts = o.get("expired_at") or o.get("canceled_at") or o.get("updated_at") or ""
        if since_iso and ts and ts <= since_iso:
            continue
        out.append(o)
    return out
