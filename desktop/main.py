"""
The Intentionality Interceptor
==============================

WAICY competition entry.

A desktop "intentionality gate": instead of letting you mindlessly open a
distracting app, the interceptor watches for blocked processes, kills them the
moment they appear, and throws up a topmost overlay that forces a pause.  The
overlay asks one question, sends it to an LLM gatekeeper, and only lets you
proceed if the gatekeeper answers APPROVE.

Architecture
------------
    CONFIG          -- everything tunable lives here
    CLAUDE_SYSTEM_PROMPT -- prompt-engineer hand-off point (Claude's work)
    ProcessMonitor  -- background thread: scan + terminate blocked processes
    IntentGatekeeper-- non-blocking OpenAI call, returns APPROVE / DENY
    InterceptorApp  -- customtkinter topmost overlay + thread-safe queue pump
    main()          -- wires it all together

Threading model
---------------
Tk is not thread-safe.  Nothing outside the main thread may touch a widget.
Both the monitor loop and the network call therefore only ever *enqueue*
events; the UI drains a queue.Queue on the Tk event loop via app.after().
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from typing import Optional

import customtkinter as ctk
import psutil

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

CONFIG = {
    # Lower-cased process names / executable stems that are never allowed to run.
    "blocked_processes": [
        "steam",
        "discord",
        "epicgameslauncher",
        "spotify",
        "riotclient",
        "leagueclient",
    ],
    # Optional: match against the full cmdline too (catches launchers).
    "match_cmdline": False,
    # Seconds between sweeps of the process table.
    "scan_interval": 1.0,
    # Grace period before escalating terminate() -> kill().
    "terminate_grace": 1.0,
    # OpenAI model used by the gatekeeper.
    "model": "gpt-4o-mini",
    # Hard timeout for the gatekeeper request, in seconds.
    "api_timeout": 20.0,
    # Where the gatekeeper decision comes from:
    #   "openai" -> call OpenAI directly from this desktop app
    #   "http"   -> call Claude's Vercel endpoint (keeps the API key off-device)
    #   "negotiator" -> multi-turn Negotiator: it asks why, may ask up to two
    #                   follow-up questions, then approves (signed JSON trigger)
    #                   or denies. See ../negotiator.py.
    "gatekeeper_mode": "negotiator",
    # Negotiator endpoint, e.g. "https://<host>/api/negotiate". Leave empty to
    # run the Negotiator engine in this process (needs OPENAI_API_KEY in the
    # environment or in the project's .env; without a key it uses local rules).
    "negotiator_url": "",
    # Vercel function URL, used when gatekeeper_mode == "http".
    # Contract: POST JSON {"app","reason","minutes_today"} ->
    #           {"reply": "APPROVE: 15"}  or  {"reply": "DENY"}
    "gatekeeper_url": "https://intentionality-interceptor.vercel.app/api/approve",
    # Optional shared secret sent as the `x-gatekeeper-token` header.
    "gatekeeper_token": "",
    # Max minutes a single approval may grant (the prompt module clamps too).
    "max_grant_minutes": 60,
    # Safety net: an unlock can never outlive this, whatever the model said.
    "max_unlock_minutes": 60,
    # Seconds between UI refreshes of today's usage counter.
    "usage_refresh_seconds": 5.0,
    # Persistent daily usage ledger (JSON). None -> default beside this file.
    "usage_path": None,

    # --- Theme ------------------------------------------------------------ #
    "bg": "#0d0f14",
    "card": "#151922",
    "accent": "#7dd3fc",
    "approve": "#4ade80",
    "deny": "#f87171",
    "text": "#e5e7eb",
    "muted": "#94a3b8",
}

DENY = "DENY"
# Suffixes that identify a helper/child process of a blocked application.
HELPER_SUFFIXES = {
    "webhelper", "helper", "crashpad", "crashhandler", "updater",
    "bootstrapper", "service", "overlay",
}

# Executable suffixes used when deciding whether an argv token is path-shaped.
EXEC_SUFFIXES = (".exe", ".app", ".bin", ".sh", ".appimage")

# --------------------------------------------------------------------------- #
# PROMPT HOOK  -- Claude (Prompt Engineer) owns the gatekeeper logic.
# --------------------------------------------------------------------------- #
#
# The system prompt and the APPROVE: <minutes> / DENY wire contract live in
# gatekeeper_prompt.py, which is Claude's file. This module imports them so no
# prompt text or parsing rule is duplicated here.
# --------------------------------------------------------------------------- #

from gatekeeper_prompt import (  # noqa: E402  (import kept below CONFIG for readability)
    CLAUDE_SYSTEM_PROMPT,
    MAX_GRANT_MINUTES,
    Verdict,
    build_user_message,
    parse_verdict,
)
from unlock_scheduler import UnlockScheduler  # noqa: E402
from usage_tracker import UsageTracker  # noqa: E402

# Used only while Claude's prompt is still empty, so the app stays demoable.
FALLBACK_SYSTEM_PROMPT = (
    "You are the Intentionality Interceptor, a strict gatekeeper. "
    "The user wants to start a distracting process. "
    "Read their reason and Today's Usage. Reply with exactly one line: "
    "APPROVE: <minutes 1-10> or DENY."
)

# Architecture-side guard: the model's ceiling can never exceed our own.
assert MAX_GRANT_MINUTES <= CONFIG["max_unlock_minutes"], (
    "gatekeeper_prompt.MAX_GRANT_MINUTES exceeds CONFIG['max_unlock_minutes']"
)


# --------------------------------------------------------------------------- #
# Events passed from worker threads to the Tk main thread
# --------------------------------------------------------------------------- #


@dataclass
class BlockedEvent:
    name: str
    pid: int


@dataclass
class GateResult:
    verdict: Verdict   # approved / minutes / reason (gatekeeper_prompt)
    raw: str           # the user message that was sent, for audit + logging
    kind: str = "verdict"   # "clarify" when the Negotiator asked a follow-up question


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Process monitor  (background thread)
# --------------------------------------------------------------------------- #


def _process_name(proc: psutil.Process) -> str:
    try:
        return (proc.name() or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def _stem(name: str) -> str:
    """Basename without a trailing executable suffix, lower-cased."""
    name = name.lower().replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".exe", ".app", ".bin"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


@dataclass
class ProcessMonitor:
    """Continuously sweeps the process table and kills blocked processes.

    Callbacks are invoked from this class's own thread, so callers must treat
    them as thread-unsafe and marshal to the UI thread themselves.
    """

    blocked: list[str]
    match_cmdline: bool
    interval: float
    grace: float
    on_blocked: "callable"
    # Returns True while `app` is under a live, granted unlock. Injected so the
    # monitor never imports the scheduler (keeps it headless and testable).
    is_allowed: "callable" = field(default=lambda _app: False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._targets = {_stem(n.lower()) for n in self.blocked}

    # -- matching ---------------------------------------------------------- #

    def _is_self_or_ancestor(self, proc: psutil.Process) -> bool:
        """Never kill ourselves or anything we were launched from."""
        try:
            me = psutil.Process(os.getpid())
            if proc.pid == me.pid:
                return True
            return proc.pid in {a.pid for a in me.parents()}
        except psutil.NoSuchProcess:
            return False

    def matched_app(self, proc: psutil.Process) -> Optional[str]:
        """Return the blocked app stem this process belongs to, else None."""
        if self._is_self_or_ancestor(proc):
            return None
        name = _process_name(proc)
        if not name:
            return None
        stem = _stem(name)
        if name in self._targets or stem in self._targets:
            return stem
        # Helper/child processes, e.g. "steamwebhelper", "discordcrashpad".
        # A bare prefix would false-positive ("discordian"), so require a
        # known helper suffix to follow the target name.
        for t in self._targets:
            if stem.startswith(t) and stem[len(t):] in HELPER_SUFFIXES:
                return t
        if self.match_cmdline:
            try:
                argv = proc.cmdline()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None
            # Scanning the whole command line would match any shell or editor
            # that merely *mentions* the target (`bash -c "... steam ..."`) and
            # kill innocent parents.  Two safe windows instead:
            #   1. the executable itself (argv[0], plus argv[1] for launchers
            #      such as `wine steam.exe`), and
            #   2. path-shaped arguments that resolve to the target basename.
            head = " ".join(argv[:2]).lower().replace("\\", "/")
            for tok in head.replace("=", " ").split():
                if _stem(tok) in self._targets:
                    return _stem(tok)
            for tok in argv[1:]:
                norm = tok.lower().replace("\\", "/")
                # Path-shaped only: must contain a separator or an executable
                # suffix, so free-text reasons can never trigger a kill.
                if ("/" in norm or norm.endswith(EXEC_SUFFIXES)) and \
                        _stem(norm) in self._targets:
                    return _stem(norm)
        return None

    def is_blocked(self, proc: psutil.Process) -> bool:
        """True if the process belongs to a blocked app with no live unlock."""
        app = self.matched_app(proc)
        if app is None:
            return False
        # A live grant keeps the app allowed. Checked per sweep so an expiry
        # takes effect on the very next pass.
        if self.is_allowed(app):
            return False
        return True

    # -- kill -------------------------------------------------------------- #

    def terminate(self, proc: psutil.Process) -> bool:
        """Terminate politely, then kill after `grace` seconds."""
        # Record the app *before* the process disappears from the table.
        app = self.matched_app(proc) or (_process_name(proc) or "?")
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return False
        try:
            proc.wait(timeout=self.grace)
        except psutil.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=self.grace)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                return False
        except psutil.NoSuchProcess:
            pass
        self.on_blocked(BlockedEvent(name=app, pid=proc.pid))
        return True

    def kill_app(self, app: str) -> int:
        """Terminate every live process belonging to `app`. Returns the count.

        Called when an unlock expires so re-locking is immediate rather than
        waiting for the next scheduled sweep.
        """
        killed = 0
        for proc in psutil.process_iter(["name", "pid"]):
            try:
                if self.matched_app(proc) == app and self.terminate(proc):
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return killed

    def sweep(self) -> int:
        """One pass over the process table. Returns number terminated."""
        killed = 0
        for proc in psutil.process_iter(["name", "pid"]):
            if self.is_blocked(proc) and self.terminate(proc):
                killed += 1
        return killed

    # -- lifecycle --------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:
                # A monitor thread must never die on one bad process.
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ProcessMonitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None


# --------------------------------------------------------------------------- #
# AI gatekeeper  (non-blocking)
# --------------------------------------------------------------------------- #


class IntentGatekeeper:
    """Wraps the OpenAI chat call so it never blocks the UI thread.

    `ask_async` spawns a daemon thread, invokes `on_result(GateResult)` when the
    answer (or failure) arrives, and returns immediately.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini",
                 timeout: float = 20.0, mode: str = "openai",
                 endpoint: str = "", token: str = "",
                 negotiator_url: str = "") -> None:
        self.model = model
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.mode = (mode or "openai").lower()
        self.endpoint = endpoint
        self.token = token
        self._client = None
        # Negotiator mode: one signed conversation per blocked app.
        self.negotiator_url = negotiator_url
        self._neg_state: dict = {}      # app -> session token from the last reply
        self._neg_day = ""
        self._neg_attempts: dict = {}   # app -> conversations started today
        self._neg_denials = 0

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI  # imported lazily so the UI works offline

            self._client = OpenAI(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def system_prompt(self) -> str:
        """Claude's prompt, with a fail-closed stand-in if it is still empty."""
        return CLAUDE_SYSTEM_PROMPT.strip() or FALLBACK_SYSTEM_PROMPT

    def ask(self, app: str, reason: str, minutes_today: int) -> GateResult:
        """Blocking single exchange. Returns a GateResult; never raises.

        Dispatches to the OpenAI backend or to Claude's HTTP endpoint. Either
        way the reply is parsed *locally* by ``parse_verdict`` -- a remote
        service can never hand us a pre-approved object.
        """
        if self.mode == "negotiator":
            return self._ask_negotiator(app, reason)
        message = build_user_message(app, reason, minutes_today)
        if self.mode == "http":
            return self._ask_http(app, reason, minutes_today, message)
        return self._ask_openai(message)

    def _ask_http(self, app: str, reason: str, minutes_today: int,
                  message: str) -> GateResult:
        """Call the Vercel gatekeeper endpoint.

        The backend replies in PLAIN TEXT in the form::

            APPROVE: 15
            <human message>

            DENY: <reason>

        Anything unparseable, any non-2xx status, and any transport failure is
        converted to a denial.  The reply is parsed locally -- the server never
        hands us a trusted decision object.
        """
        if not self.endpoint:
            return GateResult(
                Verdict(False, 0, "", "gatekeeper_url is not configured; failing closed."),
                message,
            )
        try:
            import json as _json
            import urllib.error
            import urllib.request

            payload = _json.dumps(
                {"app": app, "reason": reason, "minutes_today": int(minutes_today)}
            ).encode("utf-8")
            headers = {"content-type": "application/json"}
            if self.token:
                # The server accepts either header; send both for safety.
                headers["authorization"] = f"Bearer {self.token}"
                headers["x-gatekeeper-token"] = self.token
            req = urllib.request.Request(
                self.endpoint, data=payload, method="POST", headers=headers
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace").strip()
            verdict = parse_verdict(text)
            if not verdict.approved and not verdict.reason:
                # Give a useful reason when the server sent an opaque denial.
                first = text.splitlines()[0] if text else "empty response"
                verdict = Verdict(False, 0, text, f"Endpoint said: {first}")
            return GateResult(verdict, message)
        except urllib.error.HTTPError as exc:
            # 401/429/5xx all carry a DENY line body; surface it if present.
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            reason = detail.splitlines()[0] if detail else f"HTTP {exc.code}"
            return GateResult(Verdict(False, 0, detail, reason), message)
        except Exception as exc:  # DNS, TLS, timeout, bad encoding...
            return GateResult(
                Verdict(False, 0, "", f"Gatekeeper endpoint unavailable: {exc}"),
                message,
            )

    # -- Negotiator (multi-turn) ------------------------------------------- #

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._neg_day != today:
            self._neg_day, self._neg_attempts, self._neg_denials = today, {}, 0

    def begin(self, app: str) -> None:
        """A blocked launch starts a fresh conversation for that app."""
        self._roll_day()
        self._neg_state.pop(app, None)
        self._neg_attempts[app] = self._neg_attempts.get(app, 0) + 1

    def _negotiate(self, payload: dict) -> dict:
        """One Negotiator step, over HTTP or in this process. May raise."""
        if self.negotiator_url:
            import json as _json
            import urllib.request

            req = urllib.request.Request(
                self.negotiator_url, data=_json.dumps(payload).encode("utf-8"),
                method="POST", headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return _json.loads(resp.read().decode("utf-8", "replace"))
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from dotenv import load_dotenv

            load_dotenv(os.path.join(root, ".env"))
        except ImportError:
            pass
        import negotiator

        reply = negotiator.negotiate(payload["app"], payload["message"],
                                     payload.get("state"), payload.get("context"))
        if reply["decision"] == "APPROVE":
            ok, why = negotiator.verify_trigger(reply["trigger"])
            if not ok:
                raise ValueError(f"unlock trigger rejected ({why})")
        return reply

    def _ask_negotiator(self, app: str, reason: str) -> GateResult:
        """Send one user message to the Negotiator. Anything unexpected is a denial."""
        self._roll_day()
        payload = {
            "app": app,
            "message": reason,
            "state": self._neg_state.get(app),
            "context": {
                "local_time": time.strftime("%H:%M"),
                "attempts_today": self._neg_attempts.get(app, 1),
                "denials_today": self._neg_denials,
            },
        }
        try:
            reply = self._negotiate(payload)
            decision = str(reply.get("decision", "")).upper()
            said = str(reply.get("message") or "").strip()
            if decision == "CLARIFY":
                self._neg_state[app] = reply.get("state")
                return GateResult(Verdict(False, 0, said, said), reason, kind="clarify")
            self._neg_state.pop(app, None)
            trigger = reply.get("trigger") or {}
            if (decision == "APPROVE" and trigger.get("trigger") == "UNLOCK_APP"
                    and str(trigger.get("app", "")).lower() == app.lower()):
                minutes = int(trigger.get("minutes") or 0)
                if minutes >= 1:
                    return GateResult(
                        Verdict(True, min(minutes, CONFIG["max_unlock_minutes"]), said, said), reason)
            self._neg_denials += 1
            return GateResult(Verdict(False, 0, said, said or "Denied."), reason)
        except Exception as exc:  # network, bad JSON, bad trigger...
            return GateResult(
                Verdict(False, 0, "", f"Negotiator unavailable: {exc}"), reason)

    def _ask_openai(self, message: str) -> GateResult:
        """Call OpenAI directly from this device."""
        if not self.api_key:
            return GateResult(
                Verdict(False, 0, "", "OPENAI_API_KEY is not set; failing closed."),
                message,
            )
        try:
            resp = self._get_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt()},
                    {"role": "user", "content": message},
                ],
                temperature=0.0,
                # The reply is one short line ("APPROVE: 15"); keep it tight so
                # a chatty model cannot burn latency or tokens.
                max_tokens=16,
            )
            raw = (resp.choices[0].message.content or "").strip()
            return GateResult(parse_verdict(raw), message)
        except Exception as exc:  # network, auth, rate limit, bad key...
            return GateResult(
                Verdict(False, 0, "", f"Gatekeeper unavailable: {exc}"), message
            )

    def ask_async(self, app: str, reason: str, minutes_today: int,
                  on_result: "callable") -> threading.Thread:
        """Fire-and-forget. `on_result` runs on the worker thread."""

        def _worker() -> None:
            on_result(self.ask(app, reason, minutes_today))

        t = threading.Thread(target=_worker, name="IntentGatekeeper", daemon=True)
        t.start()
        return t


