#!/usr/bin/env python3
"""
MarketPulse — Market Risk Assessment Engine
============================================
Computes a three-lens market risk read, written into momentum_data.json["risk"]:

  Lens 1  Recession-Risk Dashboard   — leading/coincident economic indicators
  Lens 2  Market-Peak Froth Gauges   — euphoria/complacency typical of tops
  Lens 3  Price-Trend Technical      — trend confirmation (the "act" trigger)

Data sources:
  - FRED public CSV endpoint (no API key): yield curve, Sahm rule, HY OAS,
    NFCI, SLOOS, unemployment, payrolls, CPI
  - yfinance: S&P 500 SMAs, Value-vs-Growth (RPV/RPG), SPY trailing/forward P/E
  - Best-effort web scrapes: NAAIM, AAII, ISM PMI, Consumer Confidence, LEI,
    IPO count. When a scrape fails, the last good value is carried forward
    from the previous momentum_data.json and flagged stale in the UI.
"""

import datetime
import io
import json
import os
import re

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
TIMEOUT = 20


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _fred_series(series_id, days=3700):
    """Fetch a FRED series via the public fredgraph.csv endpoint (no key)."""
    import pandas as pd
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna()
    df["date"] = pd.to_datetime(df["date"])
    return df


def _gauge(key, name, sub, value, status, detail="", as_of=None, stale=False):
    return {
        "key": key, "name": name, "sub": sub,
        "value": value, "status": status,   # status: green | watch | triggered | unknown
        "detail": detail,
        "as_of": as_of or datetime.date.today().isoformat(),
        "stale": stale,
    }


def _carry_forward(key, previous_risk):
    """Return last good gauge from previous run, marked stale. None if absent."""
    if not previous_risk:
        return None
    for lens in ("lens1", "lens2", "lens3"):
        for g in previous_risk.get(lens, {}).get("gauges", []):
            if g.get("key") == key and g.get("status") != "unknown":
                g = dict(g)
                g["stale"] = True
                return g
    return None


# ─── LENS 1: RECESSION RISK (all FRED, reliable) ────────────────────────────

def lens1_recession():
    gauges = []

    # Yield curve 10yr - 3mo
    try:
        df = _fred_series("T10Y3M", days=400)
        v = df.iloc[-1]["value"]
        status = "green" if v > 0.2 else ("watch" if v > -0.1 else "triggered")
        gauges.append(_gauge("yield_curve", "Yield Curve (10yr − 3mo)",
                             "Best-documented predictor — inverted before every U.S. recession since the 1960s, ~12–18mo lead.",
                             f"{v:+.2f}%", status,
                             "NORMALIZED" if v > 0 else "INVERTED",
                             as_of=str(df.iloc[-1]["date"].date())))
    except Exception as e:
        gauges.append(_gauge("yield_curve", "Yield Curve (10yr − 3mo)", "FRED T10Y3M", "n/a", "unknown", str(e)))

    # Sahm rule
    try:
        df = _fred_series("SAHMREALTIME", days=800)
        v = df.iloc[-1]["value"]
        status = "green" if v < 0.30 else ("watch" if v < 0.50 else "triggered")
        gauges.append(_gauge("sahm", "Sahm Rule (jobs momentum)",
                             "Fires when 3-mo avg unemployment rises 0.5pt off its 12-mo low. Caught every recession start since 1970.",
                             f"{v:.2f}", status, "TRIGGER 0.50",
                             as_of=str(df.iloc[-1]["date"].date())))
    except Exception as e:
        gauges.append(_gauge("sahm", "Sahm Rule (jobs momentum)", "FRED SAHMREALTIME", "n/a", "unknown", str(e)))

    # High-yield credit spreads
    try:
        df = _fred_series("BAMLH0A0HYM2", days=400)
        v = df.iloc[-1]["value"]
        status = "green" if v < 4.0 else ("watch" if v < 5.5 else "triggered")
        note = "HISTORICALLY TIGHT" if v < 3.2 else ("NORMAL" if v < 4.5 else "WIDENING")
        gauges.append(_gauge("hy_spreads", "High-Yield Credit Spreads (HY OAS)",
                             "Cleanest market stress gauge; blows out into downturns.",
                             f"~{v:.1f}%", status, note,
                             as_of=str(df.iloc[-1]["date"].date())))
    except Exception as e:
        gauges.append(_gauge("hy_spreads", "High-Yield Credit Spreads (HY OAS)", "FRED BAMLH0A0HYM2", "n/a", "unknown", str(e)))

    # Labor market: unemployment + payrolls trend
    try:
        un = _fred_series("UNRATE", days=800)
        pay = _fred_series("PAYEMS", days=800)
        u = un.iloc[-1]["value"]
        jobs_chg = (pay.iloc[-1]["value"] - pay.iloc[-2]["value"])  # thousands
        u_12mo_low = un["value"].tail(12).min()
        rising = u - u_12mo_low
        status = "green" if (jobs_chg > 100 and rising < 0.2) else ("watch" if jobs_chg > 0 else "triggered")
        gauges.append(_gauge("labor", "Labor Market (unemployment / payrolls)",
                             f"Latest: {jobs_chg:+,.0f}k jobs, unemployment {u:.1f}% "
                             f"({rising:+.1f}pt vs 12-mo low).",
                             f"{u:.1f}%", status,
                             "SOLID" if status == "green" else "COOLING BENEATH",
                             as_of=str(un.iloc[-1]["date"].date())))
    except Exception as e:
        gauges.append(_gauge("labor", "Labor Market", "FRED UNRATE/PAYEMS", "n/a", "unknown", str(e)))

    return gauges


