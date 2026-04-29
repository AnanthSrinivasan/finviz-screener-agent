"""
Shared per-position rules for the live (RH/SnapTrade) and paper (Alpaca) monitors.

Two layers:
  - apply_position_rules()    — per-tick trail / breakeven / targets / fade alert
  - check_ma_trail_alert()    — post-close ATR%-tiered, regime-adaptive MA trail (alert only)

Plus shared streak/sizing helpers:
  - update_sizing_mode()
  - record_trade_result()

Position state schema (dict, mutated in place):
  entry_price, stop_price, atr_pct, entry_date,
  highest_price_seen, peak_gain_pct, breakeven_activated,
  target1, target1_hit, target2, last_fade_alert_gain_pct (optional)
"""

import os
import datetime


# --- Per-tick rules -------------------------------------------------------

def apply_position_rules(ticker: str, entry: dict, current_price: float,
                         day_high: float, atr_pct: float,
                         label_prefix: str = "") -> tuple:
    """
    Per-tick trail / breakeven / targets / fade alert.

    Mutates `entry` in place. Returns (alerts, modified).

    Trail order:
      1. Update highest_price_seen / peak_gain_pct (intraday-aware via day_high).
      2. ATR incremental trail (silent, pre-breakeven only): stop = max(stop, price − 2×ATR$).
      3. Breakeven at peak +20%: stop locks to entry × 1.005, breakeven_activated = True (one-way).
      4. +30% peak: 10% trail from highest_price_seen.
      5. Target 1 / Target 2 alerts.
      6. Fade alert when peak ≥ +20% AND price < peak − 1×ATR$.

    Breakeven trigger keys off `peak_gain_pct` (not live `gain_pct`) — once peak hit +20%,
    the lock arms even if price has already pulled back.
    """
    prefix = ("[" + label_prefix + "] ") if label_prefix else ""
    alerts: list = []
    modified = False

    entry_price = entry.get("entry_price", 0) or 0
    if entry_price <= 0 or current_price <= 0:
        return alerts, modified

    atr_dollar = entry_price * (atr_pct / 100.0) if atr_pct > 0 else 0

    # 1. Update peak
    high_candidate = max(current_price, day_high or current_price)
    prev_high = entry.get("highest_price_seen", entry_price)
    if high_candidate > prev_high:
        entry["highest_price_seen"] = round(high_candidate, 2)
        prev_high = entry["highest_price_seen"]
        modified = True

    gain_pct = (current_price - entry_price) / entry_price * 100
    peak_gain_pct = (prev_high - entry_price) / entry_price * 100
    if peak_gain_pct > entry.get("peak_gain_pct", 0.0):
        entry["peak_gain_pct"] = round(peak_gain_pct, 2)
        modified = True

    current_stop = float(entry.get("stop_price") or 0)
    breakeven_active = bool(entry.get("breakeven_activated"))

    # 2. ATR incremental trail (silent, pre-breakeven)
    if atr_dollar > 0 and gain_pct > 0 and not breakeven_active:
        atr_trail = round(current_price - 2 * atr_dollar, 2)
        if atr_trail > current_stop:
            entry["stop_price"] = atr_trail
            current_stop = atr_trail
            modified = True

    # 3. Breakeven trigger — keys off peak_gain_pct (once-and-locked)
    if peak_gain_pct >= 20 and not breakeven_active:
        be_stop = round(entry_price * 1.005, 2)
        if be_stop > current_stop:
            entry["stop_price"] = be_stop
            current_stop = be_stop
        entry["breakeven_activated"] = True
        modified = True
        alerts.append(
            ":lock: " + prefix + ticker + " peak +" + str(round(peak_gain_pct, 1))
            + "% — stop moved to breakeven $" + str(be_stop)
        )

    # 4. +30% trail (10% from highest)
    if peak_gain_pct >= 30:
        trail_stop = round(prev_high * 0.90, 2)
        if trail_stop > current_stop:
            entry["stop_price"] = trail_stop
            current_stop = trail_stop
            modified = True
            alerts.append(
                ":chart_with_upwards_trend: " + prefix + ticker + " peak +"
                + str(round(peak_gain_pct, 1)) + "% — trailing stop raised to $"
                + str(trail_stop)
            )

    # 5. Targets
    t1 = entry.get("target1", 0) or 0
    if t1 > 0 and current_price >= t1 and not entry.get("target1_hit"):
        entry["target1_hit"] = True
        modified = True
        alerts.append(
            ":dart: " + prefix + ticker + " HIT TARGET 1 $" + str(t1)
            + " — consider selling half, move stop to breakeven"
        )

    t2 = entry.get("target2", 0) or 0
    if t2 > 0 and current_price >= t2:
        alerts.append(
            ":dart::dart: " + prefix + ticker + " HIT TARGET 2 $" + str(t2)
            + " — trail remaining position tightly"
        )

    # 6. Fade alert
    peak_gain = entry.get("peak_gain_pct", 0.0)
    if atr_dollar > 0 and peak_gain >= 20 and current_price < (prev_high - atr_dollar):
        last_fade = entry.get("last_fade_alert_gain_pct")
        if last_fade is None or (last_fade - gain_pct) >= 5:
            given_back = peak_gain - gain_pct
            alerts.append(
                ":warning: " + prefix + ticker + " fading — peak +"
                + str(round(peak_gain, 1)) + "%, now +"
                + str(round(gain_pct, 1)) + "% (gave back "
                + str(round(given_back, 1)) + "pp)"
            )
            entry["last_fade_alert_gain_pct"] = round(gain_pct, 2)
            modified = True
    elif "last_fade_alert_gain_pct" in entry:
        entry.pop("last_fade_alert_gain_pct", None)
        modified = True

    return alerts, modified


