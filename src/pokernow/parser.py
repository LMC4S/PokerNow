"""Parser for PokerNow.club hand-history CSV log exports.

The export is a CSV with three columns: ``entry``, ``at`` (ISO timestamp) and
``order`` (monotonic integer). Rows are newest-first; we sort by ``order``
before parsing. Players are referenced as ``"Name @ id"`` inside entries.

Amount semantics (verified against real exports): ``calls N``, ``bets N`` and
``raises to N`` all give the player's *total* wager on the current street; the
increment is derived from what they already had in.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from .models import Action, ActionType, Hand, Player, SeatInfo, Session, Street, TableEvent

PLAYER_RE = r'"(?P<name>.+?) @ (?P<pid>[^"\s]+)"'
_ANY_PLAYER = r'"[^"]+? @ [^"\s]+"'  # unnamed variant, for lines with several players

_RE = {
    "start": re.compile(
        r"^-- starting hand #(?P<num>\d+)"
        r"(?:\s*\(id:\s*(?P<hid>[^)]*)\))?"
        r"\s*(?:\((?P<game_p>[^)]*)\)|(?P<game>.+?))"
        r"\s*\((?:dealer:\s*" + PLAYER_RE + r"|(?P<deadbtn>dead button))\)"
        r"\s*--\s*$"
    ),
    "end": re.compile(r"^-- ending hand #(?P<num>\d+) --\s*$"),
    "stacks": re.compile(r"^Player stacks:\s*(?P<rest>.*)$"),
    "seat": re.compile(r'#(?P<seat>\d+)\s+' + PLAYER_RE + r"\s*\((?P<stack>-?\d+)\)"),
    "hero": re.compile(r"^Your hand is\s+(?P<cards>.+?)\.?\s*$"),
    "post": re.compile(
        r"^" + PLAYER_RE + r"\s+posts\s+(?:a|an|the)?\s*(?P<kind>small blind|big blind|missing small blind|missed small blind|missed big blind|missing big blind|straddle|ante|big blind ante|dead small blind|dead big blind)"
        r"\s+of\s+(?P<amt>\d+)(?P<allin>\s+and go all in)?\s*$"
    ),
    "bomb_post": re.compile(r"^" + PLAYER_RE + r"\s+(?:posts a bet of|calls)\s+(?P<amt>\d+)(?P<allin>\s+and go all in)?\s+\(bomb pot bet\)\s*$"),
    "fold": re.compile(r"^" + PLAYER_RE + r"\s+folds\s*$"),
    "check": re.compile(r"^" + PLAYER_RE + r"\s+checks\s*$"),
    "call": re.compile(r"^" + PLAYER_RE + r"\s+calls\s+(?P<amt>\d+)(?P<allin>\s+and go all in)?\s*$"),
    "bet": re.compile(r"^" + PLAYER_RE + r"\s+bets\s+(?P<amt>\d+)(?P<allin>\s+and go all in)?\s*$"),
    "raise": re.compile(r"^" + PLAYER_RE + r"\s+raises\s+to\s+(?P<amt>\d+)(?P<allin>\s+and go all in)?\s*$"),
    "allin_only": re.compile(r"^" + PLAYER_RE + r"\s+(?:goes|go) all in(?:\s+with\s+(?P<amt>\d+))?\s*$"),
    "board": re.compile(
        r"^(?P<street>Flop|Turn|River)(?:\s*\((?P<variant>[^)]*)\))?:\s*(?P<prev>[^\[]*)\[(?P<new>[^\]]+)\]\s*$",
        re.IGNORECASE,
    ),
    "undealt": re.compile(r"^Undealt cards(?:\s*\((?P<variant>[^)]*)\))?:\s*(?P<prev>[^\[]*)\[(?P<new>[^\]]+)\]\s*$"),
    "uncalled": re.compile(r"^Uncalled bet of\s+(?P<amt>\d+)\s+returned to\s+" + PLAYER_RE + r"\s*$"),
    "collect": re.compile(
        r"^" + PLAYER_RE + r"\s+collected\s+(?P<amt>\d+)\s+from\s+(?:the\s+)?pot"
        r"(?:\s+with\s+(?P<desc>.+?)(?:\s*\(combination:\s*(?P<combo>[^)]*)\))?)?\.?\s*$"
    ),
    "show": re.compile(r"^" + PLAYER_RE + r"\s+shows\s+(?:a\s+)?(?P<cards>.+?)\.?\s*$"),
    "muck": re.compile(r"^" + PLAYER_RE + r"\s+(?:mucks|does not show|chooses not to show|decided to not show)(?:\s+.*)?$"),
    # informational lines inside a hand (kept as notes, not as actions)
    "rit_player": re.compile(r"^" + PLAYER_RE + r"\s+chooses to\s+(?P<decision>not\s+)?run it twice\.?\s*$"),
    "info": re.compile(
        r"^(?:Remaining players decide whether to run it twice\.?"
        r"|All players in hand choose to run it twice\.?"
        r"|Some players choose to not run it twice\.?"
        r"|Dead Small Blind"
        r"|Asking to busted players the rebuy decision\.?"
        r"|Waiting for the game owner to approve or reject pending rebuy requests\.?)\s*$"
    ),
    # table events (outside or inside hands)
    "join": re.compile(r"^The player\s+" + PLAYER_RE + r"\s+joined the game with a stack of\s+(?P<amt>\d+)\.?\s*$"),
    "approve": re.compile(r"^The admin approved the player\s+" + PLAYER_RE + r"\s+participation with a stack of\s+(?P<amt>\d+)\.?\s*$"),
    "quit": re.compile(r"^(?:The player\s+)?" + PLAYER_RE + r"\s+quits the game with a stack of\s+(?P<amt>\d+)\.?\s*$"),
    "standup": re.compile(r"^(?:The player\s+)?" + PLAYER_RE + r"\s+stand(?:s)? up with the stack of\s+(?P<amt>\d+)\.?\s*$"),
    "sitback": re.compile(r"^(?:The player\s+)?" + PLAYER_RE + r"\s+sit(?:s)? back with the stack of\s+(?P<amt>\d+)\.?\s*$"),
    "requested_seat": re.compile(r"^(?:The player\s+)?" + PLAYER_RE + r"\s+requested a seat\.?\s*$"),
    "rebuy_request": re.compile(r"^(?:The player\s+)?" + PLAYER_RE + r"\s+requested a rebuy of\s+(?P<amt>\d+)\.?\s*$"),
    "rebuy": re.compile(r"^(?:The player\s+)?" + PLAYER_RE + r"\s+rebought\. New stack\s+(?P<amt>\d+)\.?\s*$"),
    "stack_update": re.compile(r"^The admin updated the player\s+" + PLAYER_RE + r"\s+stack from\s+(?P<from>\d+)\s+to\s+(?P<to>\d+)\.?\s*$"),
    "stack_queue": re.compile(r"^WARNING: the admin queued the stack change for the player\s+" + PLAYER_RE + r"\s+(?:adding|removing)\s+(?P<amt>\d+)\s+chips.*$"),
    "id_change": re.compile(r"^(?:The player\s+)?" + PLAYER_RE + r"\s+changed the ID from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)\b.*$"),
    "admin_remove": re.compile(r"^The admin\s+" + _ANY_PLAYER + r"\s+enqueued the removal of the player\s+" + PLAYER_RE + r"\.?\s*$"),
    "admin_stop": re.compile(r"^The admin\s+" + PLAYER_RE + r"\s+enqueued the game stop on next hand\.?\s*$"),
    "ownership": re.compile(r"^(?:The player\s+)?" + _ANY_PLAYER + r"\s+passed the room ownership to\s+" + PLAYER_RE + r"\.?\s*$"),
    "config": re.compile(r"^Game Config Changes\b", re.DOTALL),
}

_EVENT_KINDS = (
    "join", "approve", "quit", "standup", "sitback", "requested_seat", "rebuy_request", "rebuy",
    "stack_update", "stack_queue", "id_change", "admin_remove", "admin_stop", "ownership", "config",
)

_POST_KIND = {
    "small blind": ActionType.SMALL_BLIND,
    "big blind": ActionType.BIG_BLIND,
    "missing small blind": ActionType.MISSED_SMALL_BLIND,
    "missed small blind": ActionType.MISSED_SMALL_BLIND,
    "missed big blind": ActionType.MISSED_BIG_BLIND,
    "missing big blind": ActionType.MISSED_BIG_BLIND,
    "dead small blind": ActionType.MISSED_SMALL_BLIND,
    "dead big blind": ActionType.MISSED_BIG_BLIND,
    "straddle": ActionType.STRADDLE,
    "ante": ActionType.ANTE,
    "big blind ante": ActionType.ANTE,
}

_SUIT_MAP = {"♠": "s", "♥": "h", "♦": "d", "♣": "c"}
_STREET_BY_NAME = {"flop": Street.FLOP, "turn": Street.TURN, "river": Street.RIVER}


def parse_cards(text: str) -> list[str]:
    """Normalise a card list like ``"Ah, 10d, K♠"`` to ``["Ah", "Td", "Ks"]``."""
    out: list[str] = []
    for tok in re.split(r"[,\s]+", text.strip()):
        tok = tok.strip(" .")
        if not tok:
            continue
        for k, v in _SUIT_MAP.items():
            tok = tok.replace(k, v)
        if tok.startswith("10"):
            tok = "T" + tok[2:]
        if re.fullmatch(r"[2-9TJQKA][shdc]", tok):
            out.append(tok)
    return out


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _pkey(m: re.Match) -> str:
    return Player(m.group("name"), m.group("pid")).key


def _variant_index(variant: str | None) -> int:
    """0 for the main board, 1 for "second board/run", 2 for "third"..."""
    v = (variant or "").lower()
    if not v or "first" in v:
        return 0
    if "third" in v:
        return 2
    return 1


def read_rows(text: str) -> list[tuple[str, datetime | None, int | None]]:
    """Read the CSV and return rows sorted chronologically as (entry, at, order)."""
    reader = csv.reader(io.StringIO(text))
    rows: list[tuple[str, datetime | None, int | None]] = []
    header_seen = False
    for row in reader:
        if not row:
            continue
        if not header_seen and row[0].strip().lower() == "entry":
            header_seen = True
            continue
        entry = row[0]
        at = _parse_ts(row[1]) if len(row) > 1 else None
        order: int | None = None
        if len(row) > 2:
            try:
                order = int(float(row[2]))
            except ValueError:
                order = None
        rows.append((entry, at, order))
    if rows and all(r[2] is not None for r in rows):
        rows.sort(key=lambda r: r[2])  # type: ignore[arg-type]
    elif rows and all(r[1] is not None for r in rows):
        if rows[0][1] > rows[-1][1]:  # type: ignore[operator]
            rows.reverse()
        rows.sort(key=lambda r: r[1])  # type: ignore[arg-type, return-value]
    else:
        rows.reverse()
    return rows


class _HandBuilder:
    """Accumulates lines for a single hand, tracking per-street contributions."""

    def __init__(self, hand: Hand) -> None:
        self.hand = hand
        self.street = Street.PREFLOP
        self.street_put: dict[str, int] = {}

    def _add(self, player: str, amount: int, live: bool = True) -> None:
        self.hand.contributions[player] = self.hand.contributions.get(player, 0) + amount
        if live:
            # dead posts (missed small blind) go to the pot but do not count
            # toward the player's matched wager on the street
            self.street_put[player] = self.street_put.get(player, 0) + amount

    def new_street(self, street: Street) -> None:
        self.street = street
        self.street_put = {}

    def action(self, **kw) -> Action:
        a = Action(street=self.street, **kw)
        self.hand.actions.append(a)
        return a

    def post(self, player: str, kind: ActionType, amt: int, all_in: bool, at, order, raw) -> None:
        self.action(player=player, type=kind, amount=amt, to_amount=amt, all_in=all_in, at=at, order=order, raw=raw)
        if kind is ActionType.SMALL_BLIND and self.hand.small_blind is None:
            self.hand.small_blind = amt
        if kind is ActionType.BIG_BLIND and self.hand.big_blind is None:
            self.hand.big_blind = amt
        if kind is ActionType.BOMB_POT:
            self.hand.bomb_pot = True
        self._add(player, amt, live=kind is not ActionType.MISSED_SMALL_BLIND)

    def wager(self, player: str, kind: ActionType, total: int, all_in: bool, at, order, raw) -> None:
        """call / bet / raise: ``total`` is the player's total on this street."""
        already = self.street_put.get(player, 0)
        delta = max(total - already, 0)
        self.action(player=player, type=kind, amount=delta, to_amount=total, all_in=all_in, at=at, order=order, raw=raw)
        self._add(player, delta)

    def uncalled(self, player: str, amt: int, at, order, raw) -> None:
        self.action(player=player, type=ActionType.UNCALLED_RETURN, amount=amt, at=at, order=order, raw=raw)
        self.hand.contributions[player] = self.hand.contributions.get(player, 0) - amt
        self.street_put[player] = self.street_put.get(player, 0) - amt

    def collect(self, player: str, amt: int, desc: str | None, combo: str | None, at, order, raw) -> None:
        cards = parse_cards(combo) if combo else []
        self.action(player=player, type=ActionType.COLLECT, amount=amt, hand_desc=desc, cards=cards, at=at, order=order, raw=raw)
        self.hand.collected[player] = self.hand.collected.get(player, 0) + amt

    def show(self, player: str, cards: list[str], at, order, raw) -> None:
        self.action(player=player, type=ActionType.SHOW, cards=cards, at=at, order=order, raw=raw)
        if cards:
            # union with earlier partial shows: "shows a Q♠." then "shows a A♠." -> both
            prev_shown = self.hand.shown_cards.get(player, [])
            merged = prev_shown + [c for c in cards if c not in prev_shown]
            self.hand.shown_cards[player] = merged
            prev = self.hand.known_cards.get(player)
            if not prev or len(merged) >= len(prev):
                self.hand.known_cards[player] = merged  # partial shows never downgrade full hole cards


