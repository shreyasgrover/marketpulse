#!/usr/bin/env python3
"""
MarketPulse EOD Data Pipeline
=============================
Fetches real market data and writes momentum_data.json for MarketPulse.

Data sources (all free):
  - yfinance: prices, volume, technicals (RSI/MACD/EMA/BB/ADX)
  - Wikipedia: S&P 500, S&P 400, S&P 600 full constituent lists
  - Finnhub (free key): news, company sentiment, earnings calendar
  - CBOE/Yahoo: VIX, market breadth proxy

API Keys — set these as environment variables or edit the config below:
  FINNHUB_API_KEY  — get free at https://finnhub.io/register

Usage:
  python3 momentum_pipeline.py

Output:
  momentum_data.json  (in same directory — open the dashboard HTML to view)

Runtime: ~15-30 minutes for full S&P 500/400/600 scan (~1500 stocks)
"""

import json
import os
import sys
import time
import datetime
import math
import traceback

# ─── CONFIG ──────────────────────────────────────────────────────────────────
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "YOUR_FINNHUB_KEY_HERE")
OUTPUT_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "momentum_data.json")

# Momentum score weights (mirror the HTML formula)
W_TECHNICAL  = 0.40
W_SENTIMENT  = 0.25
W_SECTOR     = 0.20
W_REGIME     = 0.15

# ─── SECTOR ETFs (for sector scoring only — stocks now pulled from index lists) ─
SECTOR_ETFS = {
    "Technology":               "XLK",
    "Communication Services":   "XLC",
    "Consumer Discretionary":   "XLY",
    "Healthcare":               "XLV",
    "Industrials":              "XLI",
    "Financials":               "XLF",
    "Materials":                "XLB",
    "Real Estate":              "XLRE",
    "Consumer Staples":         "XLP",
    "Utilities":                "XLU",
    "Energy":                   "XLE",
}

# ─── DEPENDENCY CHECK ────────────────────────────────────────────────────────
def check_deps():
    missing = []
    for pkg in ["yfinance", "pandas", "numpy", "requests"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Installing missing packages: {', '.join(missing)}")
        os.system(f"{sys.executable} -m pip install {' '.join(missing)} --quiet")

check_deps()

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import ssl
import urllib.request

# Fix macOS SSL certificate verification issue
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# ─── FETCH INDEX CONSTITUENTS FROM WIKIPEDIA ─────────────────────────────────
def _read_html_ssl(url):
    """Fetch a URL using requests (SSL-tolerant) and parse HTML tables."""
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, verify=False, timeout=20)
    resp.raise_for_status()
    from io import StringIO
    return pd.read_html(StringIO(resp.text))

def fetch_sp500_tickers():
    """Fetch S&P 500 constituents from Wikipedia."""
    try:
        url    = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = _read_html_ssl(url)
        df     = tables[0]
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        sectors = dict(zip(
            df["Symbol"].str.replace(".", "-", regex=False),
            df["GICS Sector"]
        ))
        names = dict(zip(
            df["Symbol"].str.replace(".", "-", regex=False),
            df["Security"]
        ))
        print(f"    S&P 500: {len(tickers)} stocks fetched")
        return tickers, sectors, names
    except Exception as e:
        print(f"    WARNING: S&P 500 fetch failed: {e}")
        return [], {}, {}

def fetch_sp400_tickers():
    """Fetch S&P 400 (MidCap) constituents from Wikipedia."""
    try:
        url    = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
        tables = _read_html_ssl(url)
        df     = tables[0]
        col_map    = {c.lower(): c for c in df.columns}
        ticker_col = next((col_map[k] for k in col_map if "ticker" in k or "symbol" in k), df.columns[0])
        sector_col = next((col_map[k] for k in col_map if "sector" in k or "gics" in k), None)
        name_col   = next((col_map[k] for k in col_map if "company" in k or "security" in k or "name" in k), None)

        tickers = df[ticker_col].str.replace(".", "-", regex=False).tolist()
        sectors, names = {}, {}
        if sector_col:
            sectors = dict(zip(df[ticker_col].str.replace(".", "-", regex=False), df[sector_col]))
        if name_col:
            names   = dict(zip(df[ticker_col].str.replace(".", "-", regex=False), df[name_col]))
        print(f"    S&P 400: {len(tickers)} stocks fetched")
        return tickers, sectors, names
    except Exception as e:
        print(f"    WARNING: S&P 400 fetch failed: {e}")
        return [], {}, {}

