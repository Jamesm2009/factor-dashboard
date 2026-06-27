"""
Factor & Sector Analysis Dashboard — yFinance + Upstash Redis cache
Absolute Score: where price sits in its 52-week range, mapped to [-1, +1]
Relative Score: same calculation on the ETF/SPY price ratio
Tails: weekly snapshots showing score trajectory over 4–12 weeks
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
REDIS_KEY   = "factor_sector_dash_v1"

cache = {
    "sectors": [],
    "factors": [],
    "last_updated": "Loading...",
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


# ── Main update ───────────────────────────────────────────────────────────────

def run_update():
    with _lock:
        cache["phase"]    = 1
        cache["error"]    = None
        cache["progress"] = "Downloading price data..."

    try:
        funds = load_funds()

        # Collect all unique tickers + SPY benchmark
        all_tickers = set()
        for group in ("sectors", "factors"):
            for f in funds[group]:
                all_tickers.add(f["symbol"])
        all_tickers.add("SPY")
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

        with _lock:
            cache["phase"]        = 4
            cache["progress"]     = "Complete"
            cache["last_updated"] = datetime.now(CT).strftime("%-m/%-d/%y %H:%M CT")

        # Save to Redis
        payload = {
            "sectors":      cache["sectors"],
            "factors":      cache["factors"],
            "last_updated": cache["last_updated"],
        }
        ok = redis_set(REDIS_KEY, payload)
        print(f"  Redis save: {'OK' if ok else 'FAILED'}")
        print(f"Done — {len(cache['sectors'])} sectors, {len(cache['factors'])} factors.")

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
                cache["last_updated"] = payload.get("last_updated", "—")
                cache["phase"]        = 4
                cache["progress"]     = "Loaded from cache"
            n_s = len(cache["sectors"])
            n_f = len(cache["factors"])
            print(f"  Redis restored: {n_s} sectors, {n_f} factors.")
        else:
            trigger_update()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    _ensure_started()
    with _lock:
        sectors   = list(cache.get("sectors", []))
        factors   = list(cache.get("factors", []))
        data_json = json.dumps({"sectors": sectors, "factors": factors})

    return render_template(
        "index.html",
        sectors=sectors,
        factors=factors,
        data_json=data_json,
        last_updated=cache["last_updated"],
        is_loading=cache["phase"] < 4,
        phase=cache["phase"],
        progress=cache["progress"],
        error=cache["error"],
    )


@app.route("/refresh")
def refresh():
    global _started
    redis_del(REDIS_KEY)
    with _lock:
        cache["sectors"] = []
        cache["factors"] = []
        cache["phase"]   = 0
        cache["progress"] = "Starting..."
        cache["error"]   = None
    _started = False
    trigger_update()
    _started = True
    return jsonify({"status": "refresh started — check /status"})


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
        })


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify({
            "sectors":      cache["sectors"],
            "factors":      cache["factors"],
            "last_updated": cache["last_updated"],
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
