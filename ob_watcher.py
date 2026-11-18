#!/usr/bin/env python3
"""
ob_watcher.py — real-time US market-OPEN BREAKOUT watcher (NOT financial advice).

Detection is pure deterministic Python — ZERO Claude/Anthropic usage. The
Anthropic subscription cannot supply market data, so prices come from a data
feed: Alpaca's FREE real-time IEX bars (set ALPACA_API_KEY + ALPACA_SECRET_KEY),
falling back to yfinance (~15-min DELAYED) if no Alpaca key is set.

Strategy (the one intraday edge from the backtests; see strategy-research):
  - opening range = high/low of the first 15 min after the 9:30 ET open;
  - take a break of that range ONLY in the direction of the 200-EMA(5m) trend;
  - stop = opposite side of the range; target = nearest prior-day pivot (S/R).
It pushes ONE alert per symbol per day when a trend-aligned breakout triggers,
with entry / stop / target / R:R. The ping means "go check your live Trading 212
price" — it is not a buy signal and not financial advice. You own the risk.

Run:
  python ob_watcher.py           # one pass (schedule this), or
  python ob_watcher.py loop      # poll every OB_POLL_SEC during the open window

Push backend (reused from watcher.py): Telegram or ntfy via env; else dry-run.
Risk reminder baked into every alert: size so the STOP = 1-2% of the account,
never more. (10-20%/trade blows the account — see the backtest.)
"""
import os
import sys
import json
import time
import datetime as dt
import urllib.request

import pandas as pd

from watcher import send_push, load_state, save_state  # reuse tested push + state

SYMBOLS = {
    "S&P 500 (SPY)": "SPY", "Nasdaq 100 (QQQ)": "QQQ",
    "Russell 2000 (IWM)": "IWM", "Dow 30 (DIA)": "DIA",
    "Apple": "AAPL", "Tesla": "TSLA", "Nvidia": "NVDA", "AMD": "AMD",
    "Microsoft": "MSFT", "Alphabet": "GOOGL", "Meta": "META", "Amazon": "AMZN",
}
ALPACA_KEY = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "").strip()
ALPACA_FEED = os.getenv("ALPACA_FEED", "iex").strip()
POLL_SEC = int(os.getenv("OB_POLL_SEC", "45"))
ET = "America/New_York"


# ---------------------------------------------------------------------------
# Data: Alpaca real-time IEX bars (free) — else yfinance (delayed) fallback
# ---------------------------------------------------------------------------
def _alpaca_bars(symbols, days=6):
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    base = ("https://data.alpaca.markets/v2/stocks/bars?symbols="
            + ",".join(symbols) + f"&timeframe=5Min&start={start}"
            + f"&feed={ALPACA_FEED}&adjustment=raw&limit=10000")
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET,
               "accept": "application/json"}
    out = {s: [] for s in symbols}
    url = base
    for _ in range(20):  # follow pagination
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        for s, rows in (data.get("bars") or {}).items():
            out.setdefault(s, []).extend(rows)
        tok = data.get("next_page_token")
        if not tok:
            break
        url = base + "&page_token=" + tok
    frames = {}
    for s, rows in out.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET)
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close"})
        df = df.set_index("ts")[["Open", "High", "Low", "Close"]].sort_index()
        frames[s] = df
    return frames


def _yf_bars(symbols, days=6):
    import yfinance as yf
    frames = {}
    for s in symbols:
        d = yf.download(s, period=f"{days}d", interval="5m",
                        progress=False, auto_adjust=True, prepost=False)
        if d.empty:
            continue
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.dropna()
        try:
            d.index = d.index.tz_convert(ET)
        except Exception:
            pass
        frames[s] = d[["Open", "High", "Low", "Close"]]
    return frames


def get_bars(symbols):
    if ALPACA_KEY and ALPACA_SECRET:
        return _alpaca_bars(symbols), "alpaca-iex (real-time)"
    return _yf_bars(symbols), "yfinance (~15-min DELAYED)"


# ---------------------------------------------------------------------------
# Detection — identical rules to the app's analyze_open_breakout
# ---------------------------------------------------------------------------
def _pivots(h, l, c):
    p = (h + l + c) / 3.0
    return [h + 2 * (p - l), p + (h - l), 2 * p - l, p, 2 * p - h,
            p - (h - l), l - 2 * (p - h)]


def _rth(df):
    t = df.index
    mask = [(x.hour > 9 or (x.hour == 9 and x.minute >= 30)) and x.hour < 16 for x in t]
    return df[mask]


