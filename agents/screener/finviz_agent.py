# ----------------------------
# Imports & Setup
# ----------------------------
import requests
from bs4 import BeautifulSoup
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import datetime
import time
import random
import os
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

FINVIZ_BASE = "https://finviz.com"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_BASE", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ATR_THRESHOLD = float(os.environ.get("ATR_THRESHOLD", "3.0"))
SNAPSHOT_WORKERS = int(os.environ.get("SNAPSHOT_WORKERS", "6"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    return s

session = make_session()

# ----------------------------
# Part 1: Screener Fetch & Save
# ----------------------------
screener_urls = {
    "10% Change": (
        f"{FINVIZ_BASE}/screener.ashx?v=151"
        f"&f=cap_smallover,ind_stocksonly,sh_avgvol_o500,sh_price_o5,"
        f"ta_changeopen_u10"
        f"&ft=4&o=-relativevolume&"
        f"c=0,1,2,3,4,5,6,64,67,65,66"
    ),
    "Growth": (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f=an_recom_buybetter,fa_epsqoq_o20,fa_salesqoq_o20,"
        f"ind_stocksonly,sh_avgvol_o1000,sh_price_o10,ta_perf_4wup,"
        f"ta_perf2_13wup,ta_sma20_pa,ta_sma200_pa,ta_sma50_pa&ft=4"
    ),
    "IPO": (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f=cap_midover,ind_stocksonly,ipodate_prev3yrs,sh_avgvol_o1000,"
        f"sh_price_o10,ta_beta_o0.5,ta_sma20_pa&ft=4"
    ),
    "52 Week High": (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f=ind_stocksonly,sh_avgvol_o1000,sh_price_o10,ta_beta_o1,"
        f"ta_highlow52w_nh&ft=4"
    ),
    "Week 20%+ Gain": (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f=cap_smallover,ind_stocksonly,sh_avgvol_o1000,ta_perf_1w30o,"
        f"ta_sma20_pa,ta_volatility_wo4&ft=4&o=-marketcap"
    ),
    # Bonde power-move: 9M+ actual volume + 10%+ daily change = institutional conviction move.
    # Finviz sh_vol_o* URL params are silently ignored — volume enforcement is done as a
    # post-filter in send_slack_notification() after the Volume column is parsed.
    "Power Move": (
        f"{FINVIZ_BASE}/screener.ashx?v=151"
        f"&f=ind_stocksonly,sh_price_o5,sh_avgvol_o500,"
        f"ta_change_u10"
        f"&ft=4&o=-volume&"
        f"c=0,1,2,3,4,5,6,64,67,65,66"
    ),
}

def fetch_all_tickers(screener_url: str, max_pages: int = 10) -> tuple:
    combined = []
    seen = set()
    ticker_meta = {}
    page = 1
    while page <= max_pages:
        resp = session.get(f"{screener_url}&r={1+(page-1)*20}", timeout=10)
        if resp.status_code != 200:
            log.warning(f"HTTP {resp.status_code} on page {page}, stopping.")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select('tr[valign="top"]')
        if not rows:
            break
        new_data = False
        for row in rows:
            cols = row.find_all('td')
            if len(cols) != 11:
                if cols:
                    log.debug(f"Skipping row with unexpected col count: {len(cols)}")
                continue
            ticker = cols[1].text.strip()
            if ticker and ticker not in seen:
                combined.append([c.text.strip() for c in cols])
                seen.add(ticker)
                new_data = True
                ticker_meta[ticker] = {
                    'Company':    cols[2].text.strip(),
                    'Sector':     cols[3].text.strip(),
                    'Industry':   cols[4].text.strip(),
                    'Country':    cols[5].text.strip(),
                    'Market Cap': cols[6].text.strip(),
                    'Volume':     cols[8].text.strip() if len(cols) > 8 else '',
                }
        if not new_data:
            break
        page += 1
        time.sleep(1 + random.uniform(0, 0.5))

    columns = ['No.', 'Ticker', 'Company', 'Sector', 'Industry',
               'Country', 'Market Cap', 'P/E', 'Volume', 'Price', 'Change']
    df = pd.DataFrame(combined, columns=columns) if combined else pd.DataFrame(columns=columns)
    return df, ticker_meta


def aggregate_and_save(screener_map: dict) -> tuple:
    mapping = defaultdict(list)
    meta_map = {}
    today = datetime.date.today().strftime("%Y-%m-%d")

    for name, url in screener_map.items():
        df, ticker_meta = fetch_all_tickers(url)
        log.info(f"{name}: {len(df)} tickers found")
        for t in df['Ticker'].unique():
            mapping[t].append(name)
            if t not in meta_map:
                meta_map[t] = ticker_meta.get(t, {})

    if not mapping:
        log.warning("No tickers found across all screeners — check Finviz connectivity.")
        return pd.DataFrame(columns=['Ticker', 'Appearances', 'Screeners', 'Sector', 'Industry', 'Company']), "", ""

    data = []
    for t, screens in mapping.items():
        m = meta_map.get(t, {})
        data.append({
            'Ticker':     t,
            'Appearances': len(screens),
            'Screeners':  ", ".join(screens),
            'Company':    m.get('Company', ''),
            'Sector':     m.get('Sector', ''),
            'Industry':   m.get('Industry', ''),
            'Country':    m.get('Country', ''),
            'Market Cap': m.get('Market Cap', ''),
            'Volume':     m.get('Volume', ''),
        })

    summary_df = pd.DataFrame(data).sort_values(['Appearances', 'Ticker'], ascending=[False, True])
    os.makedirs("data", exist_ok=True)
    csv_file  = f"data/finviz_screeners_{today}.csv"
    html_file = f"data/finviz_screeners_{today}.html"
    summary_df.to_csv(csv_file, index=False)
    summary_df.to_html(html_file, index=False)
    return summary_df, csv_file, html_file


# ----------------------------
# Part 2: Concurrent Snapshot Fetch
# ----------------------------

def get_snapshot_metrics(ticker: str, max_retries: int = 5):
    thread_session = make_session()
    for attempt in range(max_retries):
        try:
            resp = thread_session.get(
                f"{FINVIZ_BASE}/quote.ashx",
                params={"t": ticker},
                timeout=10,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")
            table = soup.find("table", class_="snapshot-table2")
            if not table:
                log.warning(f"{ticker}: snapshot table not found (layout may have changed)")
                return None, None, None, None, None, None, None, None, None, None, None, None

            data = {}
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                for key_cell, val_cell in zip(cells[0::2], cells[1::2]):
                    key = key_cell.get_text(strip=True).rstrip('.')
                    data[key] = val_cell.get_text(strip=True)

            price_raw = data.get("Price", "1").replace(',', '')
            price = float(price_raw) if price_raw else 1.0
            atr_pct = float(data.get("ATR (14)", 0)) / price * 100

            eps_str = data.get("EPS Y/Y TTM", '0').replace('%', '').strip()
            eps = float(eps_str) if eps_str not in ('-', '') else 0.0

            eps_qq_str = data.get("EPS Q/Q", '0').replace('%', '').strip()
            eps_qq = float(eps_qq_str) if eps_qq_str not in ('-', '') else 0.0

            sales_str = data.get("Sales Y/Y TTM", '0').replace('%', '').strip()
            sales = float(sales_str) if sales_str not in ('-', '') else 0.0

            inst_own_str = data.get("Inst Own", '0').replace('%', '').strip()
            inst_own = float(inst_own_str) if inst_own_str not in ('-', '') else 0.0

            inst_trans_str = data.get("Inst Trans", '0').replace('%', '').strip()
            inst_trans = float(inst_trans_str) if inst_trans_str not in ('-', '') else 0.0

            import re as _re
            high_52w_raw = data.get("52W High", "0").replace(",", "").strip()
            high_52w_match = _re.match(r"^(\d+\.?\d*)", high_52w_raw)
            high_52w = float(high_52w_match.group(1)) if high_52w_match else 0.0
            dist_from_high = ((price / high_52w) - 1) * 100 if high_52w > 0 else 0.0

            def parse_finviz_float(raw, default=0.0):
                import re as _re2
                if not raw or raw in ('-', ''):
                    return default
                raw = raw.replace(',', '').replace('x', '').strip()
                m = _re2.match(r'^([\d.]+)([KMBkmb]?)', raw)
                if not m:
                    return default
                val = float(m.group(1))
                suffix = m.group(2).upper()
                if suffix == 'K': val *= 1_000
                elif suffix == 'M': val *= 1_000_000
                elif suffix == 'B': val *= 1_000_000_000
                return val

            rel_vol_raw = data.get("Rel Volume", "1").strip()
            rel_vol = parse_finviz_float(rel_vol_raw, default=1.0)

            avg_vol_raw = data.get("Avg Volume", "0").strip()
            avg_vol = parse_finviz_float(avg_vol_raw, default=0.0)

            def parse_sma_pct(field):
                raw = data.get(field, "0%").replace("%", "").strip()
                try: return float(raw)
                except: return 0.0

            sma20_pct  = parse_sma_pct("SMA20")
            sma50_pct  = parse_sma_pct("SMA50")
            sma200_pct = parse_sma_pct("SMA200")

            return atr_pct, eps, sales, dist_from_high, rel_vol, avg_vol, sma20_pct, sma50_pct, sma200_pct, eps_qq, inst_own, inst_trans

        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait = (2 ** attempt) + random.random()
                log.warning(f"{ticker}: rate limited, retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                log.error(f"{ticker}: HTTP error {e.response.status_code}")
                break
        except Exception as e:
            log.error(f"{ticker}: unexpected error — {e}")
            break

    return None, None, None, None, None, None, None, None, None, None, None, None


def fetch_snapshots_concurrent(tickers: list, workers: int = SNAPSHOT_WORKERS) -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(get_snapshot_metrics, t): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            results[ticker] = future.result()
    return results


# ----------------------------
# Part 2b: Weinstein Stage Analysis
# ----------------------------

def compute_stage(row: pd.Series) -> dict:
    """
    Stan Weinstein Stage Analysis using SMA % distance fields from Finviz.

    Finviz SMA20/50/200 = % distance of price from that SMA:
      positive = price is ABOVE the SMA
      negative = price is BELOW the SMA

    MA stacking is the primary structural signal. If sma200 > sma50, the
    price is farther above the 200MA than the 50MA, which means the 50MA
    sits above the 200MA — correct uptrend stacking.

    50-day SMA is the structural gate. Stocks regularly pull back to (or
    slightly below) their 20-day and even 50-day SMA during healthy Stage 2
    uptrends — that's where Minervini enters. A -10% buffer on sma50
    accommodates high-ATR pullbacks without losing Stage 2 classification.

    Rel Volume and Distance from High are NOT gated here — they're already
    captured in the Quality Score. Stage classification answers "what stage
    is this stock in?", Quality Score answers "how good is this setup?"
    """
    sma20  = float(row.get('SMA20%',  0) or 0)
    sma50  = float(row.get('SMA50%',  0) or 0)
    sma200 = float(row.get('SMA200%', 0) or 0)

    # Stage 2 — structural uptrend: MAs stacked, price near/above 50MA
    stage2 = (
        sma200 > sma50 and       # 50MA above 200MA (structural stacking)
        sma50 > -10              # price not deeply below 50MA (allows pullbacks)
    )

    # Perfect Minervini alignment — price above all three MAs + stacked
    perfect = stage2 and sma20 > 0 and sma50 > 0

    # Stage 3 — distribution: price meaningfully below 50-day but above 200-day
    # -5% buffer avoids false Stage 3 on normal pullbacks to the 50MA
    stage3 = (sma200 > 0 and sma50 < -5)

    # Stage 4 — downtrend: price below 200-day
    stage4 = (sma200 < 0)

    # Stage 1 — basing near 200-day
    stage1 = (
        not stage2 and not stage3 and not stage4 and
        abs(sma200) < 8          # within 8% of 200-day — basing zone
    )

    if stage4:   stage_num = 4
    elif stage3: stage_num = 3
    elif stage2: stage_num = 2
    elif stage1: stage_num = 1
    else:        stage_num = 0   # transitional

    badges = {
        2: "🟢 Stage 2",
        1: "🔵 Stage 1",
        3: "🔴 Stage 3",
        4: "⚫ Stage 4",
        0: "⚪ Transitional",
    }

    return {
        "stage":   stage_num,
        "badge":   badges.get(stage_num, "⚪"),
        "perfect": perfect,
        "sma20":   round(sma20,  1),
        "sma50":   round(sma50,  1),
        "sma200":  round(sma200, 1),
    }


# ----------------------------
# Part 2c: Minervini VCP Detection
# ----------------------------

def compute_vcp(row: pd.Series) -> dict:
    """
    Minervini Volatility Contraction Pattern — daily chart approximation.
    VCP only meaningful in Stage 2.
    """
    atr_pct   = float(row.get('ATR%',           0) or 0)
    rel_vol   = float(row.get('Rel Volume',      1) or 1)
    dist_high = float(row.get('Dist From High%', 0) or 0)
    sma20     = float(row.get('SMA20%',          0) or 0)

    stage_data = row.get('Stage', {}) or {}
    stage = stage_data.get('stage', 0) if isinstance(stage_data, dict) else 0

    if stage != 2:
        return {"vcp_possible": False, "confidence": 0, "reason": "Not Stage 2"}

    signals = []
    confidence = 0

    if atr_pct < 4:
        signals.append(f"tight range ATR {atr_pct:.1f}%")
        confidence += 30
    elif atr_pct < 6:
        signals.append(f"moderate tightness ATR {atr_pct:.1f}%")
        confidence += 15

    if rel_vol < 0.8:
        signals.append(f"volume dry-up RVol {rel_vol:.1f}x")
        confidence += 30
    elif rel_vol < 1.0:
        signals.append(f"below-avg volume RVol {rel_vol:.1f}x")
        confidence += 15

    if -20 < dist_high <= -3:
        signals.append(f"tight pullback {dist_high:.0f}% from high")
        confidence += 25
    elif -30 < dist_high <= -20:
        signals.append(f"moderate pullback {dist_high:.0f}% from high")
        confidence += 10

    if sma20 > 0:
        signals.append("holding above 20-day MA")
        confidence += 15

    vcp_possible = confidence >= 50

    return {
        "vcp_possible": vcp_possible,
        "confidence":   confidence,
        "signals":      signals,
        "reason":       " · ".join(signals) if signals else "no VCP signals",
    }


# ----------------------------
# Part 2c-2: 10% Change Momentum Gate
# ----------------------------

def check_10pct_momentum_quality(row: pd.Series) -> tuple:
    """
    Gate for tickers surfaced by the '10% Change' screener.

    A 10% move means nothing if it's a dead-cat bounce from lows on thin
    volume in a Stage 4 downtrend.  Four checks must ALL pass:

      1. Above 50-day MA  (SMA50% > 0)
      2. Relative Volume >= 1.5x
      3. Within 30% of 52-week high  (Dist From High% >= -30)
      4. Stage must be 2 or 0 (Transitional) — not 3 or 4

    Returns (passes, reasons) where reasons lists what failed.
    Only call this for tickers with '10% Change' in their Screeners column.
    """
    reasons = []

    sma50 = float(row.get('SMA50%', 0) or 0)
    if sma50 <= 0:
        reasons.append(f"below 50d MA ({sma50:+.1f}%)")

    rel_vol = float(row.get('Rel Volume', 0) or 0)
    if rel_vol < 1.5:
        reasons.append(f"RVol {rel_vol:.1f}x < 1.5x")

    dist_high = float(row.get('Dist From High%', -999) or -999)
    if dist_high < -30:
        reasons.append(f"{dist_high:.0f}% from 52w high")

    stage_data = row.get('Stage', {}) or {}
    stage_num = stage_data.get('stage', 0) if isinstance(stage_data, dict) else 0
    if stage_num in (3, 4):
        labels = {3: "Distribution", 4: "Downtrend"}
        reasons.append(f"Stage {stage_num} ({labels[stage_num]})")

    return (len(reasons) == 0, reasons)


# ----------------------------
# Part 2d: Quality Score
# ----------------------------

def compute_quality_score(row: pd.Series) -> float:
    score = 0.0

    mcap_raw = str(row.get('Market Cap', '') or '').strip().upper()
    mcap = 0.0
    try:
        if mcap_raw.endswith('B'):   mcap = float(mcap_raw[:-1])
        elif mcap_raw.endswith('M'): mcap = float(mcap_raw[:-1]) / 1000
    except:
        pass

    if mcap >= 100:  score += 30
    elif mcap >= 50: score += 27
    elif mcap >= 10: score += 22
    elif mcap >= 2:  score += 14
    elif mcap >= 0.3:score += 5
    else:            score += 0

    rel_vol = row.get('Rel Volume')
    if pd.notna(rel_vol) and rel_vol is not None:
        rv = float(rel_vol)
        if rv >= 5.0:   score += 25
        elif rv >= 3.0: score += 20
        elif rv >= 2.0: score += 15
        elif rv >= 1.5: score += 10
        elif rv >= 1.0: score += 5

    eps_yy = row.get('EPS Y/Y TTM')
    eps_qq = row.get('EPS Q/Q')
    eps_yy_v = float(eps_yy) if pd.notna(eps_yy) and eps_yy is not None else None
    eps_qq_v = float(eps_qq) if pd.notna(eps_qq) and eps_qq is not None else None
    # Use the better of annual or quarterly — Q/Q rescues spin-offs/IPOs with distorted TTM
    eps_best = max(v for v in [eps_yy_v, eps_qq_v] if v is not None) if (eps_yy_v is not None or eps_qq_v is not None) else None
    if eps_best is not None:
        ev = eps_best
        if ev >= 200:   score += 20
        elif ev >= 100: score += 16
        elif ev >= 50:  score += 12
        elif ev >= 20:  score += 8
        elif ev >= 0:   score += 4

    # Institutional accumulation signal — Inst Trans > 0 means funds added last quarter
    inst_trans = row.get('Inst Trans')
    if pd.notna(inst_trans) and inst_trans is not None:
        it = float(inst_trans)
        if it >= 10:    score += 8   # strong institutional buying
        elif it >= 3:   score += 5   # moderate accumulation
        elif it >= 0:   score += 2   # mild / stable

    appearances = row.get('Appearances', 1)
    if pd.notna(appearances):
        apps = int(appearances)
        if apps >= 3:   score += 15
        elif apps >= 2: score += 10
        else:           score += 0

    stage_data = row.get('Stage', {})
    if isinstance(stage_data, dict):
        stage_num = stage_data.get('stage', 0)
        perfect   = stage_data.get('perfect', False)
        if stage_num == 2:
            score += 25
            if perfect: score += 10
        elif stage_num == 3: score -= 25
        elif stage_num == 4: score -= 40
        elif stage_num == 1: score -= 10

    vcp_data = row.get('VCP', {})
    if isinstance(vcp_data, dict) and vcp_data.get('vcp_possible'):
        score += 15

    dist = row.get('Dist From High%')
    if pd.notna(dist) and dist is not None:
        d = float(dist)
        if d <= -50:   score += 10
        elif d <= -30: score += 8
        elif d <= -15: score += 5
        elif d <= -5:  score += 2
        else:          score += 0

    return round(score, 1)


# ----------------------------
# Part 2e: Sector Rotation
# ----------------------------

def compute_sector_rotation(df: pd.DataFrame) -> list:
    """
    Compute sector rotation summary from today's quality screener data.
    Returns list of sector dicts sorted by composite score (count × avg_q × stage2 bonus).
    Call with filter_df (already ATR-filtered) for meaningful results.
    """
    if df.empty or 'Sector' not in df.columns:
        return []

    sectors: dict = {}
    for _, row in df.iterrows():
        sector = str(row.get('Sector', '') or '').strip()
        if not sector or sector in ('nan', '—', ''):
            continue
        if sector not in sectors:
            sectors[sector] = {'count': 0, 'q_sum': 0.0, 'stage2': 0, 'vcp': 0,
                               'eps_sum': 0.0, 'eps_count': 0}
        s = sectors[sector]
        s['count'] += 1
        s['q_sum'] += float(row.get('Quality Score', 0) or 0)

        stage_data = row.get('Stage', {}) or {}
        if isinstance(stage_data, dict) and stage_data.get('stage') == 2:
            s['stage2'] += 1

        vcp_data = row.get('VCP', {}) or {}
        if isinstance(vcp_data, dict) and vcp_data.get('vcp_possible'):
            s['vcp'] += 1

        eps = row.get('EPS Y/Y TTM')
        if pd.notna(eps) and eps is not None:
            ev = float(eps)
            if ev != 0:
                s['eps_sum'] += ev
                s['eps_count'] += 1

    result = []
    for sector, s in sectors.items():
        count    = s['count']
        avg_q    = s['q_sum'] / count if count else 0.0
        avg_eps  = s['eps_sum'] / s['eps_count'] if s['eps_count'] else 0.0
        stage2_r = s['stage2'] / count if count else 0.0
        # Score: volume of quality setups × avg quality, with Stage 2 ratio as tiebreaker bonus
        score    = count * avg_q * (1.0 + stage2_r * 0.5)
        result.append({
            'sector':  sector,
            'count':   count,
            'avg_q':   round(avg_q, 1),
            'stage2':  s['stage2'],
            'vcp':     s['vcp'],
            'avg_eps': round(avg_eps, 1),
            'score':   round(score, 1),
        })

    return sorted(result, key=lambda x: x['score'], reverse=True)


# ----------------------------
# Part 3: Chart Gallery
# ----------------------------

def _classify_ticker(row) -> str:
    screeners  = str(row.get('Screeners', '') or '')
    stage_data = row.get('Stage', {}) or {}
    stage_num  = stage_data.get('stage', 0) if isinstance(stage_data, dict) else 0
    rel_vol    = float(row.get('Rel Volume', 1) or 1)
    atr_pct    = float(row.get('ATR%', 0) or 0)
    dist_high  = float(row.get('Dist From High%', 0) or 0)
    sma20      = float(row.get('SMA20%', 0) or 0)

    # Power Move — 9M+ volume + 5%+ daily change (Bonde method). Time-sensitive signal.
    if 'Power Move' in screeners:
        return 'power_move'
    # IPO lifecycle: in IPO screener OR showing IPO washout-recovery pattern
    # (deeply below 52w high but strongly above 20-day MA)
    if 'IPO' in screeners or (dist_high <= -40 and sma20 >= 10):
        return 'ipo'
    if stage_num == 2:
        return 'stage2'
    if rel_vol >= 2.0 and atr_pct >= 4.0:
        return 'momentum'
    return 'watch'


def _build_card(t: str, row, finviz_base: str, top_sectors: set = None) -> str:
    chart_url  = f"{finviz_base}/chart.ashx?t={t}&ty=c&ta=1&p=d&s=m"
    finviz_url = f"{finviz_base}/quote.ashx?t={t}"

    atr     = f"{row['ATR%']:.1f}%"         if pd.notna(row.get('ATR%'))          else "—"
    eps     = f"{row['EPS Y/Y TTM']:.1f}%"  if pd.notna(row.get('EPS Y/Y TTM'))   else "—"
    apps    = row['Appearances']             if pd.notna(row.get('Appearances'))   else "—"
    screeners = row.get('Screeners', '') or ""
    sector    = row.get('Sector', '')    or ""
    industry  = row.get('Industry', '')  or ""
    company   = row.get('Company', '')   or ""
    mktcap    = row.get('Market Cap', '') or ""
    qscore    = f"{row['Quality Score']:.0f}"   if pd.notna(row.get('Quality Score'))  else "—"
    dist      = f"{row['Dist From High%']:.0f}%" if pd.notna(row.get('Dist From High%')) else "—"
    rel_vol   = f"{row['Rel Volume']:.1f}x"     if pd.notna(row.get('Rel Volume'))      else "—"
    sma20     = f"{row['SMA20%']:+.1f}%"        if pd.notna(row.get('SMA20%'))          else "—"
    sma50     = f"{row['SMA50%']:+.1f}%"        if pd.notna(row.get('SMA50%'))          else "—"
    sma200    = f"{row['SMA200%']:+.1f}%"       if pd.notna(row.get('SMA200%'))         else "—"

    stage_data   = row.get('Stage', {}) or {}
    stage_num    = stage_data.get('stage',   0)  if isinstance(stage_data, dict) else 0
    stage_badge  = stage_data.get('badge',  '')  if isinstance(stage_data, dict) else ''
    stage_perfect= stage_data.get('perfect', False) if isinstance(stage_data, dict) else False

    vcp_data = row.get('VCP', {}) or {}
    vcp_ok   = vcp_data.get('vcp_possible', False) if isinstance(vcp_data, dict) else False

    is_power_move = 'Power Move' in screeners
    if is_power_move:                card_border = "#f97316"
    elif stage_num == 2 and vcp_ok: card_border = "#facc15"
    elif stage_num == 2:             card_border = "#4ade80"
    elif stage_num == 3:             card_border = "#f87171"
    elif stage_num == 4:             card_border = "#6b7280"
    else:                            card_border = "#2d3148"

    qs_int = int(float(qscore)) if qscore != "—" else 0
    if qs_int >= 60:   qs_color = "#4ade80"
    elif qs_int >= 35: qs_color = "#facc15"
    else:              qs_color = "#64748b"

    sector_html = ""
    if sector:
        label = sector + (f" · {industry}" if industry else "")
        sector_html = f'<div class="sector-tag">{label}</div>'

    vcp_badge       = '<span class="tag-vcp">VCP</span>'             if vcp_ok       else ''
    perfect_badge   = '<span class="tag-perf">⚡ aligned</span>'    if stage_perfect else ''
    power_move_badge= '<span class="tag-power-move">⚡ Power Move</span>' if is_power_move else ''
    sector_lead_badge = (
        '<span class="tag-sector-lead">🏆 Lead Sector</span>'
        if top_sectors and sector and sector in top_sectors else ''
    )

    # Overhead resistance badge — informational only, no Q-score impact.
    # Fires when price is within 3-8% below the 52-week high (approaching resistance).
    dist_high_val = float(row.get('Dist From High%', -999) or -999)
    overhead_badge = (
        '<span class="tag-overhead" title="Near 52-week high — watch for resistance">⚠️ Overhead</span>'
        if -8 <= dist_high_val <= -3 else ''
    )

    # CC quick filter: EPS positive + Stage 2 or high-momentum = potential character change
    eps_val = float(row.get('EPS Y/Y TTM', 0) or 0) if pd.notna(row.get('EPS Y/Y TTM')) else 0
    rvol_val = float(row.get('Rel Volume', 0) or 0) if pd.notna(row.get('Rel Volume')) else 0
    atr_pct = float(row.get('ATR%', 0) or 0) if pd.notna(row.get('ATR%')) else 0
    cc_hint = (eps_val > 0 and rvol_val >= 2.0
               and (stage_num == 2 or (rvol_val >= 2.5 and atr_pct >= 4.0)))
    cc_hint_badge = '<span class="tag-cc-hint">⚡ CC?</span>' if cc_hint else ''

    sma_html = (
        f'<div class="sma-row">'
        f'<span title="vs 20-day MA">20d {sma20}</span>'
        f'<span title="vs 50-day MA">50d {sma50}</span>'
        f'<span title="vs 200-day MA">200d {sma200}</span>'
        f'</div>'
    )

    return f"""
<div class="chart-item" style="border-color:{card_border}">
  <div class="chart-header">
    <div>
      <a href="{finviz_url}" target="_blank" class="ticker-link">{t}</a>
      {f'<span class="company">{company}</span>' if company else ''}
    </div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px;flex-shrink:0;margin-left:8px">
      <span class="badge">{apps} screen{'s' if apps != 1 else ''}</span>
      <span style="font-size:10px;font-weight:700;color:{qs_color}">Q {qscore}</span>
    </div>
  </div>
  <div class="stage-row">
    <span class="stage-badge">{stage_badge}</span>{power_move_badge}{vcp_badge}{perfect_badge}{sector_lead_badge}{cc_hint_badge}{overhead_badge}
  </div>
  {sector_html}
  {sma_html}
  <a href="{finviz_url}" target="_blank">
    <img src="{chart_url}" alt="{t}" loading="lazy" class="chart-img">
  </a>
  <div class="meta">
    <span title="ATR%">ATR {atr}</span>
    <span title="EPS Y/Y TTM">EPS {eps}</span>
    {f'<span title="Market Cap">{mktcap}</span>' if mktcap else ''}
    <span title="Relative Volume">RVol {rel_vol}</span>
    <span title="Distance from 52w High">{dist} hi</span>
  </div>
  <div class="screeners">{screeners}</div>
</div>"""


def generate_finviz_gallery(tickers: list, filter_df: pd.DataFrame,
                            excluded_df: pd.DataFrame | None = None) -> str:
    today = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs("data", exist_ok=True)
    out_html = f"data/finviz_chart_grid_{today}.html"

    sections = {
        'power_move': {'title': '🔥 Power Moves',          'subtitle': '9M+ vol + 10%+ daily move — institutional conviction signal (Bonde method) · Same-day only', 'cards': []},
        'stage2':   {'title': '🟢 Stage 2 Leaders',        'subtitle': 'Weinstein Stage 2 confirmed — wealth-building trades',              'cards': []},
        'ipo':      {'title': '🚀 IPO Lifecycle',           'subtitle': 'IPO screener — evaluate on lifecycle, not SMA rules',              'cards': []},
        'momentum': {'title': '⚡ Momentum / Catalyst',    'subtitle': 'High relative volume + significant move — 2-4 week plays',        'cards': []},
        'watch':    {'title': '👀 Watch List',             'subtitle': 'Transitional or lower conviction — monitor, do not chase',        'cards': []},
        'excluded': {'title': '🚫 Excluded — check manually', 'subtitle': '10% Change tickers that failed momentum quality gate (below 50d MA, low RVol, far from highs, or Stage 3/4)', 'cards': []},
    }

    # Compute dominant sectors (top 2 by ticker count) for sector discipline badge
    if 'Sector' in filter_df.columns:
        sector_counts = filter_df['Sector'].dropna().value_counts()
        top_sectors = set(sector_counts.head(2).index.tolist())
    else:
        top_sectors = set()

    for t in tickers:
        rows = filter_df[filter_df['Ticker'] == t]
        if rows.empty:
            continue
        row     = rows.iloc[0]
        section = _classify_ticker(row)
        card    = _build_card(t, row, FINVIZ_BASE, top_sectors)
        sections[section]['cards'].append(card)

    # Add excluded 10% Change tickers to the excluded section
    if excluded_df is not None and not excluded_df.empty:
        for _, row in excluded_df.iterrows():
            t = row['Ticker']
            card = _build_card(t, row, FINVIZ_BASE, top_sectors)
            sections['excluded']['cards'].append(card)

    sections_html = ""
    for key, sec in sections.items():
        if not sec['cards']:
            continue
        count = len(sec['cards'])
        sections_html += f"""
<div class="section">
  <div class="section-header">
    <h2>{sec['title']} <span class="section-count">{count}</span></h2>
    <p class="section-sub">{sec['subtitle']}</p>
  </div>
  <div class="chart-grid">{"".join(sec['cards'])}</div>
</div>"""

    total = sum(len(s['cards']) for s in sections.values())

    # Build sector rotation panel
    sector_data = compute_sector_rotation(filter_df)
    sector_rotation_html = ""
    if sector_data:
        sr_cards = ""
        for i, s in enumerate(sector_data[:8]):
            is_lead    = i == 0
            border     = "#22c55e" if is_lead else "#2d3148"
            lead_badge = '<span class="sr-lead-badge">Leading</span>' if is_lead else ''
            s2_html    = f'<span class="sr-stat sr-s2">{s["stage2"]} S2</span>' if s['stage2'] else ''
            vcp_html   = f'<span class="sr-stat sr-vcp">{s["vcp"]} VCP</span>' if s['vcp'] else ''
            sr_cards += f"""
<div class="sr-card" style="border-color:{border}">
  <div class="sr-sector-name">{s['sector']}{lead_badge}</div>
  <div class="sr-stats-row">
    <span class="sr-stat">{s['count']} setups</span>
    <span class="sr-stat sr-q">Q{s['avg_q']:.0f}</span>
    {s2_html}{vcp_html}
  </div>
</div>"""
        sector_rotation_html = f"""
<div class="sr-panel">
  <div class="section-header" style="border-left-color:#38bdf8">
    <h2>📊 Sector Rotation</h2>
    <p class="section-sub">Quality setup distribution by sector — leading sector signals rotation opportunity · {today}</p>
  </div>
  <div class="sr-grid">{sr_cards}
  </div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Finviz Chart Gallery — {today}</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #f9fafb; color: #111827; padding: 24px; }}
.page-title {{ font-size: 1.3rem; font-weight: 700; margin-bottom: 4px; color: #111827; }}
.page-sub   {{ color: #6b7280; font-size: 0.82rem; margin-bottom: 32px; }}
.section    {{ margin-bottom: 40px; }}
.section-header {{ margin-bottom: 14px; border-left: 3px solid #d1d5db; padding-left: 12px; }}
h2 {{ font-size: 1rem; font-weight: 700; color: #111827; display:flex; align-items:center; gap:8px; }}
.section-count {{ background: #f3f4f6; color: #6b7280; font-size: 0.72rem;
                  padding: 1px 7px; border-radius: 10px; font-weight: 500; }}
.section-sub {{ font-size: 0.75rem; color: #6b7280; margin-top: 4px; }}
.chart-grid  {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }}
.chart-item  {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px;
                padding: 12px; transition: border-color .15s; box-shadow: 0 1px 3px rgba(0,0,0,.04); }}
.chart-item:hover {{ border-color: #2563eb; }}
.chart-header {{ display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 5px; }}
.ticker-link  {{ font-size: 1rem; font-weight: 700; color: #2563eb; text-decoration: none; display: block; }}
.ticker-link:hover {{ color: #1d4ed8; text-decoration: underline; }}
.company  {{ font-size: 0.7rem; color: #6b7280; display: block; margin-top: 1px; }}
.badge    {{ background: #eff6ff; color: #2563eb; font-size: 0.7rem; font-weight: 600;
             padding: 2px 7px; border-radius: 20px; white-space: nowrap; margin-left: 6px; flex-shrink: 0; }}
.stage-row {{ display: flex; align-items: center; gap: 4px; margin: 5px 0; flex-wrap: wrap; }}
.stage-badge {{ font-size: 0.72rem; color: #374151; }}
.tag-vcp  {{ font-size: 9px; background: #fef3c7; color: #92400e;
             padding: 1px 5px; border-radius: 3px; font-weight: 700; }}
.tag-perf {{ font-size: 9px; background: #dcfce7; color: #166534;
             padding: 1px 5px; border-radius: 3px; font-weight: 600; }}
.tag-sector-lead {{ font-size: 9px; background: #ede9fe; color: #5b21b6;
                    padding: 1px 5px; border-radius: 3px; font-weight: 700; }}
.tag-cc-hint {{ font-size: 9px; background: #fef9c3; color: #854d0e;
                padding: 1px 5px; border-radius: 3px; font-weight: 700; }}
.tag-overhead {{ font-size: 9px; background: #fff7ed; color: #c2410c;
                 padding: 1px 5px; border-radius: 3px; font-weight: 700;
                 border: 1px solid #fdba74; }}
.sector-tag {{ font-size: 0.7rem; color: #2563eb; background: #eff6ff;
               border-radius: 4px; padding: 2px 6px; display: inline-block; margin-bottom: 6px; }}
.sma-row {{ display: flex; gap: 8px; font-size: 0.68rem; color: #6b7280; margin-bottom: 6px; flex-wrap: wrap; }}
.sma-row span {{ background: #f3f4f6; padding: 1px 5px; border-radius: 3px; }}
.chart-img {{ width: 100%; border-radius: 6px; display: block; cursor: pointer; transition: opacity .15s; }}
.chart-img:hover {{ opacity: .85; }}
.meta {{ display: flex; gap: 7px; margin-top: 8px; font-size: 0.75rem; color: #6b7280; flex-wrap: wrap; }}
.meta span {{ background: #f3f4f6; padding: 2px 6px; border-radius: 4px; }}
.screeners {{ margin-top: 5px; font-size: 0.7rem; color: #9ca3af; line-height: 1.4; }}
.tag-power-move {{ font-size: 9px; background: #fff1f2; color: #e11d48;
                   padding: 1px 5px; border-radius: 3px; font-weight: 700;
                   border: 1px solid #fda4af; }}
/* Sector rotation panel */
.sr-panel {{ margin-bottom: 40px; }}
.sr-grid {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }}
.sr-card {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px;
            padding: 10px 14px; min-width: 150px; }}
.sr-sector-name {{ font-size: 0.82rem; font-weight: 700; color: #111827; margin-bottom: 6px; }}
.sr-lead-badge {{ font-size: 9px; background: #dcfce7; color: #166534;
                  padding: 1px 5px; border-radius: 3px; font-weight: 700; margin-left: 6px; }}
.sr-stats-row {{ display: flex; flex-wrap: wrap; gap: 4px; }}
.sr-stat {{ font-size: 0.7rem; color: #6b7280; background: #f3f4f6; padding: 1px 5px; border-radius: 3px; }}
.sr-q {{ color: #d97706 !important; }}
.sr-s2 {{ color: #16a34a !important; }}
.sr-vcp {{ color: #92400e !important; }}
/* PDF export */
.pdf-btn {{ position: fixed; bottom: 24px; right: 24px; background: #2563eb; color: #fff;
            border: none; border-radius: 50%; width: 44px; height: 44px; font-size: 1.1rem;
            cursor: pointer; z-index: 999; box-shadow: 0 2px 8px rgba(0,0,0,.2);
            display: flex; align-items: center; justify-content: center; }}
.pdf-btn:hover {{ background: #1d4ed8; }}
@media print {{
  .pdf-btn {{ display: none; }}
  body {{ padding: 0; background: #fff; }}
  .chart-item {{ break-inside: avoid; }}
  .sr-card {{ break-inside: avoid; }}
}}
</style>
</head>
<body>
<button class="pdf-btn" onclick="window.print()" title="Export PDF">⬇</button>
<div class="page-title">Finviz Chart Gallery</div>
<p class="page-sub">{today} · {total} tickers · ATR% &gt; {ATR_THRESHOLD} · Click any ticker or chart to open in Finviz</p>
{sector_rotation_html}
{sections_html}
</body>
</html>"""

    with open(out_html, 'w') as f:
        f.write(html)
    return out_html


# ----------------------------
# Part 4: AI-Generated Summary
# ----------------------------

def generate_ai_summary(filter_df: pd.DataFrame, today: str) -> str:
    if not ANTHROPIC_API_KEY:
        log.info("ANTHROPIC_API_KEY not set — skipping AI summary.")
        return ""

    sorted_df = filter_df.sort_values('Quality Score', ascending=False) if 'Quality Score' in filter_df.columns else filter_df
    rows = []
    for _, row in sorted_df.head(20).iterrows():
        atr   = f"{row['ATR%']:.1f}%"          if pd.notna(row.get('ATR%'))          else "n/a"
        sales = f"{row['Sales Y/Y TTM']:.1f}%"  if pd.notna(row.get('Sales Y/Y TTM')) else "n/a"
        qs    = f"{row['Quality Score']:.0f}"   if pd.notna(row.get('Quality Score')) else "n/a"
        dist  = f"{row['Dist From High%']:.0f}%" if pd.notna(row.get('Dist From High%')) else "n/a"
        rvol  = f"{row['Rel Volume']:.1f}x"     if pd.notna(row.get('Rel Volume'))    else "n/a"
        # Prefer Q/Q EPS when it's the stronger signal (spin-offs / IPOs)
        eps_yy_v = float(row['EPS Y/Y TTM']) if pd.notna(row.get('EPS Y/Y TTM')) else None
        eps_qq_v = float(row['EPS Q/Q'])     if pd.notna(row.get('EPS Q/Q'))     else None
        if eps_qq_v is not None and (eps_yy_v is None or eps_qq_v > eps_yy_v):
            eps_str = f"{eps_qq_v:.1f}% Q/Q"
        elif eps_yy_v is not None:
            eps_str = f"{eps_yy_v:.1f}% TTM"
        else:
            eps_str = "n/a"
        inst_trans_v = float(row['Inst Trans']) if pd.notna(row.get('Inst Trans')) else None
        inst_str = f" | InstTrans {inst_trans_v:+.1f}%" if inst_trans_v is not None and inst_trans_v != 0 else ""
        rows.append(
            f"{row['Ticker']} ({row.get('Sector','?')} / {row.get('Industry','?')}) "
            f"| Quality {qs} | {row['Appearances']} screens: {row['Screeners']} "
            f"| ATR {atr} | EPS {eps_str} | Sales {sales} | MCap {row.get('Market Cap','?')} "
            f"| RVol {rvol} | From52wHigh {dist}{inst_str}"
        )

    prompt = f"""You are a sharp momentum trader reviewing today's Finviz screener results ({today}).

Here are the top tickers that passed all filters (ATR% > {ATR_THRESHOLD}, sorted by screener appearances):

{chr(10).join(rows)}

Write a concise 4-6 sentence analyst briefing for a Slack message. Cover:
- The top 2-3 tickers by Quality Score and why they stand out
- Any high quality score tickers that are deeply beaten down (dist from high -30% or more)
- What sectors dominate today and whether macro supports them
- Any tickers to explicitly avoid — low quality score, micro cap, or too extended

Be direct and specific. Use ticker names. Quality Score above 60 = liquid leader worth sizing. Below 35 = noise. No disclaimers. No markdown headers. Plain text only."""

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if not resp.ok:
            log.error(f"AI summary HTTP {resp.status_code}: {resp.text}")
            return ""
        summary = resp.json()["content"][0]["text"].strip()
        log.info("AI summary generated.")
        return summary
    except Exception as e:
        log.error(f"AI summary failed: {e}")
        return ""


# ----------------------------
# Part 5: Slack Notification
# ----------------------------

def send_slack_notification(summary_df: pd.DataFrame, filter_df: pd.DataFrame,
                             gallery_html: str, today: str, ai_summary: str,
                             sndk_candidates: list | None = None):
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack notification.")
        return

    top = filter_df.sort_values('Quality Score', ascending=False).head(10) if 'Quality Score' in filter_df.columns else filter_df.head(10)
    ticker_lines = []
    for _, row in top.iterrows():
        atr    = f"{row['ATR%']:.1f}%"        if pd.notna(row.get('ATR%'))         else "—"
        qs     = f"{row['Quality Score']:.0f}" if pd.notna(row.get('Quality Score')) else "—"
        mktcap = row.get('Market Cap', '')
        sector = row.get('Sector', '')
        sector_str = f" · _{sector}_" if sector else ""

        # Show EPS Q/Q when it's the meaningful signal (e.g. spin-offs, IPOs with distorted TTM)
        eps_yy_v = float(row['EPS Y/Y TTM']) if pd.notna(row.get('EPS Y/Y TTM')) else None
        eps_qq_v = float(row['EPS Q/Q'])     if pd.notna(row.get('EPS Q/Q'))     else None
        if eps_qq_v is not None and (eps_yy_v is None or eps_qq_v > eps_yy_v):
            eps = f"{eps_qq_v:.1f}% Q/Q*"   # asterisk = Q/Q override
        elif eps_yy_v is not None:
            eps = f"{eps_yy_v:.1f}%"
        else:
            eps = "—"

        inst_trans_v = float(row['Inst Trans']) if pd.notna(row.get('Inst Trans')) else None
        inst_str = f" · Inst {inst_trans_v:+.1f}%" if inst_trans_v is not None and inst_trans_v != 0 else ""

        ticker_lines.append(
            f"*{row['Ticker']}*{sector_str} · Q{qs} · {mktcap} · ATR {atr} · EPS {eps}{inst_str}\n"
            f" {row['Screeners']}"
        )

    gallery_link = ""
    if GITHUB_PAGES_BASE:
        fname = os.path.basename(gallery_html)
        gallery_link = f"\n\n:bar_chart: <{GITHUB_PAGES_BASE}/data/{fname}|Open chart gallery>"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📈 Finviz Daily Screener — {today}"}},
    ]
    if ai_summary:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":brain: *Today's take:*\n{ai_summary}"}})
        blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*{len(summary_df)}* tickers scanned · "
                f"*{len(filter_df)}* passed ATR% > {ATR_THRESHOLD}\n\n"
                f"*Top picks:*\n" + "\n".join(ticker_lines) +
                gallery_link
            )
        }
    })
    # Sector rotation summary
    sector_data = compute_sector_rotation(filter_df)
    if sector_data:
        top3 = sector_data[:3]
        sector_parts = []
        for s in top3:
            badges = []
            if s['stage2']:  badges.append(f"{s['stage2']} S2")
            if s['vcp']:     badges.append(f"{s['vcp']} VCP")
            badge_str = " · " + " · ".join(badges) if badges else ""
            sector_parts.append(f"*{s['sector']}* ({s['count']}, Q{s['avg_q']:.0f}{badge_str})")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":bar_chart: *Sector rotation:* " + "  ·  ".join(sector_parts)}
        })

    # Power move tickers — post-filter to actual 9M+ share volume
    # (Finviz sh_vol_o* URL param is silently ignored, so we enforce it ourselves)
    def _parse_vol(raw) -> float:
        import re as _re
        if not raw or raw in ('-', ''):
            return 0.0
        raw = str(raw).replace(',', '').strip()
        m = _re.match(r'^([\d.]+)([KMBkmb]?)', raw)
        if not m:
            return 0.0
        val = float(m.group(1))
        s = m.group(2).upper()
        if s == 'K': val *= 1_000
        elif s == 'M': val *= 1_000_000
        elif s == 'B': val *= 1_000_000_000
        return val

    power_moves = filter_df[filter_df['Screeners'].str.contains('Power Move', na=False)] if 'Screeners' in filter_df.columns else pd.DataFrame()
    if not power_moves.empty and 'Volume' in power_moves.columns:
        before = len(power_moves)
        power_moves = power_moves[power_moves['Volume'].apply(_parse_vol) >= 9_000_000]
        log.info(f"Power Move post-filter: {before} → {len(power_moves)} tickers after 9M volume gate")
    if not power_moves.empty:
        pm_tickers = ", ".join(f"*{t}*" for t in power_moves['Ticker'].tolist())
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":fire: *Power Moves (9M+ vol, 10%+):* {pm_tickers}"}
        })

    # SNDK-pattern flag — manual research prompt, no auto API call
    # sndk_candidates is list of (ticker, eps_yy, eps_qq) tuples
    if sndk_candidates:
        ticker_parts = []
        for t, yy, qq in sndk_candidates:
            distorted = yy < -50 and qq > 0
            eps_tag = f"TTM {yy:+.0f}% / Q/Q {qq:+.0f}%"
            flag = " ⚠ distorted" if distorted else ""
            ticker_parts.append(f"`{t}` ({eps_tag}{flag})")
        tickers_str  = "  ·  ".join(ticker_parts)
        research_cmd = "  ".join(f"`/stock-research {t}`" for t, _, _ in sndk_candidates)
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":microscope: *SNDK pattern (4+/6 criteria):* {tickers_str}\n"
                    f"Check technicals, then run: {research_cmd}"
                )
            }
        })

    blocks.append({"type": "divider"})

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("Slack notification sent.")
    except Exception as e:
        log.error(f"Failed to send Slack notification: {e}")


