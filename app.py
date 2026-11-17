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


def analyze_pair(name, symbol):
    """Return an objective facts dict for one pair, or an error note."""
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

        return {
            "pair": name,
            "ok": True,
            "price": round(price, 5),
            "trend_4h": "UP" if trend_up else "DOWN",
            "setup": setup,
            "ema20_15m": round(ema20_v, 5),
            "atr_15m": round(atr_v, 5),
            "suggested_stop_dist": round(1.5 * atr_v, 5),
            "near_resistance": round(swing_hi, 5),
            "near_support": round(swing_lo, 5),
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
        return {"pair": name, "ok": False, "error": str(e)}


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
    boost = bool((request.get_json(silent=True) or {}).get("boost"))
    # 1) Technical analysis for all pairs. Each analyze_pair is I/O-bound
    #    (yfinance downloads), so threads overlap the waiting — but we CAP the
    #    worker count so a large pair list doesn't trip Yahoo's rate limiting.
    items = list(PAIRS.items())
    with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
        facts = list(pool.map(lambda it: analyze_pair(it[0], it[1]), items))

    # 2) Only the TRIGGERED (actionable) pairs get news and go to Claude. With
    #    ~40 pairs this keeps the scan fast and your subscription usage low —
    #    the cards below still show every pair's code-computed state.
    triggered = [f for f in facts if f.get("ok") and "triggered" in f.get("setup", "")]

    if triggered:
        tsyms = [PAIRS[f["pair"]] for f in triggered]
        with ThreadPoolExecutor(max_workers=2) as pool:
            news_lists = list(pool.map(fetch_news, tsyms))
        for fact, news in zip(triggered, news_lists):
            fact["news"] = news
        briefing = claude_briefing(triggered, boost=boost)
    else:
        # No setups -> don't call Claude at all (saves usage). Honest message.
        ok_n = len([f for f in facts if f.get("ok")])
        briefing = {
            "mock": False, "via": "none", "boost": boost,
            "text": (f"No setups triggered across {ok_n} pairs right now — "
                     "nothing actionable. The cards below show each pair's "
                     "trend and state in code; wait for a pullback to EMA20 to "
                     "trigger. Not financial advice."),
        }

    return jsonify({
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "pairs": facts,
        "briefing": briefing,
        "triggered_count": len(triggered),
        "scanned_count": len([f for f in facts if f.get("ok")]),
        "model": MODEL if ANTHROPIC_API_KEY else None,
    })


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