def detect(df):
    """Return a signal dict for the most recent session, or None."""
    if df is None or len(df) < 40:
        return None
    df = _rth(df)
    if df.empty:
        return None
    ema = df["Close"].ewm(span=200, adjust=False).mean()
    days = sorted({x.date() for x in df.index})
    if len(days) < 2:
        return None
    today, prev = days[-1], days[-2]
    g = df[[x.date() == today for x in df.index]]
    pg = df[[x.date() == prev for x in df.index]]
    if len(g) < 4 or len(pg) < 1:
        return None
    piv = _pivots(float(pg["High"].max()), float(pg["Low"].min()), float(pg["Close"].iloc[-1]))
    or_hi = float(g["High"].iloc[:3].max())
    or_lo = float(g["Low"].iloc[:3].min())
    price = float(g["Close"].iloc[-1])
    ema_v = float(ema.iloc[-1])
    bias = "UP" if price > ema_v else "DOWN"
    after = g.iloc[3:]
    trig = None
    for i in range(len(after)):
        if float(after["High"].iloc[i]) >= or_hi:
            trig = 1
            break
        if float(after["Low"].iloc[i]) <= or_lo:
            trig = -1
            break
    if trig is None:
        return None
    aligned = (trig == 1 and bias == "UP") or (trig == -1 and bias == "DOWN")
    if not aligned:
        return None
    if trig == 1:
        entry, stop = or_hi, or_lo
        above = [v for v in piv if v > entry]
        tp = min(above) if above else None
    else:
        entry, stop = or_lo, or_hi
        below = [v for v in piv if v < entry]
        tp = max(below) if below else None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    rr = round(abs(tp - entry) / risk, 2) if tp else None
    return {"dir": "LONG" if trig == 1 else "SHORT", "date": str(today),
            "entry": round(entry, 2), "stop": round(stop, 2),
            "tp": round(tp, 2) if tp else None, "rr": rr}


def _message(name, a):
    arrow = "▲" if a["dir"] == "LONG" else "▼"
    title = f"{arrow} {name} {a['dir']} breakout"
    tgt = f" · target {a['tp']} ({a['rr']}R)" if a["tp"] else ""
    body = (f"{a['dir']} on the open-range break (with the trend).\n"
            f"Entry {a['entry']} · stop {a['stop']}{tgt}.\n"
            f"Size so the STOP = 1-2% of your account, never more. "
            f"Check your live Trading 212 price. Not financial advice.")
    return title, body


# ---------------------------------------------------------------------------
def run_once():
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    frames, source = get_bars(list(SYMBOLS.values()))
    state = load_state()
    new_state = dict(state)
    sent = 0
    for name, sym in SYMBOLS.items():
        a = detect(frames.get(sym))
        if not a:
            continue
        key = f"ob:{a['date']}:{sym}"      # one alert per symbol per day
        if state.get(key):
            new_state[key] = a["dir"]
            continue
        title, body = _message(name, a)
        backend = send_push(title, body)
        new_state[key] = a["dir"]
        sent += 1
        print(f"[{now}] alert via {backend}: {title} ({a['entry']}/{a['stop']}/{a['tp']})")
    save_state(new_state)
    if sent == 0:
        print(f"[{now}] no new trend-aligned breakouts. source={source}")
    return sent


def _et_now():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo(ET))


def _in_window():
    """True during the open-breakout window, 9:45-11:00 ET (range has formed)."""
    n = _et_now()
    if n.weekday() >= 5:
        return False
    mins = n.hour * 60 + n.minute
    return (9 * 60 + 45) <= mins <= (11 * 60)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        n = _et_now()
        # Only operate around the open. On a CI cron that may fire in either US
        # DST season, exit fast unless we're near the 09:45 ET window start — so
        # exactly one daily run does the work and the Actions job never hangs.
        delta = (n.hour * 60 + n.minute) - (9 * 60 + 45)
        if n.weekday() >= 5 or (not (-25 <= delta <= 15) and not _in_window()):
            print(f"[{n:%H:%M} ET] outside the open window — nothing to do.")
            return
        print(f"Polling every {POLL_SEC}s through the open window (09:45-11:00 ET). Ctrl-C to stop.")
        seen = False
        while True:
            if _in_window():
                try:
                    run_once()
                except Exception as e:
                    print(f"[error] {e}")
                seen = True
            elif seen:
                print("Open window closed — done for today.")
                break
            n = _et_now()
            if n.weekday() >= 5 or (n.hour * 60 + n.minute) > (11 * 60 + 30):
                print("Past the window — exiting.")
                break
            time.sleep(POLL_SEC)
    else:
        run_once()


if __name__ == "__main__":
    main()
