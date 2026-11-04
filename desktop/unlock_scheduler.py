"""Temporary-unlock bookkeeping and expiry timers for the Intentionality Interceptor.

This module owns exactly one responsibility: remembering which blocked
applications are *temporarily* allowed and when that permission runs out.  It
contains no UI code, no process inspection and no process killing.  It does not
know how to terminate anything -- when a grant expires it simply invokes the
injected ``on_expire(app, meta)`` callback, and the wiring layer decides what to
do (ask the monitor to kill the process, refresh the UI, write a log line...).

Threading model
---------------

* All internal state is guarded by a single :class:`threading.RLock`.  Every
  public method acquires it, so the scheduler is safe to call from any thread
  (the Tk main thread, the monitor thread, timer threads, ...).
* ``unlock()`` creates a :class:`threading.Timer` per application.  Timers are
  marked ``daemon = True`` before they are started, so pending grants never
  keep the interpreter alive at shutdown.
* ``on_expire`` runs on the *timer thread*, never on the caller's thread.  It is
  always invoked **after** the internal lock has been released: a consumer that
  calls back into the scheduler (``unlock``, ``relock``, ``cancel_all``, ...)
  must never deadlock, and ``RLock`` would not save us if the consumer instead
  blocked on its own UI thread while that thread waited for our lock.
  Consequently **consumers must marshal to their UI thread themselves** (e.g.
  ``After.dispatch`` / ``widget.after(0, ...)`` in Tkinter); touching widgets
  directly from ``on_expire`` is a bug.

Generation tokens (race conditions)
-----------------------------------

Cancelling a :class:`threading.Timer` is best-effort: if the timer has already
fired (or is inside ``run()`` right now) ``cancel()`` does nothing, and the
callback will still run.  A timer may therefore fire *after* ``relock()``,
*after* ``cancel_all()``, or *after* the app was re-unlocked with a new window.

To make every stale fire a harmless no-op, each app carries a monotonically
increasing generation counter.  Each scheduled timer captures both the app name
and the generation it was created for (the *token*).  When the timer fires it
re-acquires the lock and only delivers ``on_expire`` if the token still matches
the app's current generation **and** the recorded expiry has actually been
reached per the injected clock.  Any of the following bumps the generation and
thereby invalidates outstanding timers:

* ``unlock()`` on an already-unlocked app (replace/extend),
* ``relock()``,
* ``cancel_all()``,
* an accepted expiry (the token is consumed so a double fire cannot repeat).

The expiry decision itself is always made by comparing ``clock()`` against the
stored expiry, never by trusting that the timer fired on time.  ``is_unlocked``
and ``remaining_seconds`` additionally expire lazily, so a clock that has moved
past the expiry but whose timer has not fired yet still reports "locked".

Stdlib only; Python 3.10+.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Mapping, Optional

__all__ = ["UnlockScheduler"]

#: Callback invoked when a temporary unlock expires.
#: Called with ``(app, meta)`` on the timer thread; consumers must marshal to
#: their UI thread.
ExpireCallback = Callable[[str, dict], None]


class _Grant:
    """One outstanding temporary unlock for one application."""

    __slots__ = ("expiry", "meta", "generation", "timer")

    def __init__(
        self,
        expiry: float,
        meta: dict,
        generation: int,
        timer: object,
    ) -> None:
        self.expiry = expiry
        self.meta = meta
        self.generation = generation
        self.timer = timer


class UnlockScheduler:
    """Tracks temporary unlocks and fires ``on_expire`` when they run out.

    Parameters
    ----------
    clock:
        Source of monotonic time.  Injectable so tests can drive time by hand;
        defaults to :func:`time.monotonic`.
    timer_factory:
        Callable with the :class:`threading.Timer` signature
        ``timer_factory(interval, function)`` returning an object exposing
        ``cancel()``, ``start()`` and a settable ``daemon`` attribute.  Tests
        substitute a fake implementation.
    on_expire:
        Optional callback ``on_expire(app: str, meta: dict)``.  Invoked on the
        timer thread, outside the internal lock.  Exceptions raised by it are
        swallowed (the scheduler state has already been updated by then) so a
        buggy consumer cannot poison a timer thread.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable[[float, Callable[[], None]], object] = threading.Timer,
        on_expire: Optional[ExpireCallback] = None,
    ) -> None:
        self._clock = clock
        self._timer_factory = timer_factory
        self._on_expire = on_expire
        self._lock = threading.RLock()
        # app -> _Grant for apps whose grant exists.  An entry whose expiry has
        # passed but whose timer has not fired yet still lives here; the public
        # readers treat it as not unlocked and the timer callback reaps it.
        self._grants: dict[str, _Grant] = {}
        # app -> current generation token.  Never reset, so a token from a long
        # dead grant can never accidentally match a future one.
        self._generations: dict[str, int] = {}

    # -- internal helpers (caller must hold self._lock) --------------------- #

    def _next_generation(self, app: str) -> int:
        gen = self._generations.get(app, 0) + 1
        self._generations[app] = gen
        return gen

    def _is_live_locked(self, app: str, grant: _Grant, now: float) -> bool:
        """Caller holds the lock.  True if grant is current and not yet expired."""
        return (
            grant.generation == self._generations.get(app)
            and now < grant.expiry
        )

    def _drop_grant(self, app: str, grant: _Grant) -> None:
        """Caller holds the lock.  Remove ``grant`` and cancel its timer."""
        if self._grants.get(app) is grant:
            del self._grants[app]
        timer = grant.timer
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                # A stale/foreign timer object must never break bookkeeping.
                pass

    def _discard_expired(self, app: str, now: float) -> bool:
        """Caller holds the lock.

        Lazily drop an expired-but-not-yet-fired grant.  Bumps the generation
        so the still-pending timer fire is rejected, and cancels the timer.

        Returns True if something was discarded.
        """
        grant = self._grants.get(app)
        if grant is None:
            return False
        if grant.generation != self._generations.get(app):
            # Already superseded; just make sure no timer object lingers.
            self._drop_grant(app, grant)
            return False
        if now >= grant.expiry:
            self._next_generation(app)
            self._drop_grant(app, grant)
            return True
        return False

    def _on_timer(self, app: str, token: int) -> Callable[[], None]:
        """Build the timer callback for ``app`` at generation ``token``."""

        def _fire() -> None:
            payload: Optional[tuple[str, dict]] = None
            with self._lock:
                grant = self._grants.get(app)
                if (
                    grant is not None
                    and grant.generation == token
                    and self._generations.get(app) == token
                ):
                    now = self._clock()
                    if now >= grant.expiry:
                        # Accepted expiry: consume the token first so a second
                        # stray fire for the same token can never repeat it.
                        self._next_generation(app)
                        self._drop_grant(app, grant)
                        payload = (app, grant.meta)
                    else:
                        # Fired early (clock skew / fake clock not advanced).
                        # Leave the grant live; schedule the remainder.
                        self._reschedule_locked(app, grant, grant.expiry - now)

            # Lock released -- safe to hand control to consumer code.
            if payload is not None and self._on_expire is not None:
                fired_app, fired_meta = payload
                try:
                    self._on_expire(fired_app, fired_meta)
                except Exception:
                    # Consumer errors must not escape onto the timer thread.
                    pass

        return _fire

    def _reschedule_locked(self, app: str, grant: _Grant, delay: float) -> None:
        """Caller holds the lock.  Replace ``grant.timer`` with a fresh timer."""
        # Keep the same generation/token: this is still the same grant.
        old = grant.timer
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        timer = self._timer_factory(max(0.0, delay), self._on_timer(app, grant.generation))
        grant.timer = timer
        try:
            timer.daemon = True
        except Exception:
            pass
        timer.start()

    # -- public API --------------------------------------------------------- #

    def unlock(
        self, app: str, minutes: float, meta: Optional[dict] = None
    ) -> float:
        """Register (or replace) a temporary unlock for ``app``.

        If ``app`` is already unlocked, its previous timer is cancelled and
        invalidated, and a brand-new window of ``minutes`` is scheduled -- the
        new expiry is ``clock() + minutes*60``, not an extension of the old one.

        Returns the expiry timestamp expressed in the injected clock's units.
        """
        if minutes <= 0:
            raise ValueError("minutes must be positive")
        payload = dict(meta) if meta else {}

        with self._lock:
            existing = self._grants.get(app)
            if existing is not None:
                self._drop_grant(app, existing)

            now = self._clock()
            expiry = now + float(minutes) * 60.0
            # Bump the generation *before* scheduling: any timer from a prior
            # grant can no longer match, even if cancel() lost the race.
            gen = self._next_generation(app)

            # Placeholder grant so the callback can look itself up; the timer
            # is attached right after construction.
            grant = _Grant(expiry=expiry, meta=payload, generation=gen, timer=None)
            self._grants[app] = grant

            timer = self._timer_factory(
                max(0.0, expiry - now), self._on_timer(app, gen)
            )
            grant.timer = timer
            try:
                timer.daemon = True
            except Exception:
                pass
            timer.start()

            return expiry

    def relock(self, app: str) -> None:
        """Remove any unlock for ``app`` immediately (cancel + invalidate).

        Safe to call when the app is not unlocked.  Any already-running timer
        for ``app`` will be rejected by the generation check.
        """
        with self._lock:
            grant = self._grants.get(app)
            self._next_generation(app)
            if grant is not None:
                self._drop_grant(app, grant)

    def is_unlocked(self, app: str) -> bool:
        """True only if a grant exists and ``clock() < expiry``.

        Lazily expires: if the clock has passed the expiry but the timer has
        not fired yet, this returns False (and reaps the stale grant).
        """
        with self._lock:
            now = self._clock()
            self._discard_expired(app, now)
            grant = self._grants.get(app)
            return grant is not None and self._is_live_locked(app, grant, now)

    def remaining_seconds(self, app: str) -> float:
        """Seconds left before ``app`` re-locks; ``0.0`` if locked/expired."""
        with self._lock:
            now = self._clock()
            self._discard_expired(app, now)
            grant = self._grants.get(app)
            if grant is None or not self._is_live_locked(app, grant, now):
                return 0.0
            return grant.expiry - now

    def active(self) -> dict[str, float]:
        """Mapping ``app -> remaining_seconds`` for all currently-valid unlocks."""
        with self._lock:
            now = self._clock()
            for app in list(self._grants):
                self._discard_expired(app, now)
            return {
                app: grant.expiry - now
                for app, grant in self._grants.items()
                if self._is_live_locked(app, grant, now)
            }

    def cancel_all(self) -> None:
        """Cancel every pending timer and clear all state.  Idempotent."""
        with self._lock:
            grants = list(self._grants.items())
            self._grants.clear()
            for app, grant in grants:
                self._next_generation(app)
                self._drop_grant(app, grant)

    # -- introspection (not required by callers; handy for diagnostics) ----- #

    def metas(self) -> Mapping[str, dict]:
        """Snapshot of ``app -> meta`` for currently-valid unlocks."""
        with self._lock:
            now = self._clock()
            for app in list(self._grants):
                self._discard_expired(app, now)
            return {
                app: dict(grant.meta)
                for app, grant in self._grants.items()
                if self._is_live_locked(app, grant, now)
            }
