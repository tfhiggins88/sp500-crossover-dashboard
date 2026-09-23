#!/usr/bin/env python3
"""
backtest.py
===========

Backtests the bullish half of the 5/20 SMA crossover signal from
crossover.py: if you'd bought one share on the close of the day a stock
flashed a bullish crossover, what holding period would actually have made
money?

Why this isn't just "average up the returns and see":

  - crossover.py only looks at the last couple of trading days. A backtest
    needs to scan YEARS of history to find enough past crossover events to
    draw a conclusion from.

  - In a rising market, almost any stock bought on almost any day will show
    a positive return eventually -- that measures "the market went up", not
    "this signal is useful". So for every sampled trade we also compute what
    buying-and-holding SPY (the S&P 500 index) over that exact same window
    would have returned, and report whether the signal actually beat it.

What it does, step by step:

  1. Pull a random sample of S&P 500 tickers (the full 500 works too, it's
     just slower) and download ~2 years of daily history for them, plus SPY
     as a benchmark. Reuses crossover.py's ticker list and batched downloader.

  2. Scan each ticker's ENTIRE history (not just the last few days) for every
     bullish crossover -- every day SMA5 crossed above SMA20.

  3. Randomly sample a fixed number of those events (seeded, so re-running
     this script reproduces the same sample).

  4. For each sampled event, and for several holding periods (1, 3, 5, 10...
     trading days), compute:
       - the stock's return from the crossover-day close to N trading days later
       - SPY's return over that same window, as a baseline
     Not every event has enough *future* data for every holding period yet
     (a crossover from last week can't tell you its 60-day return) -- those
     are simply excluded from that holding period's stats, which is why the
     trade count (N) shrinks for longer holding periods.

  5. Summarize per holding period: win rate, mean/median return, and how
     often the signal beat SPY over the same window.

Run it:      python backtest.py
Outputs:     printed summary table, backtest_results.csv (one row per
             sampled trade), backtest_report.html (a small chart)
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots

import crossover  # reuse the ticker list, the batched downloader, and the SMA math


# ===========================================================================
# CONFIG
# ===========================================================================

# How much history to pull per ticker. Needs to be long enough to contain a
# useful number of past crossovers.
BACKTEST_PERIOD = "2y"

# How many S&P 500 tickers to scan for crossovers. None = scan all ~500
# (much slower, more accurate). A sample of 100 finds plenty of events fast.
TICKER_SAMPLE_SIZE = 100

# How many of the crossover events found in step 2 to actually backtest.
N_SAMPLE_TRADES = 150

# Holding periods to test, in trading days (not calendar days).
HOLDING_PERIODS = [1, 3, 5, 10, 15, 20, 30, 45, 60]

# What to compare the signal against: buy-and-hold this ticker over the same
# window as each sampled trade.
BENCHMARK_TICKER = "SPY"

# Fixes the random sample so re-running this script gives the same answer.
# Change it (or set to None) if you want a fresh random sample instead.
RANDOM_SEED = 42

RESULTS_CSV = Path("backtest_results.csv")
REPORT_HTML = Path("backtest_report.html")


# ===========================================================================
# STEP 2 -- find every bullish crossover in a ticker's full history
# ===========================================================================

def find_all_bullish_crossovers(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Scan each ticker's entire downloaded history for bullish crossovers.

    Unlike crossover.py's find_crossovers() (which only checks the last few
    days for the live dashboard), this checks every day, because a backtest
    needs a pool of *past* events to sample from.

    Returns a DataFrame with one row per event: ticker, date, entry_price,
    and pos (that date's integer position in the ticker's own close series,
    used later to look up "N trading days after this one").
    """
    rows = []
    for ticker, df in data.items():
        close = df["Close"].dropna()
        if len(close) < crossover.LONG_WINDOW + 1:
            continue

        spread = crossover.compute_sma_spread(close)
        prev = spread.shift(1)
        crossed_up = (prev <= 0) & (spread > 0)

        positions = {date: i for i, date in enumerate(close.index)}
        for date, is_crossover in crossed_up.items():
            if is_crossover:
                rows.append({
                    "ticker": ticker,
                    "date": pd.Timestamp(date).date(),
                    "pos": positions[date],
                    "entry_price": float(close.loc[date]),
                })

    return pd.DataFrame(rows)


# ===========================================================================
# STEP 4 -- forward returns for the sampled trades, plus the SPY benchmark
# ===========================================================================

