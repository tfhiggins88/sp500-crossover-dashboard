# S&P 500 SMA Crossover Dashboard

A daily dashboard that scans the **current S&P 500** for **5-day / 20-day simple
moving-average (SMA) crossovers** and publishes an interactive, phone-friendly
web page.

- **Bullish crossover** – the fast average (SMA5) crosses *above* the slow one (SMA20).
- **Bearish crossover** – the fast average crosses *below* the slow one.

It looks at the **last 5 trading days**, charts every ticker that crossed, and
bundles all the charts into a single self-contained `docs/index.html`. A GitHub
Actions workflow regenerates and republishes it every morning at ~7:00 AM
Eastern.

---

## What's in here

| File | What it does |
|------|--------------|
| `crossover.py` | The whole pipeline: get tickers → download prices → compute SMAs → detect crossovers → build charts → write `docs/index.html`. Heavily commented. |
| `requirements.txt` | Python dependencies (`yfinance`, `pandas`, `plotly`, `lxml`, `requests`). |
| `.github/workflows/daily-crossover.yml` | Runs the script daily and pushes the updated page. |
| `docs/index.html` | The generated dashboard (this is what GitHub Pages serves). |
| `sp500_cache.json` | Local, git-ignored cache of the S&P 500 list (one scrape per day). |

---

## How `crossover.py` works, section by section

1. **`get_tickers()` / `get_sp500_tickers()`**
   Either uses your custom `TICKER_OVERRIDE` list, or scrapes the
   *["List of S&P 500 companies"](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies)*
   Wikipedia table with `pandas.read_html`. The result is cached in
   `sp500_cache.json` and only re-scraped once per calendar day. Ticker symbols
   with a dot (`BRK.B`) are converted to Yahoo's dash form (`BRK-B`).

2. **`download_prices()`**
   Downloads ~3 months of daily data via `yfinance` in **batches of 50**
   (one `yf.download()` call per batch, not one call per ticker). Any ticker
   that fails – delisted, rate-limited, network blip – is collected and
   **retried** up to `MAX_RETRIES` times, then finally reported as "failed"
   rather than crashing the run.

3. **`find_crossovers()`**
   For each ticker: compute `SMA5` and `SMA20` of the close, take the
   *spread* `SMA5 − SMA20`, and look for a **sign change** from one day to the
   next within the last 5 trading days. Each event is recorded with its date
   and direction.

4. **`make_chart()`**
   A Plotly figure for the last 30 trading days: close price, SMA5, SMA20, and
   a dotted vertical line + dot on each crossover date. Interactive – on a
   phone you can pinch-zoom, pan, and tap points for values.

5. **`render_page()`**
   Stitches every chart into one mobile-first `index.html`:
   - sticky header with the generation time, tickers-scanned count, and
     bullish / bearish counts;
   - one card per crossover ticker, stacked vertically, full-width;
   - a "No crossovers today." message if the list is empty;
   - a collapsible list of any tickers that couldn't be fetched;
   - Plotly's JavaScript is **bundled directly into the file** by default
     (`BUNDLE_PLOTLY_JS = True`) so the page needs no CDN and no internet to
     render. Flip it to `False` for a much smaller file that loads Plotly from
     its CDN.

### Config knobs (top of `crossover.py`)

| Setting | Default | Meaning |
|---------|---------|---------|
| `TICKER_OVERRIDE` | `[]` | Non-empty = scan *this* list instead of the S&P 500. Great for testing: `["AAPL", "MSFT", "NVDA"]`. |
| `SHORT_WINDOW` / `LONG_WINDOW` | `5` / `20` | The two SMA lengths. |
| `LOOKBACK_DAYS` | `5` | How many recent trading days to search for a crossover. |
| `CHART_DAYS` | `30` | Trading days shown per chart. |
| `BATCH_SIZE` | `50` | Tickers per download call. |
| `MAX_RETRIES` | `2` | Extra fetch attempts for failed tickers. |
| `BUNDLE_PLOTLY_JS` | `True` | Inline Plotly JS (self-contained) vs. CDN (smaller). |

---

## Run it locally

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
python crossover.py
```

Then open `docs/index.html` in a browser.

**Tip:** for a fast first run, set `TICKER_OVERRIDE = ["AAPL", "MSFT", "NVDA", "AMD", "TSLA"]`
at the top of `crossover.py` so you're not downloading all 500 tickers.

---

## One-time GitHub setup

### 1. Create the repo and push

On [github.com](https://github.com/new), create a **new empty repo** (no README,
no .gitignore, no license) under your account **@tfhiggins88**, named
**`sp500-crossover-dashboard`**.

Then, from this project folder:

```bash
git init
git add .
git commit -m "Initial commit: S&P 500 SMA crossover dashboard"
git branch -M main
git remote add origin https://github.com/tfhiggins88/sp500-crossover-dashboard.git
git push -u origin main
```

### 2. Generate the dashboard once, so `docs/index.html` exists

You can either run `python crossover.py` locally and commit the result, or just
trigger the workflow manually (next step) and let it create the file. Either
works – GitHub Pages needs `docs/index.html` to exist before it can serve it.

```bash
python crossover.py
git add docs/index.html
git commit -m "Add first generated dashboard"
git push
```

### 3. Enable GitHub Pages

1. Repo → **Settings** → **Pages**.
2. **Build and deployment** → **Source: Deploy from a branch**.
3. **Branch:** `main`, **Folder:** `/docs`. Click **Save**.
4. Wait ~1 minute. The page will be live at:

   **https://tfhiggins88.github.io/sp500-crossover-dashboard/**

   Bookmark that on your phone.

### 4. Give the workflow permission to push

The workflow already declares `permissions: contents: write`, but the repo
setting must also allow it:

1. Repo → **Settings** → **Actions** → **General**.
2. Scroll to **Workflow permissions**.
3. Select **Read and write permissions**. **Save**.

(If your account/org disables scheduled workflows on inactive repos, just visit
the repo or push a commit occasionally – GitHub pauses cron on repos with no
activity for 60 days.)

### 5. Test the automation

Repo → **Actions** tab → **Daily SMA Crossover Dashboard** → **Run workflow**.
It should install deps, run the scan, and push an updated `docs/index.html`.
Refresh the Pages URL to see it.

---

## Schedule details

`daily-crossover.yml` has two cron entries – `11:00 UTC` and `12:00 UTC` –
because 7:00 AM US Eastern is 11:00 UTC during daylight saving and 12:00 UTC
otherwise. The first job step reads the real `America/New_York` hour and exits
early on whichever run *isn't* 7 AM there, so it effectively fires **once a day
at 7 AM Eastern, all year**, with no seasonal edits. Manual runs
(`workflow_dispatch`) always execute.

---

## Notes & limitations

- **Not investment advice.** This is an educational project. SMA crossovers are
  a lagging indicator and produce plenty of false signals.
- Yahoo Finance data is free and occasionally flaky; failed tickers are listed
  on the page rather than halting the run.
- `auto_adjust=True` means prices are adjusted for splits and dividends.
