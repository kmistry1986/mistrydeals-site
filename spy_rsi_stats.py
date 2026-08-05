#!/usr/bin/env python3
"""
What percentage of trading days does SPY's RSI drop below 45 / 40 / 30?

Uses RTH-only 5m bars from price_bars (the same source and the same RSI
formula the trading engine itself uses -- a rolling 14-bar average of
gains/losses recomputed fresh each bar, NOT textbook Wilder smoothing).
That's a deliberate choice: the point is to know how often the engine's
own "RSI oversold" signal COULD have fired, not to report a generic
textbook RSI that the model doesn't actually compute.

The rolling window resets at the start of every trading day, matching how
the backtest engine works -- so the first ~14 bars of each day (the first
~70 minutes) have no RSI value yet, same blind spot the real model has.

Run this directly on the VM (it already has network access to Supabase):
    python3 spy_rsi_stats.py
"""

import urllib.request as _ur
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SUPA_URL = "https://qolksrytidvxarrlygyy.supabase.co"
SUPA_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
            "FvbGtzcnl0aWR2eGFycmx5Z3l5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjI3MzAyNDcs"
            "ImV4cCI6MjA3ODMwNjI0N30.126SOE0Mi6tB2ywtDzOYPUzqZ5cUl6Sk5QXgUGjMR0g")

ET = ZoneInfo("America/New_York")


def fetch_all_spy_bars():
    """Page through every 5m SPY bar in price_bars, ordered by ts.

    Supabase's PostgREST layer caps every response at 1000 rows regardless
    of what `limit` is requested -- passing limit=5000 does NOT get you
    5000 rows back, it silently clamps to 1000. page_size below matches
    that real cap, so the "did we get a full page?" stop condition actually
    works. (This is the same 1000-row cap server.py's own queries already
    page around elsewhere in this codebase.)
    """
    rows = []
    page_size = 1000
    offset = 0
    while True:
        q = (f"/rest/v1/price_bars?select=ts,close&ticker=eq.SPY&interval=eq.5m"
             f"&order=ts.asc&limit={page_size}&offset={offset}")
        req = _ur.Request(SUPA_URL + q, headers={
            "apikey": SUPA_KEY,
            "Authorization": f"Bearer {SUPA_KEY}",
        })
        batch = json.loads(_ur.urlopen(req, timeout=30).read())
        if not batch:
            break
        rows.extend(batch)
        print(f"  fetched {len(rows)} bars so far...")
        offset += page_size
        if len(batch) < page_size:
            break
    return rows


def to_et(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)


def is_rth(dt):
    mins = dt.hour * 60 + dt.minute
    return 570 <= mins < 960  # 09:30 .. 15:59 ET


def compute_rsi(closes):
    """Same formula the trading engine uses. None until 15 closes exist."""
    n = len(closes)
    if n < 15:
        return None
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-14:]) / 14
    al = sum(losses[-14:]) / 14
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def find_episodes(rsi_series):
    """rsi_series: list of RSI values (or None) for one trading day, in
    bar order. An "oversold episode" starts the first bar RSI drops below
    30 and ends the first bar it reaches >=50 (recovery) -- or the day
    ends first (non-recovery). Episodes don\'t span across days, matching
    the engine\'s own daily reset; two separate dips in one day count as
    two episodes, not one.
    """
    episodes = []
    in_episode = False
    start_bar = None
    for bar_idx, r in enumerate(rsi_series):
        if r is None:
            continue
        if not in_episode and r < 30:
            in_episode = True
            start_bar = bar_idx
        elif in_episode and r >= 50:
            episodes.append({"recovered": True, "bars_to_recover": bar_idx - start_bar})
            in_episode = False
    if in_episode:
        episodes.append({"recovered": False, "bars_to_recover": None})
    return episodes


def main():
    print("Fetching SPY 5m bars from Supabase...")
    raw = fetch_all_spy_bars()
    print(f"  {len(raw)} total bars fetched")

    days = {}
    for row in raw:
        dt = to_et(row["ts"])
        if not is_rth(dt):
            continue
        d = dt.date()
        days.setdefault(d, []).append(row["close"])

    print(f"  {len(days)} trading days with RTH data")

    total_days = 0
    below_45 = below_40 = below_30 = 0
    days_with_no_rsi = 0
    all_episodes = []

    for d in sorted(days.keys()):
        closes = days[d]
        rolling = list(closes[:5])
        min_rsi_today = None
        rsi_series = []

        for c in closes[5:]:
            rolling.append(c)
            r = compute_rsi(rolling)
            rsi_series.append(r)
            if r is not None:
                if min_rsi_today is None or r < min_rsi_today:
                    min_rsi_today = r

        total_days += 1
        if min_rsi_today is None:
            days_with_no_rsi += 1
            continue
        if min_rsi_today < 45:
            below_45 += 1
        if min_rsi_today < 40:
            below_40 += 1
        if min_rsi_today < 30:
            below_30 += 1

        all_episodes.extend(find_episodes(rsi_series))

    usable_days = total_days - days_with_no_rsi
    print()
    print(f"Total trading days examined: {total_days}")
    print(f"Days too short to compute RSI at all: {days_with_no_rsi}")
    print(f"Usable days: {usable_days}")
    print()
    print(f"RSI dropped below 45 on {below_45} days  ({below_45/usable_days*100:.1f}% of usable days)")
    print(f"RSI dropped below 40 on {below_40} days  ({below_40/usable_days*100:.1f}% of usable days)")
    print(f"RSI dropped below 30 on {below_30} days  ({below_30/usable_days*100:.1f}% of usable days)")

    print()
    print("--- RSI < 30 recovery to RSI >= 50 (same trading day only) ---")
    n = len(all_episodes)
    recovered = [e for e in all_episodes if e["recovered"]]
    not_recovered = n - len(recovered)
    print(f"Total oversold episodes: {n}")
    if n == 0:
        return
    print(f"Recovered to >=50 same day: {len(recovered)}  ({len(recovered)/n*100:.1f}%)")
    print(f"Never recovered that day:   {not_recovered}  ({not_recovered/n*100:.1f}%)")

    if recovered:
        mins = sorted(e["bars_to_recover"] * 5 for e in recovered)
        m = len(mins)
        median = mins[m//2] if m % 2 else (mins[m//2 - 1] + mins[m//2]) / 2
        mean = sum(mins) / m
        print()
        print(f"Of episodes that DID recover:")
        print(f"  Mean time to recover:   {mean:.0f} min")
        print(f"  Median time to recover: {median:.0f} min")
        print(f"  Fastest:  {mins[0]} min")
        print(f"  Slowest:  {mins[-1]} min")

        buckets = {"<15m": 0, "15-30m": 0, "30-60m": 0, "60-120m": 0, ">120m": 0}
        for mn in mins:
            if mn < 15: buckets["<15m"] += 1
            elif mn < 30: buckets["15-30m"] += 1
            elif mn < 60: buckets["30-60m"] += 1
            elif mn < 120: buckets["60-120m"] += 1
            else: buckets[">120m"] += 1
        print()
        print("  Recovery time distribution:")
        for label, count in buckets.items():
            print(f"    {label:8s} {count:5d}  ({count/m*100:.1f}%)")


if __name__ == "__main__":
    main()