def add_forward_returns(
    sample: pd.DataFrame, data: dict[str, pd.DataFrame], holding_periods: list[int]
) -> pd.DataFrame:
    """Add one 'ret_{h}d' column per holding period: the stock's own return
    from the crossover close to h trading days later. NaN if there isn't
    enough history yet for that horizon.
    """
    sample = sample.copy()
    for h in holding_periods:
        returns = []
        for row in sample.itertuples():
            close = data[row.ticker]["Close"].dropna()
            exit_pos = row.pos + h
            if exit_pos < len(close):
                returns.append(float(close.iloc[exit_pos]) / row.entry_price - 1)
            else:
                returns.append(np.nan)
        sample[f"ret_{h}d"] = returns
    return sample


def add_benchmark_returns(
    sample: pd.DataFrame, benchmark_close: pd.Series, holding_periods: list[int]
) -> pd.DataFrame:
    """Add one 'spy_ret_{h}d' column per holding period: what BENCHMARK_TICKER
    returned over that same crossover-date-to-h-trading-days-later window.
    """
    sample = sample.copy()
    bench_positions = {pd.Timestamp(d).date(): i for i, d in enumerate(benchmark_close.index)}

    for h in holding_periods:
        returns = []
        for row in sample.itertuples():
            entry_pos = bench_positions.get(row.date)
            exit_pos = None if entry_pos is None else entry_pos + h
            if entry_pos is None or exit_pos >= len(benchmark_close):
                returns.append(np.nan)
                continue
            entry_price = float(benchmark_close.iloc[entry_pos])
            exit_price = float(benchmark_close.iloc[exit_pos])
            returns.append(exit_price / entry_price - 1)
        sample[f"spy_ret_{h}d"] = returns
    return sample


# ===========================================================================
# STEP 5 -- summarize
# ===========================================================================

def summarize(sample: pd.DataFrame, holding_periods: list[int]) -> pd.DataFrame:
    """One row per holding period: win rate, mean/median return, and how the
    signal did against the SPY benchmark over the same windows.
    """
    rows = []
    for h in holding_periods:
        pair = sample[[f"ret_{h}d", f"spy_ret_{h}d"]].dropna()
        pair.columns = ["ret", "spy_ret"]
        if pair.empty:
            continue

        rows.append({
            "holding_days": h,
            "n_trades": len(pair),
            "win_rate": (pair["ret"] > 0).mean(),
            "mean_return": pair["ret"].mean(),
            "median_return": pair["ret"].median(),
            "spy_mean_return": pair["spy_ret"].mean(),
            "beat_spy_rate": (pair["ret"] > pair["spy_ret"]).mean(),
        })
    return pd.DataFrame(rows)


def format_summary_table(summary: pd.DataFrame) -> str:
    """Turn the summary DataFrame into a readable percentage-formatted table."""
    display = summary.copy()
    for col in ["win_rate", "mean_return", "median_return", "spy_mean_return", "beat_spy_rate"]:
        display[col] = (display[col] * 100).round(2).astype(str) + "%"
    display = display.rename(columns={
        "holding_days": "Hold (trading days)",
        "n_trades": "N trades",
        "win_rate": "Win rate",
        "mean_return": "Mean return",
        "median_return": "Median return",
        "spy_mean_return": f"Mean {BENCHMARK_TICKER} return",
        "beat_spy_rate": f"Beat {BENCHMARK_TICKER}",
    })
    return display.to_string(index=False)


# ===========================================================================
# Report -- a small self-contained HTML chart, same style as the dashboard
# ===========================================================================

