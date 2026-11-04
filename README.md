# Vouch (The Intentionality Interceptor)

A WAICY project that stands between you and a distracting app. Before it unlocks, you type a reason, and a GPT-4o-mini gatekeeper decides whether that reason is truly intentional.

- `prompts.py`: the gatekeeper's system prompt (`CLAUDE_SYSTEM_PROMPT`) and a fail-closed verdict parser.
- `app.py` + `templates/` + `static/`: a web version. Pick an app, type a reason, and the gatekeeper (`CLAUDE_TIMER_PROMPT`) grants a timer based on today's usage, which is tracked in the browser.
- Lying is the weak spot, so: a task needs proof (a link the server opens and reads, or a screenshot the AI looks at). Without proof a task gets 5 minutes at most. When the time ends you must check in with what you actually did; the AI compares it with your claim and raises or drops your trust score, which makes future grants stricter. If you keep lying to yourself, uninstall the app.
- **How a decision is made:** the AI (`CLAUDE_JUDGE_PROMPT` in `judge.py`) only judges: task, break, vague or manipulation; whether the proof matches; how long a real task takes; whether you broke a similar promise before. Plain code (`decide()`) then applies the rules, so the same facts always give the same answer: usage tiers, your daily limit and focus hours, proof requirements, and the trust score.
- **Trust works like a credit score (300–850, start 700):** Excellent 800+ keeps 100% of granted time, Very good 90%, Good 75%, Fair 50%, Poor 25% (and unproven tasks are denied). Kept your word +20, broke it −50 plus a 10-minute cooldown.
- **Your record, receipts and week:** every check-in is saved as "said vs did". A deny shows your latest broken promise as a receipt, and the AI quotes it when you try the same thing again. A 7-day chart shows minutes on purpose vs drifting.
- **Your rules:** a daily limit, focus hours (only proven tasks), and a note to yourself shown when you're denied.
- `CLAUDE_TIMER_PROMPT` / `CLAUDE_SYSTEM_PROMPT` stay in `prompts.py` for apps that call OpenAI directly with one prompt.
- Add `?demo` to the URL to show the demo sliders for usage and trust.
- The local Python UI lives in `desktop/` (see below).

## Run locally

```
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5185. Put `OPENAI_API_KEY=...` in a `.env` file next to `app.py` (it's gitignored). Without a key, simple demo rules decide instead of the AI.

## Hosting

Live at https://intentionality-interceptor.vercel.app (Vercel free plan; redeploy with `vercel deploy --prod`). It deploys as-is (Flask is detected from `app.py`). Set `OPENAI_API_KEY` under Project → Settings → Environment Variables.

Made by Legion · Awsaa-Legion@proton.me

## Desktop app endpoint

`POST /api/approve` with JSON `{"app": "YouTube", "reason": "...", "minutes_today": 42}` (optional `"sessions"`, `"trust"` 0–100, `"proof_url"`, and `"image"` as a `data:image/jpeg;base64,...` screenshot). The reply is plain text: `APPROVE: <minutes>` plus a message line, or `DENY: <reason>`. Every failure (no key, rate limit, OpenAI error, bad input) is sent as a `DENY` line. If `GATEKEEPER_TOKEN` is set on the server, send it as `Authorization: Bearer <token>`.

## Desktop app (`desktop/`)

A native `customtkinter` client that enforces the lock on the user's own machine:

- Watches the process table with `psutil` and terminates blocked apps.
- Shows a topmost overlay asking for a reason, then calls `/api/approve`.
- Injects today's usage into the request as `minutes_today`.
- On `APPROVE: N`, keeps the app open for N minutes and re-locks it
  automatically when the grant expires (`threading.Timer`), killing the process
  again and folding the elapsed minutes back into today's total.

It tracks usage locally in `usage_log.json`, so it works even if the web app is
closed. The Vercel endpoint only supplies the verdict; the desktop app parses
the reply itself and clamps any grant to its own ceiling.

```
cd desktop
pip install -r requirements.txt
python main.py
```

Point it somewhere else by editing `CONFIG["gatekeeper_mode"]` (`"http"` or
`"openai"`) and `CONFIG["gatekeeper_url"]` in `desktop/main.py`.
Set `GATEKEEPER_TOKEN` in the environment if the server requires it.

Note: the desktop app cannot be hosted on Vercel — it needs the user's process
table, a persistent local ledger and a long-running timer.