# --- Layer 1b: ATR%-tiered, regime-adaptive MA trail (alert-only) ---------

_MA_TRAIL_REGIME: dict = {
    "THRUST":   (21, 2),
    "GREEN":    (21, 2),
    "CAUTION":  (21, 1),
    "COOLING":  (8, 1),
    "RED":      None,
    "DANGER":   None,
    "BLACKOUT": None,
}


def _ema(values: list, span: int) -> list:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1 - alpha) * out[-1])
    return out


def _ma_trail_signal_for_atr(atr_pct: float, regime_span: int) -> tuple:
    if atr_pct > 8.0:
        return ("pct_trail", 1, None)
    if atr_pct > 5.0:
        return ("ema", 1, 8)
    return ("ema", 1 if regime_span <= 8 else 2, regime_span if regime_span > 0 else 21)


def check_ma_trail_alert(closes: list, market_state: str,
                         atr_pct: float = 0.0,
                         highest_price_seen: float = 0.0) -> dict | None:
    """
    Pure function — caller passes in `closes` (list of last N daily bar closes,
    oldest first). Returns dict on violation else None. None if regime disables.

    `closes` — caller fetches; live uses position_monitor.fetch_alpaca_daily_bars,
    paper passes the same.
    """
    cfg = _MA_TRAIL_REGIME.get(market_state)
    if cfg is None:
        return None
    regime_span, regime_consec = cfg
    signal_type, consec_needed, ema_span = _ma_trail_signal_for_atr(atr_pct, regime_span)
    if signal_type == "ema" and ema_span == regime_span:
        consec_needed = regime_consec

    if not closes:
        return None

    if signal_type == "pct_trail":
        if highest_price_seen <= 0:
            return None
        last_close = float(closes[-1])
        trail_floor = round(highest_price_seen * 0.90, 2)
        if last_close < trail_floor:
            return {
                "ma_type":     "10% trail",
                "consecutive": 1,
                "last_close":  round(last_close, 2),
                "last_ema":    trail_floor,
                "atr_pct":     round(atr_pct, 2),
                "tier":        "high_vol",
            }
        return None

    span = ema_span or regime_span
    if len(closes) < span + consec_needed:
        return None
    ema_series = _ema(closes, span)
    last_closes = closes[-consec_needed:]
    last_emas   = ema_series[-consec_needed:]
    if all(c < e for c, e in zip(last_closes, last_emas)):
        return {
            "ma_type":     str(span) + "EMA",
            "consecutive": consec_needed,
            "last_close":  round(last_closes[-1], 2),
            "last_ema":    round(last_emas[-1], 2),
            "atr_pct":     round(atr_pct, 2),
            "tier":        "low_vol" if span == regime_span else "mid_vol",
        }
    return None


# --- Streak / sizing mode -------------------------------------------------

def update_sizing_mode(trading_state: dict, market_state: str) -> list:
    """
    Recompute current_sizing_mode from streak + market state.
    Returns alert strings on transition.
    """
    alerts = []
    old_mode = trading_state.get("current_sizing_mode", "normal")

    if trading_state.get("consecutive_losses", 0) >= 3:
        new_mode = "suspended"
    elif trading_state.get("consecutive_losses", 0) == 2:
        new_mode = "reduced"
    elif trading_state.get("consecutive_wins", 0) >= 2 and market_state in ("GREEN", "THRUST"):
        new_mode = "aggressive"
    else:
        new_mode = "normal"

    trading_state["current_sizing_mode"] = new_mode

    if new_mode != old_mode:
        if new_mode == "suspended":
            alerts.append(
                "\U0001f6a8 SIZING SUSPENDED — 3 consecutive losses. "
                "Paper trade only until 2 consecutive wins."
            )
        elif new_mode == "reduced":
            alerts.append(
                "⚠️ SIZING REDUCED — 2 consecutive losses. "
                "Max 5% position size until streak breaks."
            )
        elif new_mode == "aggressive":
            alerts.append(
                "\U0001f680 SIZING AGGRESSIVE — 2+ wins in GREEN/THRUST."
            )
        else:
            alerts.append(":information_source: Sizing mode → normal.")
    return alerts


def record_trade_result(trading_state: dict, ticker: str, result_pct: float,
                        date_iso: str, side: str = "SELL",
                        source: str = "auto_detected") -> None:
    """
    Append to recent_trades and update consecutive_wins / consecutive_losses
    streaks. Neutral band (|result_pct| < 1.0) → 'neutral', does not bump
    either streak. Mirrors the SnapTrade-side semantics.
    """
    if abs(result_pct) < 1.0:
        result = "neutral"
    elif result_pct >= 0:
        result = "win"
    else:
        result = "loss"

    trading_state.setdefault("recent_trades", []).append({
        "ticker":     ticker,
        "result":     result,
        "result_pct": round(result_pct, 2),
        "date":       date_iso,
        "side":       side,
        "source":     source,
    })
    # Cap at 50 to mirror live retention
    trading_state["recent_trades"] = trading_state["recent_trades"][-50:]

    if result == "win":
        trading_state["consecutive_wins"]   = trading_state.get("consecutive_wins", 0) + 1
        trading_state["consecutive_losses"] = 0
        trading_state["total_wins"] = trading_state.get("total_wins", 0) + 1
    elif result == "loss":
        trading_state["consecutive_losses"] = trading_state.get("consecutive_losses", 0) + 1
        trading_state["consecutive_wins"]   = 0
        trading_state["total_losses"] = trading_state.get("total_losses", 0) + 1
    # neutral → no streak change

    trading_state["last_updated"] = date_iso
