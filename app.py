"""
Market Scan — honest, rule-based forex briefing (NOT financial advice).

What it does, when you press "Scan":
  1. Pulls recent price data for a list of FX pairs.
  2. Computes OBJECTIVE technical state in code (not guessed):
       - 4h trend via EMA(50)
       - 15m entry EMA(20) + whether a pullback setup has triggered
       - ATR(14) and nearby swing levels
  3. Sends those FACTS to Claude, which writes a plain-English briefing
     and flags the day's risks — framed as analysis, never as a
     promise that a trade will profit.

What it deliberately does NOT do:
  - It does not invent win-probabilities or "confidence %".
  - It does not tell you to place a trade.
  - It is not financial advice. Most retail traders lose money.

Run it on your own machine. Your Anthropic API key stays in a local
.env file — this app never transmits it anywhere except to Anthropic's
API, and the author of this file (Claude) never sees it.
"""

import os
import json
import shutil
import subprocess
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
# yfinance symbols. Swap the data source if you prefer another provider.
# The standard Trading-212-style FX universe (majors, crosses, common
# exotics). Each maps to its yfinance symbol. yfinance may not return
# intraday data for every exotic — analyze_pair() handles that gracefully
# (returns ok=False and the card shows "No data"). Trim any you don't trade.
PAIRS = {
    # USD majors
    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
    "USD/CHF": "USDCHF=X", "USD/CAD": "USDCAD=X", "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
    # EUR crosses
    "EUR/GBP": "EURGBP=X", "EUR/JPY": "EURJPY=X", "EUR/CHF": "EURCHF=X",
    "EUR/CAD": "EURCAD=X", "EUR/AUD": "EURAUD=X", "EUR/NZD": "EURNZD=X",
    # GBP crosses
    "GBP/JPY": "GBPJPY=X", "GBP/CHF": "GBPCHF=X", "GBP/CAD": "GBPCAD=X",
    "GBP/AUD": "GBPAUD=X", "GBP/NZD": "GBPNZD=X",
    # JPY crosses
    "AUD/JPY": "AUDJPY=X", "NZD/JPY": "NZDJPY=X", "CAD/JPY": "CADJPY=X",
    "CHF/JPY": "CHFJPY=X",
    # other crosses
    "AUD/CAD": "AUDCAD=X", "AUD/CHF": "AUDCHF=X", "AUD/NZD": "AUDNZD=X",
    "NZD/CAD": "NZDCAD=X", "NZD/CHF": "NZDCHF=X", "CAD/CHF": "CADCHF=X",
    # exotics
    "USD/ZAR": "USDZAR=X", "USD/MXN": "USDMXN=X", "USD/TRY": "USDTRY=X",
    "USD/SGD": "USDSGD=X", "USD/NOK": "USDNOK=X", "USD/SEK": "USDSEK=X",
    "USD/PLN": "USDPLN=X", "USD/HUF": "USDHUF=X", "USD/CZK": "USDCZK=X",
    "EUR/TRY": "EURTRY=X", "EUR/PLN": "EURPLN=X", "EUR/NOK": "EURNOK=X",
    "EUR/SEK": "EURSEK=X", "EUR/HUF": "EURHUF=X",
}

# US stock-index futures (trade nearly 24h, so intraday data is continuous like
# FX; these track what Trading 212 index CFDs follow). yfinance may be patchy on
# some — analyze_pair() degrades gracefully.
INDICES = {
    "S&P 500": "ES=F",
    "Nasdaq 100": "NQ=F",
    "Dow 30": "YM=F",
    "Russell 2000": "RTY=F",
}

# 20 liquid US large-caps across sectors. Note: single stocks carry gap/earnings
# risk the FX engine can't see — treat signals near earnings with extra caution.
STOCKS = {
    "AAPL": "AAPL", "MSFT": "MSFT", "NVDA": "NVDA", "AMZN": "AMZN",
    "GOOGL": "GOOGL", "META": "META", "TSLA": "TSLA", "AMD": "AMD",
    "AVGO": "AVGO", "NFLX": "NFLX", "JPM": "JPM", "V": "V",
    "MA": "MA", "UNH": "UNH", "XOM": "XOM", "JNJ": "JNJ",
    "WMT": "WMT", "PG": "PG", "KO": "KO", "DIS": "DIS",
}

