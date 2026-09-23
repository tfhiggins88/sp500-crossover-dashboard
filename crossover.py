#!/usr/bin/env python3
"""
crossover.py
============

Daily 5-day / 20-day simple-moving-average (SMA) crossover scanner for the
S&P 500.

Big picture, top to bottom:

  1. Decide which tickers to scan
     - either your own custom list (TICKER_OVERRIDE below), or
     - the *current* S&P 500 constituents, scraped once per day from Wikipedia
       and cached to disk so we don't hammer that page on every run.

  2. Download ~3 months of daily price history for those tickers from Yahoo
     Finance. This is done in batches (yfinance can fetch many tickers in one
     call) with retry logic for anything that fails.

  3. For each ticker, compute the 5-day and 20-day SMA of the closing price.

  4. Detect whether SMA5 crossed *above* (bullish) or *below* (bearish) SMA20
     during the last LOOKBACK_DAYS trading days. A "crossover" is simply a
     sign change in (SMA5 - SMA20) from one day to the next.

  5. For every ticker that had a crossover, draw an interactive Plotly chart of
     the last 30 trading days (close, SMA5, SMA20, and a marker on the exact
     crossover date).

  6. Stitch all the charts into ONE self-contained, mobile-friendly page:
     docs/index.html

Run it:      python crossover.py
Outputs:     docs/index.html   and   sp500_cache.json
"""

from __future__ import annotations

import io
import json
import time
import datetime as dt
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs


# ===========================================================================
# CONFIG  --  the knobs you might want to turn are all right here
# ===========================================================================

# --- Which tickers to scan -------------------------------------------------
# Put your own tickers here to scan a custom watchlist or a small test set,
# e.g.  TICKER_OVERRIDE = ["AAPL", "MSFT", "NVDA", "AMD"]
# Leave it empty  ( [] )  to scan the current S&P 500.
TICKER_OVERRIDE: list[str] = []

# --- Moving-average settings ---------------------------------------------
SHORT_WINDOW = 5     # "fast" SMA, in trading days
LONG_WINDOW = 20     # "slow" SMA, in trading days
LOOKBACK_DAYS = 2    # how many recent trading days to search for a crossover
CHART_DAYS = 30      # how many trading days to show on each chart

# --- Download settings ---------------------------------------------------
# We need at least LONG_WINDOW (20) days to "warm up" the 20-day average,
# plus the lookback window, plus a buffer for market holidays. "3mo" is
# roughly 63 trading days -- plenty of headroom.
DOWNLOAD_PERIOD = "3mo"
BATCH_SIZE = 50            # tickers per yfinance download call
BATCH_PAUSE_SECONDS = 1.0  # polite pause between batches
MAX_RETRIES = 2            # extra attempts for tickers that fail to fetch

# --- Output ------------------------------------------------------------
OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "index.html"

# --- S&P 500 constituent list -----------------------------------------
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP500_CACHE_FILE = Path("sp500_cache.json")

# --- Plotly bundling --------------------------------------------------
# True  -> paste Plotly's JavaScript straight into index.html. The page is
#          then fully self-contained and works with no internet at all.
#          (Adds ~3.5 MB to the HTML file. Totally fine for GitHub Pages.)
# False -> load Plotly from its CDN instead. Much smaller file, but the page
#          needs internet + the CDN to be up in order to render.
BUNDLE_PLOTLY_JS = True
PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"

# Colors used for bullish / bearish markers throughout the page.
BULL_COLOR = "#1a9850"   # green
BEAR_COLOR = "#d73027"   # red


# ===========================================================================
# STEP 1 -- work out the ticker list
# ===========================================================================

def get_tickers() -> list[str]:
    """Return the list of tickers to scan.

    If TICKER_OVERRIDE is set, use that. Otherwise fetch the current S&P 500
    constituents (cached for the day).
    """
    if TICKER_OVERRIDE:
        print(f"Using custom TICKER_OVERRIDE list ({len(TICKER_OVERRIDE)} tickers).")
        # Normalise the same way we do for the S&P 500 list.
        return [t.strip().upper().replace(".", "-") for t in TICKER_OVERRIDE]

    return get_sp500_tickers()


