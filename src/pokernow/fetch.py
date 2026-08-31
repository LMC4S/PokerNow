"""Fetch game data straight from PokerNow given a game URL or id.

PokerNow has no official API. These are the same endpoints the web client
uses (discovered from its JS bundles):

* ``GET /games/{id}/players_sessions``              ledger (public, no auth)
* ``GET /games/{id}/log?after_at=&before_at=``      session log (needs login cookie)
* ``GET /api/hand-replayer/game/{id}``               hand list (needs login cookie)
* ``GET /api/hand-replayer/hand/{handId}``           one hand, handVersion 2 JSON (needs login cookie)

The two "Download" buttons in the UI (``/games/{id}/poker_now_log_{id}.csv``
and ``/hand-replayer/game/{id}/download``) sit behind a Cloudflare Turnstile
CAPTCHA and are deliberately *not* used here.

Authentication: PokerNow identifies you by the ``npt`` cookie (plus ``apt`` for
game admins). Put it in the ``POKERNOW_NPT`` environment variable (and
``POKERNOW_APT`` if you have it). This module never stores or prints it.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

GAME_ID_RE = re.compile(r"(?:pokernow\.(?:com|club)/(?:games|hand-replayer/game)/)?(?P<id>[A-Za-z0-9_-]{10,40})")
DEFAULT_HOST = "https://www.pokernow.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)  # PokerNow's edge treats non-browser fingerprints as anonymous (cookies get ignored)


class FetchError(RuntimeError):
    pass


class AuthError(FetchError):
    pass


def parse_game_ref(ref: str) -> tuple[str, str]:
    """Return (host, game_id) from a game URL or a bare id."""
    ref = ref.strip()
    host = DEFAULT_HOST
    m = re.match(r"^(https?://[^/]+)", ref)
    if m:
        host = m.group(1)
    mm = re.search(r"/(?:games|hand-replayer/game)/([A-Za-z0-9_-]+)", ref)
    if mm:
        return host, mm.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{6,40}", ref):
        return host, ref
    raise FetchError(f"Could not find a PokerNow game id in {ref!r}")


@dataclass
class Credentials:
    npt: str | None = None
    apt: str | None = None
    raw: str | None = None  # a full Cookie header, e.g. "npt=…; other=…" (POKERNOW_COOKIES)

    @classmethod
    def from_env(cls) -> "Credentials":
        return cls(
            npt=os.environ.get("POKERNOW_NPT") or None,
            apt=os.environ.get("POKERNOW_APT") or None,
            raw=os.environ.get("POKERNOW_COOKIES") or None,
        )

    @property
    def cookie_header(self) -> str | None:
        if self.raw:
            return self.raw.strip().strip('"')
        parts = []
        if self.npt:
            parts.append(f"npt={self.npt}")
        if self.apt:
            parts.append(f"apt={self.apt}")
        return "; ".join(parts) if parts else None


@dataclass
class FetchResult:
    game_id: str
    host: str
    ledger: dict[str, Any] | None = None
    log_rows: list[dict[str, Any]] = field(default_factory=list)  # {entry, at, order}
    hands_json: dict[str, Any] | None = None  # same shape as the downloadable file
    warnings: list[str] = field(default_factory=list)
    new_hands: int = 0
    new_log_lines: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.new_hands or self.new_log_lines)

    def log_csv(self) -> str:
        """Render the log as the same CSV PokerNow's export produces (newest first)."""
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(["entry", "at", "order"])
        for r in sorted(self.log_rows, key=lambda r: -int(r["order"])):
            w.writerow([r["entry"], r["at"], r["order"]])
        return buf.getvalue()