# Crypto is scanned differently from everything else: NOT the 15m pullback
# engine (which has no edge), but a slow DAILY TREND REGIME (price vs its
# 100-day average) — the one approach that actually backtested well
# (~0.96 Sharpe long/cash on BTC). Highest risk, deepest drawdowns. See
# analyze_crypto_trend().
CRYPTO = {
    "Bitcoin": "BTC-USD", "Ethereum": "ETH-USD", "Solana": "SOL-USD",
    "BNB": "BNB-USD", "XRP": "XRP-USD", "Cardano": "ADA-USD",
    "Dogecoin": "DOGE-USD", "Litecoin": "LTC-USD",
}

# market key -> (asset class, instrument dict). "fx" stays the default and is
# what the phone watcher scans.
MARKETS = {
    "fx": ("fx", PAIRS),
    "index": ("index", INDICES),
    "stock": ("stock", STOCKS),
}
# name -> symbol across every market (for the on-demand briefing's news lookup)
ALL_SYMBOLS = {**PAIRS, **INDICES, **STOCKS, **CRYPTO}

# Trade logbook is stored in a JSON file next to this app so it survives
# browser clears and restarts. It's the user's own money record.
TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.json")
DEFAULT_LOG = {"start_balance": 260.0, "trades": []}

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
# Model id is configurable because Anthropic's model names change over time.
# Check https://docs.claude.com/en/docs/about-claude/models for current ids.
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()

# Model used for the subscription (Claude Code CLI) path. The briefing is a
# simple writing task, so Sonnet is the sweet spot (much lighter on your
# subscription usage than Opus, near-identical quality here). All trade
# numbers are computed in code and do NOT depend on this choice.
CLI_MODEL = os.getenv("CLAUDE_CLI_MODEL", "sonnet").strip()


# ----------------------------------------------------------------------
# Indicator helpers (pure, deterministic)
# ----------------------------------------------------------------------
def _ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def _atr(df, length=14):
    import pandas as pd
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


def fetch_news(symbol, limit=4):
    """Recent headlines for a pair from yfinance (no API key needed).
    Returns a list of compact dicts, newest first. Never raises.
    Retries once, since Yahoo's news endpoint can throttle under load."""
    import time
    import yfinance as yf

    raw = []
    for attempt in range(2):
        try:
            raw = yf.Ticker(symbol).news or []
            if raw:
                break
        except Exception:
            raw = []
        if attempt == 0:
            time.sleep(0.8)
    if not raw:
        return []

    items = []
    for art in raw:
        c = art.get("content", art) if isinstance(art, dict) else {}
        if not isinstance(c, dict):
            continue
        title = (c.get("title") or "").strip()
        if not title:
            continue
        summary = (c.get("summary") or c.get("description") or "").strip()
        if len(summary) > 240:
            summary = summary[:237].rstrip() + "…"
        prov = c.get("provider") or {}
        url = ((c.get("canonicalUrl") or {}).get("url")
               or (c.get("clickThroughUrl") or {}).get("url")
               or c.get("previewUrl") or "")
        when = c.get("pubDate") or c.get("displayTime") or ""
        age_days = None
        if when:
            try:
                ts = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
                age_days = (dt.datetime.now(dt.timezone.utc) - ts).days
            except Exception:
                pass
        items.append({
            "title": title,
            "summary": summary,
            "publisher": (prov.get("displayName") if isinstance(prov, dict) else "") or "",
            "published": when,
            "age_days": age_days,
            "url": url,
        })

    # newest first; items without a date sink to the bottom
    items.sort(key=lambda x: x["published"] or "", reverse=True)
    return items[:limit]


