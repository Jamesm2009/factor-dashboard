"""
Factor & Sector Analysis Dashboard — yFinance + Upstash Redis cache
Absolute Score: where price sits in its 52-week range, mapped to [-1, +1]
Relative Score: same calculation on the ETF/SPY price ratio
Tails: weekly snapshots showing score trajectory over 4–12 weeks

NEW:
- YTD Rebased chart data (per ticker, indexed to 0% at first trading day of year)
- Seasonality chart data (25-year average return path by trading-day-of-year,
  cached separately in Redis with a long TTL since it's a heavy download)
"""

from flask import Flask, render_template, jsonify
import yfinance as yf
import pandas as pd
import numpy as np
import json, os, threading, time, requests
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
CT = ZoneInfo("America/Chicago")

REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_KEY        = "factor_sector_dash_v1"
REDIS_KEY_SEASON = "factor_seasonality_v1"
SEASON_TTL       = 7 * 24 * 3600   # 7 days — seasonality rarely needs to change
SEASONALITY_YEARS = 25

# ── Style chart group definitions ─────────────────────────────────────────────

SECTOR_STYLE_GROUPS = [
    {"title": "Defensives", "tickers": ["XLV", "XLP", "XLU"]},
    {"title": "Sensitives", "tickers": ["XLK", "XLI", "XLC", "XLE"]},
    {"title": "Cyclicals",  "tickers": ["XLRE", "XLF", "XLY", "XLB"]},
]

FACTOR_STYLE_GROUPS = [
    {"title": "Beta",   "tickers": ["SPHB", "SPY", "SPLV"]},
    {"title": "Growth", "tickers": ["MGK", "IVW", "IWF", "QQQ"]},
    {"title": "Safety", "tickers": ["MTUM", "QUAL", "VYM", "SPHD"]},
    {"title": "Value",  "tickers": ["IWM", "VBR", "VTV", "IWD"]},
]

# Extra tickers only needed for style charts (not in sectors/factors lists)
STYLE_EXTRA_TICKERS = {"IWF", "QQQ", "QUAL", "SPHD", "IWD"}

cache = {
    "sectors": [],
    "factors": [],
    "style_charts": {},
    "ytd_rebased": {},
    "seasonality": {},
    "last_updated": "Loading...",
    "season_last_updated": "Never",
    "phase": 0,
    "progress": "Starting...",
    "error": None,
}
_lock    = threading.Lock()
_started = False


def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


# ── Redis helpers (pipeline endpoint) ─────────────────────────────────────────