# ─── LENS 2: MARKET-PEAK FROTH ───────────────────────────────────────────────

def lens2_froth(previous_risk):
    import yfinance as yf
    gauges = []

    # NAAIM manager exposure (scrape)
    try:
        r = requests.get("https://naaim.org/programs/naaim-exposure-index/", headers=HEADERS, timeout=TIMEOUT)
        m = re.search(r"this week[^0-9\-]{0,80}?(-?\d{1,3}\.\d{1,2})", r.text, re.I | re.S) or \
            re.search(r"(?:exposure|index)[^0-9\-]{0,40}?(-?\d{1,3}\.\d{1,2})", r.text, re.I | re.S)
        v = float(m.group(1))
        status = "triggered" if v >= 90 else ("watch" if v >= 80 else "green")
        note = "all-in" if v >= 95 else ""
        gauges.append(_gauge("naaim", "Manager Bullishness",
                             "NAAIM active-manager equity exposure", f"{v:.1f}", status, note))
    except Exception:
        cf = _carry_forward("naaim", previous_risk)
        gauges.append(cf or _gauge("naaim", "Manager Bullishness", "NAAIM active-manager equity exposure",
                                   "n/a", "unknown", "scrape failed"))

    # AAII bulls % (scrape)
    try:
        r = requests.get("https://www.aaii.com/sentimentsurvey/sent_results", headers=HEADERS, timeout=TIMEOUT)
        m = re.search(r"Bullish[^0-9]{0,60}?(\d{1,2}\.\d)\s*%", r.text, re.I | re.S)
        v = float(m.group(1))
        status = "triggered" if v >= 44 else ("watch" if v >= 40 else "green")
        gauges.append(_gauge("aaii", "Retail Euphoria",
                             "AAII bull–bear sentiment survey", f"{v:.1f}% bulls", status))
    except Exception:
        cf = _carry_forward("aaii", previous_risk)
        gauges.append(cf or _gauge("aaii", "Retail Euphoria", "AAII bull–bear sentiment survey",
                                   "n/a", "unknown", "scrape failed"))

    # Consumer Confidence (Conference Board, scrape)
    try:
        r = requests.get("https://www.conference-board.org/topics/consumer-confidence", headers=HEADERS, timeout=TIMEOUT)
        m = re.search(r"Consumer Confidence Index[^0-9]{0,120}?(\d{2,3}\.\d)", r.text, re.I | re.S)
        v = float(m.group(1))
        status = "triggered" if v > 110 else ("watch" if v > 105 else "green")
        gauges.append(_gauge("conf", "Consumer Confidence > 110",
                             "Conference Board confidence index", f"{v:.1f}", status,
                             "Not yet" if status == "green" else ""))
    except Exception:
        cf = _carry_forward("conf", previous_risk)
        gauges.append(cf or _gauge("conf", "Consumer Confidence > 110", "Conference Board confidence index",
                                   "n/a", "unknown", "scrape failed"))

    # Growth-expectation froth: SPY forward P/E
    try:
        info = yf.Ticker("SPY").info
        fpe = info.get("forwardPE")
        v = float(fpe)
        status = "triggered" if v >= 20 else ("watch" if v >= 18 else "green")
        gauges.append(_gauge("fwd_pe", "Growth-Expectation Froth",
                             "S&P 500 forward P/E (SPY proxy)", f"~{v:.0f}×", status,
                             "rich vs history" if status == "triggered" else ""))
    except Exception:
        cf = _carry_forward("fwd_pe", previous_risk)
        gauges.append(cf or _gauge("fwd_pe", "Growth-Expectation Froth", "S&P 500 forward P/E",
                                   "n/a", "unknown", "yfinance failed"))

    # Rule of 20: trailing P/E + CPI YoY
    try:
        info = yf.Ticker("SPY").info
        tpe = float(info.get("trailingPE"))
        cpi = _fred_series("CPIAUCSL", days=800)
        cpi_yoy = (cpi.iloc[-1]["value"] / cpi.iloc[-13]["value"] - 1) * 100
        v = tpe + cpi_yoy
        status = "triggered" if v > 23 else ("watch" if v > 20 else "green")
        gauges.append(_gauge("rule20", "Rule of 20 (P/E + CPI)",
                             "Trailing P/E + YoY inflation",
                             f"{v:.1f}", status, "well > 20" if v > 23 else ""))
    except Exception:
        cf = _carry_forward("rule20", previous_risk)
        gauges.append(cf or _gauge("rule20", "Rule of 20 (P/E + CPI)", "Trailing P/E + YoY inflation",
                                   "n/a", "unknown", "source failed"))

    # Value vs Growth leadership (6m), RPV vs RPG
    try:
        px = yf.download(["RPV", "RPG"], period="7mo", progress=False, auto_adjust=True)["Close"].dropna()
        rel = (px["RPV"].iloc[-1] / px["RPV"].iloc[0]) - (px["RPG"].iloc[-1] / px["RPG"].iloc[0])
        v = rel * 100
        # Growth leading strongly = froth on; value leading = eased
        status = "triggered" if v < -10 else ("watch" if v < 0 else "green")
        label = f"Value {v:+.0f}% (6m)"
        gauges.append(_gauge("val_gro", "Value vs Growth (6m)",
                             "Value vs growth leadership (RPV−RPG)", label, status,
                             "Eased" if status == "green" else ""))
    except Exception:
        cf = _carry_forward("val_gro", previous_risk)
        gauges.append(cf or _gauge("val_gro", "Value vs Growth (6m)", "RPV−RPG relative return",
                                   "n/a", "unknown", "yfinance failed"))

    # Inverted yield curve (as a froth-cycle timer, same FRED series)
    try:
        df = _fred_series("T10Y3M", days=200)
        v = df.iloc[-1]["value"]
        status = "green" if v > 0 else "triggered"
        gauges.append(_gauge("froth_curve", "Inverted Yield Curve",
                             "10yr–3mo Treasury spread (FRED)", f"{v:+.2f}%", status,
                             "Not yet" if status == "green" else "",
                             as_of=str(df.iloc[-1]["date"].date())))
    except Exception:
        gauges.append(_gauge("froth_curve", "Inverted Yield Curve", "FRED T10Y3M", "n/a", "unknown"))

    # Credit complacency: NFCI
    try:
        df = _fred_series("NFCI", days=400)
        v = df.iloc[-1]["value"]
        status = "triggered" if v < -0.45 else ("watch" if v < -0.30 else "green")
        gauges.append(_gauge("nfci", "Credit Complacency",
                             "Chicago Fed NFCI financial conditions", f"{v:.2f} ({'loose' if v < 0 else 'tight'})",
                             status, as_of=str(df.iloc[-1]["date"].date())))
    except Exception:
        gauges.append(_gauge("nfci", "Credit Complacency", "FRED NFCI", "n/a", "unknown"))

    # Tightening credit: SLOOS (C&I net tightening %)
    try:
        df = _fred_series("DRTSCILM", days=1200)
        v = df.iloc[-1]["value"]
        status = "green" if v < 5 else ("watch" if v < 20 else "triggered")
        gauges.append(_gauge("sloos", "Tightening Credit (SLOOS)",
                             "Fed Senior Loan Officer Survey — % banks tightening C&I standards",
                             f"{v:+.1f}%", status,
                             "net tightening" if v > 0 else "net easing",
                             as_of=str(df.iloc[-1]["date"].date())))
    except Exception:
        gauges.append(_gauge("sloos", "Tightening Credit (SLOOS)", "FRED DRTSCILM", "n/a", "unknown"))

    # ISM Manufacturing PMI (scrape)
    try:
        r = requests.get("https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/",
                         headers=HEADERS, timeout=TIMEOUT)
        m = re.search(r"Manufacturing PMI[^0-9]{0,120}?(\d{2}\.\d)", r.text, re.I | re.S)
        v = float(m.group(1))
        status = "green" if v >= 50 else ("watch" if v >= 47 else "triggered")
        gauges.append(_gauge("ism", "ISM Manufacturing PMI",
                             "Below 50 = contraction.", f"{v:.1f}", status,
                             "EXPANSION" if v >= 50 else "CONTRACTION"))
    except Exception:
        cf = _carry_forward("ism", previous_risk)
        gauges.append(cf or _gauge("ism", "ISM Manufacturing PMI", "ISM Report on Business",
                                   "n/a", "unknown", "scrape failed"))

    # IPO / deal froth (scrape stockanalysis.com IPO count)
    try:
        yr = datetime.date.today().year
        r = requests.get(f"https://stockanalysis.com/ipos/{yr}/", headers=HEADERS, timeout=TIMEOUT)
        m = re.search(r"(\d{2,4})\s+IPOs", r.text, re.I)
        v = int(m.group(1))
        # crude annualized comparison: >250/yr pace = hot
        doy = datetime.date.today().timetuple().tm_yday
        pace = v * 365 / max(doy, 1)
        status = "triggered" if pace > 250 else ("watch" if pace > 180 else "green")
        gauges.append(_gauge("ipo", "Deal & IPO Froth",
                             "IPO issuance volume (M&A not included)", f"{v} IPOs YTD", status,
                             f"~{pace:.0f}/yr pace"))
    except Exception:
        cf = _carry_forward("ipo", previous_risk)
        gauges.append(cf or _gauge("ipo", "Deal & IPO Froth", "IPO issuance volume",
                                   "n/a", "unknown", "scrape failed"))

    return gauges