def analyze_pair(name, symbol, cls="fx"):
    """Return an objective facts dict for one instrument, or an error note.
    cls is the asset class ("fx" | "index" | "stock") — it only changes display
    rounding and (client-side) whether distances are shown in pips or percent."""
    import pandas as pd
    import yfinance as yf

    try:
        d15 = yf.download(symbol, period="5d", interval="15m",
                          progress=False, auto_adjust=False)
        d1h = yf.download(symbol, period="1mo", interval="60m",
                          progress=False, auto_adjust=False)
        if d15.empty or d1h.empty:
            raise ValueError("no data returned")

        # yfinance sometimes returns multiindex columns for single tickers
        if isinstance(d15.columns, pd.MultiIndex):
            d15.columns = d15.columns.get_level_values(0)
        if isinstance(d1h.columns, pd.MultiIndex):
            d1h.columns = d1h.columns.get_level_values(0)

        # 4h trend from resampled 1h data
        d4h = d1h.resample("4h").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        ).dropna()
        ema50_4h = _ema(d4h["Close"], 50)
        trend_up = bool(d4h["Close"].iloc[-1] > ema50_4h.iloc[-1])

        # 15m entry reference + setup trigger
        ema20 = _ema(d15["Close"], 20)
        atr = _atr(d15, 14)
        close = d15["Close"]

        # crossover/under within the last 3 bars, in the 4h trend direction
        def crossed_up(i):
            return close.iloc[i - 1] <= ema20.iloc[i - 1] and close.iloc[i] > ema20.iloc[i]

        def crossed_dn(i):
            return close.iloc[i - 1] >= ema20.iloc[i - 1] and close.iloc[i] < ema20.iloc[i]

        recent = range(len(close) - 3, len(close))
        long_trig = trend_up and any(crossed_up(i) for i in recent)
        short_trig = (not trend_up) and any(crossed_dn(i) for i in recent)

        if long_trig:
            setup = "LONG setup triggered"
        elif short_trig:
            setup = "SHORT setup triggered"
        elif trend_up:
            setup = "No setup — uptrend, waiting for a pullback to EMA20"
        else:
            setup = "No setup — downtrend, waiting for a pullback to EMA20"

        price = float(close.iloc[-1])
        atr_v = float(atr.iloc[-1])
        ema20_v = float(ema20.iloc[-1])
        swing_hi = float(d15["High"].iloc[-20:].max())
        swing_lo = float(d15["Low"].iloc[-20:].min())

        # Pre-compute pip distances in code so the model reasons on exact
        # numbers instead of doing arithmetic in prose. Signed for EMA20
        # (positive = price above EMA20).
        # JPY- and HUF-quoted pairs trade to 2-3 decimals (pip = 0.01);
        # everything else to 4-5 decimals (pip = 0.0001). This affects the
        # pip DISPLAY only — all stop/target prices are computed in price terms.
        pip = 0.01 if ("JPY" in name or "HUF" in name) else 0.0001
        def pips(x):
            return round(x / pip, 1)

        rp = 5 if cls == "fx" else 2  # price decimals: fx fine-grained, stocks/indices 2dp

        return {
            "pair": name,
            "ok": True,
            "cls": cls,
            "price": round(price, rp),
            "trend_4h": "UP" if trend_up else "DOWN",
            "setup": setup,
            "ema20_15m": round(ema20_v, rp),
            "atr_15m": round(atr_v, rp),
            "suggested_stop_dist": round(1.5 * atr_v, rp),
            "near_resistance": round(swing_hi, rp),
            "near_support": round(swing_lo, rp),
            "pips": {
                "atr": pips(atr_v),
                "suggested_stop": pips(1.5 * atr_v),
                "price_to_resistance": pips(swing_hi - price),
                "price_to_support": pips(price - swing_lo),
                "price_to_ema20": pips(price - ema20_v),
                "range_20bar": pips(swing_hi - swing_lo),
            },
        }
    except Exception as e:  # network blocked, rate limited, etc.
        return {"pair": name, "ok": False, "cls": cls, "error": str(e)}


