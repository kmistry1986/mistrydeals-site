"""
Trading Dashboard Server
Serves the dashboard and proxies Yahoo Finance API calls to avoid CORS.

Setup (run once):
  pip3 install flask flask-cors requests

Run:
  python3 server.py

Then open: http://localhost:8080
"""

import os
import time
import threading
import requests
import numpy as np
import datetime as _dt_root
import zoneinfo as _tz_root
from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS

# ─────────────────────────────────────────────────────────────────────────────
# SHARED TIMEZONE HELPER
# An audit found four different ways this file converted a UTC epoch to
# Eastern Time: proper zoneinfo (correct), a "4 if Mar-Nov else 5" heuristic
# (approximate — wrong for a few days near the actual DST transition), a
# hardcoded "-5 hours" with no DST handling at all (wrong for ~8 months of
# the year), and no conversion at all (silently used the server's local
# timezone). This is the one correct implementation; everything below has
# been pointed at it.
# ─────────────────────────────────────────────────────────────────────────────
_ET_ZONE = _tz_root.ZoneInfo("America/New_York")

def to_et(ts):
    """UTC epoch seconds -> timezone-aware datetime in America/New_York."""
    return _dt_root.datetime.fromtimestamp(ts, tz=_dt_root.timezone.utc).astimezone(_ET_ZONE)

try:
    import talib
    # Quick sanity check — 0.7.x returns all zeros (broken)
    import numpy as _np_test
    _test_arr = _np_test.array([1.0,2.0,3.0,4.0,5.0,6.0,7.0,8.0,9.0,10.0])
    _test_res = talib.CDLMARUBOZU(_test_arr*0.99, _test_arr, _test_arr*0.98, _test_arr*0.995)
    if _test_res[-1] == 0 and all(v == 0 for v in _test_res):
        raise ValueError("TA-Lib 0.7.x returns all zeros — broken install")
    TALIB_AVAILABLE = True
    print("TA-Lib OK:", talib.__version__)
except Exception as e:
    TALIB_AVAILABLE = False
    print(f"WARNING: TA-Lib not available ({e}) — trying pandas_ta")

try:
    import pandas_ta as pdta
    import pandas as pd
    PANDAS_TA_AVAILABLE = True
    print("pandas_ta available")
except ImportError:
    PANDAS_TA_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST ENGINE VERSION
# Bump this whenever the engine changes in a way that alters results. A model
# number identifies a CONSTRAINT SET; the result is (constraints x engine).
# Without this stamp, two runs of "Model 2.0" on different engines are
# indistinguishable in the table.
#
#   e1  pre-2026-07-14   volume avg polluted by pre-market bars; VWAP dead code;
#                        no stop; SPY filter `continue` froze exits (from 1.19)
#   e2  2026-07-14       stop_loss_pct added (bar-close trigger)
#   e3  2026-07-14       stop reworked as a resting order (bar low, fill at stop)
#   e4  2026-07-14       rth_only (09:30-15:59 ET); VWAP activated w/ vwap_mode;
#                        trend_filter added
#   e5  2026-07-14       FIX: trend_filter + spy_filter no longer `continue` past
#                        the exit logic — they veto entries only
# ─────────────────────────────────────────────────────────────────────────────
#   e6  2026-07-14       added strategy=rsi — a minimal 2-parameter mean-reversion
#                        model (RSI in / RSI out), bypassing the entire bias stack
ENGINE_VERSION = "e6"

app = Flask(__name__)
CORS(app, origins="*")

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Auto-timeout settings ──────────────────────────────────────────
TIMEOUT_MINUTES = 60          # shut down after this many minutes of inactivity
last_activity   = time.time() # updated on every request

def activity_watchdog():
    """Background thread: shuts server down after TIMEOUT_MINUTES of inactivity."""
    while True:
        time.sleep(60)  # check every minute
        idle = (time.time() - last_activity) / 60
        remaining = TIMEOUT_MINUTES - idle
        if remaining <= 5:
            print(f"\n⚠️  Server idle for {idle:.0f} min — shutting down in {remaining:.0f} min.")
        if idle >= TIMEOUT_MINUTES:
            print("\n🛑 Auto-timeout reached. Server shutting down.")
            print("   Re-launch with launch_dashboard.command when ready.")
            os._exit(0)

watchdog = threading.Thread(target=activity_watchdog, daemon=True)
watchdog.start()

def touch():
    global last_activity
    last_activity = time.time()

@app.route("/")
def index():
    touch()
    return send_from_directory(DASHBOARD_DIR, "trading_dashboard.html")

@app.route("/engine-version")
def engine_version():
    return jsonify({"engine_version": ENGINE_VERSION})

@app.route("/cardops")
@app.route("/cardops.html")
def cardops():
    return send_from_directory(DASHBOARD_DIR, "cardops.html")

@app.route("/trading_dashboard.html")
def dashboard():
    touch()
    return send_from_directory(DASHBOARD_DIR, "trading_dashboard.html")

@app.route("/yf")
def yahoo_finance():
    touch()
    ticker        = request.args.get("ticker", "")
    range_        = request.args.get("range", "3mo")
    interval      = request.args.get("interval", "1d")
    include_pre   = request.args.get("includePrePost", "false")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": range_, "interval": interval, "includePrePost": include_pre}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        # fallback to query2
        try:
            url2 = url.replace("query1", "query2")
            r2 = requests.get(url2, params=params, headers=headers, timeout=10)
            return jsonify(r2.json()), r2.status_code
        except Exception as e2:
            return jsonify({"error": str(e2)}), 500

