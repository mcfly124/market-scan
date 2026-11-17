# Market Scan — honest, rule-based FX briefing

A tiny local web app. You press **Scan**, and it:

1. Pulls recent price data for a handful of FX pairs (via `yfinance`).
2. Computes **objective** technical state in code — 4h trend (EMA50),
   15m entry EMA(20), whether a pullback setup has triggered, ATR(14),
   and nearby swing levels.
3. Sends those facts to **Claude**, which writes a short plain-English
   briefing and flags the day's risks.

## What it is NOT

- It does **not** predict that any trade will profit.
- It does **not** output win-rates or "confidence %".
- It is **not** financial advice. Most retail traders lose money.

The "signal" here is an honest **setup state** ("triggered / waiting, and why"),
computed by rules — not a guess dressed up as a prophecy. Position size and
risk control matter far more than any entry.

## Setup (on your Mac)

```bash
cd market-scan
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit .env and paste YOUR api key
python app.py
```

Open the printed URL (default http://127.0.0.1:5057) and press **Scan market**.

### Your API key
- Get one at https://console.anthropic.com → API Keys.
- Put it in `.env` as `ANTHROPIC_API_KEY=...`.
- The key stays on your machine; it's only sent to Anthropic's own API.
- Costs are tiny: one scan = one short Claude call (fractions of a cent).

### No key yet?
The app still runs — it shows the rule-based cards and a clearly-labelled
**MOCK** briefing, so you can see the UI before adding a key.

## Notes / honest limits
- `yfinance` free data can be flaky or delayed; a pair may show "No data".
  Swap in a paid data feed in `analyze_pair()` if you want reliability.
- The setup rules are the same ones from the backtest we ran
  (4h-trend + 15m EMA20 pullback). A backtest that was only mildly
  positive over a short window is **not** proof of an edge — treat every
  output as information to weigh, not instructions to follow.
- Model ids change; if the Claude call errors with an unknown-model
  message, update `ANTHROPIC_MODEL` in `.env` per the docs link in that file.

## Files
- `app.py` — Flask server + data/indicator logic + Claude call
- `templates/index.html` — the dashboard (Scan button, cards, position sizer)
- `requirements.txt`, `.env.example`