def parse_session(text: str, source_name: str | None = None) -> Session:
    """Parse a full PokerNow CSV export into a :class:`Session`."""
    session = Session(source_name=source_name, source_format="log")
    rows = read_rows(text)
    cur: _HandBuilder | None = None
    last: _HandBuilder | None = None  # most recently ended hand (post-hand shows / rabbit hunts attach here)

    for entry, at, order in rows:
        line = entry.strip()
        if not line:
            continue

        m = _RE["start"].match(line)
        if m:
            if cur is not None:  # unterminated previous hand
                session.hands.append(cur.hand)
            last = None
            dealer = _pkey(m) if m.group("name") else None
            hand = Hand(
                number=int(m.group("num")),
                id=(m.group("hid") or "").strip(),
                game_type=(m.group("game_p") or m.group("game") or "").strip(),
                dealer=dealer,
                started_at=at,
            )
            cur = _HandBuilder(hand)
            continue

        m = _RE["end"].match(line)
        if m:
            if cur is not None:
                cur.hand.ended_at = at
                session.hands.append(cur.hand)
                cur.new_street(Street.SHOWDOWN)
                last, cur = cur, None
            continue

        if cur is not None and _parse_hand_line(cur, line, at, order):
            continue

        if cur is None and last is not None and _parse_post_hand_line(last, line, at, order):
            continue

        if _parse_event(session, line, at, order):
            continue

        if cur is not None:
            cur.hand.unparsed.append(line)
        else:
            session.unparsed.append(line)

    if cur is not None:
        session.hands.append(cur.hand)

    session.hands.sort(key=lambda h: h.number)
    _apply_aliases(session)
    canonicalize_names(session)
    _assign_hero(session)
    return session


