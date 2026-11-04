"""Web version of The Intentionality Interceptor.

The browser keeps today's usage and runs the timer; this server only asks the
gatekeeper (GPT-4o-mini with CLAUDE_TIMER_PROMPT) how many minutes to grant.
Without OPENAI_API_KEY it falls back to simple demo rules so the site still works.
"""

import os
import re
import time
from collections import defaultdict, deque

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from prompts import CLAUDE_TIMER_PROMPT, build_timer_message, parse_timer_verdict

load_dotenv()

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
AI_PER_HOUR = int(os.environ.get("AI_PER_HOUR", "40"))
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "600"))

app = Flask(__name__)
_recent = defaultdict(deque)
_daily = {"day": "", "count": 0}


def _key():
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _client_ip():
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()


def _allowed(ip):
    now = time.time()
    hits = _recent[ip]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    today = time.strftime("%Y-%m-%d")
    if _daily["day"] != today:
        _daily.update(day=today, count=0)
    if len(hits) >= AI_PER_HOUR or _daily["count"] >= AI_DAILY_LIMIT:
        return False
    hits.append(now)
    _daily["count"] += 1
    return True


def ask_gatekeeper(message):
    resp = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {_key()}"},
        json={
            "model": OPENAI_MODEL,
            "temperature": 0,
            "max_tokens": 80,
            "messages": [
                {"role": "system", "content": CLAUDE_TIMER_PROMPT},
                {"role": "user", "content": message},
            ],
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


VAGUE = re.compile(r"\b(bored|boring|check something|see what'?s new|scroll|catch up|notifications|idk|just because|for fun)\b", re.I)
BREAK = re.compile(r"\b(break|rest|relax|unwind|chill|tired|deserve|finished|done with)\b", re.I)
PUSHY = re.compile(r"\b(ignore|override|system|developer|admin|approve)\b|[A-Z]{6,}", re.I)


def demo_verdict(reason, usage):
    """Rough stand-in for the AI, used only when no API key is set."""
    if len(reason) < 12 or VAGUE.search(reason) or PUSHY.search(reason):
        return "DENY: That reason has no specific task or finish line. Name exactly what you'll do and when you'll be done."
    if BREAK.search(reason):
        if usage < 30:
            return "APPROVE: 25\nYou've barely touched your apps today, so enjoy a real 25-minute break."
        if usage < 90:
            return "APPROVE: 12\nA 12-minute break fits; you've already had some screen time today."
        if usage < 180:
            return "APPROVE: 5\nJust 5 minutes, since today's usage is already high."
        return "DENY: You've spent over three hours on these apps today. A walk or a glass of water will rest you more."
    return "APPROVE: 10\nTen minutes for that task. Close it when you're done."


@app.get("/")
def index():
    return render_template("index.html", ai=bool(_key()))


@app.get("/health")
def health():
    return jsonify(ok=True, ai=bool(_key()))


@app.post("/api/check")
def check():
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip()[:60]
    reason = str(data.get("reason", "")).strip()[:600]
    if not target or not reason:
        return jsonify(error="Pick an app and type your reason."), 400
    try:
        usage = max(0, min(1440, int(data.get("usage_minutes", 0))))
        sessions = max(0, min(200, int(data.get("sessions", 0))))
    except (TypeError, ValueError):
        usage, sessions = 0, 0

    source = "demo"
    reply = None
    if _key():
        if not _allowed(_client_ip()):
            return jsonify(error="Too many requests for now. Take that as a sign and try again later."), 429
        try:
            reply = ask_gatekeeper(build_timer_message(target, reason, usage, sessions))
            source = "ai"
        except Exception as exc:
            app.logger.warning("gatekeeper call failed: %s", type(exc).__name__)
    if reply is None:
        reply = demo_verdict(reason, usage)
    minutes, message = parse_timer_verdict(reply)
    return jsonify(approved=minutes > 0, minutes=minutes, message=message, source=source)


def _plain(text, status=200):
    return text, status, {"Content-Type": "text/plain; charset=utf-8"}


@app.post("/api/approve")
def approve():
    """Endpoint for the desktop app: replies with the model's raw verdict text.

    The desktop app parses the reply itself, so anything that goes wrong here is
    sent as a DENY line, never as an approval.
    """
    token = os.environ.get("GATEKEEPER_TOKEN", "").strip()
    if token:
        sent = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if sent != token and request.headers.get("X-Gatekeeper-Token", "").strip() != token:
            return _plain("DENY: Unauthorized.", 401)
    data = request.get_json(silent=True) or {}
    target = str(data.get("app") or data.get("target") or "").strip()[:60]
    reason = str(data.get("reason", "")).strip()[:600]
    if not target or not reason:
        return _plain("DENY: Missing app or reason.", 400)
    try:
        usage = max(0, min(1440, int(data.get("minutes_today", data.get("usage_minutes", 0)))))
        sessions = max(0, min(200, int(data.get("sessions", 0))))
    except (TypeError, ValueError):
        usage, sessions = 0, 0
    if not _key():
        return _plain("DENY: The gatekeeper isn't configured yet, so the lock stays on.")
    if not _allowed(_client_ip()):
        return _plain("DENY: Too many requests for now. Try again later.", 429)
    try:
        reply = ask_gatekeeper(build_timer_message(target, reason, usage, sessions))
    except Exception as exc:
        app.logger.warning("gatekeeper call failed: %s", type(exc).__name__)
        return _plain("DENY: The gatekeeper couldn't be reached, so the lock stays on.")
    minutes, message = parse_timer_verdict(reply)
    if minutes:
        return _plain(f"APPROVE: {minutes}\n{message}")
    return _plain(f"DENY: {message}")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5185")), debug=False)
