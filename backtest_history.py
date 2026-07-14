#!/usr/bin/env python3
"""
MarketPulse — Historical Price Exporter for True Backtesting
=============================================================
Fetches ~5 years of daily adjusted closes for current S&P 500 members
(plus SPY for regime classification) and writes a compact, delta-encoded
backtest_prices.json that the dashboard's Backtest tab loads lazily.

Encoding: prices are stored in integer cents. For each ticker:
    {"o": <start index into dates array>, "p": [first_price_cents, delta, delta, ...]}
Decoded price[i] = (p[0] + p[1] + ... + p[i]) / 100.

Output size: ~4-5 MB raw (~1.2 MB gzipped over GitHub Pages).

Run weekly via GitHub Actions (.github/workflows/backtest_history.yml).
"""

import datetime
import io
import json
import math
import os
import sys
import time

import requests

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_prices.json")
YEARS = 5
BATCH = 100          # tickers per yfinance download call
MIN_DAYS = 300       # skip tickers with less history than this

HEADERS = {"User-Agent": "Mozilla/5.0"}


def check_deps():
    missing = []
    for pkg in ["yfinance", "pandas", "numpy"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        os.system(f"{sys.executable} -m pip install {' '.join(missing)} --quiet")


check_deps()
import numpy as np
import pandas as pd
import yfinance as yf


def fetch_sp500():
    """Current S&P 500 members + sectors from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_html(io.StringIO(r.text))[0]
    tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
    sectors = dict(zip(df["Symbol"].str.replace(".", "-", regex=False), df["GICS Sector"]))
    names = dict(zip(df["Symbol"].str.replace(".", "-", regex=False), df["Security"]))
    return tickers, sectors, names


def encode_series(series, date_index):
    """Delta-encode a price series (in cents) aligned to the master date index."""
    s = series.reindex(date_index)
    first = s.first_valid_index()
    if first is None:
        return None
    offset = date_index.get_loc(first)
    vals = s.loc[first:].ffill()
    if len(vals) < MIN_DAYS:
        return None
    cents = (vals * 100).round().astype("int64").tolist()
    deltas = [cents[0]] + [cents[i] - cents[i - 1] for i in range(1, len(cents))]
    return {"o": int(offset), "p": deltas}


def main():
    print("═" * 60)
    print("  MarketPulse — Backtest Price History Export")
    print("═" * 60)

    tickers, sectors, names = fetch_sp500()
    print(f"  Universe: {len(tickers)} S&P 500 members + SPY")

    start = (datetime.date.today() - datetime.timedelta(days=int(YEARS * 365.25) + 10)).isoformat()
    all_tickers = tickers + ["SPY"]

    frames = []
    for i in range(0, len(all_tickers), BATCH):
        batch = all_tickers[i:i + BATCH]
        print(f"  Downloading {i + 1}-{i + len(batch)} of {len(all_tickers)}…")
        for attempt in range(3):
            try:
                df = yf.download(batch, start=start, interval="1d",
                                 auto_adjust=True, progress=False, threads=True)["Close"]
                if isinstance(df, pd.Series):
                    df = df.to_frame(batch[0])
                frames.append(df)
                break
            except Exception as e:
                print(f"    retry {attempt + 1}: {e}")
                time.sleep(10)
        time.sleep(2)

    prices = pd.concat(frames, axis=1)
    prices = prices.loc[:, ~prices.columns.duplicated()]
    prices.index = pd.to_datetime(prices.index).tz_localize(None)
    prices = prices.sort_index()

    date_index = prices.index
    if "SPY" not in prices.columns or prices["SPY"].dropna().empty:
        raise SystemExit("ERROR: SPY history missing — aborting (needed for regime classification).")

    out_prices, skipped = {}, 0
    for t in prices.columns:
        enc = encode_series(prices[t].dropna(), date_index)
        if enc is None:
            skipped += 1
            continue
        out_prices[t] = enc

    output = {
        "generated_at": datetime.datetime.now().isoformat(),
        "years": YEARS,
        "note": ("Daily adjusted closes, current S&P 500 members (survivorship bias: "
                 "companies that left the index are not included)."),
        "dates": [d.strftime("%Y-%m-%d") for d in date_index],
        "sectors": {t: sectors.get(t, "Unknown") for t in out_prices if t != "SPY"},
        "names": {t: names.get(t, t) for t in out_prices if t != "SPY"},
        "prices": out_prices,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    size_mb = os.path.getsize(OUTPUT_FILE) / 1e6
    print(f"\n  ✓ {len(out_prices)} tickers, {len(date_index)} trading days, {skipped} skipped")
    print(f"  Output: {OUTPUT_FILE} ({size_mb:.1f} MB)")
    print("═" * 60)


if __name__ == "__main__":
    main()
