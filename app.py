"""Web version of The Intentionality Interceptor.

The browser keeps today's usage, the trust score and the timer; this server asks
the gatekeeper (GPT-4o-mini with CLAUDE_TIMER_PROMPT) how many minutes to grant,
checks any proof the user attaches, and judges check-ins afterwards.
Without OPENAI_API_KEY it falls back to simple demo rules so the site still works.
"""

import ipaddress
import os
import re
import socket
import time
from collections import defaultdict, deque
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from prompts import (
    CLAUDE_CHECKIN_PROMPT,
    CLAUDE_TIMER_PROMPT,
    build_checkin_message,
    build_timer_message,
    parse_checkin_verdict,
    parse_timer_verdict,
)

load_dotenv()

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
AI_PER_HOUR = int(os.environ.get("AI_PER_HOUR", "40"))
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "600"))
MAX_IMAGE_CHARS = 2_500_000
BOT_WALL = re.compile(r"client challenge|just a moment|attention required|access denied|forbidden|captcha|are you a robot|^\s*\d{3}\b", re.I)
IMAGE_PREFIX = re.compile(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
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


def _int(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def ask_model(system, message, image=None):
    content = message
    if image:
        content = [
            {"type": "text", "text": message},
            {"type": "image_url", "image_url": {"url": image, "detail": "low"}},
        ]
    resp = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {_key()}"},
        json={
            "model": OPENAI_MODEL,
            "temperature": 0,
            "max_tokens": 80,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        },
        timeout=25,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---- proof links ---------------------------------------------------------

def _public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def _youtube(url):
    host = urlparse(url).hostname or ""
    return host == "youtu.be" or host.endswith("youtube.com")


def check_link(url):
    """Describe what a proof link really points to, for the gatekeeper to compare with the claim."""
    url = url.strip()[:500]
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        if _youtube(url):
            r = requests.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=6)
            if r.ok:
                d = r.json()
                return f'link to a YouTube video titled "{d.get("title", "")[:150]}" by "{d.get("author_name", "")[:60]}"'
            return "a YouTube link that could not be opened (unverified)"
        for _ in range(4):
            host = urlparse(url).hostname
            if not host or not _public_host(host):
                return "a link that could not be opened (unverified)"
            r = requests.get(url, timeout=6, stream=True, allow_redirects=False,
                             headers={"User-Agent": "Mozilla/5.0 IntentionalityInterceptor"})
            if r.is_redirect and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            body = r.raw.read(200_000, decode_content=True).decode(r.encoding or "utf-8", "replace")
            m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
            title = unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:150] if m else ""
            if not r.ok or BOT_WALL.search(title):
                title = ""
            if title:
                return f'link {url[:200]} with page title "{title}"'
            return f"link {url[:200]} (the site blocked reading the page, so judge by the address itself)"
    except Exception:
        pass
    return "a link that could not be opened (unverified)"


def _proof(data):
    """Return (proof_text, image) from the request body."""
    parts, image = [], None
    url = str(data.get("proof_url") or "").strip()
    if url:
        parts.append(check_link(url))
    img = str(data.get("image") or "")
    if img and len(img) <= MAX_IMAGE_CHARS and IMAGE_PREFIX.match(img):
        image = img
        parts.append("screenshot attached (look at the image)")
    return "; ".join(parts) or None, image


# ---- demo rules (no API key) --------------------------------------------

VAGUE = re.compile(r"\b(bored|boring|check something|see what'?s new|scroll|catch up|notifications|idk|just because|for fun)\b", re.I)
BREAK = re.compile(r"\b(break|rest|relax|unwind|chill|tired|deserve|finished|done with)\b", re.I)
PUSHY = re.compile(r"\b(ignore|override|system|developer|admin|approve)\b|[A-Z]{6,}", re.I)


def demo_verdict(reason, usage, proof):
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
    if not proof:
        return "APPROVE: 5\nFive minutes on your word. Add a link or screenshot next time to unlock the full time."
    return "APPROVE: 10\nTen minutes for that task. Close it when you're done."


# ---- routes ---------------------------------------------------------------

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
    usage = _int(data.get("usage_minutes"), 0, 1440, 0)
    sessions = _int(data.get("sessions"), 0, 200, 0)
    trust = _int(data.get("trust"), 0, 100, 50)

    source, reply = "demo", None
    if _key():
        if not _allowed(_client_ip()):
            return jsonify(error="Too many requests for now. Take that as a sign and try again later."), 429
        proof, image = _proof(data)
        try:
            reply = ask_model(CLAUDE_TIMER_PROMPT, build_timer_message(target, reason, usage, sessions, trust, proof), image)
            source = "ai"
        except Exception as exc:
            app.logger.warning("gatekeeper call failed: %s", type(exc).__name__)
    if reply is None:
        reply = demo_verdict(reason, usage, data.get("proof_url") or data.get("image"))
    minutes, message = parse_timer_verdict(reply)
    return jsonify(approved=minutes > 0, minutes=minutes, message=message, source=source)


@app.post("/api/checkin")
def checkin():
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip()[:60]
    reason = str(data.get("reason", "")).strip()[:600]
    report = str(data.get("report", "")).strip()[:600]
    minutes = _int(data.get("minutes"), 0, 60, 0)
    used = _int(data.get("used"), 0, 600, 0)
    if not report:
        return jsonify(error="Say what you actually did."), 400

    reply = None
    if _key() and _allowed(_client_ip()):
        try:
            reply = ask_model(CLAUDE_CHECKIN_PROMPT, build_checkin_message(target, reason, minutes, used, report))
        except Exception as exc:
            app.logger.warning("check-in call failed: %s", type(exc).__name__)
    if reply is None:
        words = {w for w in re.findall(r"[a-z]{5,}", reason.lower())}
        kept = len(report) >= 15 and bool(words & set(re.findall(r"[a-z]{5,}", report.lower())))
        reply = "KEPT: Your report matches what you planned." if kept else "BROKE: Your report doesn't match what you said you'd do."
    kept, message = parse_checkin_verdict(reply)
    return jsonify(kept=kept, message=message)


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
    usage = _int(data.get("minutes_today", data.get("usage_minutes")), 0, 1440, 0)
    sessions = _int(data.get("sessions"), 0, 200, 0)
    trust = _int(data.get("trust"), 0, 100, 50) if "trust" in data else None
    if not _key():
        return _plain("DENY: The gatekeeper isn't configured yet, so the lock stays on.")
    if not _allowed(_client_ip()):
        return _plain("DENY: Too many requests for now. Try again later.", 429)
    proof, image = _proof(data)
    try:
        reply = ask_model(CLAUDE_TIMER_PROMPT, build_timer_message(target, reason, usage, sessions, trust, proof), image)
    except Exception as exc:
        app.logger.warning("gatekeeper call failed: %s", type(exc).__name__)
        return _plain("DENY: The gatekeeper couldn't be reached, so the lock stays on.")
    minutes, message = parse_timer_verdict(reply)
    if minutes:
        return _plain(f"APPROVE: {minutes}\n{message}")
    return _plain(f"DENY: {message}")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5185")), debug=False)