def _parse_hand_line(b: _HandBuilder, line: str, at, order) -> bool:
    h = b.hand

    m = _RE["stacks"].match(line)
    if m:
        for sm in _RE["seat"].finditer(m.group("rest")):
            h.seats.append(SeatInfo(seat=int(sm.group("seat")), player=_pkey(sm), stack=int(sm.group("stack"))))
        return True

    m = _RE["hero"].match(line)
    if m:
        h.hero_cards = parse_cards(m.group("cards"))
        return True

    m = _RE["post"].match(line)
    if m:
        kind = _POST_KIND.get(m.group("kind").lower(), ActionType.ANTE)
        b.post(_pkey(m), kind, int(m.group("amt")), bool(m.group("allin")), at, order, line)
        return True

    m = _RE["bomb_post"].match(line)
    if m:
        b.post(_pkey(m), ActionType.BOMB_POT, int(m.group("amt")), bool(m.group("allin")), at, order, line)
        return True

    m = _RE["fold"].match(line)
    if m:
        b.action(player=_pkey(m), type=ActionType.FOLD, at=at, order=order, raw=line)
        return True

    m = _RE["check"].match(line)
    if m:
        b.action(player=_pkey(m), type=ActionType.CHECK, at=at, order=order, raw=line)
        return True

    for key, kind in (("call", ActionType.CALL), ("bet", ActionType.BET), ("raise", ActionType.RAISE)):
        m = _RE[key].match(line)
        if m:
            b.wager(_pkey(m), kind, int(m.group("amt")), bool(m.group("allin")), at, order, line)
            return True

    m = _RE["board"].match(line)
    if m:
        street = _STREET_BY_NAME[m.group("street").lower()]
        new_cards = parse_cards(m.group("new"))
        prev_cards = parse_cards(m.group("prev") or "")
        variant = (m.group("variant") or "").lower()
        idx = _variant_index(variant)
        if "board" in variant:
            h.double_board = True
        elif "run" in variant:
            h.run_it_twice = True
        while len(h.board_runs) <= idx:
            h.board_runs.append([])
        if prev_cards:
            h.board_runs[idx] = prev_cards + new_cards
        elif street is Street.FLOP:
            h.board_runs[idx] = new_cards
        else:
            h.board_runs[idx] = h.board_runs[idx] + new_cards
        if idx == 0:
            b.new_street(street)
            h.board = list(h.board_runs[0])
        return True

    m = _RE["undealt"].match(line)
    if m:
        idx = _variant_index(m.group("variant"))
        while len(h.undealt_cards) <= idx:
            h.undealt_cards.append([])
        h.undealt_cards[idx] = parse_cards(m.group("prev") or "") + parse_cards(m.group("new"))
        return True

    m = _RE["uncalled"].match(line)
    if m:
        b.uncalled(_pkey(m), int(m.group("amt")), at, order, line)
        return True

    m = _RE["collect"].match(line)
    if m:
        b.collect(_pkey(m), int(m.group("amt")), m.group("desc"), m.group("combo"), at, order, line)
        return True

    m = _RE["show"].match(line)
    if m:
        b.show(_pkey(m), parse_cards(m.group("cards")), at, order, line)
        return True

    m = _RE["muck"].match(line)
    if m:
        b.action(player=_pkey(m), type=ActionType.MUCK, at=at, order=order, raw=line)
        return True

    m = _RE["allin_only"].match(line)
    if m:
        amt = int(m.group("amt")) if m.group("amt") else 0
        b.wager(_pkey(m), ActionType.CALL, amt, True, at, order, line)
        return True

    m = _RE["rit_player"].match(line)
    if m:
        h.notes.append(line)
        return True

    if _RE["info"].match(line):
        h.notes.append(line)
        return True

    return False


