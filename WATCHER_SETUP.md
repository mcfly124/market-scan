# Phone alerts for good FX setups (24/7, free)

This runs a watcher on **GitHub's servers** (not your Mac) every ~15 minutes.
When a pair becomes a **triggered + good-location** setup, it pushes a
notification to your phone. Your Mac can be off.

- **Zero Claude usage** — the detection is pure Python; Claude is never called.
- The data is delayed (~15 min, free source), so a ping means *"a clean setup
  has formed"* — then you set the exact price alert on **Trading 212**.
- **Not financial advice.** It never tells you to buy; it flags conditions.

---

## Part A — pick ONE phone app

### Option 1: ntfy (simplest, no account)
1. Install **ntfy** (App Store / Google Play).
2. Open it → **Subscribe to topic** → type a secret-ish name,
   e.g. `massimo-fx-7h2kq` (hard to guess = others can't read your pings).
3. That topic name is all you need below.

### Option 2: Telegram (app you already have)
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts →
   it gives you a **bot token** like `8123456789:AAH...`.
2. Open a chat with your new bot and send it any message (e.g. "hi") —
   this is required so the bot can message you back.
3. Get your **chat id**: open this URL in a browser (paste your token):
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   Find `"chat":{"id":123456789` — that number is your chat id.

---

## Part B — put the code on GitHub (one time)

In Terminal (you can paste these after `! ` in Claude Code, or run them yourself):

```bash
cd ~/Desktop/market-scan
git init
git add .
git commit -m "FX watcher"
```

> Your `.env`, API key, and `trades.json` are in `.gitignore` and will **not**
> be uploaded. Double-check with `git status` — you should NOT see `.env`.

### Public vs private repo — read this (it affects cost)

GitHub Actions is **unlimited free on PUBLIC repos**, but a **private** repo only
gets ~2000 free minutes/month — and running every 15 min (~2880 runs/month)
would go a bit over that (~$5–7/mo after the free minutes).

- **Recommended: make the repo PUBLIC.** It's safe — this repo has **no secrets
  in the code** (your ntfy topic / Telegram token live in GitHub *Secrets*, and
  `.env` + `trades.json` are gitignored). Public = unlimited free, keep 15 min.
- **Prefer private?** Fine — then open `.github/workflows/watcher.yml` and change
  `cron: "*/15 * * * *"` to `cron: "*/30 * * * *"` (every 30 min) to stay inside
  the free 2000 minutes.

Create the repo on github.com (New repository → **Public** recommended), then:

```bash
git remote add origin https://github.com/<your-username>/market-scan.git
git branch -M main
git push -u origin main
```

---

## Part C — add your secret & turn it on

1. On GitHub: your repo → **Settings → Secrets and variables → Actions →
   New repository secret**. Add the one(s) for your chosen app:
   - ntfy: name `NTFY_TOPIC`, value = your topic (e.g. `massimo-fx-7h2kq`)
   - Telegram: add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
2. Go to the repo's **Actions** tab. If prompted, click **"I understand my
   workflows, enable them."**
3. Click **FX setup watcher** → **Run workflow** (manual test). In a few
   seconds the log should say either an alert was sent or
   "no fresh good-location setups". If there's a live setup, your phone pings.

From now on it runs itself every ~15 minutes, day and night.

---

## Tuning & notes

- **Only well-located setups ping.** `RR_MIN: "1.5"` in
  `.github/workflows/watcher.yml` means it alerts only when there's ≥1.5R room to
  structure. Raise it to `"2"` for fewer/stricter pings, or lower to `"1"` for more.
- **No spam.** It only pings when a pair *newly* becomes a good setup (or flips
  direction), not every run.
- **Weekends:** FX is closed Sat/Sun; you simply won't get pings.
- **Timing isn't tick-exact** (GitHub cron can drift a few minutes, and the data
  is delayed). The watcher says *a setup exists*; your Trading 212 alert catches
  the exact price. Use them together.
- **Cost:** free on a **public** repo (unlimited Actions minutes). On a private
  repo, use the 30-min cron above to stay within the free 2000 min/month. ntfy
  and Telegram are both free.
- **To pause it:** Actions tab → FX setup watcher → `•••` → Disable workflow.