# ----------------------------
# Part 7: Watchlist Auto-Population
# ----------------------------

def _update_watchlist(filter_df: pd.DataFrame, today: str):
    """
    Promotes top Stage 2 + Q≥60 tickers from today's screener into
    data/watchlist.json (status=watching).

    Rules:
    - Only Stage 2 tickers with Quality Score ≥ 60
    - Max 5 new additions per day (top by Q score)
    - Never overwrites an existing entry (watching, entered, or any status)
    - Sets entry_note based on VCP confirmation and Q score tier
    - Auto-archives screener_auto entries older than 14 days (manual entries never expire)
    """
    import json
    from datetime import date, timedelta
    watchlist_path = os.path.join("data", "watchlist.json")
    try:
        with open(watchlist_path) as f:
            wl_data = json.load(f)
        existing = wl_data.get("watchlist", [])
    except Exception:
        existing = []
        wl_data = {"watchlist": existing}

    # Auto-archive screener_auto entries older than 14 days
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    archived_count = 0
    for entry in existing:
        if (entry.get("source") == "screener_auto"
                and entry.get("status") == "watching"
                and entry.get("added", "9999") < cutoff):
            entry["status"] = "archived"
            entry["archive_reason"] = "age_out"
            entry["archived_date"] = today
            archived_count += 1
    if archived_count:
        log.info("Watchlist: auto-archived %d stale screener_auto entries (>14 days).", archived_count)

    existing_tickers = {e["ticker"] for e in existing if e.get("status") != "archived"}

    # Filter: Stage 2 + Q≥60 + not already in watchlist
    candidates = filter_df.copy()
    if 'Quality Score' in candidates.columns:
        candidates = candidates[candidates['Quality Score'] >= 60]

    if candidates.empty:
        log.info("Watchlist: no new Stage 2 + Q>=60 tickers to add today.")
        return

    stage2_mask = candidates['Stage'].apply(
        lambda s: s.get('stage', 0) == 2 if isinstance(s, dict) else False
    ) if 'Stage' in candidates.columns else pd.Series([False] * len(candidates))
    candidates = candidates[stage2_mask]

    candidates = candidates[~candidates['Ticker'].isin(existing_tickers)]
    candidates = candidates.sort_values('Quality Score', ascending=False).head(5)

    if candidates.empty:
        log.info("Watchlist: no new Stage 2 + Q≥60 tickers to add today.")
        return

    added = []
    for _, row in candidates.iterrows():
        ticker  = row['Ticker']
        qs      = float(row.get('Quality Score', 0) or 0)
        vcp     = row.get('VCP', {})
        atr_pct = float(row.get('ATR%', 0) or 0)
        sector  = row.get('Sector', '')
        stage_d = row.get('Stage', {})
        perfect = stage_d.get('perfect', False) if isinstance(stage_d, dict) else False

        vcp_ok = isinstance(vcp, dict) and vcp.get('vcp_possible', False)

        if vcp_ok:
            entry_note = "VCP setup — wait for volume contraction then breakout"
        elif perfect:
            entry_note = "Perfect Stage 2 alignment — 21 EMA pullback entry"
        else:
            entry_note = "Stage 2 confirmed — pullback to 10/21 EMA"

        entry = {
            "ticker":      ticker,
            "entry_note":  entry_note,
            "entry_price": None,
            "stop":        None,
            "thesis":      (
                sector
                + " | Q=" + str(int(qs))
                + (" | VCP" if vcp_ok else "")
                + (" | ATR " + str(round(atr_pct, 1)) + "%" if atr_pct else "")
            ),
            "added":       today,
            "status":      "watching",
            "priority":    "watching",
            "source":      "screener_auto",
        }
        existing.append(entry)
        added.append(ticker)
        log.info("Watchlist: added %s (Q=%.0f%s)", ticker, qs, " VCP" if vcp_ok else "")

    # ── Auto-promote watching → focus ──────────────────────────────────────────
    # Criteria: still in today's screener with Stage 2 perfect + 3+ appearances
    # + institutional buying. Only promotes screener_auto entries — manual
    # entries should be promoted intentionally via workflow_dispatch.
    promoted = []
    screener_tickers = set(filter_df['Ticker'].tolist()) if not filter_df.empty else set()
    for entry in existing:
        if entry.get("priority") != "watching" or entry.get("status") == "archived":
            continue
        if entry.get("source") == "manual":
            continue  # manual entries promoted intentionally only
        t = entry.get("ticker", "")
        if t not in screener_tickers:
            continue
        rows = filter_df[filter_df['Ticker'] == t]
        if rows.empty:
            continue
        row = rows.iloc[0]
        appearances  = int(row.get('Appearances', 0) or 0)
        inst_trans   = float(row.get('Inst Trans', 0) or 0)
        stage_d      = row.get('Stage', {}) or {}
        stage_num    = stage_d.get('stage', 0) if isinstance(stage_d, dict) else 0
        stage_perfect = stage_d.get('perfect', False) if isinstance(stage_d, dict) else False
        if appearances >= 3 and stage_num == 2 and stage_perfect and inst_trans >= 3:
            entry["priority"] = "focus"
            entry["focus_promoted_date"] = today
            promoted.append(t)
            log.info("Watchlist: auto-promoted %s to focus (appearances=%d, inst=%.1f%%)", t, appearances, inst_trans)

    if promoted:
        log.info("Watchlist: promoted to focus: %s", promoted)

    wl_data["watchlist"] = existing
    with open(watchlist_path, "w") as f:
        json.dump(wl_data, f, indent=2)
    log.info("Watchlist updated — added %d ticker(s): %s", len(added), added)
    return promoted