def fetch_sp600_tickers():
    """Fetch S&P 600 (SmallCap) constituents from Wikipedia."""
    try:
        url    = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
        tables = _read_html_ssl(url)
        df     = tables[0]
        col_map    = {c.lower(): c for c in df.columns}
        ticker_col = next((col_map[k] for k in col_map if "ticker" in k or "symbol" in k), df.columns[0])
        sector_col = next((col_map[k] for k in col_map if "sector" in k or "gics" in k), None)
        name_col   = next((col_map[k] for k in col_map if "company" in k or "security" in k or "name" in k), None)

        tickers = df[ticker_col].str.replace(".", "-", regex=False).tolist()
        sectors, names = {}, {}
        if sector_col:
            sectors = dict(zip(df[ticker_col].str.replace(".", "-", regex=False), df[sector_col]))
        if name_col:
            names   = dict(zip(df[ticker_col].str.replace(".", "-", regex=False), df[name_col]))
        print(f"    S&P 600: {len(tickers)} stocks fetched")
        return tickers, sectors, names
    except Exception as e:
        print(f"    WARNING: S&P 600 fetch failed: {e}")
        return [], {}, {}

def build_universe():
    """
    Combine S&P 500 + S&P 400 + S&P 600 into a deduplicated universe.
    Returns: (tickers list, sector_map dict, name_map dict, index_map dict)
    """
    print("  Fetching S&P 500 constituents...")
    t500, s500, n500 = fetch_sp500_tickers()
    print("  Fetching S&P 400 constituents...")
    t400, s400, n400 = fetch_sp400_tickers()
    print("  Fetching S&P 600 constituents...")
    t600, s600, n600 = fetch_sp600_tickers()

    # Build index membership map
    index_map = {}
    for t in t500: index_map[t] = "S&P 500"
    for t in t400: index_map.setdefault(t, "S&P 400")
    for t in t600: index_map.setdefault(t, "S&P 600")

    # Merge dicts (500 takes precedence for sector/name)
    all_tickers = list(dict.fromkeys(t500 + t400 + t600))  # deduplicated, order preserved
    raw_sectors = {**s600, **s400, **s500}
    name_map    = {**n600, **n400, **n500}

    # Normalize GICS sector names to match our ETF keys
    gics_to_sector = {
        "Information Technology": "Technology",
        "Communication Services": "Communication Services",
        "Consumer Discretionary": "Consumer Discretionary",
        "Health Care":            "Healthcare",
        "Industrials":            "Industrials",
        "Financials":             "Financials",
        "Materials":              "Materials",
        "Real Estate":            "Real Estate",
        "Consumer Staples":       "Consumer Staples",
        "Utilities":              "Utilities",
        "Energy":                 "Energy",
    }
    sector_map = {t: gics_to_sector.get(str(v), str(v)) for t, v in raw_sectors.items()}

    print(f"  Total universe: {len(all_tickers)} unique stocks "
          f"({len(t500)} SP500 + {len(t400)} SP400 + {len(t600)} SP600)")
    return all_tickers, sector_map, name_map, index_map

# ─── TECHNICAL INDICATORS ────────────────────────────────────────────────────
def compute_rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).round(1)

def compute_macd(series, fast=12, slow=26, signal=9):
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist        = macd_line - signal_line
    return macd_line, signal_line, hist