# US stocks + index ETFs for the Open-Breakout strategy (9:30 ET open).
OPENBREAK = {
    "S&P 500 (SPY)": "SPY", "Nasdaq 100 (QQQ)": "QQQ",
    "Russell 2000 (IWM)": "IWM", "Dow 30 (DIA)": "DIA",
    "Apple": "AAPL", "Tesla": "TSLA", "Nvidia": "NVDA", "AMD": "AMD",
    "Microsoft": "MSFT", "Alphabet": "GOOGL", "Meta": "META", "Amazon": "AMZN",
}


def _pivots(h, l, c):
    """Classic daily pivots from the prior session — these are the S/R levels
    used as take-profit targets."""
    p = (h + l + c) / 3.0
    return {"R3": h + 2 * (p - l), "R2": p + (h - l), "R1": 2 * p - l,
            "P": p, "S1": 2 * p - h, "S2": p - (h - l), "S3": l - 2 * (p - h)}


def analyze_open_breakout(name, symbol):
    """Market-open breakout (the one intraday strategy that backtested with an
    edge: +0.10R, PF 1.47, when filtered by the 200-EMA trend). Mechanics:
      - opening range = high/low of the first 15 min after the 9:30 ET open;
      - take only a break in the direction of the 200-EMA (5m) trend;
      - stop = opposite side of the range; target = nearest daily pivot.
    Returns the live state for the most recent session. NOT financial advice."""
    import pandas as pd
    import yfinance as yf
    try:
        d = yf.download(symbol, period="5d", interval="5m",
                        progress=False, auto_adjust=True, prepost=False)
        if d.empty:
            raise ValueError("no data returned")
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.dropna()
        try:
            d.index = d.index.tz_convert("America/New_York")
        except Exception:
            pass
        d = d.assign(ema=d["Close"].ewm(span=200, adjust=False).mean())
        days = sorted({ts.date() for ts in d.index})
        if len(days) < 2:
            raise ValueError("not enough sessions")
        today, prev = days[-1], days[-2]
        g = d[[ts.date() == today for ts in d.index]]
        pg = d[[ts.date() == prev for ts in d.index]]
        if len(g) < 1 or len(pg) < 1:
            raise ValueError("missing session data")

        H, L, C = float(pg["High"].max()), float(pg["Low"].min()), float(pg["Close"].iloc[-1])
        piv = _pivots(H, L, C)
        price = float(g["Close"].iloc[-1])
        ema_v = float(g["ema"].iloc[-1])
        bias = "UP" if price > ema_v else "DOWN"

        # opening range = first 15 min (first three 5-min bars)
        if len(g) < 3:
            return {"pair": name, "ok": True, "cls": "openbreak", "symbol": symbol,
                    "price": round(price, 2), "state": "pre",
                    "headline": "Opening range still forming (first 15 min).",
                    "bias": bias}
        orb = g.iloc[:3]
        or_hi, or_lo = float(orb["High"].max()), float(orb["Low"].min())
        after = g.iloc[3:]

        trig = None
        for i in range(len(after)):
            if float(after["High"].iloc[i]) >= or_hi:
                trig = 1
                break
            if float(after["Low"].iloc[i]) <= or_lo:
                trig = -1
                break

        out = {"pair": name, "ok": True, "cls": "openbreak", "symbol": symbol,
               "price": round(price, 2), "or_hi": round(or_hi, 2), "or_lo": round(or_lo, 2),
               "ema200": round(ema_v, 2), "bias": bias,
               "pivP": round(piv["P"], 2)}

        aligned = (trig == 1 and bias == "UP") or (trig == -1 and bias == "DOWN")
        if trig is None:
            out["state"] = "waiting"
            out["headline"] = (f"Range set ({or_lo:.2f}–{or_hi:.2f}). Waiting for a break; "
                               f"trend bias is {'up → longs only' if bias=='UP' else 'down → shorts only'}.")
            return out
        if not aligned:
            out["state"] = "skip"
            out["headline"] = (f"Broke {'up' if trig==1 else 'down'}, but against the "
                               f"{bias} trend — skip (counter-trend breakouts had no edge).")
            return out

        # a valid, trend-aligned signal
        if trig == 1:
            entry, stop = or_hi, or_lo
            above = [v for v in piv.values() if v > entry]
            tp = min(above) if above else None
        else:
            entry, stop = or_lo, or_hi
            below = [v for v in piv.values() if v < entry]
            tp = max(below) if below else None
        risk = abs(entry - stop)
        rr = (abs(tp - entry) / risk) if (tp and risk > 0) else None
        out.update({
            "state": "long" if trig == 1 else "short",
            "dir": "LONG" if trig == 1 else "SHORT",
            "entry": round(entry, 2), "stop": round(stop, 2),
            "tp": round(tp, 2) if tp else None,
            "rr": round(rr, 2) if rr else None,
            "headline": (f"{'LONG' if trig==1 else 'SHORT'} breakout, with the {bias} trend. "
                         f"Entry {entry:.2f}, stop {stop:.2f}"
                         + (f", target {tp:.2f} ({rr:.1f}R)." if tp else ".")),
        })
        return out
    except Exception as e:
        return {"pair": name, "ok": False, "cls": "openbreak", "error": str(e)}


