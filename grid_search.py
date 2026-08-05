#!/usr/bin/env python3
"""
Grid search for optimal model constraints.
Tests combinations on AMD (best) and TSLA (worst) from Model 1.12.
Run on GCP: python3 ~/trading/grid_search.py
"""
import urllib.request, json, time, itertools
from datetime import datetime

BASE_URL = "http://localhost:8080/paper-backtest"
TICKERS = ["AMD", "TSLA"]
DATE_FROM = "2024-07-01"
DATE_TO   = "2025-01-31"  # Batch 1 only for speed

# Fixed constraints (from Model 1.12)
FIXED = {
    "mode":             "stock",
    "bias_entry":       3,
    "touch_pct":        0.003,
    "vol_spike":        1.5,
    "rsi_oversold":     30,
    "rsi_overbought":   70,
    "require_vol":      1,
    "pat_strength":     3,
    "entry_from":       "09:30",
    "entry_to":         "14:00",
    "spy_filter":       1,
    "disabled_signals": "RSI high,Near PP,Near R1,Near S1,Near S2,Near PDH"
}

# Grid to search
GRID = {
    "bias_entry":       [2, 3],
    "max_hold_minutes": [15, 30, 45, 60, 90],
    "spy_threshold":    [-1.0, -1.5, -2.0],
    "require_combo":    [0, 1],
    "bias_exit":        [-1, 0, 1],
}

def build_url(ticker, params):
    args = {**FIXED, **params, "ticker": ticker, "from": DATE_FROM, "to": DATE_TO}
    qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k,v in args.items())
    return f"{BASE_URL}?{qs}"

def run_test(ticker, params):
    url = build_url(ticker, params)
    try:
        resp = urllib.request.urlopen(url, timeout=30)
        d = json.loads(resp.read())
        s = d.get("stats", {})
        return {
            "trades":   s.get("totalTrades", 0),
            "wr":       s.get("winRate", 0),
            "pnl":      round(d.get("totalPnl", 0), 0),
            "error":    d.get("error", "")
        }
    except Exception as e:
        return {"trades": 0, "wr": 0, "pnl": 0, "error": str(e)}

def main():
    keys = list(GRID.keys())
    values = list(GRID.values())
    combos = list(itertools.product(*values))
    total = len(combos) * len(TICKERS)

    print(f"Grid search: {len(combos)} combinations × {len(TICKERS)} tickers = {total} tests")
    print(f"Date range: {DATE_FROM} to {DATE_TO}")
    print(f"Tickers: {', '.join(TICKERS)}")
    print("-" * 80)

    results = []
    n = 0
    for combo in combos:
        params = dict(zip(keys, combo))
        combo_results = {}
        total_pnl = 0
        total_trades = 0

        for ticker in TICKERS:
            n += 1
            r = run_test(ticker, params)
            combo_results[ticker] = r
            total_pnl += r["pnl"]
            total_trades += r["trades"]
            print(f"[{n}/{total}] {ticker} | hold:{params['max_hold_minutes']}m spy:{params['spy_threshold']}% combo:{params['require_combo']} exit:{params['bias_exit']} → trades:{r['trades']} wr:{r['wr']}% pnl:${r['pnl']}")

        results.append({
            "params":       params,
            "total_pnl":    total_pnl,
            "total_trades": total_trades,
            "amd_pnl":      combo_results.get("AMD", {}).get("pnl", 0),
            "tsla_pnl":     combo_results.get("TSLA", {}).get("pnl", 0),
            "amd_wr":       combo_results.get("AMD", {}).get("wr", 0),
            "tsla_wr":      combo_results.get("TSLA", {}).get("wr", 0),
        })

    # Sort by combined P&L
    results.sort(key=lambda x: x["total_pnl"], reverse=True)

    print("\n" + "="*80)
    print("TOP 10 RESULTS (sorted by combined AMD+TSLA P&L)")
    print("="*80)
    print(f"{'hold':>5} {'spy%':>6} {'combo':>6} {'exit':>5} | {'AMD P&L':>10} {'TSLA P&L':>10} {'TOTAL':>10} | {'AMD WR':>7} {'TSLA WR':>7}")
    print("-"*80)
    for r in results[:10]:
        p = r["params"]
        print(f"{p['max_hold_minutes']:>5}m {p['spy_threshold']:>6}% {p['require_combo']:>6} {p['bias_exit']:>5} | "
              f"${r['amd_pnl']:>9,.0f} ${r['tsla_pnl']:>9,.0f} ${r['total_pnl']:>9,.0f} | "
              f"{r['amd_wr']:>6.1f}% {r['tsla_wr']:>6.1f}%")

    print("\nBOTTOM 5 RESULTS")
    print("-"*80)
    for r in results[-5:]:
        p = r["params"]
        print(f"{p['max_hold_minutes']:>5}m {p['spy_threshold']:>6}% {p['require_combo']:>6} {p['bias_exit']:>5} | "
              f"${r['amd_pnl']:>9,.0f} ${r['tsla_pnl']:>9,.0f} ${r['total_pnl']:>9,.0f} | "
              f"{r['amd_wr']:>6.1f}% {r['tsla_wr']:>6.1f}%")

    # Save full results to JSON
    with open("/home/kushal_b_mistry/trading/grid_search_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull results saved to ~/trading/grid_search_results.json")
    print(f"Completed at {datetime.now().strftime('%H:%M:%S')}")

if __name__ == "__main__":
    import sys
    log_path = "/home/kushal_b_mistry/trading/grid_search_output.log"
    with open(log_path, "w") as log:
        class Tee:
            def __init__(self, *files): self.files = files
            def write(self, obj):
                for f in self.files: f.write(obj); f.flush()
            def flush(self):
                for f in self.files: f.flush()
        sys.stdout = Tee(sys.stdout, log)
        main()