def _parse_post_hand_line(b: _HandBuilder, line: str, at, order) -> bool:
    """Lines PokerNow emits after ``-- ending hand --`` that still belong to it:
    voluntary card shows and rabbit-hunt ``Undealt cards``."""
    m = _RE["show"].match(line)
    if m:
        b.show(_pkey(m), parse_cards(m.group("cards")), at, order, line)
        return True
    m = _RE["undealt"].match(line)
    if m:
        h = b.hand
        idx = _variant_index(m.group("variant"))
        while len(h.undealt_cards) <= idx:
            h.undealt_cards.append([])
        h.undealt_cards[idx] = parse_cards(m.group("prev") or "") + parse_cards(m.group("new"))
        return True
    return False


def _parse_event(session: Session, line: str, at, order) -> bool:
    if _RE["info"].match(line):
        session.events.append(TableEvent(at=at, order=order, kind="info", player=None, amount=None, raw=line))
        return True
    for kind in _EVENT_KINDS:
        m = _RE[kind].match(line)
        if not m:
            continue
        gd = m.groupdict()
        amount = None
        if gd.get("amt") is not None:
            amount = int(gd["amt"])
        elif gd.get("to") is not None:
            amount = int(gd["to"])
        player = _pkey(m) if gd.get("name") else None
        session.events.append(TableEvent(at=at, order=order, kind=kind, player=player, amount=amount, raw=line))
        if kind == "id_change" and player is not None:
            old_key = Player(m.group("name"), m.group("old")).key
            new_key = Player(m.group("name"), m.group("new")).key
            session.aliases[old_key] = new_key
        return True
    return False


