"""Persistent daily usage tracking for AI-approved app unlocks.

The Intentionality Interceptor only lets the user touch a blocked app after a
gatekeeper approves a time-boxed unlock.  This module is the ledger for those
unlocks: it remembers which apps are currently unlocked, settles them when
their window closes, and accumulates how many minutes were actually spent
today (per app and in total).

Threading model
---------------
A single :class:`threading.RLock` guards *all* mutable state.  The Tk main
thread, a background worker and any ticking thread can therefore call the
public methods concurrently.  The lock is re-entrant because several public
methods delegate to one another (e.g. ``minutes_today`` -> ``tick`` ->
``_rollover``).  Disk writes happen inside the critical section too: the
serialized payload is computed under the lock and written atomically, so a
reader can never observe a state that does not match the file it will load.
Writes go to a temp file in the same directory and are then ``os.replace``-d
into place, which is atomic on POSIX and Windows alike.  The critical sections
are short (no network, no sleeps), so holding the lock through the write does
not meaningfully block the UI.

Persistence model
-----------------
The state file holds exactly one JSON object of the shape::

    {
      "date": "2026-11-04",
      "by_app": {"steam": 30},
      "active": [{"app": "discord", "granted_minutes": 15,
                  "started_at": 1762260000.0, "ends_at": 1762260900.0}],
      "history": {"2026-11-03": {"steam": 120}}
    }

``active`` entries are reconstructed into :class:`Unlock` dataclasses on load.
A missing, empty or malformed file is treated as a fresh tracker; loading never
raises.  Totals are keyed to the *local* calendar date, so crossing midnight
rolls the day over: the finished day is pushed into ``history`` (capped to the
last 14 days) and today's counters restart at zero.

Accounting rules
----------------
* A settled unlock contributes ``min(ends_at, now) - started_at`` seconds,
  rounded to whole minutes, and never more than ``granted_minutes`` -- leaving
  an unlock open for hours cannot inflate the day.
* Time is stored in per-app *seconds* internally (``_spent_seconds``) so that
  short sessions are not silently lost to integer rounding; the public API
  reports whole minutes.
* Settling removes the unlock from ``active`` and folds its contribution
  immediately, so repeated ``tick()`` calls are idempotent.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Optional

__all__ = ["Unlock", "UsageTracker", "HISTORY_DAYS"]

#: How many previous days of totals are retained on disk.
HISTORY_DAYS = 14

#: Version tag baked into the state file so future formats can migrate.
_STATE_VERSION = 1


def _norm_app(app: str) -> str:
    """Normalize an app/process name into a stable ledger key."""
    name = (app or "").strip().lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _today_str() -> str:
    """Local calendar date as ``YYYY-MM-DD``."""
    return date.today().isoformat()


def _seconds_to_minutes(seconds: float) -> int:
    """Convert raw accounted seconds to whole minutes (floor, clamped >= 0)."""
    if seconds <= 0:
        return 0
    return int(seconds // 60)


@dataclass
class Unlock:
    """An active, time-boxed approval for one app."""

    app: str
    granted_minutes: int
    started_at: float
    ends_at: float

    @property
    def remaining_seconds(self) -> float:
        """Seconds left in the window at wall-clock time (may be negative).

        Prefer :meth:`UsageTracker.usage_summary`, which computes this against
        the tracker's injected ``clock`` so tests and the UI agree.
        """
        return self.ends_at - time.time()

    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "granted_minutes": self.granted_minutes,
            "started_at": self.started_at,
            "ends_at": self.ends_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Unlock":
        """Rebuild an unlock from persisted JSON, coercing field types."""
        granted = int(raw.get("granted_minutes", 0) or 0)
        started = float(raw.get("started_at", 0.0) or 0.0)
        ends_raw = raw.get("ends_at")
        ends = float(ends_raw) if ends_raw is not None else started + granted * 60
        return cls(
            app=_norm_app(str(raw.get("app", ""))),
            granted_minutes=granted,
            started_at=started,
            ends_at=ends,
        )


class UsageTracker:
    """Thread-safe, atomically-persisted daily screen-time ledger."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self._path = os.fspath(path)
        self._clock = clock
        self._lock = threading.RLock()
        # date string -> {app: accounted seconds}.  The key set shrinks to one.
        self._spent_seconds: dict[str, dict[str, float]] = {}
        self._active: dict[str, Unlock] = {}
        self._history: dict[str, dict[str, int]] = {}
        self.load()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def start_unlock(self, app: str, granted_minutes: int) -> Unlock:
        """Begin (or extend) tracking for ``app`` and persist the change.

        If an unlock for the same app is already active its original
        ``started_at`` is kept and the new minutes are added to the window.
        """
        key = _norm_app(app)
        if not key:
            raise ValueError("app name must not be empty")
        minutes = int(granted_minutes)
        if minutes <= 0:
            raise ValueError("granted_minutes must be positive")

        with self._lock:
            now = float(self._clock())
            self._rollover(now)
            existing = self._active.get(key)
            if existing is not None:
                # Keep the original start, push the window out by the newly
                # granted minutes.  If the previous window already lapsed
                # (expired but not yet ticked), extend from now instead so the
                # user still gets the minutes they were just approved for.
                existing.granted_minutes += minutes
                base = existing.ends_at if existing.ends_at > now else now
                existing.ends_at = base + minutes * 60
                unlock = existing
            else:
                unlock = Unlock(
                    app=key,
                    granted_minutes=minutes,
                    started_at=now,
                    ends_at=now + minutes * 60,
                )
                self._active[key] = unlock
            self.save()
            # Return a snapshot so callers cannot mutate the guarded object.
            return Unlock(
                app=unlock.app,
                granted_minutes=unlock.granted_minutes,
                started_at=unlock.started_at,
                ends_at=unlock.ends_at,
            )

    def stop_unlock(self, app: str) -> None:
        """End tracking for ``app`` now and fold the elapsed time into today."""
        key = _norm_app(app)
        with self._lock:
            unlock = self._active.pop(key, None)
            if unlock is None:
                return
            now = float(self._clock())
            self._fold(unlock, now)
            self._rollover(now)
            self.save()

    def active_unlock(self, app: str) -> Optional[Unlock]:
        """Return the live :class:`Unlock` for ``app``, or ``None``."""
        key = _norm_app(app)
        with self._lock:
            unlock = self._active.get(key)
            if unlock is None:
                return None
            # Hand back a copy so callers cannot mutate guarded state.
            return Unlock(
                app=unlock.app,
                granted_minutes=unlock.granted_minutes,
                started_at=unlock.started_at,
                ends_at=unlock.ends_at,
            )

    def tick(self) -> dict[str, int]:
        """Settle expired unlocks and return ``{app: minutes used today}``.

        Idempotent: each unlock is folded exactly once, when its ``ends_at``
        has passed.  Also rolls the calendar day over if necessary.
        """
        with self._lock:
            now = float(self._clock())
            expired: list[Unlock] = [
                u for u in self._active.values() if u.ends_at <= now
            ]
            for unlock in expired:
                del self._active[unlock.app]
                self._fold(unlock, now)
            self._rollover(now)
            if expired:
                self.save()
            return self._minutes_by_app()

    def minutes_today(self, app: str | None = None) -> int:
        """Minutes used today, total (``app=None``) or for a single app.

        Includes time from currently-active unlocks up to ``now``.
        """
        key = _norm_app(app) if app is not None else None
        with self._lock:
            now = float(self._clock())
            self._rollover(now)
            today = self._day_bucket(_today_str())
            if key is None:
                seconds = sum(today.values())
                for unlock in self._active.values():
                    seconds += self._live_seconds(unlock, now)
                return _seconds_to_minutes(seconds)
            seconds = today.get(key, 0.0)
            unlock = self._active.get(key)
            if unlock is not None:
                seconds += self._live_seconds(unlock, now)
            return _seconds_to_minutes(seconds)

    def usage_summary(self) -> dict:
        """Return a snapshot of today's usage plus active unlock windows."""
        with self._lock:
            now = float(self._clock())
            self._rollover(now)
            by_app = self._minutes_by_app(now)
            active = [
                {
                    "app": unlock.app,
                    "remaining_seconds": max(0, int(unlock.ends_at - now)),
                }
                for unlock in self._active.values()
            ]
            return {
                "date": _today_str(),
                "total_minutes": sum(by_app.values()),
                "by_app": by_app,
                "active": active,
            }

    def save(self) -> None:
        """Atomically write the current state to ``self._path``."""
        with self._lock:
            today = _today_str()
            payload = {
                "version": _STATE_VERSION,
                "date": today,
                "by_app": {
                    app: _seconds_to_minutes(seconds)
                    for app, seconds in self._day_bucket(today).items()
                    if _seconds_to_minutes(seconds) > 0
                },
                "active": [u.to_dict() for u in self._active.values()],
                "history": self._history,
            }
            self._atomic_write(payload)

    def load(self) -> None:
        """Read state from ``self._path``; start fresh on any problem."""
        with self._lock:
            self._spent_seconds = {}
            self._active = {}
            self._history = {}

            raw = self._read_file()
            if raw is None:
                return  # missing / unreadable / malformed -> fresh tracker

            try:
                if raw.get("version") != _STATE_VERSION and "version" in raw:
                    # Unknown future version: keep history best-effort only.
                    self._load_history(raw.get("history"))
                    return
                loaded_date = str(raw.get("date") or _today_str())
                by_app = raw.get("by_app") or {}
                if isinstance(by_app, dict):
                    bucket: dict[str, float] = {}
                    for app, minutes in by_app.items():
                        try:
                            seconds = float(minutes) * 60.0
                        except (TypeError, ValueError):
                            continue
                        if seconds > 0:
                            bucket[_norm_app(str(app))] = seconds
                    if bucket:
                        self._spent_seconds[loaded_date] = bucket
                for entry in raw.get("active") or ():
                    if not isinstance(entry, dict):
                        continue
                    try:
                        unlock = Unlock.from_dict(entry)
                    except (TypeError, ValueError):
                        continue
                    if unlock.app:
                        self._active[unlock.app] = unlock
                self._load_history(raw.get("history"))
            except Exception:
                # Absolute last resort: malformed file => fresh state.
                self._spent_seconds = {}
                self._active = {}
                self._history = {}

            # Collapse any stale day buckets (e.g. a file written yesterday)
            # into history so today's view is correct immediately.
            self._rollover(float(self._clock()))

    # ------------------------------------------------------------------ #
    # Internals (callers must already hold the lock)
    # ------------------------------------------------------------------ #
    def _load_history(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        history: dict[str, dict[str, int]] = {}
        for day, apps in raw.items():
            if not isinstance(apps, dict):
                continue
            try:
                history[str(day)] = {
                    _norm_app(str(k)): int(v) for k, v in apps.items()
                }
            except (TypeError, ValueError):
                continue
        self._history = self._trim_history(history, _today_str())

    def _day_bucket(self, day: str) -> dict[str, float]:
        """Return the mutable per-app seconds bucket for ``day``."""
        return self._spent_seconds.setdefault(day, {})

    def _live_seconds(self, unlock: Unlock, now: float) -> float:
        """Accounted seconds so far for an unlock, clamped to its grant."""
        return min(max(0.0, now - unlock.started_at), self._cap_seconds(unlock))

    @staticmethod
    def _cap_seconds(unlock: Unlock) -> float:
        return max(0, unlock.granted_minutes) * 60.0

    def _fold(self, unlock: Unlock, now: float) -> None:
        """Book a settled unlock's contribution to *today's* bucket.

        The bucket is chosen by the settle time (``now``) rather than by
        ``ends_at``: when a grant expires across midnight the whole settled
        slice must land in the day the user is currently reading, otherwise
        ``minutes_today()`` right after the expiry would report yesterday's
        total.  ``now`` may precede ``ends_at`` when ``stop_unlock()`` cuts a
        session short; ``_live_seconds`` clamps the contribution either way.
        """
        seconds = self._live_seconds(unlock, now)
        if seconds <= 0:
            return
        bucket = self._day_bucket(_today_str())
        bucket[unlock.app] = bucket.get(unlock.app, 0.0) + seconds

    def _rollover(self, now: float) -> None:
        """Move non-today buckets into ``history``; keep today's bucket only."""
        today = _today_str()
        history = dict(self._history)
        for day in [d for d in self._spent_seconds if d != today]:
            bucket = self._spent_seconds.pop(day)
            totals = {app: _seconds_to_minutes(s) for app, s in bucket.items()}
            totals = {app: m for app, m in totals.items() if m > 0}
            if totals:
                prev = history.get(day, {})
                merged = dict(prev)
                for app, minutes in totals.items():
                    merged[app] = merged.get(app, 0) + minutes
                history[day] = merged
        if today not in self._spent_seconds:
            self._spent_seconds[today] = {}
        self._history = self._trim_history(history, today)

    @staticmethod
    def _trim_history(
        history: dict[str, dict[str, int]], today: str
    ) -> dict[str, dict[str, int]]:
        """Keep at most :data:`HISTORY_DAYS` days strictly before ``today``."""
        past = sorted((d for d in history if d < today), reverse=True)
        keep = set(past[:HISTORY_DAYS])
        return {d: history[d] for d in sorted(keep)}

    def _minutes_by_app(self, now: float | None = None) -> dict[str, int]:
        """Whole minutes today per app, including live active unlocks."""
        if now is None:
            now = float(self._clock())
        totals = {
            app: seconds for app, seconds in self._day_bucket(_today_str()).items()
        }
        for unlock in self._active.values():
            totals[unlock.app] = totals.get(unlock.app, 0.0) + self._live_seconds(
                unlock, now
            )
        result = {app: _seconds_to_minutes(s) for app, s in totals.items()}
        return {app: m for app, m in sorted(result.items()) if m > 0}

    # ------------------------------------------------------------------ #
    # Disk helpers (still called under the lock)
    # ------------------------------------------------------------------ #
    def _read_file(self) -> Optional[dict]:
        """Return the parsed state dict, or ``None`` if unusable."""
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return None
        if not text.strip():
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _atomic_write(self, payload: dict) -> None:
        """Serialize ``payload`` and swap it in atomically."""
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self._path}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            # Never leave a stray temp file behind on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