@app.route("/yf-options")
def yahoo_options():
    touch()
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    try:
        try:
            import yfinance as yf
        except ImportError:
            return jsonify({"error": "yfinance not installed. Run: pip3 install yfinance"}), 500
        import time
        stock = yf.Ticker(ticker)
        expirations = stock.options  # tuple of expiry date strings like '2026-07-18'
        if not expirations:
            return jsonify({"error": "No options data available for " + ticker}), 404
        # Filter to next 90 days
        now = time.time()
        ninety_days = now + (90 * 86400)
        from datetime import datetime
        filtered = []
        for exp_str in expirations:
            try:
                exp_ts = datetime.strptime(exp_str, "%Y-%m-%d").timestamp()
                if now <= exp_ts <= ninety_days:
                    filtered.append((exp_str, int(exp_ts)))
            except Exception as chain_err:
                print(f"options chain error for {exp_str}: {chain_err}")
                continue
        filtered = filtered[:4]  # max 4 expirations for speed
        options_map = {}
        for exp_str, exp_ts in filtered:
            try:
                chain = stock.option_chain(exp_str)
                calls = chain.calls.to_dict(orient="records")
                puts  = chain.puts.to_dict(orient="records")
                # Normalize field names to match what dashboard expects
                def normalize(contracts):
                    from datetime import datetime
                    out = []
                    for c in contracts:
                        ltd = c.get("lastTradeDate")
                        ltd_str = None
                        if ltd is not None:
                            try:
                                # yfinance returns lastTradeDate as a pandas Timestamp
                                # (used to be a raw epoch float). Handle both. The
                                # Timestamp is tz-aware UTC — convert to ET so it
                                # matches every other timestamp in this app.
                                import zoneinfo as _tz_ltd
                                _et = _tz_ltd.ZoneInfo("America/New_York")
                                if hasattr(ltd, "to_pydatetime"):
                                    ltd_dt = ltd.to_pydatetime()
                                    if ltd_dt.tzinfo is None:
                                        ltd_dt = ltd_dt.replace(tzinfo=datetime.timezone.utc)
                                    ltd_dt = ltd_dt.astimezone(_et)
                                else:
                                    # legacy raw epoch float — always UTC
                                    ltd_dt = datetime.fromtimestamp(ltd, tz=datetime.timezone.utc).astimezone(_et)
                                ltd_str = ltd_dt.strftime("%m/%d %I:%M %p") + " ET"
                            except Exception:
                                ltd_str = None
                        # yfinance/pandas leaves volume, openInterest, and the greeks
                        # as NaN (a float) on illiquid/far-OTM contracts. `NaN or 0`
                        # evaluates to NaN (NaN is truthy), so int(NaN) throws — this
                        # was killing EVERY expiry, for every ticker, silently.
                        def _safe_int(v):
                            try:
                                if v is None: return 0
                                fv = float(v)
                                return 0 if fv != fv else int(fv)  # fv != fv  <=>  NaN
                            except (TypeError, ValueError):
                                return 0
                        def _safe_num(v):
                            try:
                                fv = float(v)
                                return None if fv != fv else fv
                            except (TypeError, ValueError):
                                return v
                        out.append({
                            "strike":            _safe_num(c.get("strike", 0)),
                            "lastPrice":         _safe_num(c.get("lastPrice", 0)),
                            "bid":               _safe_num(c.get("bid", 0)),
                            "ask":               _safe_num(c.get("ask", 0)),
                            "volume":            _safe_int(c.get("volume", 0)),
                            "openInterest":      _safe_int(c.get("openInterest", 0)),
                            "impliedVolatility": _safe_num(c.get("impliedVolatility", 0)),
                            "delta":             _safe_num(c.get("delta")),
                            "gamma":             _safe_num(c.get("gamma")),
                            "theta":             _safe_num(c.get("theta")),
                            "vega":              _safe_num(c.get("vega")),
                            "inTheMoney":        bool(c.get("inTheMoney", False)),
                            "lastTradeDate":     ltd_str,
                        })
                    return out
                options_map[exp_str] = {
                    "calls": normalize(calls),
                    "puts":  normalize(puts),
                    "expTs": exp_ts
                }
            except Exception as chain_ex:
                import traceback
                print(f"yf-options: failed to build chain for {ticker} {exp_str}: {chain_ex}")
                traceback.print_exc()
                continue
        return jsonify({
            "ticker": ticker,
            "expirationDates": [exp_str for exp_str, _ in filtered],
            "expirationTimestamps": {exp_str: exp_ts for exp_str, exp_ts in filtered},
            "options": options_map
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/robinhood-positions")
def robinhood_positions():
    touch()
    account = request.args.get("account", "")
    if not account:
        return jsonify({"error": "Missing account number"}), 400
    try:
        # Fetch positions from Robinhood
        r = requests.get(
            f"https://api.robinhood.com/positions/?account_number={account}&nonzero=true",
            headers={"Authorization": "Bearer " + request.headers.get("X-RH-Token", "")},
            timeout=10
        )
        if r.status_code == 401:
            return jsonify({"error": "Robinhood authentication required. Holdings will show sample data."}), 401
        data = r.json()
        results = data.get("results", [])
        positions = []
        for p in results:
            ticker = p.get("symbol", "")
            qty = float(p.get("quantity", 0))
            avg = float(p.get("average_buy_price", 0))
            value = qty * avg
            positions.append({
                "ticker": ticker,
                "shares": round(qty, 4),
                "avg_cost": round(avg, 2),
                "value": round(value, 2),
                "chg": 0
            })
        return jsonify({"positions": positions}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rh-watchlist")
def rh_watchlist():
    touch()
    list_id = request.args.get("list_id", "")
    if not list_id:
        return jsonify({"error": "Missing list_id"}), 400
    try:
        r = requests.get(
            f"https://api.robinhood.com/watchlists/{list_id}/",
            headers={"Authorization": "Bearer " + request.args.get("token", "")},
            timeout=10
        )
        if r.status_code != 200:
            return jsonify({"error": f"Robinhood returned {r.status_code}. Auth required."}), r.status_code
        data = r.json()
        symbols = [item.get("symbol","") for item in data.get("results",[]) if item.get("symbol")]
        return jsonify({"symbols": symbols, "count": len(symbols)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/rh-watchlist-update", methods=["POST"])
def rh_watchlist_update():
    touch()
    data = request.json or {}
    list_id = data.get("list_id","")
    action  = data.get("action","add")
    symbol  = data.get("symbol","").upper()
    if not list_id or not symbol:
        return jsonify({"error": "Missing list_id or symbol"}), 400
    try:
        if action == "add":
            r = requests.post(
                f"https://api.robinhood.com/watchlists/{list_id}/bulk_add/",
                json={"symbols": [symbol]},
                headers={"Authorization": "Bearer " + data.get("token",""),
                         "Content-Type": "application/json"},
                timeout=10
            )
        else:
            r = requests.post(
                f"https://api.robinhood.com/watchlists/{list_id}/bulk_remove/",
                json={"symbols": [symbol]},
                headers={"Authorization": "Bearer " + data.get("token",""),
                         "Content-Type": "application/json"},
                timeout=10
            )
        return jsonify({"status": r.status_code, "action": action, "symbol": symbol}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/yf-earnings")
def yf_earnings():
    touch()
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=calendarEvents,defaultKeyStatistics"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        result = data.get("quoteSummary", {}).get("result", [])
        if not result:
            return jsonify({"error": "No data"}), 404
        calendar = result[0].get("calendarEvents", {})
        earnings_dates = calendar.get("earnings", {}).get("earningsDate", [])
        eps = result[0].get("defaultKeyStatistics", {}).get("forwardEps", {}).get("raw")
        if earnings_dates:
            ts = earnings_dates[0].get("raw", 0)
            date_str = to_et(ts).strftime("%b %d, %Y") if ts else None
            return jsonify({"ticker": ticker, "earningsDate": date_str, "epsEstimate": eps, "period": "Next quarter"}), 200
        return jsonify({"ticker": ticker, "earningsDate": None}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/yf-prepost")
def yf_prepost():
    touch()
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        # Try v8 chart with includePrePost
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=1m&includePrePost=true"
        r = requests.get(url, headers=headers, timeout=8)
        data = r.json()
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})

        regular_price = meta.get("regularMarketPrice", 0)
        post_price    = meta.get("postMarketPrice")
        pre_price     = meta.get("preMarketPrice")
        post_change   = meta.get("postMarketChange")
        pre_change    = meta.get("preMarketChange")
        post_pct      = meta.get("postMarketChangePercent")
        pre_pct       = meta.get("preMarketChangePercent")

        # If chart API didn't return extended hours, try quote summary
        if not post_price and not pre_price:
            url2 = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}&fields=regularMarketPrice,postMarketPrice,postMarketChange,postMarketChangePercent,preMarketPrice,preMarketChange,preMarketChangePercent"
            r2 = requests.get(url2, headers=headers, timeout=8)
            d2 = r2.json()
            result2 = d2.get("quoteResponse", {}).get("result", [{}])
            if result2:
                q = result2[0]
                regular_price = q.get("regularMarketPrice", regular_price)
                post_price    = q.get("postMarketPrice")
                pre_price     = q.get("preMarketPrice")
                post_change   = q.get("postMarketChange")
                pre_change    = q.get("preMarketChange")
                post_pct      = q.get("postMarketChangePercent")
                pre_pct       = q.get("preMarketChangePercent")

        result = {
            "ticker":        ticker,
            "regularPrice":  regular_price,
            "postPrice":     post_price,
            "postChange":    post_change,
            "postChangePct": post_pct,
            "prePrice":      pre_price,
            "preChange":     pre_change,
            "preChangePct":  pre_pct,
        }
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/yf-scan")
def yf_scan():
    """Single endpoint returning quote data + pre/post in one call for scanner performance."""
    touch()
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        import concurrent.futures
        def fetch_quote():
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=3mo&interval=1d"
            return requests.get(url, headers=headers, timeout=8).json()
        def fetch_prepost():
            url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}&fields=regularMarketPrice,postMarketPrice,postMarketChange,postMarketChangePercent,preMarketPrice,preMarketChange,preMarketChangePercent"
            return requests.get(url, headers=headers, timeout=8).json()
        def fetch_weekly():
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=6mo&interval=1wk"
            return requests.get(url, headers=headers, timeout=8).json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f_quote  = ex.submit(fetch_quote)
            f_pre    = ex.submit(fetch_prepost)
            f_weekly = ex.submit(fetch_weekly)
            quote_data  = f_quote.result()
            pre_data    = f_pre.result()
            weekly_data = f_weekly.result()
        # Extract pre/post
        q2 = pre_data.get("quoteResponse", {}).get("result", [{}])
        q2 = q2[0] if q2 else {}
        result = {
            "quote":  quote_data,
            "weekly": weekly_data,
            "prepost": {
                "postPrice":     q2.get("postMarketPrice"),
                "postChange":    q2.get("postMarketChange"),
                "postChangePct": q2.get("postMarketChangePercent"),
                "prePrice":      q2.get("preMarketPrice"),
                "preChange":     q2.get("preMarketChange"),
                "preChangePct":  q2.get("preMarketChangePercent"),
            }
        }
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/finnhub")
def finnhub_proxy():
    touch()
    ticker  = request.args.get("ticker", "")
    endpoint= request.args.get("endpoint", "quote")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    FINNHUB_KEY = "d929lchr01qrfbe99fj0d929lchr01qrfbe99fjg"
    headers = {"X-Finnhub-Token": FINNHUB_KEY}
    try:
        if endpoint == "quote":
            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}"
            r = requests.get(url, headers=headers, timeout=5)
            return jsonify(r.json()), r.status_code
        elif endpoint == "candles":
            resolution = request.args.get("resolution", "D")
            from_ts    = request.args.get("from", "")
            to_ts      = request.args.get("to", "")
            url = f"https://finnhub.io/api/v1/stock/candle?symbol={ticker}&resolution={resolution}&from={from_ts}&to={to_ts}"
            r = requests.get(url, headers=headers, timeout=8)
            return jsonify(r.json()), r.status_code
        elif endpoint == "news":
            from_date = request.args.get("from", "")
            to_date   = request.args.get("to", "")
            url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={from_date}&to={to_date}"
            r = requests.get(url, headers=headers, timeout=8)
            return jsonify(r.json()), r.status_code
        else:
            return jsonify({"error": "Unknown endpoint"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/odds")
def odds_proxy():
    touch()
    api_key = request.args.get("apiKey", "")
    sport   = request.args.get("sport", "")
    market  = request.args.get("market", "h2h")
    regions = request.args.get("regions", "us")
    if not api_key:
        return jsonify({"error": "Missing apiKey"}), 400
    if not sport:
        return jsonify({"error": "Missing sport"}), 400
    try:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
        params = {
            "apiKey":     api_key,
            "regions":    regions,
            "markets":    market,
            "oddsFormat": "american",
        }
        r = requests.get(url, params=params, timeout=15)
        resp = make_response(jsonify(r.json()), r.status_code)
        resp.headers["X-Requests-Remaining"] = r.headers.get("x-requests-remaining", "?")
        resp.headers["X-Requests-Used"]      = r.headers.get("x-requests-used", "?")
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sports")
def sports_proxy():
    touch()
    api_key = request.args.get("apiKey", "")
    if not api_key:
        return jsonify({"error": "Missing apiKey"}), 400
    try:
        url = "https://api.the-odds-api.com/v4/sports/"
        r = requests.get(url, params={"apiKey": api_key}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/polymarket-search")
def polymarket_search():
    touch()
    q      = request.args.get("q", "world cup")
    limit  = request.args.get("limit", "50")
    slugs  = request.args.get("slugs", "")
    headers = {"User-Agent": "Mozilla/5.0"}
    results = []
    seen_slugs = set()
    try:
        # General search
        url = f"https://gamma-api.polymarket.com/events?q={q}&limit={limit}&active=true&closed=false"
        r = requests.get(url, headers=headers, timeout=8)
        events = r.json() if r.ok else []
        if isinstance(events, list):
            for ev in events:
                s = ev.get("slug","")
                if s not in seen_slugs:
                    seen_slugs.add(s)
                    results.append(ev)
        # Fetch by specific slugs
        if slugs:
            slug_list = [s.strip() for s in slugs.split(",") if s.strip()]
            for slug in slug_list[:60]:  # limit to 60 slug fetches
                if slug in seen_slugs:
                    continue
                try:
                    r2 = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", headers=headers, timeout=5)
                    if r2.ok:
                        data2 = r2.json()
                        if isinstance(data2, list):
                            for ev in data2:
                                s = ev.get("slug","")
                                if s not in seen_slugs:
                                    seen_slugs.add(s)
                                    results.append(ev)
                        elif isinstance(data2, dict) and data2.get("slug"):
                            s = data2.get("slug","")
                            if s not in seen_slugs:
                                seen_slugs.add(s)
                                results.append(data2)
                except:
                    pass
        return jsonify(results), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/polymarket-worldcup")
def polymarket_worldcup():
    touch()
    headers = {"User-Agent": "Mozilla/5.0"}
    results = []
    seen = set()
    # Try multiple tags/queries to find World Cup 2026 events
    urls = [
        "https://gamma-api.polymarket.com/events?tag=fifa-world-cup-2026&limit=100&active=true&closed=false",
        "https://gamma-api.polymarket.com/events?tag=fifa-world-cup&limit=100&active=true&closed=false",
        "https://gamma-api.polymarket.com/events?q=FIFA+World+Cup+2026&limit=100&active=true&closed=false",
        "https://gamma-api.polymarket.com/events?q=world+cup+2026&limit=100&active=true&closed=false",
    ]
    try:
        for url in urls:
            try:
                r = requests.get(url, headers=headers, timeout=8)
                if r.ok:
                    events = r.json() if isinstance(r.json(), list) else r.json().get("events", [])
                    for ev in (events if isinstance(events, list) else []):
                        key = ev.get("slug") or ev.get("id")
                        if key and key not in seen:
                            seen.add(key)
                            results.append(ev)
            except:
                pass
        return jsonify({"events": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/odds-sports")
def odds_sports():
    touch()
    api_key = request.args.get("apiKey", "")
    if not api_key:
        return jsonify({"error": "Missing apiKey"}), 400
    try:
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}", timeout=8)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# TipRanks cached data — refreshed by Claude periodically
# Last updated: 2026-07-06 | AAPL buy/hold/sell from actual analyst ratings
TIPRANKS_CACHE = {
  "AAPL": {"smartScore":9,"analystConsensus":"Buy","priceTarget":324.4,"priceTargetLow":225.0,"priceTargetHigh":400.0,"priceTargetUpside":0.0886,"ratingsBuy":18,"ratingsHold":11,"ratingsSell":1},
  "NVDA": {"smartScore":10,"analystConsensus":"StrongBuy","priceTarget":309.33,"priceTargetLow":254.0,"priceTargetHigh":365.0,"priceTargetUpside":0.4682,"ratingsBuy":38,"ratingsHold":4,"ratingsSell":0},
  "TSLA": {"smartScore":7,"analystConsensus":"Buy","priceTarget":403.49,"priceTargetLow":331.0,"priceTargetHigh":476.0,"priceTargetUpside":0.0075,"ratingsBuy":15,"ratingsHold":12,"ratingsSell":8},
  "MSFT": {"smartScore":6,"analystConsensus":"StrongBuy","priceTarget":562.56,"priceTargetLow":461.0,"priceTargetHigh":664.0,"priceTargetUpside":0.4828,"ratingsBuy":32,"ratingsHold":5,"ratingsSell":0},
  "GOOGL": {"smartScore":10,"analystConsensus":"StrongBuy","priceTarget":427.38,"priceTargetLow":350.0,"priceTargetHigh":504.0,"priceTargetUpside":0.1613,"ratingsBuy":35,"ratingsHold":4,"ratingsSell":0},
  "META": {"smartScore":9,"analystConsensus":"StrongBuy","priceTarget":815.82,"priceTargetLow":669.0,"priceTargetHigh":963.0,"priceTargetUpside":0.4134,"ratingsBuy":36,"ratingsHold":4,"ratingsSell":0},
  "AMZN": {"smartScore":9,"analystConsensus":"StrongBuy","priceTarget":319.14,"priceTargetLow":262.0,"priceTargetHigh":377.0,"priceTargetUpside":0.3059,"ratingsBuy":37,"ratingsHold":3,"ratingsSell":0},
  "AMD":  {"smartScore":7,"analystConsensus":"StrongBuy","priceTarget":491.27,"priceTargetLow":403.0,"priceTargetHigh":580.0,"priceTargetUpside":-0.0858,"ratingsBuy":28,"ratingsHold":8,"ratingsSell":1},
  "MRVL": {"smartScore":8,"analystConsensus":"StrongBuy","priceTarget":261.62,"priceTargetLow":215.0,"priceTargetHigh":309.0,"priceTargetUpside":-0.1576,"ratingsBuy":20,"ratingsHold":3,"ratingsSell":0},
  "NBIS": {"smartScore":8,"analystConsensus":"Buy","priceTarget":235.0,"priceTargetLow":193.0,"priceTargetHigh":277.0,"priceTargetUpside":-0.1803,"ratingsBuy":6,"ratingsHold":1,"ratingsSell":0},
  "RKLB": {"smartScore":7,"analystConsensus":"StrongBuy","priceTarget":108.7,"priceTargetLow":89.0,"priceTargetHigh":128.0,"priceTargetUpside":0.0136,"ratingsBuy":8,"ratingsHold":2,"ratingsSell":0},
  "SHW":  {"smartScore":6,"analystConsensus":"Buy","priceTarget":371.2,"priceTargetLow":304.0,"priceTargetHigh":438.0,"priceTargetUpside":0.1571,"ratingsBuy":14,"ratingsHold":8,"ratingsSell":1},
  "SPY":  {"smartScore":8,"analystConsensus":"Buy","priceTarget":894.0,"priceTargetLow":733.0,"priceTargetHigh":1055.0,"priceTargetUpside":0.2004,"ratingsBuy":12,"ratingsHold":3,"ratingsSell":0},
  "QQQ":  {"smartScore":8,"analystConsensus":"Buy","priceTarget":873.0,"priceTargetLow":716.0,"priceTargetHigh":1030.0,"priceTargetUpside":0.2251,"ratingsBuy":12,"ratingsHold":3,"ratingsSell":0},
  "QQQI": {"smartScore":8,"analystConsensus":"Buy","priceTarget":67.8,"priceTargetLow":56.0,"priceTargetHigh":80.0,"priceTargetUpside":0.2242,"ratingsBuy":10,"ratingsHold":3,"ratingsSell":0},
  "VOO":  {"smartScore":8,"analystConsensus":"Buy","priceTarget":832.4,"priceTargetLow":683.0,"priceTargetHigh":982.0,"priceTargetUpside":0.2154,"ratingsBuy":12,"ratingsHold":3,"ratingsSell":0},
  "VYM":  {"smartScore":7,"analystConsensus":"Buy","priceTarget":185.1,"priceTargetLow":152.0,"priceTargetHigh":218.0,"priceTargetUpside":0.1606,"ratingsBuy":10,"ratingsHold":4,"ratingsSell":0}
}
@app.route("/tipranks")
def tipranks_proxy():
    touch()
    tickers_param = request.args.get("tickers", "")
    if not tickers_param:
        return jsonify({"error": "Missing tickers"}), 400
    tickers = [t.strip().upper() for t in tickers_param.split(",") if t.strip()]
    result = []
    for t in tickers:
        if t in TIPRANKS_CACHE:
            result.append({"ticker": t, **TIPRANKS_CACHE[t]})
        else:
            result.append({"ticker": t, "smartScore": None, "priceTarget": None,
                           "priceTargetLow": None, "priceTargetHigh": None,
                           "ratingsBuy": None, "ratingsHold": None, "ratingsSell": None})
    return jsonify(result), 200

@app.route("/patterns")
def patterns():
    touch()
    ticker   = request.args.get("ticker", "").upper()
    interval = request.args.get("interval", "5m")
    range_   = request.args.get("range", "5d")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400

    try:
        import numpy as np

        # Fetch OHLCV
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={range_}&includePrePost=false"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        result = data.get("chart", {}).get("result", [None])[0]
        if not result:
            return jsonify({"error": "No data"}), 404

        q  = result.get("indicators", {}).get("quote", [{}])[0]
        raw_o = q.get("open",   [])
        raw_h = q.get("high",   [])
        raw_l = q.get("low",    [])
        raw_c = q.get("close",  [])
        raw_v = q.get("volume", [])

        # Clean: remove bars with any None
        rows = [(o,h,l,c,v) for o,h,l,c,v in zip(raw_o,raw_h,raw_l,raw_c,raw_v)
                if o is not None and h is not None and l is not None and c is not None]
        if len(rows) < 10:
            return jsonify({"patterns":[],"indicators":{},"bars":len(rows)}), 200

        O = [r[0] for r in rows]
        H = [r[1] for r in rows]
        L = [r[2] for r in rows]
        C = [r[3] for r in rows]
        V = [r[4] or 0 for r in rows]
        n = len(C)

        # ── Indicators ────────────────────────────────────────────────
        def sma(arr, p):
            return [sum(arr[i-p:i])/p if i>=p else None for i in range(len(arr))]

        def ema(arr, p):
            result2 = [None]*len(arr)
            k = 2/(p+1)
            for i,v in enumerate(arr):
                if i < p-1: continue
                if i == p-1: result2[i] = sum(arr[:p])/p
                else: result2[i] = v*k + result2[i-1]*(1-k)
            return result2

        ma20 = sma(C, 20)
        ma50 = sma(C, 50)

        # RSI
        gains, losses = [], []
        for i in range(1, n):
            d = C[i]-C[i-1]
            gains.append(max(d,0)); losses.append(max(-d,0))
        rsi_val = None
        if len(gains) >= 14:
            ag = sum(gains[-14:])/14; al = sum(losses[-14:])/14
            rsi_val = round(100 - 100/(1+ag/al), 2) if al > 0 else 100.0

        # MACD
        ema12 = ema(C, 12); ema26 = ema(C, 26)
        macd_line = [a-b if a and b else None for a,b in zip(ema12,ema26)]
        macd_clean = [v for v in macd_line if v is not None]
        macd_val = macd_clean[-1] if macd_clean else None
        sig_arr = ema([v for v in macd_line if v is not None], 9)
        macd_sig = sig_arr[-1] if sig_arr else None
        macd_prev = macd_clean[-2] if len(macd_clean)>=2 else None
        sig_prev  = sig_arr[-2] if len(sig_arr)>=2 else None

        # Bollinger Bands
        bb_upper = bb_lower = None
        if n >= 20:
            m = sum(C[-20:])/20
            std = (sum((x-m)**2 for x in C[-20:])/20)**0.5
            bb_upper = round(m + 2*std, 2)
            bb_lower = round(m - 2*std, 2)

        # Volume ratio
        nonzero_v = [x for x in V if x > 0]
        avg_vol = sum(nonzero_v[-20:])/len(nonzero_v[-20:]) if len(nonzero_v)>=20 else (sum(nonzero_v)/len(nonzero_v) if nonzero_v else 1)
        rvol = round(nonzero_v[-1]/avg_vol if nonzero_v and avg_vol > 0 else 1.0, 2)
        price = C[-1]

        # ── Pure Python Candlestick Patterns ──────────────────────────
        detected = []

        def body(i):    return abs(C[i]-O[i])
        def upper(i):   return H[i]-max(C[i],O[i])
        def lower(i):   return min(C[i],O[i])-L[i]
        def rng(i):     return H[i]-L[i]
        def bull(i):    return C[i] > O[i]
        def bear(i):    return C[i] < O[i]
        def mid(i):     return (O[i]+C[i])/2

        def add(name, typ, strength, desc):
            vol_note = f" Volume: {rvol:.1f}x avg" + (" ✓ confirmed" if rvol>=1.5 else " (low volume)")
            detected.append({
                "name": name, "type": typ, "strength": min(5,strength+(1 if rvol>=1.5 else 0)),
                "candle_idx": n-1, "source": "python",
                "desc": desc + vol_note, "volConfirmed": rvol>=1.5
            })

        # Scan last 10 candles for patterns
        for i in range(max(2, n-10), n):
    
            # Doji
            if rng(i) > 0 and body(i)/rng(i) < 0.1:
                if lower(i) > upper(i)*2:
                    add("Dragonfly Doji","bullish",2,"Open/close at top, long lower wick. Bears drove price down but bulls reclaimed all losses.")
                elif upper(i) > lower(i)*2:
                    add("Gravestone Doji","bearish",2,"Open/close at bottom, long upper wick. Bulls tried higher but bears erased all gains.")
                else:
                    add("Doji","neutral",1,"Open and close nearly equal. Represents indecision — watch next candle for direction.")
    
            # Hammer / Shooting Star (need prior trend context)
            if i >= 1 and rng(i) > 0:
                b = body(i); u = upper(i); lo = lower(i)
                if b > 0 and lo >= 2*b and u <= 0.5*b:
                    if bear(i) or (C[i-1] > C[i]):  # after downtrend
                        add("Hammer","bullish",3,"Small body at top, long lower wick (2x body). Bears drove price down but bulls fully recovered it. Classic reversal at support.")
                if b > 0 and u >= 2*b and lo <= 0.5*b:
                    if bull(i) or (C[i-1] < C[i]):  # after uptrend
                        add("Shooting Star","bearish",3,"Long upper wick after uptrend. Bulls pushed high but bears slammed it back down. Classic reversal at resistance.")
                if b > 0 and u >= 2*b and lo <= 0.5*b and bear(i):
                    add("Inverted Hammer","bullish",2,"Long upper wick at bottom. Bulls tried to push higher — confirm with next bullish candle.")
    
            # Marubozu
            if i >= 0 and rng(i) > 0 and body(i)/rng(i) >= 0.95:
                typ = "bullish" if bull(i) else "bearish"
                add("Marubozu",typ,2,"No wicks — complete dominance by one side for the full session. Strong momentum signal.")
    
            # Engulfing
            if i >= 1:
                if bear(i-1) and bull(i) and C[i]>O[i-1] and O[i]<C[i-1] and body(i)>body(i-1):
                    add("Bullish Engulfing","bullish",3,"Current bullish body completely engulfs prior bearish body. Strong reversal — buyers overwhelmed sellers.")
                if bull(i-1) and bear(i) and C[i]<O[i-1] and O[i]>C[i-1] and body(i)>body(i-1):
                    add("Bearish Engulfing","bearish",3,"Current bearish body completely engulfs prior bullish body. Strong reversal — sellers overwhelmed buyers.")
    
            # Harami
            if i >= 1:
                if bull(i-1) and bear(i) and O[i]<C[i-1] and C[i]>O[i-1] and body(i)<body(i-1):
                    add("Bearish Harami","bearish",2,"Small bearish candle inside prior bullish body. Uptrend losing momentum — needs confirmation.")
                if bear(i-1) and bull(i) and O[i]>C[i-1] and C[i]<O[i-1] and body(i)<body(i-1):
                    add("Bullish Harami","bullish",2,"Small bullish candle inside prior bearish body. Downtrend losing momentum — needs confirmation.")
    
            # Morning/Evening Star
            if i >= 2:
                # Morning Star
                if (bear(i-2) and body(i-2)>rng(i-2)*0.6 and
                    body(i-1)<body(i-2)*0.3 and
                    bull(i) and body(i)>rng(i)*0.6 and C[i]>mid(i-2)):
                    add("Morning Star","bullish",4,"Bullish 3-candle reversal: large bearish, small indecision, large bullish recovering ground. Classic bottom pattern.")
                # Evening Star
                if (bull(i-2) and body(i-2)>rng(i-2)*0.6 and
                    body(i-1)<body(i-2)*0.3 and
                    bear(i) and body(i)>rng(i)*0.6 and C[i]<mid(i-2)):
                    add("Evening Star","bearish",4,"Bearish 3-candle reversal: large bullish, small indecision, large bearish giving back gains. Classic top pattern.")
    
            # Three White Soldiers
            if i >= 2:
                if (bull(i) and bull(i-1) and bull(i-2) and
                    body(i)>rng(i)*0.6 and body(i-1)>rng(i-1)*0.6 and body(i-2)>rng(i-2)*0.6 and
                    C[i]>C[i-1] and C[i-1]>C[i-2] and
                    O[i]>O[i-1] and O[i-1]>O[i-2] and
                    O[i]<C[i-1] and O[i-1]<C[i-2]):
                    add("Three White Soldiers","bullish",4,"Three consecutive long bullish candles each opening within prior body and closing higher. High-conviction buy signal.")
                # Three Black Crows
                if (bear(i) and bear(i-1) and bear(i-2) and
                    body(i)>rng(i)*0.6 and body(i-1)>rng(i-1)*0.6 and body(i-2)>rng(i-2)*0.6 and
                    C[i]<C[i-1] and C[i-1]<C[i-2] and
                    O[i]<O[i-1] and O[i-1]<O[i-2] and
                    O[i]>C[i-1] and O[i-1]>C[i-2]):
                    add("Three Black Crows","bearish",4,"Three consecutive long bearish candles each opening within prior body and closing lower. High-conviction sell signal.")
    
            # Dark Cloud Cover
            if i >= 1:
                if (bull(i-1) and body(i-1)>rng(i-1)*0.6 and
                    bear(i) and O[i]>H[i-1] and C[i]<mid(i-1) and C[i]>L[i-1]):
                    add("Dark Cloud Cover","bearish",3,"Gap-up open then close below midpoint of prior bullish candle. Sellers took over after a gap-up.")
                # Piercing Line
                if (bear(i-1) and body(i-1)>rng(i-1)*0.6 and
                    bull(i) and O[i]<L[i-1] and C[i]>mid(i-1) and C[i]<H[i-1]):
                    add("Piercing Line","bullish",3,"Gap-down open then close above midpoint of prior bearish candle. Buyers absorbed all sellers and more.")
    
            # Belt Hold
            if i >= 0:
                if bull(i) and abs(O[i]-L[i])<body(i)*0.05 and body(i)>rng(i)*0.7:
                    add("Bullish Belt Hold","bullish",2,"Opens at low with no lower wick, closes near high. Strong buying from the open.")
                if bear(i) and abs(O[i]-H[i])<body(i)*0.05 and body(i)>rng(i)*0.7:
                    add("Bearish Belt Hold","bearish",2,"Opens at high with no upper wick, closes near low. Strong selling from the open.")
    
            # Spinning Top
            if i >= 0 and rng(i) > 0:
                if body(i)/rng(i) < 0.3 and upper(i)>body(i)*0.5 and lower(i)>body(i)*0.5:
                    if not any(p["name"]=="Doji" for p in detected):
                        add("Spinning Top","neutral",1,"Small body with upper and lower wicks roughly equal. Indecision — trend may be losing steam.")
    
            # Long Line
            if i >= 5 and rng(i) > 0:
                avg_rng = sum(H[j]-L[j] for j in range(i-5,i))/5
                if rng(i) > avg_rng*2 and body(i)/rng(i)>0.6:
                    typ = "bullish" if bull(i) else "bearish"
                    add("Long Line",typ,2,"Unusually large candle relative to recent bars. Strong momentum — watch for follow-through.")
    
        # Deduplicate: keep only strongest pattern per candle index
        seen_bars = {}
        for p in detected:
            bi = p.get('candle_idx', n-1)
            if bi not in seen_bars or p['strength'] > seen_bars[bi]['strength']:
                seen_bars[bi] = p
        detected = list(seen_bars.values())
        # ── Chart Patterns ────────────────────────────────────────────
        chart_patterns = []

        # Golden/Death Cross
        if ma20[-1] and ma50[-1] and ma20[-2] and ma50[-2]:
            if ma20[-2] < ma50[-2] and ma20[-1] > ma50[-1]:
                chart_patterns.append({"name":"Golden Cross","type":"bullish","strength":4,"source":"python","desc":"MA20 crossed above MA50. Bullish signal — short-term momentum exceeding long-term trend. Widely watched by institutions.","volConfirmed":rvol>=1.5})
            elif ma20[-2] > ma50[-2] and ma20[-1] < ma50[-1]:
                chart_patterns.append({"name":"Death Cross","type":"bearish","strength":4,"source":"python","desc":"MA20 crossed below MA50. Bearish signal — short-term momentum falling below long-term trend.","volConfirmed":rvol>=1.5})

        # MACD Crossover
        if macd_val and macd_sig and macd_prev and sig_prev:
            if macd_prev < sig_prev and macd_val > macd_sig:
                chart_patterns.append({"name":"MACD Crossover (Bullish)","type":"bullish","strength":3,"source":"python","desc":"MACD line crossed above signal line. Momentum shifting bullish — potential buy trigger.","volConfirmed":rvol>=1.5})
            elif macd_prev > sig_prev and macd_val < macd_sig:
                chart_patterns.append({"name":"MACD Crossover (Bearish)","type":"bearish","strength":3,"source":"python","desc":"MACD line crossed below signal line. Momentum shifting bearish — potential sell trigger.","volConfirmed":rvol>=1.5})

        # RSI
        if rsi_val:
            if rsi_val < 30:
                chart_patterns.append({"name":f"RSI Oversold ({rsi_val})","type":"bullish","strength":2,"source":"python","desc":f"RSI at {rsi_val} — below 30 indicates oversold conditions. Potential mean reversion bounce.","volConfirmed":False})
            elif rsi_val > 70:
                chart_patterns.append({"name":f"RSI Overbought ({rsi_val})","type":"bearish","strength":2,"source":"python","desc":f"RSI at {rsi_val} — above 70 indicates overbought conditions. Potential pullback.","volConfirmed":False})

        # Bollinger Bands
        if bb_upper and bb_lower:
            if price >= bb_upper*0.995:
                chart_patterns.append({"name":"Bollinger Upper Touch","type":"bearish","strength":2,"source":"python","desc":f"Price touching upper Bollinger Band (${bb_upper:.2f}). 2 standard deviations above mean — statistical resistance.","volConfirmed":False})
            elif price <= bb_lower*1.005:
                chart_patterns.append({"name":"Bollinger Lower Touch","type":"bullish","strength":2,"source":"python","desc":f"Price touching lower Bollinger Band (${bb_lower:.2f}). 2 standard deviations below mean — statistical support.","volConfirmed":False})

        # 20-bar Breakout/Breakdown
        if n >= 21:
            high20 = max(H[-21:-1])
            low20  = min(L[-21:-1])
            if price > high20*1.005:
                chart_patterns.append({"name":"20-Bar Breakout","type":"bullish","strength":3,"source":"python","desc":f"Price broke above 20-bar high (${high20:.2f}). Momentum breakout into new territory.","volConfirmed":rvol>=1.5})
            elif price < low20*0.995:
                chart_patterns.append({"name":"20-Bar Breakdown","type":"bearish","strength":3,"source":"python","desc":f"Price broke below 20-bar low (${low20:.2f}). Support failed — watch for acceleration lower.","volConfirmed":rvol>=1.5})

        # Price above/below MA20
        if ma20[-1]:
            if C[-2] and C[-2] < ma20[-2] and price > ma20[-1]:
                chart_patterns.append({"name":"MA20 Reclaim (Bullish)","type":"bullish","strength":2,"source":"python","desc":f"Price crossed back above MA20 (${ma20[-1]:.2f}). Short-term momentum turning positive.","volConfirmed":rvol>=1.5})
            elif C[-2] and C[-2] > ma20[-2] and price < ma20[-1]:
                chart_patterns.append({"name":"MA20 Break (Bearish)","type":"bearish","strength":2,"source":"python","desc":f"Price crossed below MA20 (${ma20[-1]:.2f}). Short-term momentum turning negative.","volConfirmed":rvol>=1.5})

        all_patterns = sorted(detected + chart_patterns, key=lambda x: x["strength"], reverse=True)

        return jsonify({
            "ticker":     ticker,
            "bars":       n,
            "interval":   interval,
            "patterns":   all_patterns,
            "indicators": {
                "rsi":    rsi_val,
                "macd":   macd_val,
                "macdSig":macd_sig,
                "ma20":   ma20[-1],
                "ma50":   ma50[-1],
                "bbUpper":bb_upper,
                "bbLower":bb_lower,
                "rvol":   rvol,
                "price":  price
            },
            "source": "python"
        }), 200

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/yf-levels")
def yf_levels():
    """Return previous day H/L and premarket H/L for horizontal line overlays."""
    touch()
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        # Fetch 2 days of 1m data with pre/post market
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=2d&interval=1m&includePrePost=true"
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        result = data.get("chart", {}).get("result", [None])[0]
        if not result:
            return jsonify({"error": "No data"}), 404

        ts     = result.get("timestamp", [])
        q      = result.get("indicators", {}).get("quote", [{}])[0]
        highs  = q.get("high",  [])
        lows   = q.get("low",   [])
        opens  = q.get("open",  [])
        closes = q.get("close", [])
        meta   = result.get("meta", {})

        import datetime
        # Was: manual DST weekday-math producing a single fixed offset applied
        # to every timestamp in this batch. Replaced with the shared zoneinfo
        # helper, which gets DST exactly right per-timestamp (matters if a
        # batch of bars happens to straddle a DST transition).
        today_et = to_et(time.time()).date()

        prev_day_h = None
        prev_day_l = None
        pre_h      = None
        pre_l      = None

        for i, t2 in enumerate(ts):
            if highs[i] is None or lows[i] is None:
                continue
            dt_et = to_et(t2)
            date  = dt_et.date()
            hour  = dt_et.hour
            minute= dt_et.minute

            # Previous day regular session (9:30–16:00)
            if date < today_et and hour >= 9 and (hour > 9 or minute >= 30) and hour < 16:
                prev_day_h = max(prev_day_h, highs[i]) if prev_day_h else highs[i]
                prev_day_l = min(prev_day_l, lows[i])  if prev_day_l else lows[i]

            # Today premarket (4:00–9:30)
            if date == today_et and (hour < 9 or (hour == 9 and minute < 30)):
                pre_h = max(pre_h, highs[i]) if pre_h else highs[i]
                pre_l = min(pre_l, lows[i])  if pre_l else lows[i]

        # ── Pivot Points (from prior day H/L/C) ──────────────────────
        pivots = {}
        if prev_day_h and prev_day_l:
            # Use prior day close = last close before today's open
            prev_close = None
            for i in range(len(ts)-1, -1, -1):
                if closes[i] is None: continue
                dt_chk = to_et(ts[i])
                if dt_chk.date() < today_et:
                    prev_close = closes[i]
                    break
            if prev_close:
                pp = (prev_day_h + prev_day_l + prev_close) / 3
                pivots = {
                    "PP":  round(pp, 2),
                    "R1":  round(2*pp - prev_day_l, 2),
                    "R2":  round(pp + (prev_day_h - prev_day_l), 2),
                    "S1":  round(2*pp - prev_day_h, 2),
                    "S2":  round(pp - (prev_day_h - prev_day_l), 2),
                }

        # ── Swing Highs/Lows (local extrema in recent bars) ────────────
        swing_levels = []
        # Use today's 1m bars for swing detection
        today_h, today_l, today_c = [], [], []
        for i, t2 in enumerate(ts):
            if highs[i] is None or lows[i] is None: continue
            dt_chk = to_et(t2)
            if dt_chk.date() == today_et and dt_chk.hour >= 9 and (dt_chk.hour > 9 or dt_chk.minute >= 30):
                today_h.append(highs[i])
                today_l.append(lows[i])
                today_c.append(closes[i] if closes[i] else 0)

        # Find swing highs/lows with lookback of 5 bars each side
        lb = 5
        seen = set()
        for i in range(lb, len(today_h) - lb):
            # Swing high: higher than lb bars on each side
            if all(today_h[i] >= today_h[i-j] for j in range(1, lb+1)) and                all(today_h[i] >= today_h[i+j] for j in range(1, lb+1)):
                level = round(today_h[i], 2)
                bucket = round(level / 0.5) * 0.5  # bucket to nearest $0.50
                if bucket not in seen:
                    seen.add(bucket)
                    swing_levels.append({"price": level, "type": "resistance", "label": "R"})
            # Swing low: lower than lb bars on each side
            if all(today_l[i] <= today_l[i-j] for j in range(1, lb+1)) and                all(today_l[i] <= today_l[i+j] for j in range(1, lb+1)):
                level = round(today_l[i], 2)
                bucket = round(level / 0.5) * 0.5
                if bucket not in seen:
                    seen.add(bucket)
                    swing_levels.append({"price": level, "type": "support", "label": "S"})

        # Keep only the 5 closest levels to current price
        if today_c:
            cur = today_c[-1]
            swing_levels.sort(key=lambda x: abs(x["price"] - cur))
            swing_levels = swing_levels[:6]

        return jsonify({
            "ticker":     ticker,
            "prevDayH":   prev_day_h,
            "prevDayL":   prev_day_l,
            "preMarketH": pre_h,
            "preMarketL": pre_l,
            "pivots":     pivots,
            "swingLevels": swing_levels,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/backtest")
def backtest():
    """Given a ticker and date, return:
    - S/R levels from that date (PDH/PDL/pivots/swings)
    - Next day's 5m OHLCV data
    - Where price interacted with each level
    """
    touch()
    ticker = request.args.get("ticker","").upper()
    date   = request.args.get("date","")  # YYYY-MM-DD
    if not ticker or not date:
        return jsonify({"error":"Missing ticker or date"}), 400

    try:
        import datetime, numpy as np

        target = datetime.datetime.strptime(date, "%Y-%m-%d").date()

        headers = {"User-Agent": "Mozilla/5.0"}

        # Fetch 30 days of daily data ending on target date + 2 days
        end_dt   = target + datetime.timedelta(days=3)
        start_dt = target - datetime.timedelta(days=30)
        url_daily = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                     f"?interval=1d&period1={int(datetime.datetime.combine(start_dt,datetime.time()).timestamp())}"
                     f"&period2={int(datetime.datetime.combine(end_dt,datetime.time()).timestamp())}")
        r = requests.get(url_daily, headers=headers, timeout=10)
        daily = r.json().get("chart",{}).get("result",[None])[0]
        if not daily:
            return jsonify({"error":"No daily data"}), 404

        daily_ts = daily.get("timestamp",[])
        daily_q  = daily.get("indicators",{}).get("quote",[{}])[0]
        d_open   = daily_q.get("open",[])
        d_high   = daily_q.get("high",[])
        d_low    = daily_q.get("low",[])
        d_close  = daily_q.get("close",[])

        # Find target date bar and next trading day bar
        target_idx = None
        next_idx   = None
        for i, ts in enumerate(daily_ts):
            if d_close[i] is None:
                continue
            bar_date = datetime.datetime.utcfromtimestamp(ts).date()
            if bar_date == target:
                target_idx = i
            elif target_idx is not None and bar_date > target:
                next_idx = i
                break

        # If exact date not found, find closest trading day on or before target
        if target_idx is None:
            for i, ts in enumerate(daily_ts):
                if d_close[i] is None: continue
                bar_date = datetime.datetime.utcfromtimestamp(ts).date()
                if bar_date <= target:
                    target_idx = i
                elif target_idx is not None:
                    next_idx = i
                    break

        if target_idx is None:
            return jsonify({"error": f"No data for {date} — may be a weekend/holiday"}), 404
        if next_idx is None:
            return jsonify({"error": "No next trading day found"}), 404

        next_date = datetime.datetime.utcfromtimestamp(daily_ts[next_idx]).date()

        # ── S/R levels from target date ────────────────────────────────
        h = d_high[target_idx] or 0
        l = d_low[target_idx]   or 0
        c = d_close[target_idx] or 0
        o = d_open[target_idx]  or 0

        if not all([h, l, c, o]):
            return jsonify({"error": f"Incomplete OHLC data for {date}"}), 404

        # Standard pivot points
        pp = round((h + l + c) / 3, 2)
        r1 = round(2*pp - l, 2)
        r2 = round(pp + (h - l), 2)
        s1 = round(2*pp - h, 2)
        s2 = round(pp - (h - l), 2)

        levels = {
            "PDH": {"price": round(h, 2), "label": "PDH", "type": "resistance"},
            "PDL": {"price": round(l, 2), "label": "PDL", "type": "support"},
            "PP":  {"price": pp,          "label": "PP",  "type": "neutral"},
            "R1":  {"price": r1,          "label": "R1",  "type": "resistance"},
            "R2":  {"price": r2,          "label": "R2",  "type": "resistance"},
            "S1":  {"price": s1,          "label": "S1",  "type": "support"},
            "S2":  {"price": s2,          "label": "S2",  "type": "support"},
        }

        # Swing highs/lows from prior 10 daily bars
        swing_levels = []
        seen = set()
        lb = 3  # lookback bars each side
        start = max(0, target_idx - 20)
        end_i = target_idx
        sub_h = [v for v in d_high[start:end_i] if v is not None]
        sub_l = [v for v in d_low[start:end_i]  if v is not None]
        if len(sub_h) < lb*2+1 or len(sub_l) < lb*2+1:
            sub_h, sub_l = [], []  # not enough data for swing detection
        for i in range(lb, len(sub_h)-lb):
            if all(sub_h[i] >= sub_h[i-j] for j in range(1,lb+1)) and                all(sub_h[i] >= sub_h[i+j] for j in range(1,lb+1)):
                bucket = round(round(sub_h[i]/0.5)*0.5, 2)
                if bucket not in seen:
                    seen.add(bucket)
                    swing_levels.append({"price": round(sub_h[i],2), "label":"SwR", "type":"resistance"})
            if all(sub_l[i] <= sub_l[i-j] for j in range(1,lb+1)) and                all(sub_l[i] <= sub_l[i+j] for j in range(1,lb+1)):
                bucket = round(round(sub_l[i]/0.5)*0.5, 2)
                if bucket not in seen:
                    seen.add(bucket)
                    swing_levels.append({"price": round(sub_l[i],2), "label":"SwS", "type":"support"})

        # Keep 4 closest to target close
        swing_levels.sort(key=lambda x: abs(x["price"]-c))
        # Only keep swings within 5% of close price
        swing_levels = [s for s in swing_levels if abs(s["price"]-c)/c < 0.05][:4]
        all_levels = list(levels.values()) + swing_levels

        # ── Next day intraday 5m data ──────────────────────────────────
        # Auto-select interval: 5m for last 60 days, 1h for up to 2 years
        days_ago = (datetime.date.today() - next_date).days
        if days_ago <= 58:
            interval = "5m"
        elif days_ago <= 728:
            interval = "1h"
        else:
            return jsonify({"error": "Date too old - Yahoo Finance only supports up to 2 years of intraday data"}), 404

        # Use UTC noon timestamps to avoid timezone issues
        p1 = int(datetime.datetime(next_date.year, next_date.month, next_date.day, 12, 0, tzinfo=datetime.timezone.utc).timestamp()) - 7*86400
        p2 = int(datetime.datetime(next_date.year, next_date.month, next_date.day, 12, 0, tzinfo=datetime.timezone.utc).timestamp()) + 2*86400
        url_intra = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                     f"?interval={interval}&period1={p1}&period2={p2}&includePrePost=false")
        r2 = requests.get(url_intra, headers=headers, timeout=10)
        raw2 = r2.json() if r2.ok else {}
        result2 = (raw2.get("chart") or {}).get("result") or []
        intra = result2[0] if result2 else None
        if not intra:
            return jsonify({"error": f"No intraday data for {next_date} ({interval})"}), 404

        all_ts  = intra.get("timestamp", []) or []
        all_q   = intra.get("indicators", {}).get("quote", [{}])[0]
        opens2  = all_q.get("open",   []) or []
        highs2  = all_q.get("high",   []) or []
        lows2   = all_q.get("low",    []) or []
        closes2 = all_q.get("close",  []) or []

        # Group bars by date — try both UTC and ET (UTC-5) dates to handle timezone edge cases
        from collections import defaultdict
        bars_by_utc  = defaultdict(list)
        bars_by_et   = defaultdict(list)
        for i, ts in enumerate(all_ts):
            if i >= len(closes2) or closes2[i] is None: continue
            row = (ts,
                   opens2[i]  if i < len(opens2)  else None,
                   highs2[i]  if i < len(highs2)  else None,
                   lows2[i]   if i < len(lows2)   else None,
                   closes2[i])
            bars_by_utc[datetime.datetime.utcfromtimestamp(ts).date()].append(row)
            bars_by_et[to_et(ts).date()].append(row)  # was hardcoded -5h (EST only, wrong ~8mo/yr)

        chosen = None
        for try_date in [next_date,
                         next_date + datetime.timedelta(days=1),
                         next_date - datetime.timedelta(days=1)]:
            # Try UTC grouping first, then ET grouping
            for d_map in [bars_by_utc, bars_by_et]:
                if try_date in d_map and len(d_map[try_date]) >= 3:
                    chosen = d_map[try_date]
                    next_date = try_date
                    break
            if chosen:
                break

        if not chosen:
            all_dates = sorted(set(list(bars_by_utc.keys()) + list(bars_by_et.keys())))
            return jsonify({"error": f"No bars for {next_date}. Available: {[str(d) for d in all_dates]}"}), 404

        intra_ts = [b[0] for b in chosen]
        intra_q  = {
            "open":  [b[1] for b in chosen],
            "high":  [b[2] for b in chosen],
            "low":   [b[3] for b in chosen],
            "close": [b[4] for b in chosen],
        }

        intra_ts = intra.get("timestamp",[])
        intra_q  = intra.get("indicators",{}).get("quote",[{}])[0]
        bars = []
        o_arr = intra_q.get("open",[])   or []
        h_arr = intra_q.get("high",[])   or []
        l_arr = intra_q.get("low",[])    or []
        c_arr = intra_q.get("close",[])  or []
        v_arr = intra_q.get("volume",[]) or []
        for i, ts in enumerate(intra_ts):
            o2 = o_arr[i] if i < len(o_arr) else None
            h2 = h_arr[i] if i < len(h_arr) else None
            l2 = l_arr[i] if i < len(l_arr) else None
            c2 = c_arr[i] if i < len(c_arr) else None
            if o2 is None or c2 is None or h2 is None or l2 is None: continue
            bars.append({"ts": ts, "o": round(o2,2), "h": round(h2,2),
                         "l": round(l2,2), "c": round(c2,2)})

        # ── Detect interactions with each level ────────────────────────
        TOUCH_PCT = 0.003  # wick within 0.3% of level = "touched"
        for lvl in all_levels:
            p = lvl["price"]
            interactions = []
            last_side = None  # track which side price was on before touching
            for i, bar in enumerate(bars):
                # Wick touched the level
                wick_touched = bar["l"] <= p*(1+TOUCH_PCT) and bar["h"] >= p*(1-TOUCH_PCT)
                if not wick_touched:
                    # Update which side price is on
                    if bar["c"] > p: last_side = "above"
                    elif bar["c"] < p: last_side = "below"
                    continue

                # Determine approach direction from previous close
                prev_close = bars[i-1]["c"] if i > 0 else bar["o"]
                approaching_from_above = prev_close > p

                # KEY: classify by where candle CLOSED, not just wicked
                closed_through = (approaching_from_above and bar["c"] < p*(1-TOUCH_PCT*0.5)) or                                  (not approaching_from_above and bar["c"] > p*(1+TOUCH_PCT*0.5))

                if closed_through:
                    result = "broke"
                else:
                    result = "held"

                interactions.append({
                    "barIdx": i,
                    "ts": bar["ts"],
                    "result": result,
                    "price": round(bar["c"], 2),
                    "lvlPrice": round(p, 2),
                    "approachFrom": "above" if approaching_from_above else "below"
                })
            lvl["interactions"] = interactions

        # Detect patterns on the next-day bars
        bt_patterns = []
        try:
            if len(bars) >= 3:
                O2=[b["o"] for b in bars]; H2=[b["h"] for b in bars]
                L2=[b["l"] for b in bars]; C2=[b["c"] for b in bars]
                n2=len(C2)
                def body2(i): return abs(C2[i]-O2[i])
                def upper2(i): return H2[i]-max(C2[i],O2[i])
                def lower2(i): return min(C2[i],O2[i])-L2[i]
                def rng2(i): return H2[i]-L2[i]
                def bull2(i): return C2[i]>O2[i]
                def bear2(i): return C2[i]<O2[i]
                avg_v2 = sum(H2[j]-L2[j] for j in range(max(0,n2-20),n2))/min(20,n2) if n2>0 else 1

                for i in range(2, n2):
                    found = []
                    # Doji
                    if rng2(i)>0 and body2(i)/rng2(i)<0.1:
                        if lower2(i)>upper2(i)*2: found.append(("Dragonfly Doji","bullish"))
                        elif upper2(i)>lower2(i)*2: found.append(("Gravestone Doji","bearish"))
                        else: found.append(("Doji","neutral"))
                    # Hammer/Shooting Star
                    if body2(i)>0 and rng2(i)>0:
                        if lower2(i)>=2*body2(i) and upper2(i)<=0.5*body2(i):
                            found.append(("Hammer","bullish"))
                        if upper2(i)>=2*body2(i) and lower2(i)<=0.5*body2(i):
                            found.append(("Shooting Star","bearish"))
                    # Engulfing
                    if bull2(i-1) and bear2(i) and C2[i]<O2[i-1] and O2[i]>C2[i-1] and body2(i)>body2(i-1):
                        found.append(("Bearish Engulfing","bearish"))
                    if bear2(i-1) and bull2(i) and C2[i]>O2[i-1] and O2[i]<C2[i-1] and body2(i)>body2(i-1):
                        found.append(("Bullish Engulfing","bullish"))
                    # Morning/Evening Star
                    if i>=2:
                        if bear2(i-2) and body2(i-2)>rng2(i-2)*0.6 and body2(i-1)<body2(i-2)*0.3 and bull2(i) and body2(i)>rng2(i)*0.6:
                            found.append(("Morning Star","bullish"))
                        if bull2(i-2) and body2(i-2)>rng2(i-2)*0.6 and body2(i-1)<body2(i-2)*0.3 and bear2(i) and body2(i)>rng2(i)*0.6:
                            found.append(("Evening Star","bearish"))
                    # Marubozu
                    if rng2(i)>0 and body2(i)/rng2(i)>=0.95:
                        found.append(("Marubozu","bullish" if bull2(i) else "bearish"))

                    for name, typ in found:
                        # Score: look 5 bars forward for direction confirmation
                        fwd = 5
                        correct = None
                        if i + fwd < n2:
                            fwd_chg = C2[i+fwd] - C2[i]
                            if typ == "bullish": correct = fwd_chg > 0
                            elif typ == "bearish": correct = fwd_chg < 0
                            # neutral patterns: correct if any significant move
                        bt_patterns.append({
                            "barIdx": i,
                            "name": name,
                            "type": typ,
                            "ts": bars[i]["ts"],
                            "correct": correct,
                            "fwdChg": round(C2[i+fwd] - C2[i], 2) if i+fwd < n2 else None,
                            "fwdChgPct": round((C2[i+fwd]-C2[i])/C2[i]*100, 2) if i+fwd < n2 else None
                        })
        except Exception as pe:
            print(f"Pattern detection error: {pe}")

        return jsonify({
            "ticker":    ticker,
            "date":      str(target),
            "nextDate":  str(next_date),
            "interval":  interval,
            "levels":    all_levels,
            "bars":      bars,
            "patterns":  bt_patterns,
            "dayStats":  {
                "open":  round(o, 2),
                "high":  round(h, 2),
                "low":   round(l, 2),
                "close": round(c, 2),
                "range": round(h-l, 2)
            }
        }), 200

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/backtest-batch")
def backtest_batch():
    """Run single-day backtest logic across a date range and aggregate results."""
    touch()
    ticker    = request.args.get("ticker","SPY").upper()
    date_from = request.args.get("from","")
    date_to   = request.args.get("to","")
    if not date_from or not date_to:
        return jsonify({"error":"Missing from/to dates"}), 400

    try:
        import datetime, numpy as np
        from collections import defaultdict

        start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d").date()
        end_dt   = datetime.datetime.strptime(date_to,   "%Y-%m-%d").date()
        if (end_dt - start_dt).days > 365:
            return jsonify({"error":"Range too large — max 1 year"}), 400

        headers = {"User-Agent": "Mozilla/5.0"}

        # Fetch 1y daily data for the range
        p1 = int((datetime.datetime.combine(start_dt, datetime.time()) - datetime.timedelta(days=30)).timestamp())
        p2 = int((datetime.datetime.combine(end_dt,   datetime.time()) + datetime.timedelta(days=5)).timestamp())
        url_daily = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y"
        r = requests.get(url_daily, headers=headers, timeout=15)
        daily = (r.json().get("chart",{}).get("result") or [None])[0]
        if not daily:
            return jsonify({"error":"No daily data"}), 404

        daily_ts = daily.get("timestamp",[])
        daily_q  = daily.get("indicators",{}).get("quote",[{}])[0]
        d_open   = daily_q.get("open",[]);  d_high = daily_q.get("high",[])
        d_low    = daily_q.get("low",[]);   d_close= daily_q.get("close",[])

        # Build list of (target_idx, next_idx) pairs in range
        date_pairs = []
        for i, ts in enumerate(daily_ts):
            if d_close[i] is None: continue
            bar_date = datetime.datetime.utcfromtimestamp(ts).date()
            if bar_date < start_dt or bar_date >= end_dt: continue
            # Find next trading day
            for j in range(i+1, min(i+5, len(daily_ts))):
                if d_close[j] is not None:
                    date_pairs.append((i, j))
                    break

        if not date_pairs:
            return jsonify({"error":"No trading days found in range"}), 404

        # Aggregate results
        lvl_stats    = defaultdict(lambda: {"touched":0,"held":0,"broke":0})
        pat_stats    = defaultdict(lambda: {"total":0,"correct":0,"totalMove":0.0,"scored":0})
        days_run     = 0
        days_failed  = 0
        sample_days  = []

        TOUCH_PCT = 0.003

        for target_idx, next_idx in date_pairs:
            try:
                h = d_high[target_idx];  l = d_low[target_idx]
                c = d_close[target_idx]; o = d_open[target_idx]
                if not all([h,l,c,o]): continue
                next_date = datetime.datetime.utcfromtimestamp(daily_ts[next_idx]).date()

                # Build levels (same logic as /backtest)
                pp = (h+l+c)/3; r1=2*pp-l; r2=pp+(h-l); s1=2*pp-h; s2=pp-(h-l)
                all_levels = [
                    {"label":"PDH","price":round(h,2),"type":"resistance"},
                    {"label":"PDL","price":round(l,2),"type":"support"},
                    {"label":"PP", "price":round(pp,2),"type":"neutral"},
                    {"label":"R1", "price":round(r1,2),"type":"resistance"},
                    {"label":"R2", "price":round(r2,2),"type":"resistance"},
                    {"label":"S1", "price":round(s1,2),"type":"support"},
                    {"label":"S2", "price":round(s2,2),"type":"support"},
                ]

                # Swing levels
                lb=3; start=max(0,target_idx-20); end_i=target_idx
                sub_h=[v for v in d_high[start:end_i] if v is not None]
                sub_l=[v for v in d_low[start:end_i]  if v is not None]
                seen=set()
                if len(sub_h)>=lb*2+1:
                    for si in range(lb,len(sub_h)-lb):
                        if all(sub_h[si]>=sub_h[si-j] for j in range(1,lb+1)) and all(sub_h[si]>=sub_h[si+j] for j in range(1,lb+1)):
                            bucket=round(round(sub_h[si]/0.5)*0.5,2)
                            if bucket not in seen and abs(sub_h[si]-c)/c<0.05:
                                seen.add(bucket); all_levels.append({"label":"SwR","price":round(sub_h[si],2),"type":"resistance"})
                        if all(sub_l[si]<=sub_l[si-j] for j in range(1,lb+1)) and all(sub_l[si]<=sub_l[si+j] for j in range(1,lb+1)):
                            bucket=round(round(sub_l[si]/0.5)*0.5,2)
                            if bucket not in seen and abs(sub_l[si]-c)/c<0.05:
                                seen.add(bucket); all_levels.append({"label":"SwS","price":round(sub_l[si],2),"type":"support"})

                # Fetch next day intraday
                days_ago=(datetime.date.today()-next_date).days
                interval="5m" if days_ago<=58 else "1h" if days_ago<=728 else None
                if not interval: continue

                nd_p1=int((datetime.datetime(next_date.year,next_date.month,next_date.day,12,0,tzinfo=datetime.timezone.utc)-datetime.timedelta(days=8)).timestamp())
                nd_p2=int((datetime.datetime(next_date.year,next_date.month,next_date.day,12,0,tzinfo=datetime.timezone.utc)+datetime.timedelta(days=2)).timestamp())
                url_intra=f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&period1={nd_p1}&period2={nd_p2}&includePrePost=false"
                r2=requests.get(url_intra,headers=headers,timeout=10)
                raw2=r2.json() if r2.ok else {}
                intra=(raw2.get("chart",{}).get("result") or [None])[0]
                if not intra: days_failed+=1; continue

                all_ts2=intra.get("timestamp",[]) or []
                all_q2=intra.get("indicators",{}).get("quote",[{}])[0]
                opens2=all_q2.get("open",[]) or []; highs2=all_q2.get("high",[]) or []
                lows2=all_q2.get("low",[]) or [];   closes2=all_q2.get("close",[]) or []

                # Filter to next_date bars
                from collections import defaultdict as dd2
                bars_by_utc=dd2(list); bars_by_et=dd2(list)
                for bi,ts in enumerate(all_ts2):
                    if bi>=len(closes2) or closes2[bi] is None: continue
                    row=(ts,opens2[bi] if bi<len(opens2) else None,highs2[bi] if bi<len(highs2) else None,
                         lows2[bi] if bi<len(lows2) else None,closes2[bi])
                    bars_by_utc[datetime.datetime.utcfromtimestamp(ts).date()].append(row)
                    bars_by_et[to_et(ts).date()].append(row)  # was hardcoded -5h (EST only, wrong ~8mo/yr)

                chosen=None
                for try_date in [next_date,next_date+datetime.timedelta(days=1),next_date-datetime.timedelta(days=1)]:
                    for d_map in [bars_by_utc,bars_by_et]:
                        if try_date in d_map and len(d_map[try_date])>=3:
                            chosen=d_map[try_date]; break
                    if chosen: break
                if not chosen: days_failed+=1; continue

                bars=[]
                for row in chosen:
                    ts2,o2,h2,l2,c2=row
                    if o2 is None or c2 is None or h2 is None or l2 is None: continue
                    v2 = v_arr[i] if i < len(v_arr) else 0
                bars.append({"ts":ts2,"o":round(o2,2),"h":round(h2,2),"l":round(l2,2),"c":round(c2,2),"v":int(v2 or 0)})

                if len(bars)<3: days_failed+=1; continue
                # Calculate VWAP for each bar
                cum_pv = 0.0; cum_v = 0.0
                for bar in bars:
                    typ = (bar["h"]+bar["l"]+bar["c"])/3
                    cum_pv += typ * bar["v"]
                    cum_v  += bar["v"]
                    bar["vwap"] = round(cum_pv/cum_v, 2) if cum_v > 0 else bar["c"]

                # Score interactions (same logic as /backtest)
                CONFIRM_BARS = 2  # bars after touch to confirm hold/break
                for lvl in all_levels:
                    p=lvl["price"]; in_touch=False
                    for bi,bar in enumerate(bars):
                        wick_touched=bar["l"]<=p*(1+TOUCH_PCT) and bar["h"]>=p*(1-TOUCH_PCT)
                        if not wick_touched: in_touch=False; continue
                        if in_touch: continue
                        in_touch=True
                        prev_close=bars[bi-1]["c"] if bi>0 else bar["o"]
                        approaching_from_above=prev_close>p
                        # Multi-bar confirmation: check next CONFIRM_BARS closes
                        if bi+CONFIRM_BARS >= len(bars): continue
                        confirm_closes=[bars[bi+k]["c"] for k in range(1,CONFIRM_BARS+1)]
                        # Break: majority of confirm bars close on opposite side
                        if approaching_from_above:
                            broke=sum(1 for c2 in confirm_closes if c2<p*(1-TOUCH_PCT*0.3))>=CONFIRM_BARS//2+1
                        else:
                            broke=sum(1 for c2 in confirm_closes if c2>p*(1+TOUCH_PCT*0.3))>=CONFIRM_BARS//2+1
                        result="broke" if broke else "held"
                        key=lvl["label"]
                        lvl_stats[key]["touched"]+=1
                        lvl_stats[key]["held" if result=="held" else "broke"]+=1

                # Score patterns (same logic as /backtest)
                O2=[b["o"] for b in bars]; H2=[b["h"] for b in bars]
                L2=[b["l"] for b in bars]; C2=[b["c"] for b in bars]; n2=len(C2)
                def body2(i): return abs(C2[i]-O2[i])
                def upper2(i): return H2[i]-max(C2[i],O2[i])
                def lower2(i): return min(C2[i],O2[i])-L2[i]
                def rng2(i): return H2[i]-L2[i]
                def bull2(i): return C2[i]>O2[i]
                def bear2(i): return C2[i]<O2[i]

                for bi in range(2,n2):
                    found=[]
                    if rng2(bi)>0 and body2(bi)/rng2(bi)<0.1:
                        if lower2(bi)>upper2(bi)*2: found.append(("Dragonfly Doji","bullish"))
                        elif upper2(bi)>lower2(bi)*2: found.append(("Gravestone Doji","bearish"))
                        else: found.append(("Doji","neutral"))
                    if body2(bi)>0 and rng2(bi)>0:
                        if lower2(bi)>=2*body2(bi) and upper2(bi)<=0.5*body2(bi): found.append(("Hammer","bullish"))
                        if upper2(bi)>=2*body2(bi) and lower2(bi)<=0.5*body2(bi): found.append(("Shooting Star","bearish"))
                    if bull2(bi-1) and bear2(bi) and C2[bi]<O2[bi-1] and O2[bi]>C2[bi-1] and body2(bi)>body2(bi-1):
                        found.append(("Bearish Engulfing","bearish"))
                    if bear2(bi-1) and bull2(bi) and C2[bi]>O2[bi-1] and O2[bi]<C2[bi-1] and body2(bi)>body2(bi-1):
                        found.append(("Bullish Engulfing","bullish"))
                    if bi>=2:
                        if bear2(bi-2) and body2(bi-2)>rng2(bi-2)*0.6 and body2(bi-1)<body2(bi-2)*0.3 and bull2(bi) and body2(bi)>rng2(bi)*0.6:
                            found.append(("Morning Star","bullish"))
                        if bull2(bi-2) and body2(bi-2)>rng2(bi-2)*0.6 and body2(bi-1)<body2(bi-2)*0.3 and bear2(bi) and body2(bi)>rng2(bi)*0.6:
                            found.append(("Evening Star","bearish"))
                    if rng2(bi)>0 and body2(bi)/rng2(bi)>=0.95:
                        found.append(("Marubozu","bullish" if bull2(bi) else "bearish"))

                    for name,typ in found:
                        fwd=5
                        if bi+fwd<n2:
                            fwd_chg=C2[bi+fwd]-C2[bi]
                            correct=(fwd_chg>0) if typ=="bullish" else (fwd_chg<0) if typ=="bearish" else None
                            pat_stats[name]["total"]+=1
                            pat_stats[name]["scored"]+=1
                            if correct: pat_stats[name]["correct"]+=1
                            pat_stats[name]["totalMove"]+=abs(fwd_chg/C2[bi]*100)
                        else:
                            pat_stats[name]["total"]+=1

                days_run+=1
                if len(sample_days)<5:
                    sample_days.append(str(next_date))

            except Exception as day_err:
                days_failed+=1
                continue

        # Build level summary
        lvl_summary=[]
        for label,stats in sorted(lvl_stats.items()):
            touched=stats["touched"]; held=stats["held"]; broke=stats["broke"]
            hold_pct=round(held/touched*100) if touched else None
            lvl_summary.append({"label":label,"touched":touched,"held":held,"broke":broke,"holdPct":hold_pct})
        lvl_summary.sort(key=lambda x:["PDH","PDL","PP","R1","R2","S1","S2","SwR","SwS"].index(x["label"]) if x["label"] in ["PDH","PDL","PP","R1","R2","S1","S2","SwR","SwS"] else 99)

        # Build pattern summary
        pat_summary=[]
        for name,stats in pat_stats.items():
            acc=round(stats["correct"]/stats["scored"]*100) if stats["scored"] else None
            avgMove=round(stats["totalMove"]/stats["total"],2) if stats["total"] else 0
            pat_summary.append({"name":name,"total":stats["total"],"correct":stats["correct"],
                                 "scored":stats["scored"],"accuracy":acc,"avgMove":avgMove})
        pat_summary.sort(key=lambda x:-(x["total"]))

        return jsonify({
            "ticker":     ticker,
            "from":       date_from,
            "to":         date_to,
            "skippedDays": skipped_days,
            "daysRun":    days_run,
            "daysFailed": days_failed,
            "lvlSummary": lvl_summary,
            "patSummary": pat_summary,
            "sampleDays": sample_days
        }), 200

    except Exception as e:
        import traceback
        return jsonify({"error":str(e),"trace":traceback.format_exc()}), 500

@app.route("/paper-backtest")
def paper_backtest():
    """Simulate paper trading over a historical date range using same S/R + pattern signals."""
    touch()
    ticker         = request.args.get("ticker", "SPY").upper()
    date_from      = request.args.get("from", "")
    date_to        = request.args.get("to", "")
    mode           = request.args.get("mode", "stock")
    model_version  = request.args.get("version", "Model 1.0")
    bias_entry     = int(request.args.get("bias_entry", 2))
    bias_exit      = int(request.args.get("bias_exit", -1))
    touch_pct      = float(request.args.get("touch_pct", 0.003))
    vol_spike      = float(request.args.get("vol_spike", 2.0))
    rsi_oversold   = int(request.args.get("rsi_oversold", 30))
    rsi_overbought = int(request.args.get("rsi_overbought", 70))
    require_vol    = request.args.get("require_vol", "0") == "1"
    pat_strength   = int(request.args.get("pat_strength", 3))
    entry_from       = request.args.get("entry_from", "09:30")  # HH:MM ET
    entry_to         = request.args.get("entry_to",   "16:00")  # HH:MM ET
    max_hold_minutes   = int(request.args.get("max_hold_minutes", 0))  # 0 = no limit
    min_hold_minutes   = int(request.args.get("min_hold_minutes", 0))  # 0 = no minimum
    stop_loss_pct      = abs(float(request.args.get("stop_loss_pct", 0)))  # 0 = off; 1.0 = exit at -1% (sign-agnostic)
    strategy           = request.args.get("strategy", "bias")           # bias | rsi
    rsi_entry          = float(request.args.get("rsi_entry", 30))       # rsi strategy: buy below
    rsi_exit           = float(request.args.get("rsi_exit", 45))        # rsi strategy: sell above
    rth_only           = request.args.get("rth_only", "1") != "0"      # 09:30-15:59 ET bars only
    trend_filter       = request.args.get("trend_filter", "0") == "1"   # skip entries below the MA
    trend_ma           = int(request.args.get("trend_ma", 50))          # MA period, in 5m bars
    vwap_mode          = request.args.get("vwap_mode", "off")           # off | reversion | momentum
    disabled_signals_raw = request.args.get("disabled_signals", "")
    disabled_signals = [s.strip().lower() for s in disabled_signals_raw.split(",") if s.strip()]
    # Separate entry/exit signal filters
    disabled_entry_raw = request.args.get("disabled_entry_signals", "")
    disabled_entry_signals = [s.strip().lower() for s in disabled_entry_raw.split(",") if s.strip()]
    disabled_exit_raw = request.args.get("disabled_exit_signals", "")
    disabled_exit_signals = [s.strip().lower() for s in disabled_exit_raw.split(",") if s.strip()]
    spy_filter       = request.args.get("spy_filter", "0") == "1"
    require_combo    = request.args.get("require_combo", "0") == "1"
    spy_threshold    = float(request.args.get("spy_threshold", "-1.0"))
    if not date_from or not date_to:
        return jsonify({"error":"Missing from/to dates"}), 400

    try:
        import datetime, numpy as np
        from collections import defaultdict

        start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d").date()
        end_dt   = datetime.datetime.strptime(date_to,   "%Y-%m-%d").date()
        if (end_dt - start_dt).days > 730:
            return jsonify({"error":"Max 2 years for paper backtest"}), 400

        headers = {"User-Agent": "Mozilla/5.0"}
        INITIAL  = 100000.0
        TOUCH    = 0.003
        CONFIRM  = 2

        # Fetch daily data for S/R levels
        url_daily = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y"
        r = requests.get(url_daily, headers=headers, timeout=15)
        daily = (r.json().get("chart",{}).get("result") or [None])[0]
        if not daily:
            return jsonify({"error":"No daily data"}), 404

        daily_ts = daily.get("timestamp",[])
        daily_q  = daily.get("indicators",{}).get("quote",[{}])[0]
        d_high   = daily_q.get("high",[]);  d_low  = daily_q.get("low",[])
        d_close  = daily_q.get("close",[]); d_open = daily_q.get("open",[])

        # Build trading day list
        trade_days = []
        for i, ts in enumerate(daily_ts):
            if d_close[i] is None: continue
            bar_date = datetime.datetime.utcfromtimestamp(ts).date()
            if bar_date < start_dt or bar_date > end_dt: continue
            trade_days.append((i, bar_date))

        if not trade_days:
            return jsonify({"error":"No trading days in range"}), 404

        # State
        cash    = INITIAL
        shares  = 0
        avg_cost = 0.0
        entry_ts = None
        entry_support_price = 0
        trades  = []
        equity  = []
        last_bias = 0
        bar_vols = []

        def compute_levels(target_idx):
            h = d_high[target_idx]; l = d_low[target_idx]; c = d_close[target_idx]
            if not all([h,l,c]): return {}
            pp = (h+l+c)/3
            return {
                "PDH":pp*0, "PDL":pp*0,  # placeholders
                "prevDayH":h,"prevDayL":l,
                "pivots":{"PP":round(pp,2),"R1":round(2*pp-l,2),"R2":round(pp+(h-l),2),
                          "S1":round(2*pp-h,2),"S2":round(pp-(h-l),2)}
            }

        def compute_bias(closes, price, levels, vwap=None, volume=0, volumes=None):
            bias = 0
            n = len(closes)
            reasons = []
            RSI_OS = rsi_oversold
            RSI_OB = rsi_overbought
            TOUCH  = touch_pct
            VOL_SPIKE = vol_spike

            # Volume spike detection
            if volume and volumes and len(volumes) >= 5:
                recent_vols = volumes[-20:] if len(volumes) >= 20 else volumes
                avg_vol = sum(recent_vols) / len(recent_vols)
                if avg_vol > 0 and volume >= avg_vol * VOL_SPIKE:
                    direction = 1 if bias >= 0 else -1
                    bias += direction; reasons.append(f"Vol spike({round(volume/avg_vol,1)}x)")
            # RSI
            if n >= 15:
                gains=[]; losses=[]
                for i in range(1,n):
                    d=closes[i]-closes[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
                ag=sum(gains[-14:])/14; al=sum(losses[-14:])/14
                rsi = round(100-100/(1+ag/al),1) if al>0 else 100
                if rsi<RSI_OS: bias+=2; reasons.append(f"RSI oversold({rsi})")
                elif rsi<RSI_OS+10: bias+=1; reasons.append(f"RSI low({rsi})")
                elif rsi>RSI_OB: bias-=2; reasons.append(f"RSI overbought({rsi})")
                elif rsi>RSI_OB-10: bias-=1; reasons.append(f"RSI high({rsi})")

            # Level proximity
            if levels.get("pivots"):
                piv = levels["pivots"]
                all_lvls = [(k,v) for k,v in piv.items() if v]
                all_lvls += [("PDH",levels.get("prevDayH",0)),("PDL",levels.get("prevDayL",0))]
                above = [(l,p) for l,p in all_lvls if p and p>price]
                below = [(l,p) for l,p in all_lvls if p and p<price]
                if above:
                    nearest_res = min(above,key=lambda x:x[1])
                    if (nearest_res[1]-price)/price < TOUCH:
                        bias-=2; reasons.append(f"Near {nearest_res[0]} resistance")
                if below:
                    nearest_sup = max(below,key=lambda x:x[1])
                    if (price-nearest_sup[1])/price < TOUCH:
                        bias+=2; reasons.append(f"Near {nearest_sup[0]} support")

            # VWAP — dead until now. Two readings of the same fact:
            #   reversion: below VWAP = cheap  -> +1   (matches the rest of the model)
            #   momentum:  above VWAP = strong -> +1   (the opposite thesis)
            if vwap and vwap > 0 and vwap_mode != "off":
                vdist = (price - vwap) / vwap
                if vdist > 0.001:
                    if vwap_mode == "reversion": bias -= 1; reasons.append("Above VWAP")
                    else:                        bias += 1; reasons.append("Above VWAP")
                elif vdist < -0.001:
                    if vwap_mode == "reversion": bias += 1; reasons.append("Below VWAP")
                    else:                        bias -= 1; reasons.append("Below VWAP")

            # Candlestick patterns
            if n >= 3:
                O=closes; H=closes; L=closes; C=closes  # simplified - use close only
                i=n-1
                if n>1 and C[i]>C[i-1] and C[i-1]<C[i-2] and (C[i]-C[i-2])/C[i-2]>0.01:
                    bias+=1; reasons.append("Bullish reversal candle")
                elif n>1 and C[i]<C[i-1] and C[i-1]>C[i-2] and (C[i-2]-C[i])/C[i-2]>0.01:
                    bias-=1; reasons.append("Bearish reversal candle")

            # Filter out disabled signals
            if disabled_signals:
                filtered_reasons = []
                filtered_bias = 0
                for r in reasons:
                    r_lower = r.lower()
                    if not any(ds in r_lower for ds in disabled_signals):
                        filtered_reasons.append(r)
                # Recompute bias from remaining reasons
                for r in filtered_reasons:
                    if 'rsi oversold' in r.lower(): filtered_bias += 2
                    elif 'rsi low' in r.lower(): filtered_bias += 1
                    elif 'rsi overbought' in r.lower(): filtered_bias -= 2
                    elif 'rsi high' in r.lower(): filtered_bias -= 1
                    elif 'above vwap' in r.lower(): filtered_bias += (-1 if vwap_mode == "reversion" else 1)
                    elif 'below vwap' in r.lower(): filtered_bias += (1 if vwap_mode == "reversion" else -1)
                    elif 'vol spike' in r.lower(): filtered_bias += (1 if filtered_bias >= 0 else -1)
                    elif 'near' in r.lower() and 'resistance' in r.lower(): filtered_bias -= 2
                    elif 'near' in r.lower() and 'support' in r.lower(): filtered_bias += 2
                    elif 'bullish reversal' in r.lower(): filtered_bias += 1
                    elif 'bearish reversal' in r.lower(): filtered_bias -= 1
                return max(-3,min(3,filtered_bias)), filtered_reasons
            return max(-3,min(3,bias)), reasons

        # Pre-fetch all bars for this ticker from Supabase price_bars (batch)
        from collections import defaultdict as _dd_sb
        _sb_bbd = _dd_sb(list)
        bbd = _dd_sb(list)  # main bar dict used by day loop
        _sb_url_pb = os.environ.get("SUPABASE_URL","")
        _sb_key_pb = os.environ.get("SUPABASE_KEY","")
        print(f"price_bars prefetch: url={bool(_sb_url_pb)} key={bool(_sb_key_pb)} from={date_from} to={date_to}")

        # Pre-fetch SPY bars if spy_filter enabled and ticker is not SPY
        _spy_bbd = {}
        if spy_filter and ticker != 'SPY' and _sb_url_pb and _sb_key_pb:
            try:
                import urllib.request as _ur_spy, json as _js_spy, urllib.parse as _up_spy
                import datetime as _dt_spy
                _spy_bbd_dd = __import__('collections').defaultdict(list)
                _spy_page = 0
                while True:
                    _d_from_spy = _dt_spy.datetime(int(date_from[:4]),int(date_from[5:7]),int(date_from[8:10]),0,0,0,tzinfo=_dt_spy.timezone.utc) - _dt_spy.timedelta(days=1)
                    _d_to_spy   = _dt_spy.datetime(int(date_to[:4]),int(date_to[5:7]),int(date_to[8:10]),23,59,0,tzinfo=_dt_spy.timezone.utc) + _dt_spy.timedelta(days=1)
                    _spy_url = (f"{_sb_url_pb}/rest/v1/price_bars"
                               f"?select=close,ts,volume&ticker=eq.SPY&interval=eq.5m"
                               f"&ts=gte.{int(_d_from_spy.timestamp())}&ts=lte.{int(_d_to_spy.timestamp())}"
                               f"&order=ts.asc&limit=1000&offset={_spy_page*1000}")
                    _spy_req = _ur_spy.Request(_spy_url, headers={"apikey":_sb_key_pb,"Authorization":f"Bearer {_sb_key_pb}"})
                    _spy_rows = _js_spy.loads(_ur_spy.urlopen(_spy_req, timeout=15).read())
                    if not _spy_rows: break
                    for row in _spy_rows:
                        if not row.get("close"): continue
                        _ts2 = row["ts"]
                        _et2 = to_et(_ts2)  # was month-based DST heuristic
                        _d2 = _et2.date()
                        # Same RTH gate. The SPY filter measures "down X% from the day open"
                        # off spy_bars[0] — which was a 4am pre-market print, not the 9:30 open.
                        if rth_only:
                            _mins2 = _et2.hour * 60 + _et2.minute
                            if _mins2 < 570 or _mins2 >= 960:
                                continue
                        _spy_bbd_dd[_d2].append((row["close"], _ts2, row.get("volume",0)))
                    if len(_spy_rows) < 1000: break
                    _spy_page += 1
                _spy_bbd = dict(_spy_bbd_dd)
            except Exception as _spy_e:
                print(f"SPY filter prefetch error: {_spy_e}")
        if _sb_url_pb and _sb_key_pb and date_from and date_to:
            try:
                import urllib.request as _ur_pb, json as _js_pb, urllib.parse as _up_pb
                # Fetch all 5m bars for this ticker in date range (paginated)
                _pb_page = 0
                while True:
                    # Use ts range (UTC timestamps) for filtering
                    import datetime as _dt_range
                    print(f"  {ticker}: fetching price_bars {date_from} to {date_to}")
                    # Add 1 day buffer on each side to capture all ET market hours
                    _d_from = _dt_range.datetime(
                        int(date_from[:4]),int(date_from[5:7]),int(date_from[8:10]),
                        0,0,0,tzinfo=_dt_range.timezone.utc) - _dt_range.timedelta(days=1)
                    _d_to = _dt_range.datetime(
                        int(date_to[:4]),int(date_to[5:7]),int(date_to[8:10]),
                        23,59,0,tzinfo=_dt_range.timezone.utc) + _dt_range.timedelta(days=1)
                    _ts_from = int(_d_from.timestamp())
                    _ts_to   = int(_d_to.timestamp())
                    _pb_url = (f"{_sb_url_pb}/rest/v1/price_bars"
                               f"?select=close,ts,volume,low&ticker=eq.{_up_pb.quote(ticker)}&interval=eq.5m"
                               f"&ts=gte.{_ts_from}&ts=lte.{_ts_to}"
                               f"&order=ts.asc&limit=1000&offset={_pb_page*1000}")
                    _pb_req = _ur_pb.Request(_pb_url,
                        headers={"apikey":_sb_key_pb,"Authorization":f"Bearer {_sb_key_pb}"})
                    _pb_rows = _js_pb.loads(_ur_pb.urlopen(_pb_req, timeout=15).read())
                    if not _pb_rows: break
                    for row in _pb_rows:
                        if not row.get("close"): continue
                        import datetime as _dt_pb
                        _ts = row["ts"]
                        _et = to_et(_ts)  # was month-based DST heuristic
                        _d = _et.date()
                        # Regular trading hours only. Pre-market averages ~23k shares vs
                        # ~720k in RTH — leaving those bars in poisons avg_vol (every
                        # opening bar reads as a 30x "volume spike"), seeds RSI off
                        # overnight prints, and pushes the last bar of the day out to 8pm.
                        if rth_only:
                            _mins = _et.hour * 60 + _et.minute
                            if _mins < 570 or _mins >= 960:   # 09:30 .. 15:59 ET
                                continue
                        _sb_bbd[_d].append((row["close"], _ts, row.get("volume", 0), row.get("low")))
                    if len(_pb_rows) < 1000: break
                    _pb_page += 1
                if _sb_bbd:
                    print(f"  {ticker}: {sum(len(v) for v in _sb_bbd.values())} bars from Supabase price_bars ({len(_sb_bbd)} days)")
            except Exception as _pbe:
                print(f"  {ticker}: Supabase price_bars error: {_pbe}")

        skipped_days = []

        # Simulate day by day using 5m intraday bars
        for day_idx, (didx, bar_date) in enumerate(trade_days):
            days_ago = (datetime.date.today() - bar_date).days
            interval = "5m"  # Always use 5m from Supabase price_bars

            levels = compute_levels(max(0,didx-1))  # prior day levels

            if bar_date in _sb_bbd:
                bbd[bar_date] = _sb_bbd[bar_date]
            else:
                # No bars in Supabase for this day — skip and log
                skipped_days.append(str(bar_date))
                continue

            day_bars = bbd.get(bar_date,[])
            if len(day_bars)<3: continue
            bar_vols = []  # reset per day

            closes_today = [b[0] for b in day_bars]
            rolling = list(closes_today[:5])  # seed with first 5 bars

            for bi, bar_entry in enumerate(day_bars[5:], 5):
                price = bar_entry[0]; ts = bar_entry[1]
                bar_vol = bar_entry[2] if len(bar_entry) > 2 else 0
                bar_low = bar_entry[3] if len(bar_entry) > 3 and bar_entry[3] is not None else price
                rolling.append(price)
                vwap_val = closes_today[bi]["vwap"] if isinstance(closes_today[bi], dict) else None
                bar_vols.append(bar_vol)
                bias, reasons_list = compute_bias(rolling, price, levels, vwap_val, bar_vol, bar_vols)
                reason = " | ".join(reasons_list) if reasons_list else "neutral"

                # Apply vol confirmation if required
                if require_vol:
                    has_vol_spike = any('Vol spike' in r for r in reasons_list)
                    if not has_vol_spike and abs(bias) < 3:
                        bias = max(-1, min(1, bias))  # dampen signal without vol
                if mode == "stock":
                    is_last_bar = (bi == len(day_bars)-1)

                    # Trend filter — a veto on ENTRY only.
                    # BUG (fixed): this used to `continue`, skipping the whole bar — which
                    # also skipped the exit logic, trapping open positions until the EOD
                    # close whenever price dipped under the MA. It must gate entries and
                    # leave exits alone.
                    trend_ok = True
                    if trend_filter and len(rolling) >= 10:
                        _ma_win = rolling[-trend_ma:]
                        _ma = sum(_ma_win) / len(_ma_win)
                        trend_ok = price >= _ma
                    # SPY market filter — a veto on ENTRY only.
                    # BUG (fixed): this used to `continue`, skipping the whole bar. On a
                    # day where SPY fell through the threshold, open positions could not
                    # be exited at all — the engine stopped evaluating exits for the rest
                    # of the session. Present in every model from 1.19 onward.
                    spy_ok = True
                    if spy_filter and ticker != 'SPY':
                        spy_bars = _spy_bbd.get(bar_date, [])
                        if spy_bars and len(spy_bars) >= 2:
                            spy_open = spy_bars[0][0]
                            spy_idx = min(bi, len(spy_bars)-1)
                            spy_current = spy_bars[spy_idx][0]
                            spy_pct = (spy_current - spy_open) / spy_open * 100
                            spy_ok = spy_pct > spy_threshold

                    # Check entry time constraint — proper DST-aware ET conversion
                    bar_dt_et = to_et(ts)
                    bar_time_str = bar_dt_et.strftime("%H:%M")
                    in_entry_window = entry_from <= bar_time_str <= entry_to
                    # Filter entry signals
                    if disabled_entry_signals:
                        entry_reasons = [r for r in reasons_list if not any(ds in r.lower() for ds in disabled_entry_signals)]
                        # Recompute entry bias without disabled signals
                        _entry_bias = 0
                        for _r in entry_reasons:
                            _rl = _r.lower()
                            if 'rsi oversold' in _rl: _entry_bias += 2
                            elif 'rsi low' in _rl: _entry_bias += 1
                            elif 'rsi overbought' in _rl: _entry_bias -= 2
                            elif 'rsi high' in _rl: _entry_bias -= 1
                            elif 'above vwap' in _rl: _entry_bias -= 1
                            elif 'below vwap' in _rl: _entry_bias += 1
                            elif 'vol spike' in _rl: _entry_bias += (1 if _entry_bias >= 0 else -1)
                            elif 'near' in _rl and 'resistance' in _rl: _entry_bias -= 2
                            elif 'near' in _rl and 'support' in _rl: _entry_bias += 2
                            elif 'bullish reversal' in _rl: _entry_bias += 1
                            elif 'bearish reversal' in _rl: _entry_bias -= 1
                        entry_bias_check = max(-3, min(3, _entry_bias))
                    else:
                        entry_bias_check = bias

                    # Require at least one vol/RSI signal AND one support level signal
                    if require_combo:
                        _has_vol_rsi = any(any(s in r.lower() for s in ['vol spike','rsi oversold','rsi low']) for r in reasons_list)
                        _has_support = any(any(s in r.lower() for s in ['near pdl','near r2','near pdh support','near s1 support','near s2 support','near pp support']) for r in reasons_list)
                        _combo_ok = _has_vol_rsi and _has_support
                    else:
                        _combo_ok = True

                    # ── Minimal RSI strategy ────────────────────────────────
                    # Two parameters. No bias score, no volume, no pivots, no
                    # disabled-signal lists. Deliberately hard to overfit.
                    if strategy == "rsi":
                        # BUG (fixed): this used n_r, which is only bound inside the
                        # exit branch (`if shares > 0`). With no position open it was
                        # unbound, so the entry never evaluated.
                        _n = len(rolling)
                        _rsi_now = None
                        if _n >= 15:
                            _g=[]; _l=[]
                            for _i in range(1, _n):
                                _d = rolling[_i]-rolling[_i-1]; _g.append(max(_d,0)); _l.append(max(-_d,0))
                            _ag=sum(_g[-14:])/14; _al=sum(_l[-14:])/14
                            _rsi_now = round(100-100/(1+_ag/_al),1) if _al>0 else 100
                        _rsi_entry_ok = (_rsi_now is not None and _rsi_now < rsi_entry)
                    else:
                        _rsi_now = None
                        _rsi_entry_ok = False

                    _entry_ok = (_rsi_entry_ok if strategy == "rsi"
                                 else (entry_bias_check >= bias_entry and _combo_ok and trend_ok))

                    if _entry_ok and shares == 0 and cash > price and not is_last_bar and in_entry_window and spy_ok:
                        # Dynamic position sizing based on vol spike magnitude
                        _vol_mult = 0.0
                        for _r in reasons_list:
                            if 'vol spike' in _r.lower():
                                try:
                                    _vol_mult = float(_r.lower().split('vol spike(')[1].split('x)')[0])
                                except: pass
                                break

                        if _vol_mult >= 8.0:
                            # Skip — extreme vol spike (falling knife)
                            sh = 0
                        elif _vol_mult >= 2.0 and _vol_mult < 8.0:
                            pos_pct = 0.50  # 2-8x: increase size
                        elif _vol_mult >= 1.5 and _vol_mult < 2.0:
                            pos_pct = 0.20  # 1.5-2x: reduce size
                        else:
                            pos_pct = 0.35  # No vol spike: normal size

                        sh = int(cash * pos_pct / price) if _vol_mult < 8.0 else 0
                        if sh > 0:
                            shares = sh; avg_cost = price
                            cash -= sh * price
                            entry_ts = ts
                            # Track support level at entry for Exit 2
                            entry_support_price = 0
                            if levels.get("pivots"):
                                piv = levels["pivots"]
                                all_lvls_e = [(k,v) for k,v in piv.items() if v and v < price]
                                all_lvls_e += [("PDH",levels.get("prevDayH",0)),("PDL",levels.get("prevDayL",0))]
                                below_e = [(l,p) for l,p in all_lvls_e if p and p < price]
                                if below_e:
                                    entry_support_price = max(below_e, key=lambda x:x[1])[1]
                            buy_ts = ts  # track entry timestamp
                            import zoneinfo as _tz
                            dt = datetime.datetime.utcfromtimestamp(ts).replace(tzinfo=datetime.timezone.utc).astimezone(_tz.ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
                            _reason = f"RSI oversold({_rsi_now})" if strategy == "rsi" else reason
                            _rbias  = 3 if strategy == "rsi" else bias
                            trades.append({"type":"BUY","date":dt,"ticker":ticker,"price":round(price,2),"shares":sh,"bias":_rbias,"reason":_reason})
                    # ── Exit strategy ───────────────────────────────────────
                    exit_reason = None

                    if shares > 0:
                        held_mins = (ts - buy_ts) / 60 if buy_ts else 0
                        n_r = len(rolling)

                        # Stop loss — checked first; ignores min_hold_minutes.
                        # Models a RESTING STOP ORDER: the bar's LOW triggers it, and we fill
                        # at the stop price. If the bar GAPPED through the stop (open already
                        # below it) we can't fill at the stop — fill at the open instead.
                        exit_price = None
                        if stop_loss_pct > 0 and avg_cost > 0:
                            stop_price = avg_cost * (1 - stop_loss_pct/100.0)
                            if bar_low <= stop_price:
                                exit_price = stop_price
                                loss_pct = (exit_price/avg_cost - 1) * 100
                                exit_reason = f"Stop loss ({loss_pct:.1f}%)"

                        # RSI resolved exit — oversold condition resolved (any time)
                        _rsi_exit_lvl = rsi_exit if strategy == "rsi" else 45
                        if not exit_reason and n_r >= 15:
                            gains_e=[]; losses_e=[]
                            for _i in range(1,n_r):
                                _d=rolling[_i]-rolling[_i-1]; gains_e.append(max(_d,0)); losses_e.append(max(-_d,0))
                            ag_e=sum(gains_e[-14:])/14; al_e=sum(losses_e[-14:])/14
                            rsi_e = round(100-100/(1+ag_e/al_e),1) if al_e>0 else 100
                            if rsi_e > _rsi_exit_lvl:
                                exit_reason = f"RSI resolved({rsi_e})"

                        # Bias-based exit — the rsi strategy has no bias exit at all.
                        # It holds until RSI recovers, the stop fires, or max_hold.
                        if strategy != "rsi" and not exit_reason and bias <= bias_exit and held_mins >= min_hold_minutes:
                            if disabled_exit_signals:
                                _exit_reasons = [r for r in reasons_list if not any(ds in r.lower() for ds in disabled_exit_signals)]
                                if _exit_reasons:
                                    exit_reason = " | ".join(_exit_reasons)
                                # If all reasons are disabled, don't exit on bias
                            else:
                                exit_reason = reason

                        # Max hold backstop
                        if not exit_reason and max_hold_minutes > 0 and held_mins >= max_hold_minutes:
                            exit_reason = f"Max hold ({max_hold_minutes}m)"

                        if exit_reason:
                            fill = exit_price if exit_price is not None else price
                            pnl = round(shares*(fill-avg_cost),2)
                            cash += shares*fill
                            import zoneinfo as _tz_ex
                            dt = datetime.datetime.utcfromtimestamp(ts).replace(tzinfo=datetime.timezone.utc).astimezone(_tz_ex.ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
                            trades.append({"type":"SELL","date":dt,"ticker":ticker,"price":round(fill,2),"shares":shares,"pnl":pnl,"bias":bias,"reason":exit_reason,"hold_minutes":round(held_mins)})
                            shares = 0; avg_cost = 0; buy_ts = None; entry_ts = None; entry_support_price = 0

                last_bias = bias
                equity.append({"ts":ts,"value":round(cash+shares*price,2)})

            # End of day equity snapshot
            if day_bars:
                eod_price = day_bars[-1][0]
                # Force close all positions at end of day
                if shares > 0:
                    eod_pnl = round(shares*(eod_price-avg_cost),2)
                    cash += shares*eod_price
                    import zoneinfo as _tz_eod
                    dt_eod = datetime.datetime.utcfromtimestamp(day_bars[-1][1]).replace(tzinfo=datetime.timezone.utc).astimezone(_tz_eod.ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
                    hold_mins_eod = round((day_bars[-1][1] - buy_ts)/60) if buy_ts else None
                    trades.append({"type":"SELL","date":dt_eod,"ticker":ticker,"price":round(eod_price,2),"shares":shares,"pnl":eod_pnl,"bias":last_bias,"reason":"EOD close","hold_minutes":hold_mins_eod})
                    shares=0; avg_cost=0; buy_ts=None

                equity.append({"ts":day_bars[-1][1],"value":round(cash+shares*eod_price,2),"eod":True})

        # Close any open position at end
        if shares > 0 and trade_days:
            last_price = d_close[trade_days[-1][0]] or avg_cost
            pnl = round(shares*(last_price-avg_cost),2)
            cash += shares*last_price
            trades.append({"type":"CLOSE","date":str(trade_days[-1][1]),"ticker":ticker,"price":round(last_price,2),"shares":shares,"pnl":pnl,"bias":last_bias,"avgCost":round(avg_cost,2),"reason":f"End of backtest period (avg cost ${avg_cost:.2f})"})
            shares = 0

        final_value = cash
        total_pnl   = round(final_value - INITIAL, 2)
        win_trades  = [t for t in trades if t.get("pnl",0)>0]
        lose_trades = [t for t in trades if t.get("pnl",0)<0]

        # Save to backtest_trades Supabase table
        _sb_url = os.environ.get("SUPABASE_URL","")
        _sb_key = os.environ.get("SUPABASE_KEY","")
        print(f"Supabase: url={bool(_sb_url)}, key={bool(_sb_key)}, trades={len([t for t in trades if t.get('pnl') is not None])}")
        if _sb_url and _sb_key:
            try:
                import json as _json, urllib.request as _ur

                def _sb_request(method, path, data=None, params=""):
                    url = f"{_sb_url}/rest/v1/{path}?{params}"
                    req = _ur.Request(url, data=_json.dumps(data).encode() if data else None,
                        headers={"apikey":_sb_key,"Authorization":f"Bearer {_sb_key}",
                                 "Content-Type":"application/json","Prefer":"return=minimal"},
                        method=method)
                    try: _ur.urlopen(req, timeout=10)
                    except Exception as e: print(f"Supabase {method} error: {e}")

                # Delete overlapping trades for same ticker+version+date range
                from urllib.parse import quote
                _del_params = (
                    f"ticker=eq.{quote(ticker)}"
                    f"&version=eq.{quote(model_version)}"
                    f"&created_at=gte.{date_from}"
                    f"&created_at=lte.{date_to}%2023%3A59"
                )
                _sb_request("DELETE", "backtest_trades", params=_del_params)
                print(f"Deleted overlapping trades for {ticker} {model_version} {date_from}→{date_to}")

                # Insert new trades
                _run_at = __import__("datetime").datetime.now(
                              __import__("datetime").timezone.utc).isoformat()
                _rows = [{"ticker":t["ticker"],"type":t["type"],"price":t["price"],
                          "shares":t.get("shares"),"pnl":t.get("pnl"),"reason":t.get("reason"),
                          "bias":t.get("bias"),"mode":mode,"version":model_version,
                          "hold_minutes":t.get("hold_minutes"),
                          "engine_version":ENGINE_VERSION,"run_at":_run_at,
                          "created_at":t.get("date","")} for t in trades]  # save ALL trades incl buys
                if _rows:
                    _sb_request("POST", "backtest_trades", data=_rows)
                    print(f"Saved {len(_rows)} trades → backtest_trades ({ticker} {model_version})")
            except Exception as _se:
                import traceback
                print(f"Supabase save error: {_se}")
                print(traceback.format_exc())

        return jsonify({
            "ticker":     ticker,
            "from":       date_from,
            "to":         date_to,
            "skippedDays": skipped_days,
            "mode":       mode,
            "initial":    INITIAL,
            "finalValue": round(final_value,2),
            "totalPnl":   total_pnl,
            "pnlPct":     round(total_pnl/INITIAL*100,2),
            "trades":     trades,
            # One point per day — use EOD snapshots only
            "equity": [e for e in equity if e.get("eod")] or equity[::max(1,len(equity)//60)],
            "stats": {
                "totalTrades": len(trades),
                "wins":        len(win_trades),
                "losses":      len(lose_trades),
                "winRate":     round(len(win_trades)/max(1,len(win_trades)+len(lose_trades))*100,1),
                "avgWin":      round(sum(t["pnl"] for t in win_trades)/max(1,len(win_trades)),2),
                "avgLoss":     round(sum(t["pnl"] for t in lose_trades)/max(1,len(lose_trades)),2),
                "bestTrade":   max((t.get("pnl",0) for t in trades),default=0),
                "worstTrade":  min((t.get("pnl",0) for t in trades),default=0),
            }
        }), 200

    except Exception as e:
        import traceback
        return jsonify({"error":str(e),"trace":traceback.format_exc()}), 500

@app.route("/paper-backtest-multi")
def paper_backtest_multi():
    """Run paper backtest across multiple tickers and return combined results."""
    touch()
    tickers_raw = request.args.get("tickers", "SPY,QQQ,NVDA")
    tickers     = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()][:10]
    date_from   = request.args.get("from", "")
    date_to     = request.args.get("to", "")
    mode           = request.args.get("mode", "stock")
    model_version  = request.args.get("version", "")
    bias_entry     = request.args.get("bias_entry", "2")
    bias_exit      = request.args.get("bias_exit", "-1")
    touch_pct      = request.args.get("touch_pct", "0.003")
    vol_spike      = request.args.get("vol_spike", "2.0")
    rsi_oversold   = request.args.get("rsi_oversold", "30")
    rsi_overbought = request.args.get("rsi_overbought", "70")
    require_vol    = request.args.get("require_vol", "0")
    pat_strength   = request.args.get("pat_strength", "3")
    entry_from       = request.args.get("entry_from", "09:30")
    entry_to         = request.args.get("entry_to", "16:00")
    max_hold_minutes   = request.args.get("max_hold_minutes", "0")
    min_hold_minutes   = request.args.get("min_hold_minutes", "0")
    stop_loss_pct      = request.args.get("stop_loss_pct", "0")
    strategy           = request.args.get("strategy", "bias")
    rsi_entry          = request.args.get("rsi_entry", "30")
    rsi_exit           = request.args.get("rsi_exit", "45")
    rth_only           = request.args.get("rth_only", "1")
    trend_filter       = request.args.get("trend_filter", "0")
    trend_ma           = request.args.get("trend_ma", "50")
    vwap_mode          = request.args.get("vwap_mode", "off")
    disabled_signals   = request.args.get("disabled_signals", "")
    spy_filter_str     = request.args.get("spy_filter", "0")
    require_combo_str  = request.args.get("require_combo", "0")
    disabled_entry_str = request.args.get("disabled_entry_signals", "")
    disabled_exit_str  = request.args.get("disabled_exit_signals", "")
    spy_threshold_str  = request.args.get("spy_threshold", "-1.0")
    if not date_from or not date_to:
        return jsonify({"error":"Missing from/to dates"}), 400

    try:
        import datetime, requests as req2

        all_trades   = []
        ticker_stats = []

        for ticker in tickers:
            try:
                # Reuse paper-backtest logic by calling it internally
                r = req2.get(
                    f"http://localhost:8080/paper-backtest",
                    params={"ticker":ticker,"from":date_from,"to":date_to,"mode":mode,
                            "version":model_version,"bias_entry":bias_entry,"bias_exit":bias_exit,
                            "touch_pct":touch_pct,"vol_spike":vol_spike,"rsi_oversold":rsi_oversold,
                            "rsi_overbought":rsi_overbought,"require_vol":require_vol,
                            "pat_strength":pat_strength,"entry_from":entry_from,"entry_to":entry_to,
                            "max_hold_minutes":max_hold_minutes,
                            "min_hold_minutes":min_hold_minutes,
                            "stop_loss_pct":stop_loss_pct,
                            "strategy":strategy,
                            "rsi_entry":rsi_entry,
                            "rsi_exit":rsi_exit,
                            "rth_only":rth_only,
                            "trend_filter":trend_filter,
                            "trend_ma":trend_ma,
                            "vwap_mode":vwap_mode,
                            "disabled_signals":disabled_signals,
                            "spy_filter":spy_filter_str,
                            "require_combo":require_combo_str,
                            "disabled_entry_signals":disabled_entry_str,
                            "disabled_exit_signals":disabled_exit_str,
                            "spy_threshold":spy_threshold_str},
                    timeout=60
                )
                d = r.json()
                if d.get("error"): 
                    ticker_stats.append({"ticker":ticker,"error":d["error"]})
                    continue

                # Tag trades with ticker
                for t in (d.get("trades") or []):
                    t["ticker"] = ticker
                    all_trades.append(t)

                ticker_stats.append({
                    "ticker":     ticker,
                    "finalValue": d.get("finalValue"),
                    "totalPnl":   d.get("totalPnl"),
                    "pnlPct":     d.get("pnlPct"),
                    "stats":      d.get("stats",{})
                })
            except Exception as te:
                ticker_stats.append({"ticker":ticker,"error":str(te)})

        # Aggregate trigger analysis
        from collections import defaultdict
        trigger_stats = defaultdict(lambda:{"total":0,"wins":0,"losses":0,"totalPnl":0.0})
        hour_stats    = defaultdict(lambda:{"total":0,"wins":0,"losses":0,"totalPnl":0.0})
        bias_stats    = defaultdict(lambda:{"total":0,"wins":0,"losses":0,"totalPnl":0.0})

        for t in all_trades:
            if t.get("pnl") is None: continue
            pnl  = t["pnl"]
            won  = pnl > 0
            reason = t.get("reason","") or ""
            bias   = t.get("bias",0) or 0

            # Trigger breakdown
            for kw in ["RSI oversold","RSI low","RSI high","RSI overbought",
                       "support","resistance","Bullish reversal","Bearish reversal"]:
                if kw.lower() in reason.lower():
                    trigger_stats[kw]["total"]   += 1
                    trigger_stats[kw]["wins"]     += 1 if won else 0
                    trigger_stats[kw]["losses"]   += 0 if won else 1
                    trigger_stats[kw]["totalPnl"] += pnl

            # Hour of day
            try:
                dt = datetime.datetime.strptime(t["date"], "%Y-%m-%d %H:%M")
                # Convert UTC to ET (UTC-5 standard, UTC-4 daylight)
                import zoneinfo
                dt_utc = dt.replace(tzinfo=datetime.timezone.utc)
                dt_et  = dt_utc.astimezone(zoneinfo.ZoneInfo("America/New_York"))
                hour   = dt_et.hour
                hour_stats[hour]["total"]   += 1
                hour_stats[hour]["wins"]     += 1 if won else 0
                hour_stats[hour]["losses"]   += 0 if won else 1
                hour_stats[hour]["totalPnl"] += pnl
            except: pass

            # Bias level
            bias_stats[bias]["total"]   += 1
            bias_stats[bias]["wins"]     += 1 if won else 0
            bias_stats[bias]["losses"]   += 0 if won else 1
            bias_stats[bias]["totalPnl"] += pnl

        # Format trigger analysis
        trigger_analysis = []
        for kw, s in sorted(trigger_stats.items(), key=lambda x:-x[1]["total"]):
            if s["total"]==0: continue
            trigger_analysis.append({
                "trigger":  kw,
                "total":    s["total"],
                "wins":     s["wins"],
                "losses":   s["losses"],
                "winRate":  round(s["wins"]/s["total"]*100,1),
                "avgPnl":   round(s["totalPnl"]/s["total"],2),
                "totalPnl": round(s["totalPnl"],2)
            })

        hour_analysis = []
        for hour in sorted(hour_stats.keys()):
            s = hour_stats[hour]
            if s["total"]==0: continue
            hour_analysis.append({
                "hour":    hour,
                "label":   f"{hour%12 or 12}{'am' if hour<12 else 'pm'}",
                "total":   s["total"],
                "wins":    s["wins"],
                "winRate": round(s["wins"]/s["total"]*100,1),
                "avgPnl":  round(s["totalPnl"]/s["total"],2)
            })

        bias_analysis = []
        for bias in sorted(bias_stats.keys()):
            s = bias_stats[bias]
            if s["total"]==0: continue
            bias_analysis.append({
                "bias":    bias,
                "total":   s["total"],
                "wins":    s["wins"],
                "winRate": round(s["wins"]/s["total"]*100,1),
                "avgPnl":  round(s["totalPnl"]/s["total"],2)
            })

        total_pnl    = sum(t.get("pnl",0) or 0 for t in all_trades)
        scored       = [t for t in all_trades if t.get("pnl") is not None]
        win_trades   = [t for t in scored if t["pnl"]>0]

        return jsonify({
            "tickers":          tickers,
            "from":             date_from,
            "to":               date_to,
            "mode":             mode,
            "totalTrades":      len(all_trades),
            "totalPnl":         round(total_pnl,2),
            "winRate":          round(len(win_trades)/max(1,len(scored))*100,1),
            "tickerStats":      ticker_stats,
            "triggerAnalysis":  trigger_analysis,
            "hourAnalysis":     hour_analysis,
            "biasAnalysis":     bias_analysis,
            "trades":           all_trades
        }), 200

    except Exception as e:
        import traceback
        return jsonify({"error":str(e),"trace":traceback.format_exc()}), 500

@app.route("/health")
def health():
    touch()
    idle = (time.time() - last_activity) / 60
    remaining = max(0, TIMEOUT_MINUTES - idle)
    return jsonify({
        "status": "ok",
        "message": "Trading server running",
        "idle_minutes": round(idle, 1),
        "timeout_minutes": TIMEOUT_MINUTES,
        "remaining_minutes": round(remaining, 1)
    }), 200

@app.route("/ping")
def ping():
    touch()
    idle = (time.time() - last_activity) / 60
    remaining = max(0, TIMEOUT_MINUTES - idle)
    return jsonify({
        "status": "ok",
        "idle_minutes": round(idle, 1),
        "remaining_minutes": round(remaining, 1),
        "timeout_minutes": TIMEOUT_MINUTES
    }), 200

if __name__ == "__main__":
    print("\n✅  Trading dashboard server running")
    print("   Open in Chrome: http://localhost:8080")
    print("   Keep this Terminal window open.\n")
    app.run(host="0.0.0.0", port=8080, debug=False)