def _split_key(key: str) -> tuple[str, str]:
    name, _, pid = key.rpartition(" @ ")
    return name, pid


def _remap_session(session: Session, canon) -> None:
    """Rewrite every player key in the session through ``canon``."""

    def remap(d: dict) -> dict:
        out: dict = {}
        for k, v in d.items():
            ck = canon(k)
            if isinstance(v, int) and ck in out:
                out[ck] = out[ck] + v
            else:
                out[ck] = v
        return out

    for h in session.hands:
        if h.dealer:
            h.dealer = canon(h.dealer)
        if h.hero:
            h.hero = canon(h.hero)
        for st in h.seats:
            st.player = canon(st.player)
        for a in h.actions:
            a.player = canon(a.player)
        h.contributions = remap(h.contributions)
        h.collected = remap(h.collected)
        h.shown_cards = remap(h.shown_cards)
        h.known_cards = remap(h.known_cards)
    for e in session.events:
        if e.player:
            e.player = canon(e.player)
    if session.hero:
        session.hero = canon(session.hero)


def _apply_aliases(session: Session) -> None:
    """Rewrite player keys so that a player whose ID changed mid-session is one player."""
    if not session.aliases:
        return

    def canon(key: str) -> str:
        seen: set[str] = set()
        while key in session.aliases and key not in seen:
            seen.add(key)
            key = session.aliases[key]
        return key

    _remap_session(session, canon)