# ─── LENS 3: PRICE-TREND TECHNICAL ───────────────────────────────────────────

def lens3_trend():
    import yfinance as yf
    gauges = []
    try:
        px = yf.download("^GSPC", period="2y", progress=False, auto_adjust=True)["Close"].dropna()
        if hasattr(px, "columns"):  # DataFrame if multi-col
            px = px.iloc[:, 0]
        sma50 = px.rolling(50).mean()
        sma150 = px.rolling(150).mean()
        sma200 = px.rolling(200).mean()
        price = float(px.iloc[-1])
        s50, s150, s200 = float(sma50.iloc[-1]), float(sma150.iloc[-1]), float(sma200.iloc[-1])
        slope50 = s50 - float(sma50.iloc[-21])     # ~1 month slope
        slope150 = s150 - float(sma150.iloc[-21])
        death_cross = s50 < s150 and slope50 < 0 and slope150 <= 0
        uptrend = s50 > s150 and price > s150 and price > s200

        if death_cross:
            status, verdict = "triggered", "DEATH CROSS"
        elif uptrend:
            status, verdict = "green", "UPTREND ↑"
        else:
            status, verdict = "watch", "MIXED"

        detail = (f"S&P ≈ {price:,.0f}. 50-day {'above' if s50 > s150 else 'below'} 150-day "
                  f"(200-day ≈ {s200:,.0f}); both slopes {'up' if slope50 > 0 and slope150 > 0 else 'flattening'}. "
                  f"{'No death cross.' if not death_cross else 'Death cross in place.'}")
        gauges.append(_gauge("sma_trend", "50-day vs 150-day SMA",
                             "Bear trigger: 50-day crosses below 150-day AND both flatten or slope down. "
                             "The signal that historically tells you a bear is actually underway, not just feared.",
                             "50 > 150" if s50 > s150 else "50 < 150",
                             status, detail))
        return gauges, verdict
    except Exception as e:
        gauges.append(_gauge("sma_trend", "50-day vs 150-day SMA", "S&P 500 daily candles",
                             "n/a", "unknown", str(e)))
        return gauges, "UNKNOWN"


