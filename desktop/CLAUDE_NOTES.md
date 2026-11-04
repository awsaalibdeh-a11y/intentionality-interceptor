# Claude's Integration Contract (v2 — screen time + dynamic timers)

You are the Prompt Engineer. You own `gatekeeper_prompt.py`. `main.py` imports
it and must not duplicate any prompt text or parsing rule.

## The one thing you own

In `gatekeeper_prompt.py`:

```python
CLAUDE_SYSTEM_PROMPT: str = "..."
```

Everything else in that file is the wire contract and is already tested.

## The wire contract

**Request** — built only by `build_user_message(app, reason, minutes_today)`:

```
The user is asking to use: <app>
Their stated reason: "<reason>"
Today's Usage: <X> minutes
Decide. Reply with exactly one line: APPROVE: <minutes 1-60> or DENY.
```

**Response** — parsed only by `parse_verdict(raw) -> Verdict`:

| reply | result |
|---|---|
| `APPROVE: 15` | approved, 15 min |
| `APPROVE: 500` | approved, clamped to `MAX_GRANT_MINUTES` (60) |
| `APPROVE: 0`, `-5`, `abc`, `3.7` | **DENY** (fail closed) |
| `DENY` | deny |
| `APPROVE: 10` + `DENY` anywhere | **DENY** (contradiction) |
| empty / None / prose without a verdict | **DENY** |

Invariants enforced by tests: `approved is False => minutes == 0`, and
`approved is True => 0 < minutes <= MAX_GRANT_MINUTES`.

## What the architecture does with your answer

1. `main.py` clamps again to `CONFIG["max_unlock_minutes"]` (never trusts one ceiling).
2. `UsageTracker.start_unlock(app, minutes)` starts booking real time to today's total.
3. `UnlockScheduler.unlock(app, minutes)` keeps the process **allowed** (the
   monitor checks `is_allowed()` every sweep) and starts the countdown.
4. On expiry the scheduler fires -> the app re-locks, calls `kill_app(app)` to
   terminate it immediately, and folds the elapsed minutes into today's usage.

## Guardrails you must not break

- Never grant more than `MAX_GRANT_MINUTES`; the parser clamps, but your prompt
  should also refuse.
- Higher "Today's Usage" should mean a smaller grant or a deny.
- No PII: the reason text is the only free-form user input.
- The system prompt produces a verdict string only — never an action.

---

# Vercel integration (v3)

Claude's work lives on Vercel. Only the gatekeeper layer can run there.

## Split

| Runs locally (desktop) | Runs on Vercel |
|---|---|
| `main.py`, `usage_tracker.py`, `unlock_scheduler.py` | `api/approve.py` |
| needs the user's process table + local ledger | needs only the OpenAI key |

The desktop app **cannot** be deployed to Vercel: no process table, no display,
ephemeral filesystem, and function timeouts shorter than a 15-minute grant.

## Contract

Client -> `POST {gatekeeper_url}` with `content-type: application/json`:

```json
{"app": "steam", "reason": "finish homework", "minutes_today": 42}
```

Function -> `200`:

```json
{"reply": "APPROVE: 15"}
```

`{"reply": "DENY"}` or any error status. **The client parses `reply` locally
with `parse_verdict`** — Vercel never returns a trusted decision object, so a
compromised or buggy endpoint can only ever produce denials.

Optional auth: set `GATEKEEPER_TOKEN` on Vercel and the same value in
`CONFIG["gatekeeper_token"]`; it travels as `x-gatekeeper-token`.

## Switching the desktop app over

```python
CONFIG["gatekeeper_mode"] = "http"
CONFIG["gatekeeper_url"]  = "https://<project>.vercel.app/api/approve"
```

Leave it as `"openai"` to call OpenAI directly (current default).
