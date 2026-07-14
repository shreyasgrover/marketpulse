# MarketPulse

Autonomous momentum scanner, market-risk assessment, and true historical backtester.
Runs entirely on GitHub Actions + GitHub Pages — no server, no computer left on, $0/month.

**Quick start: run `./setup.sh` (automated) or follow [Manual Setup](#manual-setup) below.**

## What it does

```
┌─ Weekdays 22:00 UTC ──────────────────────────────────────────────┐
│ momentum_pipeline.py (~15-30 min)                                 │
│  • Scans ~1,500 stocks (S&P 500/400/600) — technicals via         │
│    yfinance, news/sentiment/earnings via Finnhub                  │
│  • market_risk.py: 3-lens Market Risk Assessment                  │
│    (recession gauges · froth gauges · trend confirmation)         │
│  → commits momentum_data.json                                     │
├─ Sundays 10:00 UTC ───────────────────────────────────────────────┤
│ backtest_history.py (~5-10 min)                                   │
│  • 5 years of daily adjusted closes, current S&P 500 members      │
│  → commits backtest_prices.json (compact, delta-encoded)          │
├─ Always ──────────────────────────────────────────────────────────┤
│ GitHub Pages serves the dashboard:                                │
│   https://YOUR_USERNAME.github.io/marketpulse/                    │
│ Tabs: Sector ETFs · Stock Scanner · Market Regime (+ Risk         │
│ Assessment) · News & Sentiment · Build Guide · Backtest (real     │
│ historical replay)                                                │
└───────────────────────────────────────────────────────────────────┘
```

## Repository layout

| File | Purpose |
|---|---|
| `momentum_pipeline.py` | Nightly engine: scans ~1,500 stocks, scores momentum, calls the risk engine, writes `momentum_data.json` |
| `market_risk.py` | 3-lens Market Risk Assessment (FRED + yfinance + best-effort scrapes with stale-data fallback) |
| `backtest_history.py` | Weekly export of 5-year price history for the Backtest tab |
| `MarketPulse_v8_live.html` | The dashboard (single file — all tabs, charts, backtest engine) |
| `index.html` | Redirects the Pages root URL to the dashboard |
| `momentum_data.json` | Latest scan output (auto-committed nightly) |
| `backtest_prices.json` | 5-year price history (auto-committed weekly) |
| `.github/workflows/momentum_pipeline.yml` | Daily data refresh workflow |
| `.github/workflows/backtest_history.yml` | Weekly price history workflow |
| `setup.sh` | One-command setup (repo, secret, Pages, first runs) |

## API keys

| Key | Needed? | What it powers |
|---|---|---|
| `FINNHUB_API_KEY` | Recommended (free — [finnhub.io/register](https://finnhub.io/register)) | News, sentiment, earnings calendar (25% of momentum score). Everything else runs without it. |

All other data sources (Yahoo Finance via yfinance, FRED, Wikipedia) need no key.

## Automated setup

Requires [GitHub CLI](https://cli.github.com) (`brew install gh`), then:

```bash
cd ~/Desktop/MomentumOS
./setup.sh
```

It creates the public repo, pushes, sets the secret, enables Pages, and kicks off
both workflows. ~3 minutes of prompts, then wait for the first runs to finish.

## Manual setup

1. **Create repo** — [github.com/new](https://github.com/new) → name `marketpulse` → **Public**
   (public is required for free GitHub Pages; your API key stays in encrypted Secrets, never in code).

2. **Push this folder**
   ```bash
   cd ~/Desktop/MomentumOS
   git init && git add . && git commit -m "MarketPulse initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/marketpulse.git
   git push -u origin main
   ```

3. **Add the secret** — repo **Settings → Secrets and variables → Actions → New repository secret**:
   name `FINNHUB_API_KEY`, value = your key.

4. **Enable Pages** — repo **Settings → Pages** → Deploy from a branch → `main` / `/ (root)` → Save.
   Dashboard URL: `https://YOUR_USERNAME.github.io/marketpulse/`

5. **First runs** — repo **Actions** tab:
   - Run **"MarketPulse Data Refresh (Daily)"** (~15–30 min) → dashboard shows live data
   - Run **"Backtest Price History (Weekly)"** (~5–10 min) → Backtest tab works

## Data-quality indicators

- Header chip on the Market Risk section: **✓ all gauges live** / **◐ n stale** / **⚠ n missing**
- Stale gauges show a `STALE · date` badge (live source failed; last good reading shown)
- The five scraped gauges (NAAIM, AAII, ISM, Consumer Confidence, IPO count) are the only
  ones that can go stale — FRED and yfinance sources are reliable

## Known limitations (by design, disclosed in the UI)

- **Backtest**: current S&P 500 members only (survivorship bias); exits on daily closes
  (no intraday stops); options/spread P&L approximated from the real underlying move.
- **Momentum score history**: backtest signals are price-only (no historical news sentiment).
- **Cron is UTC**: schedules don't shift with daylight saving (22:00 UTC = 6pm ET summer, 5pm ET winter).

## Troubleshooting

- **Workflow failed** → Actions tab → click the red run for full logs. GitHub also emails you on failure.
- **Dashboard stale** → check the last "Refresh momentum data" commit; Pages redeploys 1–2 min after each commit.
- **Backtest says price file not found** → run the weekly workflow once manually.
- **Schedules stopped after ~60 days** → GitHub pauses crons on inactive repos; the nightly data commits prevent this, but if all workflows fail for weeks, crons can pause — re-enable from the Actions tab.