def get_sp500_tickers() -> list[str]:
    """Scrape the current S&P 500 constituents from Wikipedia, with a daily cache."""
    today = dt.date.today().isoformat()

    # 1a. Try the cache first -- but only if it was written *today*.
    if SP500_CACHE_FILE.exists():
        try:
            cached = json.loads(SP500_CACHE_FILE.read_text())
            if cached.get("date") == today and cached.get("tickers"):
                print(f"Using cached S&P 500 list from {today} "
                      f"({len(cached['tickers'])} tickers).")
                return cached["tickers"]
        except (json.JSONDecodeError, OSError):
            pass  # cache is unreadable -- just re-scrape

    # 1b. Fetch the page. Wikipedia is picky about anonymous user agents, so
    #     we set a descriptive one and hand the HTML to pandas ourselves.
    print("Scraping current S&P 500 list from Wikipedia...")
    headers = {"User-Agent": "sp500-crossover-dashboard (educational project)"}
    resp = requests.get(SP500_WIKI_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    # The first table on that page is the list of constituents.
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]
    tickers = [str(sym).strip().upper() for sym in df["Symbol"].tolist()]

    # Yahoo Finance writes class shares with a dash, Wikipedia uses a dot:
    #   BRK.B  ->  BRK-B        BF.B  ->  BF-B
    tickers = [t.replace(".", "-") for t in tickers]

    # 1c. Save the cache for the rest of the day.
    try:
        SP500_CACHE_FILE.write_text(
            json.dumps({"date": today, "tickers": tickers}, indent=2)
        )
        print(f"Found {len(tickers)} tickers; cached to {SP500_CACHE_FILE}.")
    except OSError as e:
        print(f"(Could not write cache file: {e})")

    return tickers


# ===========================================================================
# STEP 2 -- download price history
# ===========================================================================

