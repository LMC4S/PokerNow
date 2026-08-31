"""Live tracking: poll a running game and append to its archive incrementally.

Each poll costs 1 request for the hand list + 1 per new hand + 1 for the log
tail (+ an occasional ledger refresh), well under PokerNow's ~2 req/s limit
at a 15 s interval. Everything goes through :class:`GameArchive.refresh`, the
same code path as a one-off ``pokernow fetch``, so live and post-hoc pulls
dedupe against each other and can run in any order.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .export import write_exports
from .fetch import Credentials, GameArchive, parse_game_ref
from .parser import load_archive
from .stats import compute_session_stats


@dataclass
class LiveState:
    game_id: str
    url: str
    interval: float
    running: bool = False
    polls: int = 0
    version: int = 0  # bumps whenever the archive changed
    hands: int = 0
    log_lines: int = 0
    last_poll_at: str | None = None
    last_change_at: str | None = None
    next_poll_at: str | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveTracker:
    """Polls one game in a daemon thread until :meth:`stop`."""

    def __init__(self, url: str, *, data_dir: str | None = None, interval: float = 15.0,
                 creds: Credentials | None = None, ledger_every: int = 20,
                 creds_provider: Callable[[], Credentials | None] | None = None,
                 on_update: Callable[["LiveTracker", Any], None] | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        host, game_id = parse_game_ref(url)
        self.archive = GameArchive(game_id, data_dir)
        self.state = LiveState(game_id=game_id, url=url, interval=interval)
        self.creds = creds
        self.creds_provider = creds_provider  # re-read each poll, so a cookie pasted mid-session applies
        self.ledger_every = ledger_every
        self.on_update = on_update
        self._log = log or (lambda s: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.session = None
        self.stats = None

    # -- lifecycle --
    def start(self) -> "LiveTracker":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self.state.running = True
        self._thread = threading.Thread(target=self._loop, name=f"live-{self.state.game_id}", daemon=True)
        self._thread.start()
        return self

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        self.state.running = False
        if wait and self._thread:
            self._thread.join(timeout=30)

    def poll_now(self) -> bool:
        """Run one refresh synchronously. Returns True if anything changed."""
        st = self.state
        what: tuple[str, ...] = ("hands", "log") if st.polls % self.ledger_every else ("ledger", "hands", "log")
        creds = self.creds_provider() if self.creds_provider else self.creds
        try:
            res = self.archive.refresh(st.url, what=what, creds=creds, log=self._note)
            st.polls += 1
            st.last_poll_at = _now()
            errors = [w for w in res.warnings if not w.startswith("No login cookie")]
            if errors:
                st.last_error = "; ".join(errors)
                st.consecutive_errors += 1
                self._note(st.last_error)
            else:
                st.last_error = None
                st.consecutive_errors = 0
            st.hands = len((res.hands_json or {}).get("hands", [])) or st.hands
            st.log_lines = len(res.log_rows) or st.log_lines
            first = self.session is None
            if res.changed or first:
                self.session = load_archive(self.archive)
                self.stats = compute_session_stats(self.session)
                write_exports(self.archive.dir, self.session, self.stats, self.archive.game_id)
                st.version += 1
                st.last_change_at = st.last_poll_at
                if res.changed:
                    self._note(f"+{res.new_hands} hands, +{res.new_log_lines} log lines (total {st.hands} hands)")
                if self.on_update:
                    self.on_update(self, res)
                return True
            return False
        except Exception as e:  # noqa: BLE001 - keep polling, surface the error
            st.polls += 1
            st.last_poll_at = _now()
            st.last_error = str(e)
            st.consecutive_errors += 1
            self._note(f"error: {e}")
            return False

    def _note(self, msg: str) -> None:
        self.state.messages = (self.state.messages + [f"{datetime.now().strftime('%H:%M:%S')} {msg}"])[-30:]
        self._log(msg)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_now()
            # back off a little on repeated errors (rate limit, network), cap at 2 min
            delay = min(self.state.interval * (2 ** min(self.state.consecutive_errors, 3)), 120.0)
            self.state.next_poll_at = datetime.fromtimestamp(time.time() + delay, tz=timezone.utc).isoformat()
            if self._stop.wait(delay):
                break
        self.state.running = False
        self.state.next_poll_at = None


class LiveRegistry:
    """All trackers in this process, keyed by game id."""

    def __init__(self, data_dir: str | None = None,
                 creds_provider: Callable[[], Credentials | None] | None = None) -> None:
        self.data_dir = data_dir
        self.creds_provider = creds_provider
        self._lock = threading.Lock()
        self._trackers: dict[str, LiveTracker] = {}

    def start(self, url: str, *, interval: float = 15.0, on_update=None) -> LiveTracker:
        _, game_id = parse_game_ref(url)
        with self._lock:
            t = self._trackers.get(game_id)
            if t is None or not t.state.running:
                t = LiveTracker(url, data_dir=self.data_dir, interval=interval, on_update=on_update,
                                creds_provider=self.creds_provider)
                self._trackers[game_id] = t
                t.start()
            return t

    def stop(self, game_id: str) -> bool:
        with self._lock:
            t = self._trackers.get(game_id)
        if t is None:
            return False
        t.stop()
        return True

    def get(self, game_id: str) -> LiveTracker | None:
        with self._lock:
            return self._trackers.get(game_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.state.to_dict() for t in self._trackers.values()]

    def stop_all(self) -> None:
        with self._lock:
            ts = list(self._trackers.values())
        for t in ts:
            t.stop()