def canonicalize_names(session: Session) -> None:
    """Merge the same player ID appearing under different display names.

    PokerNow identifies players by ID; the display name can change between
    sit-downs (the ledger groups by ID too). The canonical name is the one
    from the latest hand the ID appears in; older names become aliases.
    """
    latest: dict[str, str] = {}
    for h in session.hands:  # sorted by number = chronological
        for st in h.seats:
            name, pid = _split_key(st.player)
            if pid:
                latest[pid] = name

    def canon(key: str) -> str:
        name, pid = _split_key(key)
        new = latest.get(pid)
        return f"{new} @ {pid}" if new and new != name else key

    # record renames as aliases (for display as "aka …")
    seen_keys: set[str] = set()
    for h in session.hands:
        for st in h.seats:
            seen_keys.add(st.player)
        for a in h.actions:
            seen_keys.add(a.player)
    for e in session.events:
        if e.player:
            seen_keys.add(e.player)
    changed = {k: canon(k) for k in seen_keys if canon(k) != k}
    if not changed:
        return
    session.aliases.update(changed)
    _remap_session(session, canon)


def _assign_hero(session: Session) -> None:
    """Best-effort: the hero is the player whose shown cards match 'Your hand is'."""
    votes: dict[str, int] = {}
    for h in session.hands:
        if not h.hero_cards:
            continue
        for p, cards in h.shown_cards.items():
            if sorted(cards) == sorted(h.hero_cards) or (len(cards) == 1 and cards[0] in h.hero_cards):
                votes[p] = votes.get(p, 0) + 1
    if votes:
        session.hero = max(votes.items(), key=lambda kv: kv[1])[0]
    for h in session.hands:
        if h.hero_cards:
            h.hero = session.hero
            if session.hero:
                h.known_cards.setdefault(session.hero, h.hero_cards)