def _cdp(v):
    """Sensible decimals for wildly different crypto prices (BTC vs DOGE)."""
    a = abs(v)
    if a >= 100:
        return 2
    if a >= 1:
        return 4
    return 6


def analyze_crypto_trend(name, symbol):
    """DAILY TREND REGIME for one coin — NOT the 15m engine.

    The only rule that backtested with a real edge (long/cash, ~0.96 Sharpe on
    BTC): is today's price above or below its 100-day average? It predicts
    nothing; it reacts. 'IN' = above (ride the uptrend); 'OUT' = below (sit in
    cash). Also reports the 200-day line and the fall from the 1-year high so
    the drawdown risk is visible. NOT financial advice."""
    import pandas as pd
    import yfinance as yf
    try:
        d = yf.download(symbol, period="2y", interval="1d",
                        progress=False, auto_adjust=True)
        if d.empty:
            raise ValueError("no data returned")
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        close = d["Close"].dropna()
        if len(close) < 110:
            raise ValueError("not enough history")
        price = float(close.iloc[-1])
        sma100 = float(close.rolling(100).mean().iloc[-1])
        sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        hi_1y = float(close.iloc[-365:].max())
        rp = _cdp(price)
        above = price > sma100
        return {
            "pair": name, "ok": True, "cls": "crypto", "mode": "trend",
            "price": round(price, rp),
            "sma100": round(sma100, rp),
            "sma200": round(sma200, rp) if sma200 else None,
            "regime": "UP" if above else "DOWN",
            "in_market": bool(above),
            "dist100_pct": round((price - sma100) / sma100 * 100, 1),
            "dist200_pct": round((price - sma200) / sma200 * 100, 1) if sma200 else None,
            "from_high_pct": round((price - hi_1y) / hi_1y * 100, 1),
        }
    except Exception as e:
        return {"pair": name, "ok": False, "cls": "crypto", "error": str(e)}


# ----------------------------------------------------------------------
# Claude briefing
# ----------------------------------------------------------------------
BRIEFING_SYSTEM = (
    "You are a markets analyst writing a short daily FX briefing for one "
    "retail trader with a very small account. For each pair you are given "
    "objective, pre-computed TECHNICAL facts AND a list of recent NEWS "
    "headlines. Combine both into an honest read: the mechanical setup "
    "state is the technical foundation; the news is the fundamental "
    "backdrop and tells you where the event risk is.\n"
    "Hard rules:\n"
    "- Never state or imply a probability of profit, a win-rate, or a "
    "'confidence %'. Never say 'buy now'.\n"
    "- You MAY give a directional CONCLUSION, but ONLY as a conditional "
    "scenario tied to explicit price levels — e.g. 'if it breaks <level> "
    "the picture leans <up/down> toward <level>; the view is invalidated "
    "below/above <level>'. Always give the opposing scenario too. This is "
    "a scenario, never a guarantee of what will happen or that it will "
    "profit.\n"
    "- Use the news to weigh the fundamental bias and to flag the main "
    "event risk (central-bank decisions, data releases). If a headline is "
    "old (see age_days) or thin, say so and lean on the technicals.\n"
    "- If news and technicals disagree, say so plainly rather than forcing "
    "a view.\n"
    "Keep it SHORT and plain. No long paragraphs.\n"
    "Structure (use these markdown headings):\n"
    "## Per pair\n"
    "  For each pair, exactly ONE short line: the setup state + the single "
    "most important level, plus a 2-4 word fundamental tag if the news is "
    "fresh. If a setup is TRIGGERED, start the line with 'ACTIONABLE —' and "
    "name the direction; otherwise start with 'Wait —'.\n"
    "  Then one short line starting 'Outlook:' — the conditional scenario "
    "(one 'if <level> then leans <dir>' clause and its invalidation level).\n"
    "## Overall\n"
    "  ONE line: the session tone + the single biggest event risk.\n"
    "## Bottom line\n"
    "  One short line: scenario analysis, NOT financial advice or a profit "
    "prediction; set stop-loss and size first."
)

