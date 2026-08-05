#!/usr/bin/env python3
"""
Daily 5m bar downloader — run via cron or manually.
Downloads last 7 days of 5m bars per ticker and upserts into Supabase price_bars.
Run daily after market close (e.g. 5pm ET).
"""
import os, json, datetime, time, requests, urllib.request, urllib.parse

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TICKERS = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","QQQ","SPY","AMD",
    "SNOW","PLTR","CRWD","AVGO","UBER","COIN"
]
INTERVAL = "5m"
HEADERS = {"User-Agent": "Mozilla/5.0"}

def sb_upsert(rows):
    """Upsert rows into price_bars."""
    url = f"{SUPABASE_URL}/rest/v1/price_bars"
    req = urllib.request.Request(
        url + "?on_conflict=ticker,interval,ts",
        data=json.dumps(rows).encode(),
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"
        },
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        print(f"  Supabase error: {e}")
        return False

def fetch_bars(ticker, days=7):
    """Fetch 5m bars from Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={INTERVAL}&range={days}d"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        result = r.json().get("chart", {}).get("result", [None])[0]
        if not result:
            return []
        timestamps = result.get("timestamp", [])
        quotes = result.get("indicators", {}).get("quote", [{}])[0]
        opens   = quotes.get("open",   [])
        highs   = quotes.get("high",   [])
        lows    = quotes.get("low",    [])
        closes  = quotes.get("close",  [])
        volumes = quotes.get("volume", [])
        rows = []
        for i, ts in enumerate(timestamps):
            if closes[i] is None: continue
            dt = (datetime.datetime.utcfromtimestamp(ts)
                  - datetime.timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
            rows.append({
                "ticker": ticker, "interval": INTERVAL, "ts": ts, "dt": dt,
                "open":   round(opens[i] or 0, 4),
                "high":   round(highs[i] or 0, 4),
                "low":    round(lows[i] or 0, 4),
                "close":  round(closes[i] or 0, 4),
                "volume": int(volumes[i] or 0)
            })
        return rows
    except Exception as e:
        print(f"  Yahoo error for {ticker}: {e}")
        return []

def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set")
        return

    print(f"Bar downloader starting — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Tickers: {', '.join(TICKERS)}")

    total_saved = 0
    for ticker in TICKERS:
        print(f"  Fetching {ticker}...", end=" ", flush=True)
        bars = fetch_bars(ticker, days=7)
        if not bars:
            print("no data")
            continue
        # Upsert in batches of 500
        saved = 0
        for i in range(0, len(bars), 500):
            if sb_upsert(bars[i:i+500]):
                saved += len(bars[i:i+500])
        print(f"{saved} bars saved")
        total_saved += saved
        time.sleep(1)  # rate limit

    print(f"\nDone — {total_saved} total bars saved")

if __name__ == "__main__":
    main()