def download_prices(
    tickers: list[str], period: str = DOWNLOAD_PERIOD
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Download daily OHLC history for every ticker.

    `period` defaults to DOWNLOAD_PERIOD (enough for the dashboard's SMAs),
    but callers that need more history -- e.g. backtest.py scanning years of
    crossovers -- can pass a longer one like "2y".

    Returns a tuple (data, failed):
      data   -- { ticker: DataFrame indexed by date, with a 'Close' column }
      failed -- list of tickers we could not fetch after all retries
    """
    data: dict[str, pd.DataFrame] = {}
    remaining = list(tickers)

    # attempt 1 is the first pass; the rest are retries.
    for attempt in range(1, MAX_RETRIES + 2):
        if not remaining:
            break

        if attempt > 1:
            print(f"\nRetry {attempt - 1}/{MAX_RETRIES}: "
                  f"{len(remaining)} ticker(s) still to fetch...")
            time.sleep(BATCH_PAUSE_SECONDS * 3)  # back off a bit longer

        still_failing: list[str] = []

        for start in range(0, len(remaining), BATCH_SIZE):
            batch = remaining[start:start + BATCH_SIZE]
            print(f"  downloading {start + 1}-{start + len(batch)} "
                  f"of {len(remaining)}...")

            try:
                raw = yf.download(
                    batch,
                    period=period,
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,   # adjust for splits/dividends
                    threads=True,       # parallelise within the batch
                    progress=False,
                )
            except Exception as e:  # network error, rate limit, etc.
                print(f"    whole batch failed ({e}); will retry those tickers")
                still_failing.extend(batch)
                continue

            # Pull each ticker's sub-frame out of the combined result.
            for t in batch:
                df = _extract_one_ticker(raw, t)
                if df is not None and not df["Close"].dropna().empty:
                    data[t] = df
                else:
                    still_failing.append(t)

            time.sleep(BATCH_PAUSE_SECONDS)

        remaining = still_failing

    return data, remaining


def _extract_one_ticker(raw: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """Get a single ticker's DataFrame out of a yf.download() result.

    yfinance returns different shapes depending on how many tickers came back:
      - many tickers  -> columns are a MultiIndex like ('AAPL', 'Close')
      - one ticker    -> columns are just ('Close', 'High', ...)
    This helper copes with both.
    """
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker not in raw.columns.get_level_values(0):
                return None
            df = raw[ticker].copy()
        else:
            df = raw.copy()
    except (KeyError, TypeError):
        return None

    df = df.dropna(how="all")
    if df.empty or "Close" not in df.columns:
        return None
    return df


# ===========================================================================
# STEP 3 & 4 -- moving averages + crossover detection
# ===========================================================================

def compute_sma_spread(close: pd.Series) -> pd.Series:
    """Return SMA{SHORT_WINDOW} - SMA{LONG_WINDOW} for a closing-price series.

    Positive means the fast average is above the slow one. The first
    LONG_WINDOW-1 days (not enough history yet to form a full SMA20) are
    dropped. Shared by find_crossovers() (recent days only) and backtest.py
    (the entire history), so the two never disagree about what a crossover is.
    """
    sma_short = close.rolling(SHORT_WINDOW).mean()
    sma_long = close.rolling(LONG_WINDOW).mean()
    return (sma_short - sma_long).dropna()


def find_crossovers(data: dict[str, pd.DataFrame]) -> list[dict]:
    """Scan every ticker for SMA5/SMA20 crossovers in the last LOOKBACK_DAYS.

    Returns a list of dicts, one per crossover event:
      { "ticker": "AAPL", "date": date(2026, 8, 28), "direction": "bullish" }
    """
    results: list[dict] = []

    for ticker, df in sorted(data.items()):
        close = df["Close"].dropna()

        # Need at least LONG_WINDOW days to form one SMA20 value, plus one more
        # day to compare against for a sign change.
        if len(close) < LONG_WINDOW + 1:
            continue

        spread = compute_sma_spread(close)
        if len(spread) < 2:
            continue

        prev = spread.shift(1)
        crossed_up = (prev <= 0) & (spread > 0)    # fast crossed ABOVE slow
        crossed_down = (prev >= 0) & (spread < 0)  # fast crossed BELOW slow

        # Only look at the most recent few trading days.
        for date in spread.index[-LOOKBACK_DAYS:]:
            if bool(crossed_up.get(date, False)):
                direction = "bullish"
            elif bool(crossed_down.get(date, False)):
                direction = "bearish"
            else:
                continue

            results.append({
                "ticker": ticker,
                "date": pd.Timestamp(date).date(),
                "direction": direction,
            })

    return results


# ===========================================================================
# STEP 5 -- one interactive chart per crossover ticker
# ===========================================================================

def make_chart(ticker: str, crossings: list[dict], df: pd.DataFrame) -> go.Figure:
    """Build a Plotly figure: close price + both SMAs + crossover markers."""
    close = df["Close"].dropna()
    sma_short = close.rolling(SHORT_WINDOW).mean()
    sma_long = close.rolling(LONG_WINDOW).mean()

    # Show only the most recent CHART_DAYS trading days.
    window = close.index[-CHART_DAYS:]
    c = close.loc[window]
    s = sma_short.loc[window]
    l = sma_long.loc[window]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=c.index, y=c.values, name="Close",
                             mode="lines", line=dict(width=2, color="#333333")))
    fig.add_trace(go.Scatter(x=s.index, y=s.values, name=f"SMA{SHORT_WINDOW}",
                             mode="lines", line=dict(width=1.5, color="#4575b4")))
    fig.add_trace(go.Scatter(x=l.index, y=l.values, name=f"SMA{LONG_WINDOW}",
                             mode="lines", line=dict(width=1.5, color="#fc8d59")))

    # Vertical dotted line + dot on each crossover date. We draw the vertical
    # line as a 2-point Scatter (rather than fig.add_vline) because that has
    # historically been buggy with date axes across Plotly versions.
    y_lo = float(pd.concat([c, s, l]).min())
    y_hi = float(pd.concat([c, s, l]).max())

    for cr in crossings:
        d = pd.Timestamp(cr["date"])
        color = BULL_COLOR if cr["direction"] == "bullish" else BEAR_COLOR

        fig.add_trace(go.Scatter(
            x=[d, d], y=[y_lo, y_hi], mode="lines",
            line=dict(color=color, width=1, dash="dot"),
            showlegend=False, hoverinfo="skip",
        ))

        price = close.get(d)
        if price is not None:
            fig.add_trace(go.Scatter(
                x=[d], y=[price], mode="markers", showlegend=False,
                marker=dict(size=12, color=color, symbol="circle",
                            line=dict(width=1, color="white")),
                hovertext=[f"{cr['direction'].title()} crossover<br>{cr['date']}"],
                hoverinfo="text",
            ))

    fig.update_layout(
        title=dict(text=ticker, font=dict(size=18)),
        margin=dict(l=8, r=8, t=64, b=8),
        height=340,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    font=dict(size=11)),
        hovermode="x unified",
        dragmode="pan",
        plot_bgcolor="white",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eeeeee")
    fig.update_yaxes(showgrid=True, gridcolor="#eeeeee")
    return fig


# ===========================================================================
# STEP 6 -- assemble one self-contained HTML page
# ===========================================================================

PAGE_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1a1a1a;
  background: #f5f5f7;
  line-height: 1.4;
}
header {
  background: #ffffff;
  padding: 16px;
  border-bottom: 1px solid #e0e0e0;
  position: sticky;
  top: 0;
  z-index: 10;
}
header h1 { margin: 0 0 4px; font-size: 1.15rem; }
header .meta { font-size: 0.85rem; color: #666; }
.counts { margin-top: 8px; font-size: 0.95rem; }
.pill {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  color: #fff;
  font-weight: 600;
  margin-right: 6px;
}
.pill.bull { background: #1a9850; }
.pill.bear { background: #d73027; }
main { padding: 12px; max-width: 900px; margin: 0 auto; }
.chart-card {
  background: #fff;
  border: 1px solid #e0e0e0;
  border-radius: 10px;
  margin-bottom: 14px;
  padding: 6px;
  overflow: hidden;
}
.chart-card .tag {
  font-size: 0.8rem;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 6px;
  color: #fff;
}
.tag.bull { background: #1a9850; }
.tag.bear { background: #d73027; }
.chart-head { padding: 8px 6px 0; }
.empty {
  text-align: center;
  padding: 60px 20px;
  color: #555;
  font-size: 1.1rem;
}
details { margin-top: 20px; font-size: 0.85rem; color: #666; }
details summary { cursor: pointer; padding: 8px 0; }
.failed-list { word-break: break-word; }
footer { text-align: center; padding: 24px 12px; color: #999; font-size: 0.8rem; }
.plotly-graph-div { width: 100% !important; }
"""


def render_page(results: list[dict],
                data: dict[str, pd.DataFrame],
                failed: list[str],
                scanned_count: int) -> str:
    """Return the complete HTML for index.html."""
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    bull = sum(1 for r in results if r["direction"] == "bullish")
    bear = sum(1 for r in results if r["direction"] == "bearish")

    # Group crossover events by ticker (a ticker could cross twice in 5 days).
    by_ticker: dict[str, list[dict]] = {}
    for r in results:
        by_ticker.setdefault(r["ticker"], []).append(r)

    # --- Plotly JS: bundled inline, or a CDN <script> tag ---
    if BUNDLE_PLOTLY_JS:
        plotly_js = f"<script>{get_plotlyjs()}</script>"
    else:
        plotly_js = f'<script src="{PLOTLY_CDN}"></script>'

    # --- Build the body ---
    if not by_ticker:
        body = '<div class="empty">No crossovers today.<br>Check back tomorrow.</div>'
    else:
        cards = []
        for ticker in sorted(by_ticker):
            crossings = sorted(by_ticker[ticker], key=lambda r: r["date"])
            fig = make_chart(ticker, crossings, data[ticker])
            chart_div = fig.to_html(full_html=False, include_plotlyjs=False,
                                    default_width="100%",
                                    config={"responsive": True,
                                            "displayModeBar": False,
                                            "scrollZoom": True})
            tags = " ".join(
                f'<span class="tag {"bull" if cr["direction"] == "bullish" else "bear"}">'
                f'{cr["direction"].upper()} {cr["date"]}</span>'
                for cr in crossings
            )
            cards.append(
                f'<div class="chart-card">'
                f'<div class="chart-head">{tags}</div>'
                f'{chart_div}'
                f'</div>'
            )
        body = "\n".join(cards)

    # --- Failed-ticker disclosure ---
    failed_block = ""
    if failed:
        failed_block = (
            f'<details><summary>{len(failed)} ticker(s) could not be fetched '
            f'and were skipped</summary>'
            f'<p class="failed-list">{", ".join(sorted(failed))}</p></details>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>S&amp;P 500 SMA Crossover Dashboard</title>
{plotly_js}
<style>{PAGE_CSS}</style>
</head>
<body>
<header>
  <h1>S&amp;P 500 &mdash; 5/20 SMA Crossovers</h1>
  <div class="meta">Last generated: {generated} &middot; {scanned_count} tickers scanned</div>
  <div class="counts">
    <span class="pill bull">{bull} bullish</span>
    <span class="pill bear">{bear} bearish</span>
    in the last {LOOKBACK_DAYS} trading days
  </div>
</header>
<main>
{body}
{failed_block}
</main>
<footer>
  Built with yfinance + Plotly &middot; Educational use only, not investment advice.
</footer>
</body>
</html>
"""


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    print("=" * 60)
    print("SMA 5/20 crossover scan")
    print("=" * 60)

    tickers = get_tickers()
    print(f"\nScanning {len(tickers)} tickers.\n")

    data, failed = download_prices(tickers)
    print(f"\nFetched {len(data)} tickers OK; {len(failed)} failed.")

    results = find_crossovers(data)
    bull = sum(1 for r in results if r["direction"] == "bullish")
    bear = sum(1 for r in results if r["direction"] == "bearish")
    print(f"\nCrossovers in the last {LOOKBACK_DAYS} trading days: "
          f"{bull} bullish, {bear} bearish "
          f"(across {len({r['ticker'] for r in results})} tickers).")
    for r in sorted(results, key=lambda r: (r["date"], r["ticker"])):
        print(f"  {r['date']}  {r['ticker']:<6}  {r['direction']}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    html = render_page(results, data, failed, scanned_count=len(tickers))
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    size_mb = OUTPUT_FILE.stat().st_size / 1_000_000
    print(f"\nWrote {OUTPUT_FILE}  ({size_mb:.1f} MB)")
    print("Done.")


if __name__ == "__main__":
    main()