# --------------------------------------------------------------------------- #
# Overlay UI
# --------------------------------------------------------------------------- #


class InterceptorApp(ctk.CTk):
    """Topmost overlay. Owns the tracker + scheduler; all UI work stays here."""

    def __init__(self, monitor: ProcessMonitor, gatekeeper: IntentGatekeeper,
                 tracker: "UsageTracker") -> None:
        super().__init__()
        self.monitor = monitor
        self.gatekeeper = gatekeeper
        self.tracker = tracker
        self.events: "queue.Queue[object]" = queue.Queue()
        self._countdown_job: Optional[str] = None

        # Expiry fires on a timer thread -> hop to the Tk thread via the queue.
        self.scheduler = UnlockScheduler(on_expire=self._on_unlock_expired)

        ctk.set_appearance_mode("dark")
        self._build_window()
        self._build_widgets()
        self._refresh_usage()
        self.after(100, self._pump_events)

    # -- construction ------------------------------------------------------ #

    def _build_window(self) -> None:
        self.title("The Intentionality Interceptor")
        self.geometry("560x480")
        self.configure(fg_color=CONFIG["bg"])
        self.attributes("-topmost", True)
        self.minsize(480, 420)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_widgets(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(self, fg_color=CONFIG["card"], corner_radius=18)
        card.grid(row=0, column=0, padx=18, pady=18, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)

        self.usage = ctk.CTkLabel(
            card,
            text="Today's usage: 0 minutes",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=CONFIG["accent"],
            wraplength=470,
            justify="left",
            anchor="w",
        )
        self.usage.grid(row=0, column=0, padx=24, pady=(20, 0), sticky="w")

        self.status = ctk.CTkLabel(
            card,
            text="Watching for distractions.",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=CONFIG["text"],
            wraplength=470,
            justify="left",
        )
        self.status.grid(row=1, column=0, padx=24, pady=(10, 8), sticky="w")

        self.detail = ctk.CTkLabel(
            card,
            text="Blocked processes are terminated the instant they appear.",
            font=ctk.CTkFont(size=13),
            text_color=CONFIG["muted"],
            wraplength=470,
            justify="left",
        )
        self.detail.grid(row=2, column=0, padx=24, pady=(0, 16), sticky="w")

        self.entry = ctk.CTkEntry(
            card,
            placeholder_text="Why do you want to open it? Be honest.",
            height=42,
            font=ctk.CTkFont(size=14),
        )
        self.entry.grid(row=3, column=0, padx=24, pady=(0, 12), sticky="ew")
        self.entry.bind("<Return>", lambda _e: self._on_submit())

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=4, column=0, padx=24, pady=(0, 12), sticky="ew")
        buttons.grid_columnconfigure((0, 1), weight=1)

        self.submit_btn = ctk.CTkButton(
            buttons,
            text="Ask the gatekeeper",
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=CONFIG["accent"],
            text_color="#04121c",
            hover_color="#38bdf8",
            command=self._on_submit,
        )
        self.submit_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        self.dismiss_btn = ctk.CTkButton(
            buttons,
            text="Stay focused",
            height=40,
            font=ctk.CTkFont(size=14),
            fg_color="transparent",
            border_width=1,
            border_color=CONFIG["muted"],
            text_color=CONFIG["text"],
            hover_color="#1e2430",
            command=self._on_relock_now,
        )
        self.dismiss_btn.grid(row=0, column=1, padx=(6, 0), sticky="ew")

        self.verdict = ctk.CTkLabel(
            card,
            text="",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=CONFIG["muted"],
            wraplength=470,
            justify="left",
        )
        self.verdict.grid(row=5, column=0, padx=24, pady=(4, 24), sticky="w")

        # The app currently being gated (None while idle).
        self._pending_app: Optional[str] = None

    # -- thread bridge ----------------------------------------------------- #

    def _pump_events(self) -> None:
        """Drain worker-thread events on the Tk loop. Never blocks."""
        try:
            while True:
                event = self.events.get_nowait()
                if isinstance(event, BlockedEvent):
                    self._on_blocked_event(event)
                elif isinstance(event, GateResult):
                    self._on_gate_result(event)
                elif isinstance(event, tuple) and event and event[0] == "expire":
                    self._handle_expiry(event[1])
        except queue.Empty:
            pass
        self.after(100, self._pump_events)

    # -- usage ------------------------------------------------------------- #

    def _refresh_usage(self) -> None:
        """Settle expired unlocks and repaint the counter. Main thread only."""
        try:
            self.tracker.tick()
            total = self.tracker.minutes_today()
            self.usage.configure(text=f"Today's usage: {total} minutes")
        except Exception:
            # A tracker problem must never take the gate down.
            pass
        self.after(int(CONFIG["usage_refresh_seconds"] * 1000), self._refresh_usage)

    # -- event handlers ---------------------------------------------------- #

    def _on_blocked_event(self, event: BlockedEvent) -> None:
        if self.gatekeeper.mode == "negotiator" and event.name != self._pending_app:
            self.gatekeeper.begin(event.name)
        self._pending_app = event.name
        self.status.configure(text=f"Blocked: {event.name}")
        self.detail.configure(
            text=f"Process {event.pid} was terminated. "
                 "If you truly need it, justify it below."
        )
        self._show_overlay()

    def _on_submit(self) -> None:
        app = self._pending_app or "unknown"
        text = self.entry.get().strip()
        if not text:
            self.verdict.configure(text="Say something. Silence is not a reason.",
                                   text_color=CONFIG["deny"])
            return
        self.submit_btn.configure(state="disabled", text="Deciding...")
        self.verdict.configure(text="The gatekeeper is thinking...",
                               text_color=CONFIG["muted"])
        # Non-blocking: the worker thread only enqueues the result.
        self.gatekeeper.ask_async(app, text, self.tracker.minutes_today(),
                                  self.events.put)

    def _on_gate_result(self, result: GateResult) -> None:
        self.submit_btn.configure(state="normal", text="Ask the gatekeeper")
        verdict: Verdict = result.verdict
        app = self._pending_app or "unknown"

        if result.kind == "clarify":
            # The Negotiator wants one more detail: show its question, keep the lock.
            self.status.configure(text="One question.")
            self.detail.configure(text="Answer it specifically: what exactly, and for how long.")
            self.verdict.configure(text=verdict.reason, text_color=CONFIG["accent"])
            self.entry.delete(0, "end")
            self.entry.focus_set()
            return

        if self.gatekeeper.mode == "negotiator":
            self.entry.delete(0, "end")

        if not verdict.approved:
            self.status.configure(text="Denied.")
            self.detail.configure(text="The distraction stays blocked.")
            self.verdict.configure(text=f"DENY — {verdict.reason}",
                                   text_color=CONFIG["deny"])
            return

        # Clamp again on the architecture side; never trust one ceiling.
        minutes = min(verdict.minutes, CONFIG["max_unlock_minutes"])
        self._grant(app, minutes, verdict.reason)

    def _grant(self, app: str, minutes: int, reason: str) -> None:
        """Open the gate for `minutes`, then auto re-lock on expiry."""
        # 1. Scheduler owns the countdown (survives UI churn).
        self.scheduler.unlock(app, float(minutes), {"granted": minutes})
        # 2. Tracker books real time to today's total.
        self.tracker.start_unlock(app, minutes)

        self.status.configure(text=f"Approved: {app}")
        self.detail.configure(
            text=f"Unlocked for {minutes} minute(s). It re-locks automatically."
        )
        self.verdict.configure(text=f"APPROVE: {minutes} — {reason}",
                               text_color=CONFIG["approve"])
        self._start_countdown()
        self._refresh_usage()

    def _start_countdown(self) -> None:
        self._cancel_countdown()
        self._tick_countdown()

    def _cancel_countdown(self) -> None:
        if self._countdown_job is not None:
            try:
                self.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None

    def _tick_countdown(self) -> None:
        active = self.scheduler.active()
        if not active:
            return
        app, remaining = max(active.items(), key=lambda kv: kv[1])
        secs = max(0, int(round(remaining)))
        mm, ss = divmod(secs, 60)
        self.detail.configure(
            text=f"{app} re-locks in {mm:02d}:{ss:02d}."
        )
        self._countdown_job = self.after(1000, self._tick_countdown)

    def _on_unlock_expired(self, app: str, meta: dict) -> None:
        """Runs on the scheduler's timer thread -- must not touch widgets."""
        self.events.put(("expire", app))

    def _handle_expiry(self, app: str) -> None:
        """Main thread: close the gate, book usage, kill the app again."""
        self.tracker.stop_unlock(app)
        self.scheduler.relock(app)
        self._cancel_countdown()

        # Terminate anything still running for this app, right now. The next
        # monitor sweep would catch it within `scan_interval`, but re-locking
        # must be immediate.
        self.monitor.kill_app(app)

        self._pending_app = None
        self.status.configure(text=f"Re-locked: {app}")
        self.detail.configure(
            text="The granted time is over. It is blocked again."
        )
        self.verdict.configure(text="Unlock expired.", text_color=CONFIG["muted"])
        self.entry.delete(0, "end")
        self._refresh_usage()

    def _on_relock_now(self) -> None:
        """User chose to give up the unlock early."""
        if self._pending_app and self.scheduler.is_unlocked(self._pending_app):
            self._handle_expiry(self._pending_app)
            return
        self._reset_to_idle()

    # -- overlay strategy -------------------------------------------------- #

    def _show_overlay(self) -> None:
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        try:
            self.grab_set()  # modal-ish focus grab; ignore WMs that refuse
        except tk.TclError:
            pass
        self.entry.focus_set()
        for delay in (50, 250, 800):
            self.after(delay, lambda: self.attributes("-topmost", True))

    def _reset_to_idle(self) -> None:
        self.entry.delete(0, "end")
        self.verdict.configure(text="", text_color=CONFIG["muted"])
        self.status.configure(text="Watching for distractions.")
        self.detail.configure(
            text="Blocked processes are terminated the instant they appear."
        )
        try:
            self.grab_release()
        except tk.TclError:
            pass

    # -- teardown ---------------------------------------------------------- #

    def _on_close(self) -> None:
        self._cancel_countdown()
        self.scheduler.cancel_all()
        self.monitor.stop()
        try:
            self.tracker.save()
        except Exception:
            pass
        self.destroy()

    def run(self) -> None:
        self.mainloop()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def default_usage_path() -> str:
    """Daily ledger beside the app, or in the user's data dir if that fails."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "usage_log.json")


def main() -> None:
    gatekeeper = IntentGatekeeper(
        model=CONFIG["model"],
        timeout=CONFIG["api_timeout"],
        mode=CONFIG["gatekeeper_mode"],
        negotiator_url=CONFIG["negotiator_url"],
        endpoint=CONFIG["gatekeeper_url"],
        token=CONFIG["gatekeeper_token"] or os.environ.get("GATEKEEPER_TOKEN", ""),
    )

    usage_path = CONFIG.get("usage_path") or default_usage_path()
    tracker = UsageTracker(usage_path)

    # The app must exist before the monitor can be built, but the monitor's
    # callbacks need the app's queue and scheduler. Resolve both cycles with
    # late-bound callables that read `holder` once the app exists.
    holder: dict[str, object] = {}

    def on_blocked(event: BlockedEvent) -> None:
        q = holder.get("queue")
        if q is not None:
            q.put(event)

    def is_allowed(app_name: str) -> bool:
        # Queried from the monitor thread on every sweep. UnlockScheduler is
        # internally locked, so this is safe; a lapsed grant returns False and
        # the app is blocked on the very next pass.
        sched = holder.get("scheduler")
        return bool(sched and sched.is_unlocked(app_name))

    monitor = ProcessMonitor(
        blocked=CONFIG["blocked_processes"],
        match_cmdline=CONFIG["match_cmdline"],
        interval=CONFIG["scan_interval"],
        grace=CONFIG["terminate_grace"],
        on_blocked=on_blocked,
        is_allowed=is_allowed,
    )

    app = InterceptorApp(monitor, gatekeeper, tracker)
    holder["queue"] = app.events
    holder["scheduler"] = app.scheduler
    monitor.start()
    app.run()


if __name__ == "__main__":
    main()