def compute_adx(high, low, close, period=14):
    tr1    = high - low
    tr2    = (high - close.shift()).abs()
    tr3    = (low  - close.shift()).abs()
    tr     = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr    = tr.ewm(span=period, adjust=False).mean()
    up_move   = high - high.shift()
    down_move = close.shift() - low
    pos_dm = up_move.where((up_move > down_move) & (up_move > 0), 0)
    neg_dm = down_move.where((down_move > up_move) & (down_move > 0), 0)
    pos_di = 100 * (pos_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan))
    neg_di = 100 * (neg_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan))
    dx     = 100 * (pos_di - neg_di).abs() / (pos_di + neg_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean().round(1)

def compute_mfi(high, low, close, volume, period=14):
    typical  = (high + low + close) / 3
    raw_mf   = typical * volume
    pos_mf   = raw_mf.where(typical > typical.shift(), 0)
    neg_mf   = raw_mf.where(typical <= typical.shift(), 0)
    mf_ratio = pos_mf.rolling(period).sum() / neg_mf.rolling(period).sum().replace(0, np.nan)
    return (100 - (100 / (1 + mf_ratio))).round(1)

def technical_score(rsi, macd_hist, price, ema9, ema21, vwap, vol_ratio, bb_width, adx, roc10):
    rsi_s  = min(100, max(0, (rsi - 20) * 1.25)) if not math.isnan(rsi) else 50
    macd_s = 75 if macd_hist > 0 else 35 if not math.isnan(macd_hist) else 50
    if not math.isnan(ema9) and not math.isnan(ema21):
        if price > ema9 > ema21:   ema_s = 85
        elif price > ema21:        ema_s = 65
        elif price > ema9:         ema_s = 55
        else:                      ema_s = 30
    else:
        ema_s = 50
    vwap_s = 75 if (not math.isnan(vwap) and price > vwap) else 40
    if not math.isnan(vol_ratio):
        vol_s = min(100, 40 + vol_ratio * 20) if vol_ratio > 1 else max(0, vol_ratio * 40)
    else:
        vol_s = 50
    bb_s  = min(100, bb_width * 2) if not math.isnan(bb_width) else 50
    adx_s = min(100, adx * 2)      if not math.isnan(adx)      else 50
    roc_s = min(100, max(0, 50 + roc10 * 5)) if not math.isnan(roc10) else 50
    score = (rsi_s * 0.20 + macd_s * 0.15 + ema_s * 0.15 + vwap_s * 0.15 +
             vol_s * 0.15 + bb_s * 0.05 + adx_s * 0.10 + roc_s * 0.05)
    return round(min(99, max(5, score)))

# ─── SECTOR ETF DATA ─────────────────────────────────────────────────────────
def fetch_sector_data():
    """Fetch real sector ETF data and compute momentum scores."""
    etf_tickers  = list(SECTOR_ETFS.values())
    all_tickers  = etf_tickers + ["SPY"]

    print("  Fetching sector ETF data...")
    raw = yf.download(all_tickers, period="3mo", interval="1d",
                      auto_adjust=True, progress=False, group_by="ticker")

    sector_results = {}
    spy_close      = None

    try:
        spy_series = raw["SPY"]["Close"].dropna()
        if not spy_series.empty:
            spy_close = spy_series
    except Exception:
        pass

    for sector_name, etf in SECTOR_ETFS.items():
        try:
            df    = raw[etf].dropna()
            close = df["Close"]
            high  = df["High"]
            low   = df["Low"]
            vol   = df["Volume"]

            price         = float(close.iloc[-1])
            rsi           = float(compute_rsi(close).iloc[-1])
            _, _, hist    = compute_macd(close)
            macd_hist_val = float(hist.iloc[-1])
            ema9          = float(close.ewm(span=9,  adjust=False).mean().iloc[-1])
            ema21         = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
            mfi           = float(compute_mfi(high, low, close, vol).iloc[-1])

            if spy_close is not None and len(close) >= 20:
                etf_ret      = float(close.iloc[-1]) / float(close.iloc[-20]) - 1
                spy_ret      = float(spy_close.iloc[-1]) / float(spy_close.iloc[-20]) - 1
                rel_strength = round(float(etf_ret / spy_ret) if spy_ret != 0 else 1.0, 2)
            else:
                rel_strength = 1.0

            ema20       = close.ewm(span=20, adjust=False).mean()
            breadth_pct = int((close.tail(20) > ema20.tail(20)).mean() * 100)

            if rsi > 60 and rel_strength > 1.05:   trend = "rising"
            elif rsi < 45 or rel_strength < 0.92:  trend = "falling"
            else:                                   trend = "stable"

            tech_s = technical_score(rsi, macd_hist_val, price, ema9, ema21,
                                     price * 0.999, 1.0, 30, 25, 0)
            score  = round(min(99, max(5, tech_s * 0.6 + mfi * 0.25 + (rel_strength * 40) * 0.15)))

            if score >= 75:
                setup = (f"Strong momentum. RSI {rsi:.0f} with positive MACD crossover. "
                         f"Relative strength {rel_strength:.2f}x vs SPY. Broad breadth at {breadth_pct}% above 20d EMA.")
            elif score >= 55:
                setup = (f"Consolidating. RSI {rsi:.0f} in neutral zone. "
                         f"Relative strength {rel_strength:.2f}x vs SPY. Breadth {breadth_pct}% — mixed signals.")
            else:
                setup = (f"Underperforming. RSI {rsi:.0f} declining. "
                         f"Relative strength {rel_strength:.2f}x vs SPY. Breadth weak at {breadth_pct}%. Avoid new longs.")

            sector_results[sector_name] = {
                "etf": etf, "score": score, "rsi": round(rsi, 1),
                "relStrength": rel_strength, "mfi": round(mfi, 1),
                "breadth": f"{breadth_pct}%", "trend": trend,
                "setup": setup, "topStocks": [], "price": round(price, 2),
            }
            print(f"    {etf} ({sector_name[:18]}): score={score}, RSI={rsi:.0f}, RS={rel_strength:.2f}")

        except Exception as e:
            print(f"    WARNING: Could not fetch {etf}: {e}")
            sector_results[sector_name] = {
                "etf": etf, "score": 55, "rsi": 50.0, "relStrength": 1.0,
                "mfi": 50.0, "breadth": "50%", "trend": "stable",
                "setup": "Data unavailable.", "topStocks": [], "price": 0,
            }

    return sector_results

# ─── MARKET REGIME ────────────────────────────────────────────────────────────
def fetch_regime():
    """Fetch real VIX and SPY trend data. Robust scalar extraction."""
    print("  Fetching market regime data...")
    try:
        vix_raw = yf.download("^VIX", period="5d", interval="1d",
                              auto_adjust=True, progress=False)
        spy_raw = yf.download("SPY",  period="1y", interval="1d",
                              auto_adjust=True, progress=False)

        # Always flatten to Series then extract scalar
        vix_close = vix_raw["Close"].squeeze().dropna()
        vix_val   = float(vix_close.iloc[-1])
        vix_prev  = float(vix_close.iloc[-2]) if len(vix_close) > 1 else vix_val
        vix_trend = "declining" if vix_val < vix_prev else "rising"

        spy_close  = spy_raw["Close"].squeeze().dropna()
        spy_price  = float(spy_close.iloc[-1])
        spy_ema50  = float(spy_close.ewm(span=50,  adjust=False).mean().iloc[-1])
        spy_ema200 = float(spy_close.ewm(span=200, adjust=False).mean().iloc[-1])

        if spy_price > spy_ema50 > spy_ema200:   spy_trend = "above 50 & 200 EMA — bull"
        elif spy_price > spy_ema200:              spy_trend = "above 200 EMA only — transitional"
        else:                                     spy_trend = "below 200 EMA — bear"

        pc_ratio = round(max(0.5, min(1.5, 0.6 + (vix_val - 15) * 0.012)), 2)
        fg_score = max(10, min(90, int(100 - (vix_val - 10) * 2.5)))

        if vix_val < 15 and spy_price > spy_ema200:    regime = "Risk-On Bull"
        elif vix_val < 20 and spy_price > spy_ema200:  regime = "Cautious Bull"
        elif vix_val < 25:                              regime = "Choppy / Transitional"
        elif vix_val < 35:                              regime = "Elevated Risk — Defensive"
        else:                                           regime = "Bear Market / Crisis"

        print(f"    VIX={vix_val:.1f} ({vix_trend}), SPY trend: {spy_trend}, Regime: {regime}")
        return {
            "regime": regime, "vix": round(vix_val, 1), "vixTrend": vix_trend,
            "sp500Trend": spy_trend,
            "breadth": f"SPY ${spy_price:.0f} vs 50EMA ${spy_ema50:.0f}",
            "putCallRatio": pc_ratio, "fearGreed": fg_score,
            "yieldCurve": "check TradingView", "dollarIndex": "check DXY",
        }
    except Exception as e:
        print(f"    WARNING: Regime fetch failed: {e}")
        return {
            "regime": "Data Unavailable", "vix": 20.0, "vixTrend": "unknown",
            "sp500Trend": "unknown", "breadth": "unknown", "putCallRatio": 0.85,
            "fearGreed": 50, "yieldCurve": "unknown", "dollarIndex": "unknown",
        }

# ─── FINNHUB NEWS & SENTIMENT ─────────────────────────────────────────────────
def fetch_news_sentiment(tickers_sample=None):
    if FINNHUB_API_KEY == "YOUR_FINNHUB_KEY_HERE":
        print("  SKIPPING news: No Finnhub API key set. Set FINNHUB_API_KEY env var.")
        return get_fallback_news()

    if tickers_sample is None:
        tickers_sample = ["NVDA","MSFT","AAPL","AMZN","META","GOOGL","TSLA","AMD","LLY","JPM"]

    print(f"  Fetching Finnhub news for {len(tickers_sample)} tickers...")
    news_items = []
    today     = datetime.date.today()
    from_date = (today - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    to_date   = today.strftime("%Y-%m-%d")
    sentiment_map = {"strongBuy": "very bullish", "buy": "bullish", "neutral": "neutral",
                     "sell": "bearish", "strongSell": "very bearish"}

    for ticker in tickers_sample[:10]:
        try:
            url  = (f"https://finnhub.io/api/v1/company-news?symbol={ticker}"
                    f"&from={from_date}&to={to_date}&token={FINNHUB_API_KEY}")
            r    = requests.get(url, timeout=8)
            articles = r.json() if r.status_code == 200 else []

            url2      = f"https://finnhub.io/api/v1/news-sentiment?symbol={ticker}&token={FINNHUB_API_KEY}"
            r2        = requests.get(url2, timeout=8)
            sent_data = r2.json() if r2.status_code == 200 else {}

            buzz           = int(sent_data.get("buzz", {}).get("weeklyAverage", 50) * 100) if sent_data else 50
            buzz           = max(10, min(99, buzz))
            sentiment_label = sentiment_map.get(
                sent_data.get("sentiment", {}).get("signal", "neutral"), "neutral")

            if articles:
                top       = articles[0]
                headline  = top.get("headline", "No headline")[:120]
                hours_ago = max(1, int((time.time() - top.get("datetime", time.time())) / 3600))
                news_items.append({
                    "source": "Finnhub", "ticker": ticker, "sentiment": sentiment_label,
                    "buzz": buzz, "headline": headline, "time": f"{hours_ago}h ago",
                })
            time.sleep(0.3)
        except Exception as e:
            print(f"    WARNING: Finnhub failed for {ticker}: {e}")

    return news_items if news_items else get_fallback_news()

def get_fallback_news():
    return [{"source": "Finnhub", "ticker": "—", "sentiment": "neutral", "buzz": 50,
             "headline": "Set your FINNHUB_API_KEY to see live news & sentiment.", "time": "now"}]

# ─── EARNINGS CALENDAR ────────────────────────────────────────────────────────
def fetch_earnings_calendar():
    if FINNHUB_API_KEY == "YOUR_FINNHUB_KEY_HERE":
        return {}
    print("  Fetching earnings calendar from Finnhub...")
    today   = datetime.date.today()
    to_date = today + datetime.timedelta(days=90)
    url = (f"https://finnhub.io/api/v1/calendar/earnings"
           f"?from={today}&to={to_date}&token={FINNHUB_API_KEY}")
    try:
        r    = requests.get(url, timeout=10)
        data = r.json() if r.status_code == 200 else {}
        calendar = {}
        for item in data.get("earningsCalendar", []):
            sym = item.get("symbol", "")
            if sym:
                calendar[sym] = {
                    "date": item.get("date", ""),
                    "epsEstimate": item.get("epsEstimate"),
                    "revenueEstimate": item.get("revenueEstimate"),
                    "timing": "AMC",
                }
        print(f"    Got {len(calendar)} upcoming earnings dates")
        return calendar
    except Exception as e:
        print(f"    WARNING: Earnings calendar failed: {e}")
        return {}

# ─── STOCK BATCH SCORING ──────────────────────────────────────────────────────
def fetch_stock_batch(tickers, sector_data, sector_map, name_map, index_map,
                      regime_score, earnings_cal):
    import random
    results = []
    if not tickers:
        return results

    try:
        raw = yf.download(
            tickers, period="3mo", interval="1d",
            auto_adjust=True, progress=False,
            group_by="ticker", threads=True
        )
    except Exception as e:
        print(f"    ERROR downloading batch: {e}")
        return results

    single = len(tickers) == 1

    for ticker in tickers:
        try:
            df = raw.dropna() if single else raw[ticker].dropna()
            if df is None or len(df) < 30:
                continue

            close  = df["Close"]
            high   = df["High"]
            low    = df["Low"]
            volume = df["Volume"]

            price     = float(close.iloc[-1])
            vol_today = float(volume.iloc[-1])
            vol_20avg = float(volume.tail(20).mean())
            vol_ratio = vol_today / vol_20avg if vol_20avg > 0 else 1.0

            rsi_series    = compute_rsi(close)
            rsi           = float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0
            _, _, hist    = compute_macd(close)
            macd_hist_val = float(hist.iloc[-1])
            macd_signal   = ("bullish" if hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2]
                             else "bearish" if hist.iloc[-1] < 0 else "neutral")

            ema9   = float(close.ewm(span=9,   adjust=False).mean().iloc[-1])
            ema21  = float(close.ewm(span=21,  adjust=False).mean().iloc[-1])
            ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])

            last_day = df.iloc[-1]
            vwap     = float((last_day["High"] + last_day["Low"] + last_day["Close"]) / 3)

            bb_mid   = close.rolling(20).mean()
            bb_std   = close.rolling(20).std()
            bb_width = float(bb_std.iloc[-1] / bb_mid.iloc[-1] * 100) if float(bb_mid.iloc[-1]) > 0 else 30.0

            adx_val = float(compute_adx(high, low, close).iloc[-1])
            roc10   = float((close.iloc[-1] / close.iloc[-11] - 1) * 100) if len(close) >= 11 else 0.0
            mfi_val = float(compute_mfi(high, low, close, volume).iloc[-1])

            tech_s = technical_score(rsi, macd_hist_val, price, ema9, ema21,
                                     vwap, vol_ratio, bb_width, adx_val, roc10)
            sent_s = round(min(99, max(5,
                (rsi * 0.4) + (75 if macd_hist_val > 0 else 35) * 0.35 +
                min(90, vol_ratio * 30) * 0.25
            )))

            sector_name  = sector_map.get(ticker, "Unknown")
            sector_score = sector_data.get(sector_name, {}).get("score", 55)
            sector_m     = min(99, max(5, sector_score + int((rsi - 50) * 0.2)))
            regime_m     = min(99, max(5, regime_score))

            momentum = round(min(99, max(5,
                tech_s * W_TECHNICAL + sent_s * W_SENTIMENT +
                sector_m * W_SECTOR + regime_m * W_REGIME
            )))

            if momentum >= 75:
                setups = ["Breakout above 200 EMA — strong trend",
                          "EMA9/21 golden crossover + volume surge",
                          "Bull flag continuation on daily",
                          "Cup-and-handle breakout — watch entry"]
            elif momentum >= 50:
                setups = ["EMA21 pullback bounce", "Consolidation base forming",
                          "Support bounce + volume", "Bull flag forming"]
            else:
                setups = ["Below 50 DMA — no setup", "Lower highs pattern",
                          "Volume declining on bounces", "Bearish MACD divergence"]
            setup = random.choice(setups)

            atr_val = float(df["High"].tail(14).max() - df["Low"].tail(14).min()) / 14
            entry   = round(price, 2)
            t1      = round(price * (1 + max(0.03, atr_val / price * 2)), 2)
            t2      = round(price * (1 + max(0.07, atr_val / price * 4)), 2)
            stop    = round(price * (1 - max(0.02, atr_val / price)), 2)

            def fmt_vol(v):
                if v >= 1e9: return f"{v/1e9:.1f}B"
                if v >= 1e6: return f"{v/1e6:.0f}M"
                return f"{v/1e3:.0f}K"

            record = {
                "ticker":        ticker,
                "name":          name_map.get(ticker, ticker),
                "sector":        sector_name,
                "index":         index_map.get(ticker, "Unknown"),
                "momentum":      momentum,
                "sentiment":     sent_s,
                "technical":     tech_s,
                "sectorMomentum": sector_m,
                "regime":        regime_m,
                "rsi":           round(rsi, 1),
                "macdSignal":    macd_signal,
                "mfi":           round(mfi_val, 1),
                "adx":           round(adx_val, 1),
                "roc10":         round(roc10, 2),
                "ema9":          round(ema9, 2),
                "ema21":         round(ema21, 2),
                "ema200":        round(ema200, 2),
                "vwap":          round(vwap, 2),
                "volume":        fmt_vol(vol_today),
                "avgVol":        fmt_vol(vol_20avg),
                "volRatio":      round(vol_ratio, 2),
                "entry":         entry,
                "t1":            t1,
                "t2":            t2,
                "stop":          stop,
                "setup":         setup,
                "price":         entry,
            }

            # Attach earnings if available
            if ticker in earnings_cal:
                e = earnings_cal[ticker]
                try:
                    edate     = datetime.date.fromisoformat(e["date"])
                    days_away = (edate - datetime.date.today()).days
                    record["earningsDate"]     = e["date"]
                    record["earningsDaysAway"] = days_away
                    record["earningsIsPast"]   = days_away < 0
                    record["earningsEstEPS"]   = e.get("epsEstimate")
                    record["earningsTiming"]   = e.get("timing", "AMC")
                except Exception:
                    pass

            results.append(record)

        except Exception:
            pass

    return results

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    start_ts = time.time()
    print("\n" + "═" * 60)
    print("  MarketPulse EOD Pipeline  (S&P 500 + 400 + 600)")
    print(f"  {datetime.datetime.now().strftime('%A %B %d, %Y  %I:%M %p ET')}")
    print("═" * 60)

    # 1. Market Regime
    print("\n[1/5] Market Regime...")
    regime_data  = fetch_regime()
    regime_score = round(min(90, max(20, 50 + (30 - regime_data["vix"]) * 1.5)))

    # 2. Sector ETFs
    print("\n[2/5] Sector ETFs...")
    sector_data = fetch_sector_data()

    # 3. Build stock universe
    print("\n[3/5] Building Stock Universe (S&P 500 + 400 + 600)...")
    all_tickers, sector_map, name_map, index_map = build_universe()

    # 4. Earnings calendar
    print("\n[4/5] Earnings Calendar...")
    earnings_cal = fetch_earnings_calendar()

    # 5. Score stocks in batches of 100
    print(f"\n[5/5] Scoring {len(all_tickers)} stocks in batches of 100...")
    BATCH_SIZE = 100
    all_stocks = []
    batches    = [all_tickers[i:i+BATCH_SIZE] for i in range(0, len(all_tickers), BATCH_SIZE)]

    for i, batch in enumerate(batches):
        pct = round((i + 1) / len(batches) * 100)
        print(f"  Batch {i+1}/{len(batches)} ({len(batch)} stocks) [{pct}%]...")
        results = fetch_stock_batch(
            batch, sector_data, sector_map, name_map, index_map,
            regime_score, earnings_cal
        )
        all_stocks.extend(results)
        time.sleep(0.5)

    # Sort by momentum descending
    all_stocks.sort(key=lambda x: x["momentum"], reverse=True)

    # Update sector topStocks with actual top 5 performers
    sector_top = {}
    for s in all_stocks:
        sec = s["sector"]
        if sec not in sector_top:
            sector_top[sec] = []
        if len(sector_top[sec]) < 5:
            sector_top[sec].append(s["ticker"])
    for sec, tops in sector_top.items():
        if sec in sector_data:
            sector_data[sec]["topStocks"] = tops

    # News & Sentiment
    print("\n[+] News & Sentiment...")
    top_tickers = [s["ticker"] for s in all_stocks if s["momentum"] >= 70][:15]
    news_data   = fetch_news_sentiment(top_tickers if top_tickers else None)

    # ─── Index breakdown stats ───────────────────────────────────────────────
    elapsed   = round(time.time() - start_ts, 1)
    idx_stats = {}
    for s in all_stocks:
        idx = s.get("index", "Unknown")
        if idx not in idx_stats:
            idx_stats[idx] = {"total": 0, "strong_buy": 0, "watch": 0, "avoid": 0}
        idx_stats[idx]["total"] += 1
        if s["momentum"] >= 75:   idx_stats[idx]["strong_buy"] += 1
        elif s["momentum"] >= 50: idx_stats[idx]["watch"] += 1
        else:                     idx_stats[idx]["avoid"] += 1

    # Market Risk Assessment (3-lens) — failures never block the momentum scan
    try:
        from market_risk import compute_risk
        risk_data = compute_risk(previous_output_file=OUTPUT_FILE)
    except Exception as e:
        print(f"  ✗ Risk module unavailable: {e}")
        risk_data = {"error": str(e)}

    output = {
        "generated_at":         datetime.datetime.now().isoformat(),
        "generated_at_display": datetime.datetime.now().strftime("%b %d, %Y %I:%M %p ET"),
        "elapsed_seconds":      elapsed,
        "regime":               regime_data,
        "risk":                 risk_data,
        "sectors":              sector_data,
        "stocks":               all_stocks,
        "news":                 news_data,
        "stats": {
            "total_stocks": len(all_stocks),
            "strong_buy":   sum(1 for s in all_stocks if s["momentum"] >= 75),
            "watch":        sum(1 for s in all_stocks if 50 <= s["momentum"] < 75),
            "avoid":        sum(1 for s in all_stocks if s["momentum"] < 50),
            "by_index":     idx_stats,
        }
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n{'═' * 60}")
    print(f"  ✓ Done in {elapsed}s")
    print(f"  Total stocks scanned: {len(all_stocks)}")
    print(f"  Strong Buy (≥75): {output['stats']['strong_buy']}")
    print(f"  Watch (50-74):    {output['stats']['watch']}")
    print(f"  Avoid (<50):      {output['stats']['avoid']}")
    print(f"\n  Breakdown by index:")
    for idx, st in idx_stats.items():
        print(f"    {idx:10s}: {st['total']:4d} stocks | "
              f"Strong Buy: {st['strong_buy']:3d} | Watch: {st['watch']:3d} | Avoid: {st['avoid']:3d}")
    print(f"\n  Output: {OUTPUT_FILE}")
    print(f"{'═' * 60}\n")

if __name__ == "__main__":
    main()
