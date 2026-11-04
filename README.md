# The Intentionality Interceptor

A WAICY project that stands between you and a distracting app. Before it unlocks, you type a reason, and a GPT-4o-mini gatekeeper decides whether that reason is truly intentional.

- `prompts.py`: the gatekeeper's system prompt (`CLAUDE_SYSTEM_PROMPT`) and a fail-closed verdict parser.
- `app.py` + `templates/` + `static/`: a web version. Pick an app, type a reason, and the gatekeeper (`CLAUDE_TIMER_PROMPT`) grants a timer based on today's usage, which is tracked in the browser.
- Lying is the weak spot, so: a task needs proof (a link the server opens and reads, or a screenshot the AI looks at). Without proof a task gets 5 minutes at most. When the time ends you must check in with what you actually did; the AI compares it with your claim and raises or drops your trust score, which makes future grants stricter. If you keep lying to yourself, uninstall the app.
- Add `?demo` to the URL to show the demo sliders for usage and trust.
- The local Python UI is being built separately (DeepSeek).

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