# "AI Boost" mode — an INTRADAY strategist that reasons over ALL the numbers,
# triggered or not, and lays out a concrete conditional plan per pair.
# Heavier on tokens; opt-in via the page toggle.
BRIEFING_SYSTEM_BOOST = (
    "You are an intraday FX strategist writing a per-pair trade plan for one "
    "retail trader with a small account, trading off the 15-minute chart with a "
    "4h trend filter. For each pair you get pre-computed TECHNICAL facts: price, "
    "4h trend (vs EMA50), the 15m EMA20 value, the setup state, ATR(15m), a "
    "1.5*ATR suggested stop, 20-bar swing high (resistance) and low (support), "
    "a 'pips' object with EXACT distances already computed in pips "
    "(atr, suggested_stop, price_to_resistance, price_to_support, "
    "price_to_ema20 [signed, + = price above EMA20], range_20bar), and recent "
    "NEWS headlines. Plan EVERY pair, triggered or not.\n"
    "Hard rules:\n"
    "- NOT financial advice and never a profit/win probability. Every plan is a "
    "CONDITIONAL scenario ('IF price does X, THEN the plan is...'), never 'buy "
    "now'. Always give the invalidation.\n"
    "- Use the pre-computed 'pips' numbers for all distances — do NOT do your "
    "own arithmetic on the raw prices. Quote levels from the facts as-is.\n"
    "- Intraday realism: judge the reward using the distance to the nearest "
    "opposing structure (support for shorts, resistance for longs) vs the "
    "suggested stop. If that structure is closer than ~1.5x the stop, say the "
    "location is poor / little room and flag it as a skip or 'wait for a better "
    "price', even if the setup is triggered.\n"
    "- Respect the 4h trend: prefer plans in the trend direction; call out any "
    "counter-trend idea as higher-risk.\n"
    "- If news is old (age_days) or thin, say so and lean on the technicals.\n"
    "For EACH pair, under a '### <PAIR>' heading, write a SHORT intraday plan "
    "with these labelled lines (one line each, skip a line if N/A):\n"
    "  'Bias:' trend + current location (use the pip distances to support/"
    "resistance/EMA20; e.g. 'mid-range', 'pressing resistance', 'extended into "
    "support').\n"
    "  'Trigger:' the exact condition to act on intraday (e.g. 'pullback to "
    "EMA20 near <level> that holds', or 'break and 15m close below <level>'). "
    "If a setup is already triggered, say so and whether the current price is "
    "still a usable entry or already extended.\n"
    "  'If taken:' entry area, stop (use suggested_stop pips), and a realistic "
    "first target = the nearest opposing structure, with its R:R from the pips; "
    "note a 2R target too. State plainly if R:R to structure is poor.\n"
    "  'Invalidation:' the level/condition that voids the idea.\n"
    "  'Risk:' the main session/news event risk for this pair, or 'none fresh'.\n"
    "Then:\n"
    "## Session read\n  2-3 sentences: the cross-pair theme, which pair has the "
    "cleanest intraday plan right now, and the biggest event risk.\n"
    "## Bottom line\n  One line: these are conditional intraday plans, NOT "
    "financial advice or a profit prediction; stop-loss and position size first."
)


