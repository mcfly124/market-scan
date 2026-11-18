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

## 3. Run it during the open window
Your Mac must be on for that one hour (15:30–16:30 your time). In the project:
```
source .venv/bin/activate
python ob_watcher.py loop        # polls every 45s during 9:45–11:00 ET, then idles
```
- `python ob_watcher.py` (no `loop`) = one check, for scheduling via cron instead.
- `OB_POLL_SEC=30` to poll faster; `ALPACA_FEED=iex` is the default.

## 4. The honest limits (read these)
- Even real-time, there's ~a minute of human lag (see phone → open T212 → click);
  on ~1%-wide stops that's real slippage. This is the ceiling of phone-alert trading.
- 60-day backtest edge — **promising, not proven.** Forward-test on demo first.
- **Size so the stop = 1–2% of the account, never 10–20%.** The backtest showed
  10–20%/trade reaching a 67–92% drawdown — i.e. account destruction.