def redis_set(key, value, ex_seconds=90000):
    """Store JSON value via Upstash pipeline. ex_seconds=90000 ≈ 25 hours."""
    if not REDIS_URL or not REDIS_TOKEN:
        return False
    try:
        payload = json.dumps(value)
        r = requests.post(
            f"{REDIS_URL}/pipeline",
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"},
            data=json.dumps([["SET", key, payload, "EX", str(ex_seconds)]]),
            timeout=15,
        )
        ok = r.status_code == 200
        if not ok:
            print(f"  Redis SET failed: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"  Redis SET error: {e}")
        return False


def redis_get(key):
    """Retrieve and parse JSON value from Redis."""
    if not REDIS_URL or not REDIS_TOKEN:
        return None
    try:
        r = requests.post(
            f"{REDIS_URL}/pipeline",
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"},
            data=json.dumps([["GET", key]]),
            timeout=15,
        )
        if r.status_code != 200:
            return None
        results = r.json()
        if not results or not results[0]:
            return None
        result = results[0].get("result")
        if result is None:
            return None
        return json.loads(result)
    except Exception as e:
        print(f"  Redis GET error: {e}")
        return None


def redis_del(key):
    if not REDIS_URL or not REDIS_TOKEN:
        return
    try:
        requests.post(
            f"{REDIS_URL}/pipeline",
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"},
            data=json.dumps([["DEL", key]]),
            timeout=10,
        )
    except Exception:
        pass


# ── Score computation ─────────────────────────────────────────────────────────

def compute_scores(ticker_closes, spy_closes, lookback_weeks=52, tail_weeks=12):
    """
    Compute absolute and relative scores with weekly trail snapshots.

    Absolute score = 2 × (price − 52wk low) / (52wk high − 52wk low) − 1
    Relative score = same formula applied to ETF/SPY price ratio

    Returns dict with current scores and trail data, or None.
    """
    common = ticker_closes.dropna().index.intersection(spy_closes.dropna().index)
    if len(common) < 50:
        return None

    tc = ticker_closes.loc[common].sort_index()
    sc = spy_closes.loc[common].sort_index()
    ratio = tc / sc

    # Weekly snapshot dates (current → oldest)
    dates  = tc.index
    latest = dates[-1]
    snapshots = [latest]
    for w in range(1, tail_weeks + 1):
        target     = latest - pd.Timedelta(days=w * 7)
        candidates = dates[dates <= target]
        if len(candidates) == 0:
            break
        snapshots.append(candidates[-1])

    lookback_td = pd.Timedelta(weeks=lookback_weeks)
    trail = []

    for snap_date in snapshots:
        window_start = snap_date - lookback_td

        pw = tc[(tc.index >= window_start) & (tc.index <= snap_date)]
        rw = ratio[(ratio.index >= window_start) & (ratio.index <= snap_date)]

        if len(pw) < 20 or len(rw) < 20:
            continue

        p    = tc.loc[snap_date]
        p_lo = pw.min()
        p_hi = pw.max()
        abs_score = (2 * (p - p_lo) / (p_hi - p_lo) - 1) if p_hi > p_lo else 0.0

        r    = ratio.loc[snap_date]
        r_lo = rw.min()
        r_hi = rw.max()
        rel_score = (2 * (r - r_lo) / (r_hi - r_lo) - 1) if r_hi > r_lo else 0.0

        trail.append({
            "date": snap_date.strftime("%Y-%m-%d"),
            "abs":  round(float(abs_score), 2),
            "rel":  round(float(rel_score), 2),
        })

    if not trail:
        return None

    return {
        "abs_score": trail[0]["abs"],
        "rel_score": trail[0]["rel"],
        "trail":     trail,
    }


def compute_style_charts(closes_all, days=63):
    """
    Compute trailing cumulative returns for style chart groups.
    Returns dict with sector_styles and factor_styles panel data.
    """
    def panel_series(groups):
        panels = []
        for group in groups:
            title   = group["title"]
            tickers = group["tickers"]
            series  = []
            common_dates = None

            for ticker in tickers:
                if ticker not in closes_all.columns:
                    continue
                closes = closes_all[ticker].dropna()
                tail   = closes.tail(days)
                if len(tail) < 10:
                    continue
                base    = tail.iloc[0]
                returns = ((tail / base) - 1) * 100
                dates   = [d.strftime("%b %d") for d in returns.index]
                values  = [round(float(v), 2) for v in returns.values]
                series.append({"symbol": ticker, "values": values})
                if common_dates is None:
                    common_dates = dates

            if series:
                panels.append({
                    "title":  title,
                    "dates":  common_dates,
                    "series": series,
                })
        return panels

    sector_panels = panel_series(SECTOR_STYLE_GROUPS)
    factor_panels = panel_series(FACTOR_STYLE_GROUPS)

    # Build sector overview panel (group averages)
    overview = None
    if sector_panels:
        ref_dates = sector_panels[0]["dates"]
        overview_series = []
        for panel in sector_panels:
            vals = [s["values"] for s in panel["series"]]
            if not vals:
                continue
            min_len = min(len(v) for v in vals)
            avg = [round(sum(v[i] for v in vals) / len(vals), 2)
                   for i in range(min_len)]
            overview_series.append({"symbol": panel["title"], "values": avg})
        if overview_series:
            overview = {
                "title":  "Overview",
                "dates":  ref_dates[:min(len(s["values"]) for s in overview_series)],
                "series": overview_series,
            }

    return {
        "sector_overview": overview,
        "sector_panels":   sector_panels,
        "factor_panels":   factor_panels,
    }


# ── YTD rebased chart ──────────────────────────────────────────────────────────

def compute_ytd_rebased(closes_all, ticker_list):
    """
    For each ticker, rebase this calendar year's closes to 0% at the first
    trading day of the year. Returns {ticker: [pct_return, pct_return, ...]}
    indexed by trading-day-of-year (0-based).
    """
    this_year = date.today().year
    out = {}
    for ticker in ticker_list:
        if ticker not in closes_all.columns:
            continue
        c = closes_all[ticker].dropna()
        yr = c[c.index.year == this_year].sort_index()
        if len(yr) < 2:
            continue
        base = yr.iloc[0]
        vals = ((yr / base) - 1) * 100
        out[ticker] = [round(float(v), 2) for v in vals.values]
    return out


# ── Seasonality (25-year average by trading-day-of-year) ─────────────────────

def compute_seasonality(ticker_list, years=SEASONALITY_YEARS):
    """
    Downloads `years` of daily history for each ticker, computes each
    calendar year's return path rebased to 0% at day 1, then averages
    across all *complete* prior years (current partial year excluded).
    Also computes average trading-day index of each month-end (from SPY)
    so the frontend can draw aligned vertical month markers.
    """
    end_date   = date.today() + timedelta(days=1)
    start_date = end_date - timedelta(days=365 * years + 30)

    print(f"  Seasonality: downloading {years}y history for {len(ticker_list)} tickers...")
    df = yf.download(
        ticker_list,
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        progress=False,
        threads=True,
    )
    if df.empty:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        level0 = df.columns.get_level_values(0)
        closes_all = df["Close"] if "Close" in level0 else df["Adj Close"]
    else:
        closes_all = df

    this_year   = date.today().year
    series_out  = {}
    spy_years   = {}   # year -> sorted Series, used for month-end index calc

    for ticker in ticker_list:
        if ticker not in closes_all.columns:
            continue
        c = closes_all[ticker].dropna()
        if c.empty:
            continue

        by_year = {}
        for yr, grp in c.groupby(c.index.year):
            grp = grp.sort_index()
            if len(grp) < 30:
                continue
            # Skip partial years — e.g. the first calendar year of our 25y
            # download window (which starts mid-year, not Jan 1), or a
            # ticker's actual inception year. Only count years that genuinely
            # begin near January 1st.
            if grp.index.min().dayofyear > 5:
                continue
            base = grp.iloc[0]
            rets = ((grp / base) - 1) * 100
            by_year[yr] = rets.values.tolist()
            if ticker == "SPY":
                spy_years[yr] = grp

        complete = {y: v for y, v in by_year.items() if y != this_year}
        if not complete:
            continue

        # Trim to the shortest year's length so every averaged point uses the
        # FULL set of years — avoids a noisy, thin-sample tail near year-end
        # (previously the last few days were averaged over only 3-5 years,
        # letting a single bad year drag the whole line down sharply).
        min_days = min(len(v) for v in complete.values())
        avg = []
        for day_i in range(min_days):
            vals = [v[day_i] for v in complete.values()]
            avg.append(round(sum(vals) / len(vals), 2))
        if avg:
            series_out[ticker] = avg

    # Average month-end trading-day index across complete years, from SPY
    month_end_idx = []
    complete_spy_years = {y: g for y, g in spy_years.items() if y != this_year}
    if complete_spy_years:
        per_year_idx = []
        for yr, grp in complete_spy_years.items():
            months = grp.index.month
            idxs = []
            for m in range(1, 13):
                positions = [i for i, mm in enumerate(months) if mm == m]
                if positions:
                    idxs.append(positions[-1])
            if len(idxs) == 12:
                per_year_idx.append(idxs)
        if per_year_idx:
            for mi in range(12):
                vals = [p[mi] for p in per_year_idx]
                month_end_idx.append(round(sum(vals) / len(vals)))

    if not series_out:
        return None

    return {"series": series_out, "month_end_idx": month_end_idx}


def ensure_seasonality(force=False):
    """Load seasonality from Redis if cached; otherwise compute it in the
    background (non-blocking) since it's a heavy 25-year download."""
    if not force:
        cached = redis_get(REDIS_KEY_SEASON)
        if cached:
            with _lock:
                cache["seasonality"]         = cached
                cache["season_last_updated"] = cached.get("_updated", "—")
            print("  Seasonality: loaded from Redis cache.")
            return

    def _bg():
        try:
            funds = load_funds()
            tickers = sorted(
                {f["symbol"] for f in funds["sectors"]}
                | {f["symbol"] for f in funds["factors"]}
                | {"SPY"}
            )
            result = compute_seasonality(tickers)
            if result:
                result["_updated"] = datetime.now(CT).strftime("%-m/%-d/%y %H:%M CT")
                with _lock:
                    cache["seasonality"]         = result
                    cache["season_last_updated"] = result["_updated"]
                redis_set(REDIS_KEY_SEASON, result, ex_seconds=SEASON_TTL)
                print("  Seasonality: computed and cached.")
            else:
                print("  Seasonality: computation returned no data.")
        except Exception as e:
            import traceback; traceback.print_exc()

    threading.Thread(target=_bg, daemon=True).start()


# ── Main update ───────────────────────────────────────────────────────────────

def run_update():
    with _lock:
        cache["phase"]    = 1
        cache["error"]    = None
        cache["progress"] = "Downloading price data..."

    try:
        funds = load_funds()

        # Collect all unique tickers + SPY + style chart extras
        all_tickers = set()
        for group in ("sectors", "factors"):
            for f in funds[group]:
                all_tickers.add(f["symbol"])
        all_tickers.add("SPY")
        all_tickers.update(STYLE_EXTRA_TICKERS)
        ticker_list = sorted(all_tickers)

        with _lock:
            cache["progress"] = f"Downloading {len(ticker_list)} tickers..."
        print(f"  Downloading {len(ticker_list)} tickers from Yahoo Finance...")

        # Need 52-week lookback + 12-week tail ≈ 16 months; pad to 20 months
        end_date   = date.today() + timedelta(days=1)
        start_date = end_date - timedelta(days=610)

        df = yf.download(
            ticker_list,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            progress=False,
            threads=True,
        )

        if df.empty:
            with _lock:
                cache["error"] = "No data returned from Yahoo Finance."
                cache["phase"] = 4
            return

        # Extract close prices — handle MultiIndex from yfinance
        if isinstance(df.columns, pd.MultiIndex):
            level0 = df.columns.get_level_values(0)
            if "Close" in level0:
                closes_all = df["Close"]
            elif "Adj Close" in level0:
                closes_all = df["Adj Close"]
            else:
                closes_all = df.iloc[:, :len(ticker_list)]
        else:
            closes_all = df

        if "SPY" not in closes_all.columns:
            with _lock:
                cache["error"] = "SPY data not available."
                cache["phase"] = 4
            return

        spy_closes = closes_all["SPY"].dropna()

        # Process each group
        for group in ("sectors", "factors"):
            results   = []
            gf        = funds[group]
            total     = len(gf)

            for i, fund in enumerate(gf):
                ticker = fund["symbol"]
                name   = fund["name"]

                with _lock:
                    cache["progress"] = f"Computing {group}: {ticker} ({i+1}/{total})"
                print(f"  [{group}] {ticker} ({i+1}/{total})")

                if ticker not in closes_all.columns:
                    print(f"    skip — no data")
                    continue

                tc = closes_all[ticker].dropna()
                scores = compute_scores(tc, spy_closes)

                if scores is None:
                    print(f"    skip — insufficient data")
                    continue

                results.append({
                    "symbol":    ticker,
                    "name":      name,
                    "abs_score": scores["abs_score"],
                    "rel_score": scores["rel_score"],
                    "trail":     scores["trail"],
                })
                print(f"    OK  abs={scores['abs_score']:+.2f}  rel={scores['rel_score']:+.2f}")

            # Sort by relative score ascending (most oversold at top)
            results.sort(key=lambda x: x["rel_score"])

            with _lock:
                cache[group] = results

        # Compute style charts
        with _lock:
            cache["progress"] = "Computing style charts..."
        print("  Computing style charts...")
        style_data = compute_style_charts(closes_all)
        with _lock:
            cache["style_charts"] = style_data

        # Compute YTD rebased chart data
        with _lock:
            cache["progress"] = "Computing YTD chart data..."
        print("  Computing YTD rebased data...")
        sector_factor_tickers = sorted(
            {f["symbol"] for f in funds["sectors"]} | {f["symbol"] for f in funds["factors"]} | {"SPY"}
        )
        ytd_data = compute_ytd_rebased(closes_all, sector_factor_tickers)
        with _lock:
            cache["ytd_rebased"] = ytd_data

        with _lock:
            cache["phase"]        = 4
            cache["progress"]     = "Complete"
            cache["last_updated"] = datetime.now(CT).strftime("%-m/%-d/%y %H:%M CT")

        # Save to Redis
        payload = {
            "sectors":      cache["sectors"],
            "factors":      cache["factors"],
            "style_charts": cache["style_charts"],
            "ytd_rebased":  cache["ytd_rebased"],
            "last_updated": cache["last_updated"],
        }
        ok = redis_set(REDIS_KEY, payload)
        print(f"  Redis save: {'OK' if ok else 'FAILED'}")
        print(f"Done — {len(cache['sectors'])} sectors, {len(cache['factors'])} factors.")

        # Seasonality is heavy (25y history) — only (re)compute if not already cached
        ensure_seasonality(force=False)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with _lock:
            cache["error"] = str(e)
            cache["phase"] = 4


def trigger_update():
    threading.Thread(target=run_update, daemon=True).start()


def _ensure_started():
    global _started
    if not _started:
        _started = True
        payload = redis_get(REDIS_KEY)
        if payload and payload.get("sectors"):
            with _lock:
                cache["sectors"]      = payload["sectors"]
                cache["factors"]      = payload["factors"]
                cache["style_charts"] = payload.get("style_charts", {})
                cache["ytd_rebased"]  = payload.get("ytd_rebased", {})
                cache["last_updated"] = payload.get("last_updated", "—")
                cache["phase"]        = 4
                cache["progress"]     = "Loaded from cache"
            n_s = len(cache["sectors"])
            n_f = len(cache["factors"])
            print(f"  Redis restored: {n_s} sectors, {n_f} factors.")
            ensure_seasonality(force=False)
        else:
            trigger_update()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    _ensure_started()
    with _lock:
        sectors      = list(cache.get("sectors", []))
        factors      = list(cache.get("factors", []))
        style_charts = cache.get("style_charts", {})
        ytd_rebased  = cache.get("ytd_rebased", {})
        seasonality  = cache.get("seasonality", {})
        data_json    = json.dumps({
            "sectors":      sectors,
            "factors":      factors,
            "style_charts": style_charts,
            "ytd_rebased":  ytd_rebased,
            "seasonality":  seasonality,
        })

    return render_template(
        "index.html",
        sectors=sectors,
        factors=factors,
        data_json=data_json,
        last_updated=cache["last_updated"],
        season_last_updated=cache["season_last_updated"],
        is_loading=cache["phase"] < 4,
        phase=cache["phase"],
        progress=cache["progress"],
        error=cache["error"],
    )


@app.route("/refresh")
def refresh():
    """Force a full fresh reload of scores/YTD data (does NOT touch seasonality —
    use /refresh-seasonality for that, since it's a much heavier 25-year pull)."""
    global _started
    redis_del(REDIS_KEY)
    with _lock:
        cache["sectors"] = []
        cache["factors"] = []
        cache["style_charts"] = {}
        cache["ytd_rebased"] = {}
        cache["phase"]   = 0
        cache["progress"] = "Starting..."
        cache["error"]   = None
    _started = False
    trigger_update()
    _started = True
    return jsonify({"status": "refresh started — check /status"})


@app.route("/refresh-seasonality")
def refresh_seasonality():
    """Force a full recompute of the 25-year seasonality averages.
    Only needs to run occasionally (e.g. monthly) — not part of the daily cron."""
    redis_del(REDIS_KEY_SEASON)
    with _lock:
        cache["seasonality"] = {}
        cache["season_last_updated"] = "Recomputing..."
    ensure_seasonality(force=True)
    return jsonify({"status": "seasonality refresh started — this can take a few minutes"})


@app.route("/redis-test")
def redis_test():
    """Diagnostic: round-trip write/read/delete to verify Redis connectivity."""
    test_key = "factor_dash_test"
    test_val = {"test": True, "ts": datetime.now(CT).isoformat()}
    w = redis_set(test_key, test_val, ex_seconds=30)
    r = redis_get(test_key)
    redis_del(test_key)
    return jsonify({
        "redis_url_set": bool(REDIS_URL),
        "redis_token_set": bool(REDIS_TOKEN),
        "write_ok": w,
        "read_back": r,
        "cache_key": REDIS_KEY,
        "cache_phase": cache["phase"],
        "cache_sectors": len(cache.get("sectors", [])),
        "cache_factors": len(cache.get("factors", [])),
    })


@app.route("/status")
def status():
    _ensure_started()
    with _lock:
        return jsonify({
            "phase":        cache["phase"],
            "progress":     cache["progress"],
            "last_updated": cache["last_updated"],
            "error":        cache["error"],
            "sectors":      len(cache.get("sectors", [])),
            "factors":      len(cache.get("factors", [])),
            "seasonality_ready": bool(cache.get("seasonality", {}).get("series")),
            "season_last_updated": cache.get("season_last_updated", "Never"),
        })


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify({
            "sectors":      cache["sectors"],
            "factors":      cache["factors"],
            "ytd_rebased":  cache["ytd_rebased"],
            "seasonality":  cache["seasonality"],
            "last_updated": cache["last_updated"],
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
