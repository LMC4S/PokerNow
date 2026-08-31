"""FastAPI application exposing parsed PokerNow sessions and stats.

Sessions are held in memory keyed by a short id. This is intentionally simple
for a first version; swap :class:`SessionStore` for a database-backed one later.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import os
import uuid

from .export import write_exports
from .fetch import Credentials, FetchError, GameArchive, default_data_dir, parse_game_ref
from .insights import compute_insights
from .live import LiveRegistry
from .models import ActionType, Session, Street
from .parser import load_archive, parse_text
from .stats import SessionStats, compute_session_stats

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class StoredSession:
    id: str
    name: str
    session: Session
    stats: SessionStats


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, StoredSession] = {}

    def add(self, name: str, text: str) -> StoredSession:
        sid = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        with self._lock:
            if sid in self._items:
                return self._items[sid]
        session = parse_text(text, source_name=name)
        return self.put(sid, name, session)

    def put(self, sid: str, name: str, session: Session) -> StoredSession:
        stats = compute_session_stats(session)
        stored = StoredSession(id=sid, name=name, session=session, stats=stats)
        with self._lock:
            self._items[sid] = stored
        return stored

    def get(self, sid: str) -> StoredSession:
        with self._lock:
            item = self._items.get(sid)
        if item is None:
            raise HTTPException(status_code=404, detail=f"session {sid!r} not found")
        return item

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": s.id,
                    "name": s.name,
                    "hands": s.stats.hands,
                    "players": [p.name for p in s.stats.players],
                    "started_at": s.stats.started_at,
                }
                for s in self._items.values()
            ]

    def delete(self, sid: str) -> None:
        with self._lock:
            self._items.pop(sid, None)


def _hand_summary(h, big_blind: int | None) -> dict[str, Any]:
    return {
        "number": h.number,
        "id": h.id,
        "started_at": h.started_at.isoformat() if h.started_at else None,
        "dealer": h.dealer,
        "players": h.players,
        "hero_cards": h.hero_cards,
        "board": h.board,
        "pot": h.pot,
        "pot_bb": round(h.pot / big_blind, 1) if big_blind else None,
        "winners": h.winners,
        "net": {p: h.net(p) for p in h.players},
        "showdown": h.went_to_showdown,
        "unparsed": len(h.unparsed),
        "bomb_pot": h.bomb_pot,
        "double_board": h.double_board,
        "run_it_twice": h.run_it_twice,
        "chip_mismatch": h.chip_mismatch,
        "hero_net": h.net(h.hero) if h.hero else None,
        "known_cards": h.known_cards,
        "preflop_raises": sum(
            1 for a in h.actions_on(Street.PREFLOP) if a.type in (ActionType.RAISE, ActionType.BET)
        ),
        "all_in": any(a.all_in for a in h.actions),
    }


class FetchRequest(BaseModel):
    url: str
    log: bool = True
    hands: bool = True
    ledger: bool = True
    force_hands: bool = False  # re-download cached hands too (backfill hole cards after adding a cookie)


class LiveRequest(BaseModel):
    url: str
    interval: float = 15.0


class CookieRequest(BaseModel):
    cookie: str


class FetchJobs:
    """Background fetch jobs (one thread each) with progress for the UI to poll."""

    def __init__(self, store: SessionStore, data_dir: str | None = None,
                 creds_provider=None) -> None:
        self.store = store
        self.data_dir = data_dir or default_data_dir()
        self.creds_provider = creds_provider
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self, ref: str, what: tuple[str, ...], force_hands: bool = False) -> dict[str, Any]:
        host, game_id = parse_game_ref(ref)
        job_id = uuid.uuid4().hex[:8]
        job: dict[str, Any] = {
            "id": job_id, "game_id": game_id, "status": "running", "stage": "starting",
            "hands_done": 0, "hands_total": 0, "log_lines": 0, "messages": [], "warnings": [],
            "session_id": None, "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job
        t = threading.Thread(target=self._run, args=(job, ref, what, force_hands), daemon=True)
        t.start()
        return job

    def _run(self, job: dict[str, Any], ref: str, what: tuple[str, ...], force_hands: bool = False) -> None:
        arch = GameArchive(job["game_id"], self.data_dir)

        def progress(kind: str, done: int, total: int) -> None:
            job["stage"] = kind
            if kind == "hands":
                job["hands_done"], job["hands_total"] = done, total
            elif kind == "log":
                job["log_lines"] = done

        def log(msg: str) -> None:
            job["messages"] = (job["messages"] + [msg])[-20:]

        try:
            creds = self.creds_provider() if self.creds_provider else None
            res = arch.refresh(ref, what=what, creds=creds, progress=progress, log=log, force_hands=force_hands)
            job["warnings"] = res.warnings
            job["stage"] = "parsing"
            session = load_archive(arch)
            stored = self.store.put(f"game-{arch.game_id}", f"PokerNow game {arch.game_id}", session)
            write_exports(arch.dir, session, stored.stats, arch.game_id)
            job["session_id"] = stored.id
            job["status"] = "done"
            job["stage"] = "done"
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            job["status"] = "error"
            job["error"] = str(e)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return dict(job)

    def list_archives(self) -> list[dict[str, Any]]:
        out = []
        if not os.path.isdir(self.data_dir):
            return out
        for name in sorted(os.listdir(self.data_dir)):
            arch = GameArchive(name, self.data_dir)
            if arch.exists():
                meta = arch.load_meta()
                out.append({"game_id": name, "files": list(arch.files()), **{k: meta.get(k) for k in ("last_refresh", "hands", "log_lines", "host")}})
        return out

    def load_archive(self, game_id: str) -> StoredSession:
        arch = GameArchive(game_id, self.data_dir)
        if not arch.exists():
            raise HTTPException(status_code=404, detail="archive not found")
        return self.store.put(f"game-{game_id}", f"PokerNow game {game_id}", load_archive(arch))


def create_app(data_dir: str | None = None) -> FastAPI:
    app = FastAPI(title="PokerNow Hand History", version="0.1.0")
    store = SessionStore()

    # Login cookie pasted through the UI. Kept only in this process's memory
    # (never written to disk) and read fresh on every fetch/poll, so pasting or
    # clearing it mid-session takes effect immediately. Falls back to the
    # POKERNOW_NPT / POKERNOW_COOKIES environment variables.
    ui_creds: dict[str, Credentials | None] = {"creds": None}

    def current_creds() -> Credentials:
        return ui_creds["creds"] or Credentials.from_env()

    jobs = FetchJobs(store, data_dir, creds_provider=current_creds)
    live = LiveRegistry(jobs.data_dir, creds_provider=current_creds)
    app.state.store = store
    app.state.jobs = jobs
    app.state.live = live

    @app.get("/api/credentials")
    def credentials_status() -> dict[str, Any]:
        if ui_creds["creds"] is not None:
            return {"set": True, "source": "ui"}
        if Credentials.from_env().cookie_header:
            return {"set": True, "source": "env"}
        return {"set": False, "source": None}

    @app.post("/api/credentials")
    def set_credentials(req: CookieRequest) -> dict[str, Any]:
        raw = req.cookie.strip().strip('"').strip()
        if not raw:
            raise HTTPException(status_code=400, detail="Paste the npt cookie value first.")
        if "npt=" in raw:
            # a whole cookie header ("npt=…; apt=…") or a copied name=value pair
            pairs = dict(p.split("=", 1) for p in (x.strip() for x in raw.split(";")) if "=" in p)
            creds = Credentials(npt=(pairs.get("npt") or "").strip() or None,
                                apt=(pairs.get("apt") or "").strip() or None)
            if not creds.npt:
                raise HTTPException(status_code=400, detail="Could not find an npt value in what you pasted.")
        elif any(c in raw for c in " ;,="):
            raise HTTPException(
                status_code=400,
                detail="That doesn't look like an npt cookie value — copy just the value of the npt cookie "
                       "(a long token with no spaces), or the whole 'npt=…' string.")
        else:
            creds = Credentials(npt=raw)
        ui_creds["creds"] = creds
        return {"set": True, "source": "ui"}

    @app.delete("/api/credentials")
    def clear_credentials() -> dict[str, Any]:
        ui_creds["creds"] = None
        return credentials_status()

    def _on_live_update(tracker, res) -> None:
        if tracker.session is not None:
            store.put(f"game-{tracker.archive.game_id}", f"PokerNow game {tracker.archive.game_id}", tracker.session)

    @app.post("/api/live")
    def live_start(req: LiveRequest) -> dict[str, Any]:
        try:
            t = live.start(req.url, interval=max(5.0, req.interval), on_update=_on_live_update)
        except FetchError as e:
            raise HTTPException(status_code=400, detail=str(e))
        d = t.state.to_dict()
        d["session_id"] = f"game-{t.archive.game_id}"
        return d

    @app.get("/api/live")
    def live_list() -> list[dict[str, Any]]:
        return live.list()

    @app.get("/api/live/{game_id}")
    def live_state(game_id: str) -> dict[str, Any]:
        t = live.get(game_id)
        if t is None:
            return {"game_id": game_id, "running": False, "version": 0}
        d = t.state.to_dict()
        d["session_id"] = f"game-{game_id}"
        return d

    @app.post("/api/live/{game_id}/poll")
    def live_poll(game_id: str) -> dict[str, Any]:
        t = live.get(game_id)
        if t is None:
            raise HTTPException(status_code=404, detail="not live-tracked")
        changed = t.poll_now()
        d = t.state.to_dict()
        d["changed"] = changed
        return d

    @app.delete("/api/live/{game_id}")
    def live_stop(game_id: str) -> dict[str, Any]:
        return {"stopped": live.stop(game_id)}

    @app.on_event("shutdown")
    def _shutdown() -> None:
        live.stop_all()

    @app.post("/api/fetch")
    def start_fetch(req: FetchRequest) -> dict[str, Any]:
        what = tuple(k for k, v in (("ledger", req.ledger), ("hands", req.hands), ("log", req.log)) if v)
        try:
            return jobs.start(req.url, what, force_hands=req.force_hands)
        except FetchError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/fetch/{job_id}")
    def fetch_status(job_id: str) -> dict[str, Any]:
        return jobs.get(job_id)

    @app.get("/api/archives")
    def archives() -> list[dict[str, Any]]:
        return jobs.list_archives()

    @app.post("/api/archives/{game_id}/load")
    def load_archived(game_id: str) -> dict[str, Any]:
        stored = jobs.load_archive(game_id)
        return {"id": stored.id, "name": stored.name, "summary": stored.stats.to_dict()}

    @app.get("/api/archives/{game_id}/files")
    def archive_files(game_id: str) -> dict[str, Any]:
        arch = GameArchive(game_id, jobs.data_dir)
        if not arch.exists():
            raise HTTPException(status_code=404, detail="archive not found")
        files = arch.files()
        for name in ("stats.json", "players.csv", "hands.csv", "summary.md", "meta.json"):
            p = os.path.join(arch.dir, name)
            if os.path.exists(p):
                files[name] = p
        return {"dir": arch.dir, "files": files}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/sessions")
    async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
        raw = await file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        head = text.lstrip()[:200].lower()
        if not (head.startswith("{") or "entry" in head or "starting hand" in text):
            raise HTTPException(status_code=400, detail="This does not look like a PokerNow log (.csv) or hands (.json) export.")
        stored = store.add(file.filename or "upload.csv", text)
        return {"id": stored.id, "name": stored.name, "summary": stored.stats.to_dict()}

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        return store.list()

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str) -> dict[str, str]:
        store.get(sid)
        store.delete(sid)
        return {"status": "deleted"}

    @app.get("/api/sessions/{sid}")
    def session_summary(sid: str) -> dict[str, Any]:
        s = store.get(sid)
        game_id = s.id[5:] if s.id.startswith("game-") else None
        return {"id": s.id, "name": s.name, "summary": s.stats.to_dict(), "game_id": game_id,
                "hero": s.session.hero, "source_format": s.session.source_format, "events": len(s.session.events)}

    @app.get("/api/sessions/{sid}/players")
    def players(sid: str) -> list[dict[str, Any]]:
        s = store.get(sid)
        return [p.to_dict(s.stats.big_blind) for p in s.stats.players]

    @app.get("/api/sessions/{sid}/players/{player}")
    def player(sid: str, player: str) -> dict[str, Any]:
        s = store.get(sid)
        for p in s.stats.players:
            if p.player == player or p.name == player or player in p.aka:
                d = p.to_dict(s.stats.big_blind)
                d["hands"] = [
                    _hand_summary(h, s.stats.big_blind) for h in s.session.hands if p.player in h.players
                ]
                d["hands_count"] = p.hands
                return d
        raise HTTPException(status_code=404, detail=f"player {player!r} not found")

    @app.get("/api/sessions/{sid}/hands")
    def hands(
        sid: str,
        player: str | None = Query(default=None, description="Only hands involving this player key or name"),
        involvement: str | None = Query(default=None, description="With player: vpip | flop | showdown | won"),
        min_pot: int = Query(default=0),
        showdown: bool | None = Query(default=None),
        limit: int = Query(default=500, le=5000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        s = store.get(sid)
        items = s.session.hands

        def match_key(h, name: str) -> str | None:
            for p in h.players:
                if p == name or p.rsplit(" @ ", 1)[0] == name:
                    return p
            return None

        def involved(h, pkey: str) -> bool:
            if involvement == "won":
                return h.collected.get(pkey, 0) > 0
            if involvement == "showdown":
                return h.went_to_showdown and pkey in h.survivors
            if involvement == "vpip":
                return any(
                    a.player == pkey and a.type in (ActionType.CALL, ActionType.BET, ActionType.RAISE)
                    for a in h.actions_on(Street.PREFLOP)
                )
            if involvement == "flop":
                folded_pre = any(
                    a.player == pkey and a.type is ActionType.FOLD and a.street is Street.PREFLOP for a in h.actions
                )
                return bool(h.board) and not folded_pre
            return True

        if player:
            keyed = [(h, match_key(h, player)) for h in items]
            items = [h for h, k in keyed if k is not None and involved(h, k)]
        if min_pot:
            items = [h for h in items if h.pot >= min_pot]
        if showdown is not None:
            items = [h for h in items if h.went_to_showdown == showdown]
        total = len(items)
        items = items[offset : offset + limit]
        return {"total": total, "hands": [_hand_summary(h, s.stats.big_blind) for h in items]}

    @app.get("/api/sessions/{sid}/hands/{number}")
    def hand(sid: str, number: int) -> dict[str, Any]:
        s = store.get(sid)
        for h in s.session.hands:
            if h.number == number:
                d = h.to_dict()
                d["big_blind"] = s.stats.big_blind
                return d
        raise HTTPException(status_code=404, detail=f"hand #{number} not found")

    _insights_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

    @app.get("/api/sessions/{sid}/insights")
    def insights(sid: str) -> dict[str, Any]:
        s = store.get(sid)
        # key on the parsed-session object identity too: a refetch can add hole
        # cards without changing the hand count
        key = (len(s.session.hands), id(s.session))
        cached = _insights_cache.get(sid)
        if cached and cached[0] == key:
            return cached[1]
        result = compute_insights(s.session, s.stats.big_blind)
        _insights_cache[sid] = (key, result)
        return result

    @app.get("/api/sessions/{sid}/events")
    def events(sid: str) -> list[dict[str, Any]]:
        s = store.get(sid)
        return [
            {
                "at": e.at.isoformat() if e.at else None,
                "kind": e.kind,
                "player": e.player,
                "amount": e.amount,
                "raw": e.raw,
            }
            for e in s.session.events
        ]

    @app.get("/api/sessions/{sid}/unparsed")
    def unparsed(sid: str) -> dict[str, Any]:
        s = store.get(sid)
        return {
            "session": s.session.unparsed,
            "hands": {h.number: h.unparsed for h in s.session.hands if h.unparsed},
        }

    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()
