# Prompt for Claude Code

Paste everything below into Claude Code (in the folder where you want the project created).

---

Build me a daily moving-average crossover dashboard. Here's what I need:

## 1. Data & calculation
- Python script (`crossover.py`) using the `yfinance` library (install via pip, add to a `requirements.txt`).
- Ticker list: the current S&P 500 constituents. Don't hardcode all 500 tickers — pull the current list programmatically at runtime (e.g., from the Wikipedia "List of S&P 500 companies" page, which is a common reliable source for this) so it stays accurate as the index changes. Cache it locally for the day so we're not re-scraping it on every run. Also leave an easy override at the top of the script (an editable list) in case I want to point it at a custom watchlist or a smaller test set instead.
- For each ticker, pull the last 40 trading days of daily OHLC data (extra buffer so the 20-day average is always fully calculable).
- Note: with 500 tickers, be mindful of `yfinance` rate limits — batch the downloads (yfinance supports pulling multiple tickers in one call) rather than looping one ticker at a time, and add basic retry logic for tickers that fail to fetch.
- Compute the 5-day and 20-day simple moving averages (SMA) of the closing price.
- For each of the last 5 trading days, check whether the SMA5 crossed above or below the SMA20 (a sign change in SMA5 − SMA20 versus the prior day).
- Build a results list of every ticker with at least one crossover in the last 5 trading days, noting the date and direction (bullish/up or bearish/down).

## 2. Visual output
- For every ticker that had a crossover, generate a chart of the last 30 trading days showing:
  - the daily closing price line
  - the 5-day SMA line
  - the 20-day SMA line
  - a marker (dot or vertical line) at the exact crossover date
- Use Plotly (not matplotlib) so the charts are interactive and render well on a phone browser — pinch-zoom, tap for values, etc.
- Combine all the individual charts into a single self-contained HTML page (`index.html`):
  - Mobile-first responsive layout — charts stacked vertically, full-width, readable without zooming on a phone screen.
  - A header showing the date the dashboard was last generated and how many tickers crossed over (bullish count / bearish count).
  - If no tickers crossed over that day, show a simple "No crossovers today" message instead of an empty page.
  - Keep the page lightweight — no external CDN dependencies that could break; bundle Plotly's JS directly or use their standard CDN link, whichever keeps the file smallest.

## 3. Automation (GitHub Actions + GitHub Pages)
- My GitHub account is **@tfhiggins88**. Set this up as a git repository ready to push to a new repo under that account (suggest a repo name like `sp500-crossover-dashboard` unless you have a better one), and give me the exact commands to create and push to it.
- Add a GitHub Actions workflow (`.github/workflows/daily-crossover.yml`) that:
  - Runs on a daily schedule at roughly 7:00 AM Eastern time (after the prior day's market close and before I'd check it in the morning) — remember GitHub Actions cron is in UTC, so convert accordingly and account for daylight saving.
  - Also supports manual trigger (`workflow_dispatch`) so I can re-run it on demand.
  - Installs dependencies, runs `crossover.py`, and outputs `index.html` into a `docs/` folder (or a dedicated branch — pick whichever is simpler to wire up with GitHub Pages).
  - Commits and pushes the updated `index.html` back to the repo automatically.
- Walk me through the one-time setup steps I need to do manually: creating the GitHub repo, enabling GitHub Pages (pointing at the `docs/` folder or output branch), and any permissions the Actions workflow needs to push commits.
- Once it's live, the GitHub Pages URL will be something like `https://tfhiggins88.github.io/sp500-crossover-dashboard/` (adjust based on the actual repo name we land on) — confirm the exact URL for me once it's deployed so I know what to bookmark on my phone.

## 4. Extras
- Add basic error handling: if `yfinance` fails to fetch a ticker (delisted, rate-limited, etc.), skip it and note it in the page rather than crashing the whole run.
- Keep the code organized and commented — I'm still learning Python, so I'd like to be able to read through it and understand what each part does, not just have it work as a black box.