class PokerNowClient:
    """Thin HTTP client with a global rate limiter.

    PokerNow allows roughly 2 requests/second per client; going faster puts you
    in a penalty box where everything returns 429 for a while. We pace at
    ``min_interval`` seconds between requests and back off hard on 429.
    """

    def __init__(self, host: str = DEFAULT_HOST, creds: Credentials | None = None, min_interval: float = 0.6,
                 timeout: float = 20.0, log: Callable[[str], None] | None = None) -> None:
        self.host = host.rstrip("/")
        self.creds = creds or Credentials.from_env()
        self.min_interval = min_interval
        self.timeout = timeout
        self._log = log or (lambda s: None)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.requests_made = 0

    # ---- http ----
    def _pace(self) -> None:
        with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            self.requests_made += 1

    def _get(self, path: str, *, json_expected: bool = True, retries: int = 6) -> Any:
        url = self.host + path
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.host,
            "Referer": self.host + "/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        ck = self.creds.cookie_header
        if ck:
            headers["Cookie"] = ck
        last_err: Exception | None = None
        for attempt in range(retries):
            self._pace()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    if not json_expected:
                        return body
                    try:
                        return json.loads(body.decode("utf-8"))
                    except json.JSONDecodeError:
                        raise FetchError(f"{path}: expected JSON, got {body[:120]!r}")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise AuthError(f"{path}: HTTP {e.code} — login cookie missing, expired, or not a participant of this game")
                if e.code == 429 and attempt < retries - 1:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = float(retry_after) if retry_after else 5.0 * (2 ** attempt)
                    except ValueError:
                        wait = 5.0 * (2 ** attempt)
                    wait = min(wait, 120.0)
                    self._log(f"rate limited on {path}; sleeping {wait:.0f}s")
                    time.sleep(wait)
                    last_err = e
                    continue
                if e.code == 404:
                    raise FetchError(f"{path}: not found (wrong game id or game expired)")
                raise FetchError(f"{path}: HTTP {e.code}")
            except urllib.error.URLError as e:
                last_err = e
                time.sleep(2.0)
        raise FetchError(f"{path}: failed after retries: {last_err}")

    # ---- endpoints ----
    def ledger(self, game_id: str) -> dict[str, Any]:
        return self._get(f"/games/{game_id}/players_sessions")

    def log_page(self, game_id: str, after_at: int, before_at: int) -> dict[str, Any]:
        q = urllib.parse.urlencode({"after_at": after_at, "before_at": before_at})
        return self._get(f"/games/{game_id}/log?{q}")

    def fetch_log(self, game_id: str, *, after_at: int = 0, before_at: int | None = None, max_pages: int = 2000,
                  progress: Callable[[int], None] | None = None) -> list[dict[str, Any]]:
        """Page backwards through the session log. Returns rows as {entry, at, order}."""
        # created_at / order values are epoch milliseconds x 100; the endpoint's
        # after_at/before_at use the same units (fallback to plain ms below).
        if before_at is None:
            before_at = (int(time.time() * 1000) + 60_000) * 100
        rows: dict[int, dict[str, Any]] = {}
        cursor = before_at
        tried_ms_fallback = False
        for _ in range(max_pages):
            data = self.log_page(game_id, after_at, cursor)
            logs = data.get("logs") or []
            if not logs:
                break
            new = 0
            min_created = None
            for item in logs:
                created = item.get("created_at") or item.get("createdAt") or item.get("order")
                if created is None:
                    continue
                created = int(created)
                if created not in rows:
                    rows[created] = {"entry": item.get("msg") or item.get("entry") or "", "at": item.get("at"), "order": created}
                    new += 1
                min_created = created if min_created is None else min(min_created, created)
            if len(rows) % 500 < 50:
                self._log(f"log: {len(rows)} lines so far")
            if progress:
                progress(len(rows))
            if min_created is None:
                break
            if not new:
                if not tried_ms_fallback and min_created > 10**13:
                    tried_ms_fallback = True
                    cursor = min_created // 100
                    continue
                break
            cursor = min_created
            if cursor <= after_at:
                break
        if not rows and after_at == 0 and not self.creds.cookie_header:
            self._log("log is empty (game may not have started, or it expired)")
        return list(rows.values())

    def hand_list(self, game_id: str) -> Any:
        return self._get(f"/api/hand-replayer/game/{game_id}")

    def hand(self, hand_id: str) -> dict[str, Any]:
        return self._get(f"/api/hand-replayer/hand/{hand_id}")

    def fetch_hands(self, game_id: str, *, workers: int = 1, known: dict[str, dict[str, Any]] | None = None,
                    progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        """Reconstruct the downloadable hands JSON from the replayer API.

        ``known`` maps hand id -> previously fetched hand (skipped, for incremental refresh).
        """
        from concurrent.futures import ThreadPoolExecutor

        data = self.hand_list(game_id)
        hands = data.get("hands") if isinstance(data, dict) else data
        if not isinstance(hands, list):
            raise FetchError(f"unexpected hand list payload: {str(data)[:200]}")
        player_id = data.get("playerId") if isinstance(data, dict) else None
        known = known or {}
        full: list[dict[str, Any]] = []
        todo: list[str] = []
        for h in hands:
            if isinstance(h, dict) and h.get("events") and h.get("players"):
                full.append(h)
                continue
            hid = str(h.get("id") if isinstance(h, dict) else h)
            if hid in known:
                full.append(known[hid])
            elif hid:
                todo.append(hid)
        done = 0
        total = len(todo)

        def one(hid: str) -> dict[str, Any] | None:
            d = self.hand(hid)
            d.setdefault("id", hid)
            if not hand_is_finished(d):
                self._log(f"hand {d.get('number', hid)} still in progress; will retry later")
                return None
            return d

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for d in ex.map(one, todo):
                if d is not None:
                    full.append(d)
                done += 1
                if progress:
                    progress(done, total)
                if done % 50 == 0 or done == total:
                    self._log(f"hands: {done}/{total}")
        full.sort(key=lambda h: int(h.get("number") or 0))
        return {
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "playerId": player_id,
            "gameId": game_id,
            "hands": full,
        }


def hand_is_finished(hand: dict[str, Any]) -> bool:
    """True when the hand has a HAND_FINISHED (15) event (or a pot prize, for older data)."""
    types = {e.get("payload", {}).get("type") for e in hand.get("events", []) if isinstance(e, dict)}
    return 15 in types or 10 in types


def _atomic_write(path: str, data: str) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(data)
    os.replace(tmp, path)


@contextlib.contextmanager
def game_lock(game_dir: str):
    """Cross-process lock on a game folder (fcntl.flock), plus an in-process lock."""
    os.makedirs(game_dir, exist_ok=True)
    lock = _PROCESS_LOCKS.setdefault(game_dir, threading.RLock())
    with lock:
        fh = open(os.path.join(game_dir, ".lock"), "a+")
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # filesystem without flock support; in-process lock still holds
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def fetch_game(ref: str, *, what: tuple[str, ...] = ("ledger", "log", "hands"), creds: Credentials | None = None,
               log: Callable[[str], None] | None = None, progress: Callable[[str, int, int], None] | None = None,
               known_hands: dict[str, dict[str, Any]] | None = None, log_after_at: int = 0,
               client: PokerNowClient | None = None) -> FetchResult:
    """Fetch ledger / log / hands for a game. Hands are fetched first (they are
    what stats need); the log is paged afterwards."""
    host, game_id = parse_game_ref(ref)
    client = client or PokerNowClient(host=host, creds=creds, log=log)
    res = FetchResult(game_id=game_id, host=host)
    if not client.creds.cookie_header:
        res.warnings.append(
            "No login cookie set (web UI cookie box or POKERNOW_NPT): fetched anonymously, so your own "
            "un-shown hole cards ('Your hand is …' / players[].hand) and playerNet are not included."
        )
    if "ledger" in what:
        try:
            res.ledger = client.ledger(game_id)
        except FetchError as e:
            res.warnings.append(f"ledger: {e}")
    if "hands" in what:
        try:
            res.hands_json = client.fetch_hands(
                game_id, known=known_hands, progress=(lambda d, t: progress("hands", d, t)) if progress else None
            )
        except FetchError as e:
            res.warnings.append(f"hands: {e}")
    if "log" in what:
        try:
            res.log_rows = client.fetch_log(
                game_id, after_at=log_after_at, progress=(lambda n: progress("log", n, 0)) if progress else None
            )
        except FetchError as e:
            res.warnings.append(f"log: {e}")
    return res


def default_data_dir() -> str:
    return os.environ.get("POKERNOW_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".pokernow", "games")


class GameArchive:
    """On-disk copy of one game's raw exports, refreshed incrementally.

    Layout: ``<data_dir>/<game_id>/poker-now-hands-game-<id>.json``,
    ``poker_now_log_<id>.csv``, ``ledger-<id>.json``, ``meta.json``.
    PokerNow deletes hands after ~5 days, so this is also your long-term archive.
    """

    def __init__(self, game_id: str, data_dir: str | None = None) -> None:
        self.game_id = game_id
        self.dir = os.path.join(data_dir or default_data_dir(), game_id)

    # paths
    @property
    def hands_path(self) -> str:
        return os.path.join(self.dir, f"poker-now-hands-game-{self.game_id}.json")

    @property
    def log_path(self) -> str:
        return os.path.join(self.dir, f"poker_now_log_{self.game_id}.csv")

    @property
    def ledger_path(self) -> str:
        return os.path.join(self.dir, f"ledger-{self.game_id}.json")

    @property
    def meta_path(self) -> str:
        return os.path.join(self.dir, "meta.json")

    def exists(self) -> bool:
        return os.path.exists(self.hands_path) or os.path.exists(self.log_path)

    # load
    def load_hands(self) -> dict[str, Any] | None:
        if not os.path.exists(self.hands_path):
            return None
        with open(self.hands_path, encoding="utf-8") as f:
            return json.load(f)

    def load_log_rows(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            rows = []
            for i, row in enumerate(reader):
                if i == 0 and row and row[0].lower() == "entry":
                    continue
                if len(row) >= 3:
                    try:
                        rows.append({"entry": row[0], "at": row[1], "order": int(float(row[2]))})
                    except ValueError:
                        pass
            return rows

    def load_meta(self) -> dict[str, Any]:
        if not os.path.exists(self.meta_path):
            return {}
        with open(self.meta_path, encoding="utf-8") as f:
            return json.load(f)

    # refresh
    def refresh(self, ref: str | None = None, *, what: tuple[str, ...] = ("ledger", "hands", "log"),
                creds: Credentials | None = None, log: Callable[[str], None] | None = None,
                progress: Callable[[str, int, int], None] | None = None,
                force_hands: bool = False, full_log: bool = False) -> FetchResult:
        """Fetch what is new and persist after every stage (hands first, so a
        failing log download never loses them).

        ``force_hands`` re-downloads every hand even if cached (use after adding
        POKERNOW_NPT so your own hole cards get backfilled). ``full_log`` re-pages
        the whole log instead of only the tail; merged by ``order``, so no dupes.
        """
        with game_lock(self.dir):
            return self._refresh_locked(ref, what=what, creds=creds, log=log, progress=progress,
                                        force_hands=force_hands, full_log=full_log)

    def _refresh_locked(self, ref, *, what, creds, log, progress, force_hands=False, full_log=False) -> FetchResult:
        ref = ref or self.game_id
        host, _ = parse_game_ref(ref)
        client = PokerNowClient(host=host, creds=creds, log=log)
        res = FetchResult(game_id=self.game_id, host=host)
        if not client.creds.cookie_header:
            res.warnings.append(
                "No login cookie set (web UI cookie box or POKERNOW_NPT): fetched anonymously, so your own "
                "un-shown hole cards ('Your hand is …' / players[].hand) and playerNet are not included."
            )
        prev_hands = self.load_hands() or {}

        if "ledger" in what:
            try:
                res.ledger = client.ledger(self.game_id)
                _atomic_write(self.ledger_path, json.dumps(res.ledger, ensure_ascii=False, indent=2))
            except FetchError as e:
                res.warnings.append(f"ledger: {e}")

        if "hands" in what:
            known = {} if force_hands else {str(h.get("id")): h for h in prev_hands.get("hands", []) if h.get("id")}
            try:
                res.hands_json = client.fetch_hands(
                    self.game_id, known=known, progress=(lambda d, t: progress("hands", d, t)) if progress else None
                )
                if prev_hands.get("playerId") and not res.hands_json.get("playerId"):
                    res.hands_json["playerId"] = prev_hands["playerId"]
                res.new_hands = len(res.hands_json["hands"]) - len(known)
                if res.new_hands or force_hands or not os.path.exists(self.hands_path):
                    _atomic_write(self.hands_path, json.dumps(res.hands_json, ensure_ascii=False))
            except FetchError as e:
                res.warnings.append(f"hands: {e}")
                res.hands_json = prev_hands or None

        if "log" in what:
            prev_rows = self.load_log_rows()
            last_order = 0 if full_log else max((r["order"] for r in prev_rows), default=0)
            try:
                new_rows = client.fetch_log(
                    self.game_id, after_at=last_order, progress=(lambda n: progress("log", n, 0)) if progress else None
                )
            except FetchError as e:
                res.warnings.append(f"log: {e}")
                new_rows = []
            merged = {r["order"]: r for r in prev_rows}
            for r in new_rows:
                merged[r["order"]] = r
            res.new_log_lines = len(merged) - len(prev_rows)
            res.log_rows = list(merged.values())
            if res.log_rows and (res.new_log_lines or full_log or not os.path.exists(self.log_path)):
                _atomic_write(self.log_path, res.log_csv())

        meta = self.load_meta()
        meta.update({
            "game_id": self.game_id,
            "host": host,
            "last_refresh": datetime.now(timezone.utc).isoformat(),
            "hands": len((res.hands_json or prev_hands).get("hands", [])),
            "log_lines": len(res.log_rows) if "log" in what else meta.get("log_lines"),
            "requests": client.requests_made,
            "warnings": res.warnings,
        })
        _atomic_write(self.meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
        return res

    def files(self) -> dict[str, str]:
        out = {}
        for k, p in (("hands", self.hands_path), ("log", self.log_path), ("ledger", self.ledger_path)):
            if os.path.exists(p):
                out[k] = p
        return out
