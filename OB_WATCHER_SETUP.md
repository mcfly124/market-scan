# Open-Breakout watcher — setup (real-time, free)

Real-time US market-open breakout alerts to your phone. **Zero Claude usage** —
detection is pure Python. Not financial advice; you own the risk.

## What it does
At the US open (15:30 your time), for 12 US stocks/ETFs it marks the first
15-min range, checks the 200-EMA trend, and pushes **one alert per symbol per
day** when a trend-aligned breakout triggers — with entry, stop, target, R:R.

## 1. Get a FREE Alpaca data key (no money, not a brokerage)
1. Sign up at **https://alpaca.markets/** (free "Paper" account is enough).
2. In the dashboard, generate **API Keys** — you get a **Key ID** and a **Secret**.
3. No funding, no card. This is only for the free real-time **IEX** data feed.

> Why Alpaca and not Finnhub: Alpaca's free tier still gives real-time intraday
> **bars** (needed to build the opening range + EMA). Finnhub moved candles to paid.

## 2. Put the keys in `.env` (stays private, gitignored — never commit)
Add to `/Users/massimocaramel/Desktop/market-scan/.env`:
```
ALPACA_API_KEY=your_key_id_here
ALPACA_SECRET_KEY=your_secret_here
NTFY_TOPIC=msfx-50f6d5d9e8      # your existing ntfy topic (reused from the FX watcher)
```
Without the Alpaca keys it still runs, but on **~15-min delayed** yfinance data.

## 3. (Optional) Run it locally during the open window
Only for a manual/one-off run — the recommended path is section 4 (cloud, Mac
off). For a local run your Mac must be on for that one hour (15:30–16:30 your
time). In the project:
```
source .venv/bin/activate
python ob_watcher.py loop        # polls every 45s during 9:45–11:00 ET, then idles
```
- `python ob_watcher.py` (no `loop`) = one check, for scheduling via cron instead.
- `OB_POLL_SEC=30` to poll faster; `ALPACA_FEED=iex` is the default.

## 4. Run it 24/7 WITHOUT your Mac (recommended — GitHub Actions + external cron)
The watcher runs on GitHub's servers via `.github/workflows/ob_watcher.yml`, so
your Mac can be off. The catch: **GitHub's own `schedule:` is best-effort** and
regularly delays runs by hours or skips them (it fired the Nov-19 open crons
~8 h late — useless for a market-open alert). So the reliable morning fire comes
from a **free external scheduler** that pokes the workflow via `repository_dispatch`.

**One-time setup (~5 min):**

1. **GitHub token** — GitHub → Settings → Developer settings → **Fine-grained
   tokens** → Generate new:
   - Repository access: **Only select repositories → `market-scan`**
   - Permissions → Repository → **Contents: Read and write** (required for
     `repository_dispatch`)
   - Copy the `github_pat_…` string. Treat it like a password; paste it only into
     cron-job.org. If it leaks, revoke it on that same page.

2. **cron-job.org** (free account) → Create cronjob → open **Advanced**:
   - **URL:** `https://api.github.com/repos/mcfly124/market-scan/dispatches`
   - **Method:** POST
   - **Schedule:** Mon–Fri at **09:40**, timezone **America/New_York** (this
     auto-handles EST/EDT — set once, never touch again)
   - **Headers:**
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer github_pat_…`
     - `X-GitHub-Api-Version: 2022-11-28`
   - **Body:** `{"event_type":"open-breakout"}`

**Why 09:40 ET:** the job needs ~40 s to spin up and the script waits until
09:45 ET (opening range formed) before alerting, so a 09:40 fire lands it ready
on time. The script safely accepts starts from 09:20–10:00 ET.

**Secrets on GitHub:** add `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `NTFY_TOPIC` as
repo Secrets (Settings → Secrets and variables → Actions). Already done.

**Redundancy, no spam:** the `schedule:` crons stay as a free backup. If both fire
on the same day you still get no duplicate — the 2nd run starts only after the
window closes and exits immediately (concurrency group `ob-watcher`).

**Verify:** `gh run list --workflow=ob_watcher.yml` should show a
`repository_dispatch` run ~14:40 UTC each weekday; or just watch for the phone ping.

## 5. The honest limits (read these)
- Even real-time, there's ~a minute of human lag (see phone → open T212 → click);
  on ~1%-wide stops that's real slippage. This is the ceiling of phone-alert trading.
- 60-day backtest edge — **promising, not proven.** Forward-test on demo first.
- **Size so the stop = 1–2% of the account, never 10–20%.** The backtest showed
  10–20%/trade reaching a 67–92% drawdown — i.e. account destruction.
