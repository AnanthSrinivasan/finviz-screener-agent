"""
Shared per-position rules for the live (RH/SnapTrade) and paper (Alpaca) monitors.

Two layers:
  - apply_position_rules()    — per-tick trail / breakeven / targets / fade
  - check_ma_trail_alert()    — post-close ATR%-tiered, regime-adaptive MA trail (alert only)

Plus shared streak/sizing helpers:
  - update_sizing_mode()
  - record_trade_result()

Position state schema (dict, mutated in place):
  entry_price, stop_price, atr_pct, entry_date,
  highest_price_seen, peak_gain_pct, breakeven_activated,
  target1, target1_hit, target2, last_fade_alert_gain_pct (optional)

Returned events (list of dicts) — caller formats Slack / logs / side effects:
  {kind: "breakeven"|"trailing_stop"|"target1"|"target2"|"fade",
   ticker, message, ...payload}

Stop-hit (Rule 1 — hard stop crossed) is NOT an event from this engine. The caller
handles it separately so the engine stays focused on trail / target logic and the
caller decides how to render the alert (and whether to mutate `status`).
"""

import os
import datetime


# --- Per-tick rules -------------------------------------------------------

def apply_position_rules(ticker: str, entry: dict, current_price: float,
                         day_high: float, atr_pct: float,
                         label_prefix: str = "") -> tuple:
    """
    Per-tick trail / breakeven / targets / fade alert.

    Mutates `entry` in place. Returns (events, modified) where events is a list
    of dicts, each `{kind, ticker, message, ...payload}`.

    Trail order:
      1. Update highest_price_seen / peak_gain_pct (intraday-aware via day_high).
      2. Loss-cap floor (peak ≥ +5%): stop ≥ max(entry × 0.97, entry − 0.5×ATR$).
         Hybrid — vol-aware (β) for low-vol names, fixed -3% (α) cap for high-vol.
      3. ATR-tiered trail (continuous, ratchets off highest_price_seen):
            peak < 10%   → 2.0 × ATR$  (room to breathe)
            peak ≥ 10%   → 1.5 × ATR$  (start locking)
            peak ≥ 20%   → 1.0 × ATR$  (lock — supersedes old breakeven)
      4. Breakeven flag — informational. Set when peak ≥ +20% to drive the
         dashboard / Slack `BE` indicator. No longer gates trail logic.
      5. +30% floor: stop ≥ max(1.0×ATR trail, peak × 0.90). Caps post-+30%
         give-back at 10% from peak even on very high-vol names.
      6. Target 1 / Target 2 alerts.
      7. Fade alert when peak ≥ +20% AND price < peak − 1×ATR$.

    Trail ratchets off `highest_price_seen` (not `current_price`) so hourly
    snapshots cannot miss intraday peaks for the lock — fixes the VIK 2026‑04
    case where peak $86.75 only registered $76.35 on the stop.
    """
    prefix = ("[" + label_prefix + "] ") if label_prefix else ""
    events: list = []
    modified = False

    entry_price = entry.get("entry_price", 0) or 0
    if entry_price <= 0 or current_price <= 0:
        return events, modified

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
    was_breakeven = bool(entry.get("breakeven_activated"))

    # 2. Loss-cap floor at peak ≥ +5% (hybrid: max of -3% fixed cap and -0.5×ATR%)
    if peak_gain_pct >= 5 and entry_price > 0:
        floor_pct  = round(entry_price * 0.97, 2)
        floor_atr  = round(entry_price - 0.5 * atr_dollar, 2) if atr_dollar > 0 else floor_pct
        loss_floor = max(floor_pct, floor_atr)
        if loss_floor > current_stop:
            entry["stop_price"] = loss_floor
            current_stop = loss_floor
            modified = True

    # 3. ATR-tiered trail (continuous, ratchets off highest_price_seen)
    if atr_dollar > 0 and peak_gain_pct > 0:
        if peak_gain_pct >= 20:
            mult = 1.0
        elif peak_gain_pct >= 10:
            mult = 1.5
        else:
            mult = 2.0
        atr_trail = round(prev_high - mult * atr_dollar, 2)
        if atr_trail > current_stop:
            entry["stop_price"] = atr_trail
            current_stop = atr_trail
            modified = True

    # 4. Breakeven crossover — drives Slack/dashboard BE indicator. Also a
    #    fallback floor for the atr=0 edge case where the ATR trail couldn't
    #    compute: at peak ≥ +20%, stop must be at least entry × 1.005.
    if peak_gain_pct >= 20:
        be_floor = round(entry_price * 1.005, 2)
        if be_floor > current_stop:
            entry["stop_price"] = be_floor
            current_stop = be_floor
            modified = True
        if not was_breakeven:
            entry["breakeven_activated"] = True
            modified = True
            events.append({
                "kind": "breakeven",
                "ticker": ticker,
                "stop_price": current_stop,
                "peak_gain_pct": round(peak_gain_pct, 1),
                "message": (
                    ":lock: " + prefix + ticker + " peak +" + str(round(peak_gain_pct, 1))
                    + "% — stop moved to breakeven, now locked at $" + str(current_stop)
                ),
            })

    # 5. +30% floor — max of ATR trail and 10%-from-peak (the 10% guard is the
    #    real protection on high-vol names where 1×ATR is wider than 10%)
    if peak_gain_pct >= 30:
        floor30 = round(prev_high * 0.90, 2)
        if floor30 > current_stop:
            entry["stop_price"] = floor30
            current_stop = floor30
            modified = True
            events.append({
                "kind": "trailing_stop",
                "ticker": ticker,
                "stop_price": floor30,
                "peak_gain_pct": round(peak_gain_pct, 1),
                "message": (
                    ":chart_with_upwards_trend: " + prefix + ticker + " peak +"
                    + str(round(peak_gain_pct, 1)) + "% — trailing stop raised to $"
                    + str(floor30)
                ),
            })

    # 5. Targets
    t1 = entry.get("target1", 0) or 0
    if t1 > 0 and current_price >= t1 and not entry.get("target1_hit"):
        entry["target1_hit"] = True
        modified = True
        events.append({
            "kind": "target1",
            "ticker": ticker,
            "target": t1,
            "message": (
                ":dart: " + prefix + ticker + " HIT TARGET 1 $" + str(t1)
                + " — consider selling half, move stop to breakeven"
            ),
        })

    t2 = entry.get("target2", 0) or 0
    if t2 > 0 and current_price >= t2:
        events.append({
            "kind": "target2",
            "ticker": ticker,
            "target": t2,
            "message": (
                ":dart::dart: " + prefix + ticker + " HIT TARGET 2 $" + str(t2)
                + " — trail remaining position tightly"
            ),
        })

    # 6. Fade alert
    peak_gain = entry.get("peak_gain_pct", 0.0)
    if atr_dollar > 0 and peak_gain >= 20 and current_price < (prev_high - atr_dollar):
        last_fade = entry.get("last_fade_alert_gain_pct")
        if last_fade is None or (last_fade - gain_pct) >= 5:
            given_back = peak_gain - gain_pct
            events.append({
                "kind": "fade",
                "ticker": ticker,
                "peak_gain_pct": round(peak_gain, 1),
                "current_gain_pct": round(gain_pct, 1),
                "given_back_pp": round(given_back, 1),
                "message": (
                    ":warning: " + prefix + ticker + " fading — peak +"
                    + str(round(peak_gain, 1)) + "%, now +"
                    + str(round(gain_pct, 1)) + "% (gave back "
                    + str(round(given_back, 1)) + "pp)"
                ),
            })
            entry["last_fade_alert_gain_pct"] = round(gain_pct, 2)
            modified = True
    elif "last_fade_alert_gain_pct" in entry:
        entry.pop("last_fade_alert_gain_pct", None)
        modified = True

    return events, modified


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

RECENT_TRADES_CAP = 30


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
                        source: str = "auto_detected",
                        profit_loss_usd: float | None = None) -> str:
    """
    Append to recent_trades and update consecutive_wins / consecutive_losses
    streaks. Neutral band (|result_pct| < 1.0) → 'neutral', does not bump
    either streak. Mirrors the SnapTrade-side semantics.

    Returns the result label: 'win' | 'loss' | 'neutral'.
    """
    if abs(result_pct) < 1.0:
        result = "neutral"
    elif result_pct >= 0:
        result = "win"
    else:
        result = "loss"

    trade_record = {
        "ticker":     ticker,
        "result":     result,
        "result_pct": round(result_pct, 2),
        "date":       date_iso,
        "side":       side,
        "source":     source,
    }
    if profit_loss_usd is not None:
        trade_record["profit_loss_usd"] = round(float(profit_loss_usd), 2)

    trading_state.setdefault("recent_trades", []).append(trade_record)
    trading_state["recent_trades"] = trading_state["recent_trades"][-RECENT_TRADES_CAP:]

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
    return result