USER_PROMPT = (
    "Here are today's pre-computed facts per pair as JSON. "
    "Write the briefing.\n\n"
)

MOCK_TEXT = (
    "[MOCK BRIEFING — no Claude available (no ANTHROPIC_API_KEY and the "
    "Claude Code CLI was not found), so Claude was not called.]\n\n"
    "Overall: the cards above show each pair's 4h trend and whether "
    "your pullback setup has triggered. Where it says 'No setup', "
    "the rules are telling you to wait. Where a setup is triggered, "
    "that is a condition being met — not a prediction that it will "
    "work. Check the economic calendar before risking anything, and "
    "size every trade to risk ~1% of your account. This is analysis, "
    "not advice."
)


def _briefing_via_api(payload, system, max_tokens):
    """Use the Anthropic Messages API (bills against API credits)."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": USER_PROMPT + payload}],
    )
    return msg.content[0].text


def _briefing_via_cli(payload, system):
    """Use the local Claude Code CLI in headless mode (uses your
    Claude subscription — no API key or API credits needed)."""
    prompt = system + "\n\n" + USER_PROMPT + payload
    cmd = ["claude", "-p", prompt]
    if CLI_MODEL:
        cmd = ["claude", "--model", CLI_MODEL, "-p", prompt]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=240,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "claude CLI failed")
    return result.stdout.strip()


def claude_briefing(facts, boost=False):
    """Write the narrative. Prefers an API key if present, else falls back
    to the Claude Code CLI (subscription), else a clearly-labelled mock.
    boost=True uses the deeper, token-heavier per-pair analysis prompt."""
    payload = json.dumps(facts, indent=2)
    system = BRIEFING_SYSTEM_BOOST if boost else BRIEFING_SYSTEM
    max_tokens = 3000 if boost else 1200

    if ANTHROPIC_API_KEY:
        try:
            return {"mock": False, "via": "api", "boost": boost,
                    "text": _briefing_via_api(payload, system, max_tokens)}
        except Exception as e:
            return {"mock": True, "text": f"[Claude API call failed: {e}]"}

    if shutil.which("claude"):
        try:
            return {"mock": False, "via": "cli", "boost": boost,
                    "text": _briefing_via_cli(payload, system)}
        except Exception as e:
            return {"mock": True, "text": f"[Claude CLI call failed: {e}]"}

    return {"mock": True, "text": MOCK_TEXT}


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


def quick_price(symbol):
    """Just the latest price for one pair — fast, for alert checks.
    No indicators, no news, no Claude. Returns (name-less) float or None."""
    import pandas as pd
    import yfinance as yf
    try:
        d = yf.download(symbol, period="1d", interval="5m",
                        progress=False, auto_adjust=False)
        if d.empty:
            d = yf.download(symbol, period="5d", interval="15m",
                            progress=False, auto_adjust=False)
        if d.empty:
            return None
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        return round(float(d["Close"].dropna().iloc[-1]), 5)
    except Exception:
        return None


def _load_log():
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("start_balance", DEFAULT_LOG["start_balance"])
        data.setdefault("trades", [])
        return data
    except Exception:
        return dict(DEFAULT_LOG)


def _save_log(data):
    clean = {
        "start_balance": float(data.get("start_balance", 0) or 0),
        "trades": data.get("trades", []) if isinstance(data.get("trades"), list) else [],
    }
    tmp = TRADES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(clean, f, indent=2)
    os.replace(tmp, TRADES_FILE)  # atomic
    return clean


@app.route("/log", methods=["GET"])
def get_log():
    return jsonify(_load_log())


@app.route("/log", methods=["POST"])
def post_log():
    """Replace the whole logbook (single-user local app)."""
    data = request.get_json(silent=True) or {}
    return jsonify(_save_log(data))


@app.route("/prices", methods=["GET"])
def prices():
    """Lightweight price snapshot for all pairs (for alert checking)."""
    symbols = list(PAIRS.items())
    with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
        vals = list(pool.map(lambda it: quick_price(it[1]), symbols))
    out = {name: price for (name, _), price in zip(symbols, vals)}
    return jsonify({
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "prices": out,
    })


@app.route("/scan", methods=["POST"])
def scan():
    """Scan one market. PURE CODE — no Claude call, so a scan costs ZERO tokens.
    The written briefing is a separate, opt-in call (see /briefing)."""
    body = request.get_json(silent=True) or {}
    market = body.get("market", "fx")

    # Crypto is a different beast: slow DAILY TREND REGIME (price vs 100-day
    # average), not the 15m engine. It's the only thing that backtested with an
    # edge — and the highest risk. Returns a 'trend' payload, not setups.
    if market == "crypto":
        items = list(CRYPTO.items())
        with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
            facts = list(pool.map(lambda it: analyze_crypto_trend(it[0], it[1]), items))
        in_n = len([f for f in facts if f.get("ok") and f.get("in_market")])
        return jsonify({
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "market": "crypto", "mode": "trend",
            "pairs": facts,
            "in_count": in_n,
            "scanned_count": len([f for f in facts if f.get("ok")]),
        })

    if market == "openbreak":
        items = list(OPENBREAK.items())
        with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
            facts = list(pool.map(lambda it: analyze_open_breakout(it[0], it[1]), items))
        sig = len([f for f in facts if f.get("ok") and f.get("state") in ("long", "short")])
        return jsonify({
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "market": "openbreak", "mode": "openbreak",
            "pairs": facts,
            "signal_count": sig,
            "scanned_count": len([f for f in facts if f.get("ok")]),
        })

    cls, universe = MARKETS.get(market, MARKETS["fx"])

    # Technical analysis for the market's instruments. I/O-bound (yfinance), so
    # threads overlap the waiting; cap workers to avoid Yahoo rate limiting.
    items = list(universe.items())
    with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
        facts = list(pool.map(lambda it: analyze_pair(it[0], it[1], cls), items))

    triggered = [f for f in facts if f.get("ok") and "triggered" in f.get("setup", "")]
    return jsonify({
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "market": market,
        "pairs": facts,
        "triggered_count": len(triggered),
        "scanned_count": len([f for f in facts if f.get("ok")]),
    })


@app.route("/briefing", methods=["POST"])
def briefing():
    """On-demand written briefing (THIS is the only route that uses tokens).
    The client POSTs the already-scanned facts; we fetch news for the triggered
    ones and ask Claude to write it up."""
    body = request.get_json(silent=True) or {}
    boost = bool(body.get("boost"))
    facts = body.get("pairs") or []
    triggered = [f for f in facts if f.get("ok") and "triggered" in f.get("setup", "")]

    if not triggered:
        return jsonify({
            "mock": False, "via": "none", "boost": boost,
            "text": ("No setups triggered right now — nothing to write up. "
                     "The cards show each instrument's state in code; wait for a "
                     "pullback to EMA20 to trigger. Not financial advice."),
        })

    tsyms = [ALL_SYMBOLS.get(f["pair"]) for f in triggered]
    with ThreadPoolExecutor(max_workers=2) as pool:
        news_lists = list(pool.map(lambda s: fetch_news(s) if s else [], tsyms))
    for fact, news in zip(triggered, news_lists):
        fact["news"] = news

    result = claude_briefing(triggered, boost=boost)
    result["model"] = MODEL if ANTHROPIC_API_KEY else None
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5057"))
    # HOST=0.0.0.0 lets other devices on your home Wi-Fi (your iPhone) reach the
    # app at http://<this-mac's-LAN-IP>:PORT. Set HOST=127.0.0.1 to lock it back
    # to this Mac only. Your .env / API key is never exposed in the web UI.
    host = os.getenv("HOST", "0.0.0.0").strip()

    def _lan_ip():
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no packets sent; just to pick the iface
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    print(f"\n  Market Scan running:")
    print(f"    on this Mac:   http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f"    on your iPhone (same Wi-Fi):  http://{_lan_ip()}:{port}\n")
    else:
        print()
    app.run(host=host, port=port, debug=False)