# ----------------------------
# Part 6: Main Execution
# ----------------------------

if __name__ == "__main__":
    today = datetime.date.today().strftime("%Y-%m-%d")
    log.info(f"=== Finviz agent starting — {today} ===")

    # Step 1: Screener fetch
    summary_df, csv_path, html_summary = aggregate_and_save(screener_urls)
    log.info(f"Total unique tickers: {len(summary_df)}")
    if summary_df.empty:
        log.error("No tickers — aborting.")
        exit(1)

    # Step 2: Concurrent snapshot metrics
    log.info(f"Fetching snapshots with {SNAPSHOT_WORKERS} workers...")
    snapshot_results = fetch_snapshots_concurrent(summary_df['Ticker'].tolist())
    summary_df['ATR%']           = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[0])
    summary_df['EPS Y/Y TTM']    = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[1])
    summary_df['Sales Y/Y TTM']  = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[2])
    summary_df['Dist From High%']= summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[3])
    summary_df['Rel Volume']     = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[4])
    summary_df['Avg Volume']     = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[5])
    summary_df['SMA20%']         = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[6])
    summary_df['SMA50%']         = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[7])
    summary_df['SMA200%']        = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[8])
    summary_df['EPS Q/Q']        = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[9])
    summary_df['Inst Own']       = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[10])
    summary_df['Inst Trans']     = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*12)[11])

    # Step 3: Stage, VCP, Quality Score
    log.info("Computing Weinstein Stage analysis...")
    summary_df['Stage'] = summary_df.apply(compute_stage, axis=1)
    stage_counts = summary_df['Stage'].apply(lambda x: x.get('stage', 0) if isinstance(x, dict) else 0).value_counts()
    for s, count in sorted(stage_counts.items()):
        labels = {1:"Basing", 2:"Uptrend", 3:"Distribution", 4:"Downtrend", 0:"Transitional"}
        log.info(f"  Stage {s} ({labels.get(s,'?')}): {count} tickers")

    log.info("Computing VCP signals...")
    summary_df['VCP'] = summary_df.apply(compute_vcp, axis=1)
    vcp_count = summary_df['VCP'].apply(lambda x: x.get('vcp_possible', False) if isinstance(x, dict) else False).sum()
    log.info(f"  VCP possible: {vcp_count} tickers")

    # ── 10% Change momentum gate ──
    # Tickers surfaced only/partly by '10% Change' must pass quality checks.
    # Failures get excluded from scoring, gallery main sections, and top 5.
    # EXCEPTION: IPO lifecycle stocks are never excluded — they appear in
    # the IPO Lifecycle section regardless of stage or quality score.
    # A stock is IPO lifecycle if: (a) 'IPO' is in its Screeners column, OR
    # (b) it shows an IPO washout-recovery pattern: deeply below 52w high
    # but strongly above its 20-day MA (IPO pop-and-drop + recovery).
    log.info("Applying 10%% Change momentum gate...")
    excluded_flags = []
    excluded_reasons_list = []
    for _, row in summary_df.iterrows():
        screeners = str(row.get('Screeners', '') or '')
        is_ipo_lifecycle = (
            'IPO' in screeners or (
                float(row.get('Dist From High%', 0) or 0) <= -40 and
                float(row.get('SMA20%', 0) or 0) >= 10
            )
        )
        if '10% Change' in screeners and not is_ipo_lifecycle:
            passes, reasons = check_10pct_momentum_quality(row)
            excluded_flags.append(not passes)
            excluded_reasons_list.append(", ".join(reasons) if reasons else "")
        else:
            excluded_flags.append(False)
            excluded_reasons_list.append("")
    summary_df['_10pct_excluded'] = excluded_flags
    summary_df['_10pct_exclude_reasons'] = excluded_reasons_list
    n_excluded = sum(excluded_flags)
    if n_excluded:
        excluded_tickers = summary_df.loc[summary_df['_10pct_excluded'], 'Ticker'].tolist()
        log.info(f"  10%% Change gate excluded {n_excluded} tickers: {excluded_tickers}")
    else:
        log.info("  10%% Change gate: all passed.")

    summary_df['Quality Score'] = summary_df.apply(compute_quality_score, axis=1)

    filter_df = summary_df[(summary_df['ATR%'] > ATR_THRESHOLD) & (~summary_df['_10pct_excluded'])].copy()
    filter_df = filter_df.sort_values('Quality Score', ascending=False)
    excluded_df = summary_df[(summary_df['ATR%'] > ATR_THRESHOLD) & (summary_df['_10pct_excluded'])].copy()
    log.info(f"Tickers with ATR% > {ATR_THRESHOLD}: {len(filter_df)} active, {len(excluded_df)} excluded by 10%% gate")

    # ── FIX: re-save enriched CSV so earnings alert can read ATR%, Quality Score ──
    summary_df.to_csv(csv_path, index=False)
    log.info(f"Enriched CSV re-saved: {csv_path}")

    # ── Proactive research: SNDK-pattern detection ──
    # Only fires when a ticker hits 4+ of 6 specific criteria — these are rare.
    # Max 3 tickers per day (highest signal score only). Not a quality score gate.
    # Excluded: non-growth sectors (utilities, energy, real estate, basic materials,
    # consumer defensive) and specific slow-growth industries (construction, oil & gas, mining).
    _SNDK_EXCLUDED_SECTORS = {
        'Utilities', 'Energy', 'Real Estate', 'Basic Materials', 'Consumer Defensive',
    }
    _SNDK_EXCLUDED_INDUSTRIES = {
        'Engineering & Construction', 'Infrastructure Operations',
        'Oil & Gas E&P', 'Oil & Gas Integrated', 'Oil & Gas Midstream',
        'Oil & Gas Refining & Marketing', 'Oil & Gas Equipment & Services',
        'Specialty Chemicals', 'Agricultural Inputs', 'Steel', 'Aluminum',
        'Copper', 'Gold', 'Silver', 'Coal', 'Lumber & Wood Production',
        'Farm & Construction Equipment', 'Waste Management',
    }
    try:
        sndk_scored = []
        for _, row in filter_df.iterrows():
            sector    = str(row.get('Sector', '') or '').strip()
            industry  = str(row.get('Industry', '') or '').strip()

            # Skip non-growth sectors/industries — these are slow contracted businesses,
            # not earnings-driven momentum candidates.
            if sector in _SNDK_EXCLUDED_SECTORS or industry in _SNDK_EXCLUDED_INDUSTRIES:
                log.debug(f"SNDK skip {row['Ticker']}: non-growth sector ({sector} / {industry})")
                continue

            screeners_str = str(row.get('Screeners', '') or '')
            eps_yy   = float(row.get('EPS Y/Y TTM', 0) or 0)
            eps_qq   = float(row.get('EPS Q/Q', 0) or 0)
            inst_trans = float(row.get('Inst Trans', 0) or 0)
            appearances = int(row.get('Appearances', 0) or 0)
            stage_data = row.get('Stage', {}) or {}
            stage_num = stage_data.get('stage', 0) if isinstance(stage_data, dict) else 0
            stage_perfect = stage_data.get('perfect', False) if isinstance(stage_data, dict) else False

            # 6 criteria — need 4+ to trigger research
            criteria = {
                'persistence':    appearances >= 3,
                'eps_qq_strong':  eps_qq > 50 or (eps_yy < 0 and eps_qq > 20),  # Q/Q strong, TTM may be distorted
                'ttm_distorted':  eps_yy < -50 and eps_qq > 0,                   # classic spin-off/IPO EPS blind spot
                'inst_buying':    inst_trans >= 3,                                # funds actively adding
                'stage2':         stage_num == 2 and stage_perfect,
                'ipo_lifecycle':  'IPO' in screeners_str,
            }
            signal_score = sum(criteria.values())
            if signal_score >= 4:
                sndk_scored.append((signal_score, row['Ticker'], eps_yy, eps_qq))
                log.info(
                    f"SNDK candidate: {row['Ticker']} signal={signal_score}/6 "
                    f"[{', '.join(k for k, v in criteria.items() if v)}] "
                    f"EPS TTM={eps_yy:.0f}% Q/Q={eps_qq:.0f}% InstTrans={inst_trans:.1f}% {appearances}d"
                )

        # Take top 3 by signal score — these are genuinely rare setups
        sndk_scored.sort(reverse=True)
        sndk_candidates = [(t, yy, qq) for _, t, yy, qq in sndk_scored[:3]]

        if sndk_candidates:
            log.info(f"SNDK candidates flagged for manual research: {[t for t,_,_ in sndk_candidates]}")
        else:
            log.info("No SNDK-pattern candidates today (need 4/6 criteria).")
    except Exception as e:
        log.error(f"SNDK detection failed (non-fatal): {e}")

    # ── Write daily quality JSON for weekly agent signal merge ──
    import json
    quality_data = {}
    for _, row in summary_df.iterrows():
        t = row['Ticker']
        stage_data = row.get('Stage', {}) or {}
        stage_num = stage_data.get('stage', 0) if isinstance(stage_data, dict) else 0
        stage_labels = {1: "Basing", 2: "Uptrend", 3: "Distribution", 4: "Downtrend", 0: "Transitional"}
        is_excluded = bool(row.get('_10pct_excluded', False))
        section = "excluded" if is_excluded else _classify_ticker(row)
        entry = {
            "q_rank": round(float(row.get('Quality Score', 0) or 0)),
            "stage": stage_num,
            "stage_label": stage_labels.get(stage_num, "Transitional"),
            "section": section,
        }
        if is_excluded:
            entry["excluded_reason"] = row.get('_10pct_exclude_reasons', '')
        quality_data[t] = entry
    quality_path = f"data/daily_quality_{today}.json"
    with open(quality_path, "w") as f:
        json.dump(quality_data, f, indent=2)
    log.info(f"Daily quality JSON written: {quality_path} ({len(quality_data)} tickers)")

    if not filter_df.empty:
        top3 = filter_df.head(3)[['Ticker','Quality Score','Market Cap','Appearances']].to_string(index=False)
        log.info(f"Top 3 by quality score:\n{top3}")

    # Step 4: Gallery
    gallery_path = generate_finviz_gallery(filter_df['Ticker'].tolist(), filter_df, excluded_df)
    log.info(f"Chart gallery: {gallery_path}")

    # Step 5: AI summary
    ai_summary = generate_ai_summary(filter_df, today)

    # Step 6: Slack
    send_slack_notification(summary_df, filter_df, gallery_path, today, ai_summary,
                            sndk_candidates=sndk_candidates if 'sndk_candidates' in dir() else None)

    # Step 7: Auto-populate watchlist + auto-promote watching → focus
    promoted_to_focus = _update_watchlist(filter_df, today)
    if promoted_to_focus:
        log.info("Auto-promoted to Focus List: %s", promoted_to_focus)

    # ── EventBridge: PersistencePick (SetupOfDay moved to premarket_alert.py at 9am ET) ──
    try:
        from agents.publishing.event_publisher import publish_persistence_pick
        import glob as _glob
        import json as _json

        # Load market context — screener doesn't call market_monitor itself
        _ts = {}
        if os.path.exists("data/trading_state.json"):
            with open("data/trading_state.json") as _f:
                _ts = _json.load(_f)
        _market_state = _ts.get("market_state", "RED")
        _fear_greed   = int(_ts.get("fng") or 0)

        if not filter_df.empty:
            # Persistence pick — most recent weekly persistence CSV
            _stage_map = {
                "Uptrend": "stage2", "Distribution": "stage3",
                "Downtrend": "stage4", "Basing": "stage1",
            }
            _csv_files = sorted(_glob.glob("data/finviz_weekly_persistence_*.csv"))
            if _csv_files:
                import pandas as _pd
                _persist_df = _pd.read_csv(_csv_files[-1])
                _candidates = _persist_df[_persist_df["Days Seen"] >= 3].copy()
                if not _candidates.empty:
                    _best_p = _candidates.nlargest(1, "Q Rank").iloc[0]
                    publish_persistence_pick(
                        date=today,
                        ticker=str(_best_p["Ticker"]),
                        persistence_days=int(_best_p["Days Seen"]),
                        quality_score=int(_best_p["Q Rank"]),
                        section=_stage_map.get(str(_best_p.get("Stage", "")), "transitional"),
                        market_state=_market_state,
                        fear_greed=_fear_greed,
                        spy_above_200ma=bool(_ts.get("spy_above_200d", False)),
                    )
                else:
                    log.info("PersistencePick: no ticker with 3+ days this week — skipping")
    except Exception as e:
        log.warning(f"Publisher events skipped (non-fatal): {e}")

    log.info("=== Done ===")