# ─── OVERALL READ ────────────────────────────────────────────────────────────

def overall_read(l1, l2, l3_verdict):
    def worst(gs):
        order = {"green": 0, "watch": 1, "unknown": 1, "triggered": 2}
        known = [g for g in gs if g["status"] != "unknown"]
        if not known:
            return "unknown"
        mx = max(order[g["status"]] for g in known)
        return {0: "green", 1: "watch", 2: "triggered"}[mx]

    l1_triggered = sum(1 for g in l1 if g["status"] == "triggered")
    l1_status = "GREEN" if l1_triggered == 0 else ("MIXED" if l1_triggered == 1 else "ELEVATED")

    l2_known = [g for g in l2 if g["status"] != "unknown"]
    l2_pct = round(100 * sum(1 for g in l2_known if g["status"] == "triggered") / max(len(l2_known), 1))

    l3_status = "GREEN" if l3_verdict.startswith("UPTREND") else ("RED" if l3_verdict == "DEATH CROSS" else "MIXED")

    if l3_status == "RED" and l2_pct >= 60:
        headline = "Trend break with high froth — the historic sell signal. Reduce risk."
        badge = "Defensive — trend broken while froth is high"
    elif l1_status != "GREEN" and l3_status == "RED":
        headline = "Economic deterioration confirmed by a trend break — recession playbook."
        badge = "Risk-off"
    elif l2_pct >= 60 and l1_status == "GREEN" and l3_status == "GREEN":
        headline = ('Peak-like positioning, but no recession and no trend break. '
                    'The classic "late-cycle, not end-of-cycle" setup.')
        badge = "Economy calm · Market frothy · Uptrend intact — stay invested, stay disciplined"
    elif l2_pct >= 60:
        headline = "Froth is high and the trend is wobbling — tighten stops, slow new buys."
        badge = "Caution"
    else:
        headline = "No major warnings across economy, positioning, or trend."
        badge = "Constructive — normal bull-market conditions"

    return {
        "lens1_status": l1_status,
        "lens2_pct_triggered": l2_pct,
        "lens3_status": l3_status,
        "headline": headline,
        "badge": badge,
    }


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