def parse_file(path: str) -> Session:
    """Parse a PokerNow export (CSV log or JSON hand export) from disk."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return parse_text(f.read(), source_name=path)


def parse_text(text: str, source_name: str | None = None) -> Session:
    """Parse either export format, sniffing from the content."""
    if text.lstrip()[:1] == "{":
        from .parser_json import parse_json_session

        return parse_json_session(text, source_name=source_name)
    return parse_session(text, source_name=source_name)


def combine_sessions(primary: Session, log: Session | None) -> Session:
    """Merge what only the CSV log knows (table events, ID aliases, hero cards,
    rabbit-hunt cards, notes) into a session parsed from the JSON export."""
    if log is None:
        return primary
    if not primary.hands:
        return log
    primary.events = list(log.events)
    primary.aliases = dict(log.aliases)
    primary.unparsed = list(primary.unparsed) + list(log.unparsed)
    if not primary.hero and log.hero:
        primary.hero = log.hero
    by_num = {h.number: h for h in log.hands}
    for h in primary.hands:
        lh = by_num.get(h.number)
        if lh is None:
            continue
        if not h.hero_cards and lh.hero_cards:
            h.hero_cards = lh.hero_cards
            hero = h.hero or primary.hero
            if hero:
                h.hero = hero
                h.known_cards.setdefault(hero, lh.hero_cards)
        if not h.undealt_cards and lh.undealt_cards:
            h.undealt_cards = lh.undealt_cards
        for n in lh.notes:
            if n not in h.notes:
                h.notes.append(n)
        if lh.unparsed:
            h.unparsed.extend(lh.unparsed)
    return primary


def load_archive(game_dir_or_archive) -> Session:
    """Build the best possible Session from a :class:`~pokernow.fetch.GameArchive`
    (or a directory containing the raw files)."""
    from .fetch import GameArchive

    arch = game_dir_or_archive
    if isinstance(arch, str):
        import os

        arch = GameArchive(os.path.basename(arch.rstrip("/")), os.path.dirname(arch.rstrip("/")))
    files = arch.files()
    js = parse_file(files["hands"]) if "hands" in files else None
    lg = parse_file(files["log"]) if "log" in files else None
    if js is None and lg is None:
        raise FileNotFoundError(f"no hands/log files in {arch.dir}")
    sess = combine_sessions(js, lg) if js is not None else lg
    assert sess is not None
    sess.source_name = f"pokernow:{arch.game_id}"
    return sess