def render_report(summary: pd.DataFrame, n_events_found: int, n_sampled: int) -> str:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12,
        subplot_titles=(
            "Win rate by holding period (dashed line = coin-flip odds)",
            f"Mean return vs. buy-and-hold {BENCHMARK_TICKER}, by holding period",
        ),
    )

    fig.add_trace(go.Scatter(
        x=summary["holding_days"], y=summary["win_rate"] * 100,
        mode="lines+markers", name="Win rate (% profitable trades)",
        line=dict(color="#1a9850", width=2),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=summary["holding_days"], y=summary["beat_spy_rate"] * 100,
        mode="lines+markers", name=f"Beat {BENCHMARK_TICKER} rate",
        line=dict(color="#4575b4", width=2, dash="dot"),
    ), row=1, col=1)
    fig.add_hline(y=50, line=dict(color="#999999", dash="dash", width=1), row=1, col=1)

    fig.add_trace(go.Bar(
        x=summary["holding_days"], y=summary["mean_return"] * 100,
        name="Signal mean return", marker_color="#1a9850",
    ), row=2, col=1)
    fig.add_trace(go.Bar(
        x=summary["holding_days"], y=summary["spy_mean_return"] * 100,
        name=f"{BENCHMARK_TICKER} mean return", marker_color="#999999",
    ), row=2, col=1)

    fig.update_yaxes(title_text="%", row=1, col=1)
    fig.update_yaxes(title_text="% return", row=2, col=1)
    fig.update_xaxes(title_text="Holding period (trading days)", row=2, col=1)
    fig.update_layout(
        height=700, barmode="group", plot_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=-0.18),
        margin=dict(t=60, b=20),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eeeeee")
    fig.update_yaxes(showgrid=True, gridcolor="#eeeeee")

    chart_html = fig.to_html(full_html=False, include_plotlyjs=False,
                             default_width="100%", config={"displayModeBar": False})

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bullish Crossover Backtest</title>
<script>{get_plotlyjs()}</script>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 900px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; }}
h1 {{ font-size: 1.3rem; margin-bottom: 4px; }}
.meta {{ color: #666; font-size: 0.9rem; margin-bottom: 16px; }}
.note {{ color: #666; font-size: 0.85rem; margin-top: 16px; }}
</style>
</head>
<body>
<h1>Bullish 5/20 SMA Crossover Backtest</h1>
<div class="meta">
  Generated {generated} &middot; {n_events_found} bullish crossovers found over
  the last {BACKTEST_PERIOD} &middot; {n_sampled} randomly sampled and backtested
  &middot; benchmark: buy-and-hold {BENCHMARK_TICKER}
</div>
{chart_html}
<p class="note">
  Not investment advice. Past crossovers do not predict future ones, and this
  sample is a snapshot of one market period -- rerun it periodically rather
  than trusting a single result.
</p>
</body>
</html>
"""


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    if RANDOM_SEED is not None:
        random.seed(RANDOM_SEED)

    print("=" * 60)
    print("Bullish crossover backtest")
    print("=" * 60)

    tickers = crossover.get_tickers()
    if TICKER_SAMPLE_SIZE and len(tickers) > TICKER_SAMPLE_SIZE:
        tickers = random.sample(tickers, TICKER_SAMPLE_SIZE)
        print(f"Sampled {len(tickers)} of the S&P 500 tickers to scan.")

    print(f"\nDownloading {BACKTEST_PERIOD} of history for {len(tickers)} tickers "
          f"plus the {BENCHMARK_TICKER} benchmark...\n")
    data, failed = crossover.download_prices(tickers, period=BACKTEST_PERIOD)
    bench_data, bench_failed = crossover.download_prices(
        [BENCHMARK_TICKER], period=BACKTEST_PERIOD
    )
    if BENCHMARK_TICKER not in bench_data:
        raise SystemExit(f"Could not download benchmark {BENCHMARK_TICKER}; aborting.")
    benchmark_close = bench_data[BENCHMARK_TICKER]["Close"].dropna()

    print(f"\nFetched {len(data)} tickers OK; {len(failed)} failed and were skipped.")

    events = find_all_bullish_crossovers(data)
    if events.empty:
        raise SystemExit("No bullish crossovers found in this history -- nothing to backtest.")
    print(f"Found {len(events)} bullish crossovers across "
          f"{events['ticker'].nunique()} tickers over the last {BACKTEST_PERIOD}.")

    n_sample = min(N_SAMPLE_TRADES, len(events))
    sample = events.sample(n=n_sample, random_state=RANDOM_SEED).reset_index(drop=True)
    print(f"Randomly sampling {len(sample)} of those events to backtest "
          f"(seed={RANDOM_SEED}).")

    sample = add_forward_returns(sample, data, HOLDING_PERIODS)
    sample = add_benchmark_returns(sample, benchmark_close, HOLDING_PERIODS)

    summary = summarize(sample, HOLDING_PERIODS)
    if summary.empty:
        raise SystemExit("None of the sampled events had enough future data yet. "
                          "Try a longer BACKTEST_PERIOD or shorter HOLDING_PERIODS.")

    print("\n" + format_summary_table(summary))

    best_win_rate = summary.loc[summary["win_rate"].idxmax()]
    best_vs_benchmark = summary.loc[summary["beat_spy_rate"].idxmax()]
    print(f"\nHighest win rate: {best_win_rate.holding_days:.0f} trading days "
          f"({best_win_rate.win_rate:.0%} of sampled trades were profitable, "
          f"n={best_win_rate.n_trades:.0f}).")
    print(f"Best vs. buy-and-hold {BENCHMARK_TICKER}: "
          f"{best_vs_benchmark.holding_days:.0f} trading days "
          f"(beat {BENCHMARK_TICKER} on {best_vs_benchmark.beat_spy_rate:.0%} of trades).")

    sample.to_csv(RESULTS_CSV, index=False)
    print(f"\nWrote per-trade results to {RESULTS_CSV}")

    html = render_report(summary, n_events_found=len(events), n_sampled=len(sample))
    REPORT_HTML.write_text(html, encoding="utf-8")
    print(f"Wrote chart report to {REPORT_HTML}")


if __name__ == "__main__":
    main()