def compute_risk(previous_output_file=None):
    """Returns the full risk block. Never raises — always returns a dict."""
    previous_risk = None
    try:
        if previous_output_file and os.path.exists(previous_output_file):
            with open(previous_output_file) as f:
                previous_risk = json.load(f).get("risk")
    except Exception:
        previous_risk = None

    print("\n▶ Market Risk Assessment (3 lenses)…")
    try:
        l1 = lens1_recession()
        print(f"  Lens 1 (recession): {len(l1)} gauges")
        l2 = lens2_froth(previous_risk)
        print(f"  Lens 2 (froth):     {len(l2)} gauges")
        l3, l3_verdict = lens3_trend()
        print(f"  Lens 3 (trend):     {l3_verdict}")
        summary = overall_read(l1, l2, l3_verdict)
        return {
            "computed_at": datetime.datetime.now().isoformat(),
            "lens1": {"title": "Recession-Risk Dashboard",
                      "sub": "Leading & coincident indicators of an economic downturn.", "gauges": l1},
            "lens2": {"title": "Market-Peak Froth — Public-Data Gauges",
                      "sub": "A signal is triggered when it shows the euphoria or complacency typical of market tops.",
                      "gauges": l2},
            "lens3": {"title": "Price-Trend Technical",
                      "sub": "A trend-confirmation gauge — the signal that historically tells you a bear is actually underway, not just feared.",
                      "gauges": l3, "verdict": l3_verdict},
            "summary": summary,
        }
    except Exception as e:
        print(f"  ✗ Risk assessment failed: {e}")
        return {"computed_at": datetime.datetime.now().isoformat(), "error": str(e)}


if __name__ == "__main__":
    out = compute_risk("momentum_data.json")
    print(json.dumps(out, indent=2, default=str)[:4000])
