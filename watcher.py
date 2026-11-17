#!/usr/bin/env python3
"""
watcher.py — background FX setup watcher (NOT financial advice).

Runs the SAME deterministic detection as the app (imported from app.py),
with NO Claude call at all, and pushes a phone notification when a pair
becomes a FRESH, WELL-LOCATED entry. Built to run on GitHub Actions
(free, 24/7 — your Mac can be off).

  >>> Zero Claude subscription usage: detection is pure Python code.

The ping means "a clean setup has formed" — the data is delayed (~15 min,
free source), so use it to then set the exact price alert on your own
platform (Trading 212). It is NOT a buy signal and NOT financial advice.

Push backends (set via env; the first one configured wins):
  - Telegram:  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  - ntfy:      NTFY_TOPIC          (plus optional NTFY_SERVER, default https://ntfy.sh)
  - none set:  dry-run (just prints what it WOULD send — safe to test)

Tuning (env):
  RR_MIN      minimum reward:risk-to-structure to alert (default 2.0 = only
              the "good spot" tier; lower to 1.0 to also get "tight" setups)
  STATE_FILE  path for the de-dup state (default watcher_state.json)
"""
import os
import json
import datetime as dt
import urllib.request
import urllib.parse  # noqa: F401 (kept for Telegram form-encoding)

# Reuse the EXACT detection logic the app shows you, so alerts never drift
# from the cards. (Importing app.py does not start the web server.)
from app import analyze_pair, PAIRS

RR_MIN = float(os.getenv("RR_MIN", "2"))
STATE_FILE = os.getenv(
    "STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher_state.json"),
)


# ----------------------------------------------------------------------
# Detection — same rules as the app's qualBadge (code, not AI)
# ----------------------------------------------------------------------
def _setup_dir(setup):
    if setup.startswith("LONG"):
        return "LONG"
    if setup.startswith("SHORT"):
        return "SHORT"
    return None


def actionable(f):
    """Return a dict describing a fresh, well-located entry, or None.

    Mirrors the card badge: a setup is actionable only when it is TRIGGERED
    (in the 4h trend direction) AND the room to the nearest opposing
    structure is >= RR_MIN times the suggested stop (a 'good spot')."""
    if not f.get("ok"):
        return None
    d = _setup_dir(f["setup"])
    if not d:
        return None
    pips = f.get("pips") or {}
    risk = pips.get("suggested_stop") or 0
    if risk <= 0:
        return None
    reward = pips.get("price_to_resistance") if d == "LONG" else pips.get("price_to_support")
    if reward is None:
        return None
    rr = reward / risk
    if rr < RR_MIN:
        return None
    target = f["near_resistance"] if d == "LONG" else f["near_support"]
    return {
        "dir": d, "rr": round(rr, 1), "reward": reward, "risk": risk,
        "price": f["price"], "target": target, "trend": f["trend_4h"],
    }


# ----------------------------------------------------------------------
# De-dup state: only alert when a pair NEWLY becomes actionable (or flips
# direction). Prevents re-pinging the same setup every run.
# ----------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


# ----------------------------------------------------------------------
# Push (stdlib only — no extra dependency)
# ----------------------------------------------------------------------
def _post(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def send_push(title, body):
    tg_tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    topic = os.getenv("NTFY_TOPIC", "").strip()

    if tg_tok and tg_chat:
        url = f"https://api.telegram.org/bot{tg_tok}/sendMessage"
        payload = urllib.parse.urlencode(
            {"chat_id": tg_chat, "text": f"{title}\n{body}"}
        ).encode()
        _post(url, data=payload,
              headers={"Content-Type": "application/x-www-form-urlencoded"})
        return "telegram"

    if topic:
        # Publish via ntfy's JSON API so the title/message travel in the UTF-8
        # body, not in HTTP headers (headers are latin-1 only, which would choke
        # on the ▲/▼/· characters).
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        payload = json.dumps({
            "topic": topic, "title": title, "message": body,
            "priority": 4, "tags": ["chart_with_upwards_trend"],
        }).encode("utf-8")
        _post(server, data=payload, headers={"Content-Type": "application/json"})
        return "ntfy"

    print(f"[DRY-RUN — no push backend set] {title} :: {body}")
    return "dry-run"


def _message(name, a):
    arrow = "▲" if a["dir"] == "LONG" else "▼"
    title = f"{arrow} {name} {a['dir']} — good setup ({a['rr']}R)"
    body = (
        f"{a['dir']} triggered, {a['rr']}R room to structure (4h trend {a['trend']}).\n"
        f"Price {a['price']} · target {a['target']} (~{a['reward']:.0f}p) · "
        f"stop ~{a['risk']:.0f}p.\n"
        f"Go set your Trading 212 price alert. Not financial advice."
    )
    return title, body


# ----------------------------------------------------------------------
def main():
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()
    new_state = dict(state)
    alerts = []

    for name, sym in PAIRS.items():
        f = analyze_pair(name, sym)
        a = actionable(f)
        prev = state.get(name)
        if a:
            if prev != a["dir"]:          # newly actionable, or flipped side
                alerts.append((name, a))
            new_state[name] = a["dir"]
        elif name in new_state:           # no longer actionable -> re-arm
            new_state.pop(name)

    save_state(new_state)

    sent = 0
    for name, a in alerts:
        title, body = _message(name, a)
        backend = send_push(title, body)
        sent += 1
        print(f"[{now}] alert via {backend}: {title}")

    if not alerts:
        print(f"[{now}] no fresh good-location setups (RR_MIN={RR_MIN}).")
    return sent


if __name__ == "__main__":
    main()
