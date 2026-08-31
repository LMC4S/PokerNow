"""Parser for the PokerNow.club JSON hand export
(``poker-now-hands-game-<gameId>.json``).

Structure (handVersion 2)::

    {"generatedAt", "playerId", "gameId",
     "hands": [{"id", "number", "gameType", "smallBlind", "bigBlind", "ante",
                "straddleSeat", "dealerSeat", "startedAt", "bombPot",
                "doubleBoard", "players": [{"id","seat","name","stack","hand"?}],
                "events": [{"at", "payload": {"type": N, ...}}],
                "playerNet"}]}

Event payload types (official enum extracted from PokerNow's replayer bundle,
cross-checked against the CSV log of the same game)::

    0  CHECK                  {seat}
    1  BET                    {seat, value, bombPot?}   (bomb-pot post when bombPot)
    2  BIG_BLIND              {seat, value}
    3  SMALL_BLIND            {seat, value}
    4  MISSED_BIG_BLIND       {seat, value}
    5  MISSED_SMALL_BLIND     {seat, value}
    6  STRADDLE               {seat, value}
    7  CALL                   {seat, value(total on street), allIn?, bombPot?}
    8  RAISE                  {seat, value(total on street), allIn?}
    9  GAME_TURN (board)      {turn: 1 flop / 2 turn / 3 river, run, cards, handsLabels}
    10 POT_PRIZE (collect)    {seat, value, pot, position, cards?, combination?, handDescription?, runNumber?, hiLo?}
    11 FOLD                   {seat}
    12 SHOW_CARDS             {seat, cards}
    13 ANTE                   {seat, value}
    14 RUN_IT_TWICE_DECISION  {approved, autoApproved, approvedSeats, deniedSeats}
    15 HAND_FINISHED
    16 UNCALLED_BET           {seat, value}
    17 HAND_STARTED
    18 SEVEN_DEUCE_BOUNTY
    19 RAKE_VALUE             {value}
    20 NIT_CLEARED_LEAVE, 21 NIT_PENALTY, 22 NIT_CYCLE_STARTED

Note: PokerNow emits a min-bet with nothing in front of the bettor as CALL (7),
so bet/call/raise are classified here by amount, not by type.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import Action, ActionType, Hand, Player, SeatInfo, Session, Street

_GAME_TYPES = {  # from the replayer bundle
    "th": "No Limit Texas Hold'em",
    "omaha": "Pot Limit Omaha Hi",
    "plo8": "Pot Limit Omaha Hi/Lo (8 or Better)",
    "plo5": "Pot Limit Omaha 5 Hi",
    "plo5hl": "Pot Limit Omaha 5 Hi/Lo (8 or Better)",
}

_POST_TYPES = {
    2: ActionType.BIG_BLIND,
    3: ActionType.SMALL_BLIND,
    4: ActionType.MISSED_BIG_BLIND,
    5: ActionType.MISSED_SMALL_BLIND,
    6: ActionType.STRADDLE,
    13: ActionType.ANTE,
}
_WAGER_TYPES = {1, 7, 8}  # BET, CALL, RAISE -- classified by amount
_NOTE_TYPES = {14: "run it twice", 18: "seven-deuce bounty", 20: "nit cleared leave", 21: "nit penalty", 22: "nit cycle started"}

_STREET_BY_TURN = {1: Street.FLOP, 2: Street.TURN, 3: Street.RIVER}


def _ts(ms) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _norm_cards(cards) -> list[str]:
    out = []
    for c in cards or []:
        if c is None:
            continue
        c = str(c)
        if c.startswith("10"):
            c = "T" + c[2:]
        out.append(c)
    return out


def parse_json_session(text: str, source_name: str | None = None) -> Session:
    data = json.loads(text)
    session = Session(source_name=source_name, source_format="json")
    hero_id = data.get("playerId") or _infer_hero_id(data.get("hands", []))

    for jh in data.get("hands", []):
        hand = _parse_hand(jh, hero_id)
        session.hands.append(hand)

    session.hands.sort(key=lambda h: h.number)
    from .parser import canonicalize_names

    canonicalize_names(session)
    heroes = {h.hero for h in session.hands if h.hero}
    if len(heroes) == 1:
        session.hero = heroes.pop()
    elif heroes:
        # Name may change; pick the most frequent.
        counts: dict[str, int] = {}
        for h in session.hands:
            if h.hero:
                counts[h.hero] = counts.get(h.hero, 0) + 1
        session.hero = max(counts.items(), key=lambda kv: kv[1])[0]
    return session


def _infer_hero_id(hands: list) -> str | None:
    """The exporter's own hole cards are present even in hands they never showed;
    use that to identify the hero when the export lacks ``playerId``."""
    votes: dict[str, int] = {}
    for jh in hands:
        shown_seats = {e.get("payload", {}).get("seat") for e in jh.get("events", []) if e.get("payload", {}).get("type") == 12}
        for p in jh.get("players", []):
            if p.get("hand") and p.get("seat") not in shown_seats:
                votes[str(p.get("id"))] = votes.get(str(p.get("id")), 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def _parse_hand(jh: dict, hero_id: str | None) -> Hand:
    seat_to_key: dict[int, str] = {}
    seats: list[SeatInfo] = []
    known: dict[str, list[str]] = {}
    for p in jh.get("players", []):
        key = Player(str(p.get("name", "?")), str(p.get("id", "?"))).key
        seat = int(p.get("seat", 0))
        seat_to_key[seat] = key
        seats.append(SeatInfo(seat=seat, player=key, stack=int(p.get("stack") or 0)))
        if p.get("hand"):
            known[key] = _norm_cards(p["hand"])
    seats.sort(key=lambda s: s.seat)

    dealer_seat = jh.get("dealerSeat")
    dealer = seat_to_key.get(int(dealer_seat)) if dealer_seat is not None else None
    gt = str(jh.get("gameType") or "")
    hand = Hand(
        number=int(jh.get("number") or 0),
        id=str(jh.get("id") or ""),
        game_type=_GAME_TYPES.get(gt.lower(), gt),
        dealer=dealer,
        started_at=_ts(jh.get("startedAt")),
        seats=seats,
        small_blind=jh.get("smallBlind"),
        big_blind=jh.get("bigBlind"),
        bomb_pot=bool(jh.get("bombPot")),
        double_board=bool(jh.get("doubleBoard")),
        known_cards=known,
        hero_net_reported=jh.get("playerNet"),
    )
    hero_key = None
    if hero_id is not None:
        for p in jh.get("players", []):
            if str(p.get("id")) == str(hero_id):
                hero_key = Player(str(p.get("name", "?")), str(p.get("id"))).key
                break
    hand.hero = hero_key
    if hero_key and hero_key in known:
        hand.hero_cards = known[hero_key]

    street = Street.PREFLOP
    street_put: dict[str, int] = {}
    show_slots: dict[str, list] = {}  # positional card slots per player, filled across partial shows
    last_at = None

    def add(player: str, amount: int, live: bool = True) -> None:
        hand.contributions[player] = hand.contributions.get(player, 0) + amount
        if live:
            street_put[player] = street_put.get(player, 0) + amount

    def act(**kw) -> Action:
        a = Action(street=street, **kw)
        hand.actions.append(a)
        return a

    for idx, ev in enumerate(jh.get("events", [])):
        pl = ev.get("payload") or {}
        t = pl.get("type")
        at = _ts(ev.get("at"))
        last_at = at or last_at
        seat = pl.get("seat")
        player = seat_to_key.get(int(seat), f"seat {seat}") if seat is not None else None
        raw = json.dumps(pl, ensure_ascii=False)

        if t in _POST_TYPES and player:
            kind = _POST_TYPES[t]
            amt = int(pl.get("value") or 0)
            act(player=player, type=kind, amount=amt, to_amount=amt, all_in=bool(pl.get("allIn")), at=at, order=idx, raw=raw)
            if kind is ActionType.BOMB_POT:
                hand.bomb_pot = True
            # MISSED_SMALL_BLIND (5) is a dead post: in the pot, not part of the live wager
            add(player, amt, live=kind is not ActionType.MISSED_SMALL_BLIND)
        elif t == 0 and player:
            act(player=player, type=ActionType.CHECK, at=at, order=idx, raw=raw)
        elif t == 11 and player:
            act(player=player, type=ActionType.FOLD, at=at, order=idx, raw=raw)
        elif t in _WAGER_TYPES and player:
            total = int(pl.get("value") or 0)
            already = street_put.get(player, 0)
            high = max(street_put.values(), default=0)
            if pl.get("bombPot"):
                kind = ActionType.BOMB_POT
                hand.bomb_pot = True
            elif total > high:
                # PokerNow encodes a min-bet as type 7 ("call") when nothing is
                # in front of the player, so classify by amount, not by type.
                kind = ActionType.BET if high == 0 else ActionType.RAISE
            else:
                kind = ActionType.CALL
            delta = max(total - already, 0)
            act(player=player, type=kind, amount=delta, to_amount=total, all_in=bool(pl.get("allIn")), at=at, order=idx, raw=raw)
            add(player, delta)
        elif t == 9:
            turn = int(pl.get("turn") or 0)
            run = int(pl.get("run") or 1)
            cards = _norm_cards(pl.get("cards"))
            ridx = max(run - 1, 0)
            while len(hand.board_runs) <= ridx:
                hand.board_runs.append([])
            if turn == 1:
                hand.board_runs[ridx] = cards
            else:
                hand.board_runs[ridx] = hand.board_runs[ridx] + cards
            if run > 1 and not hand.double_board:
                hand.run_it_twice = True
            if ridx == 0:
                new_street = _STREET_BY_TURN.get(turn, street)
                if new_street is not street:
                    street = new_street
                    street_put = {}
                hand.board = list(hand.board_runs[0])
        elif t == 16 and player:
            amt = int(pl.get("value") or 0)
            act(player=player, type=ActionType.UNCALLED_RETURN, amount=amt, at=at, order=idx, raw=raw)
            hand.contributions[player] = hand.contributions.get(player, 0) - amt
            street_put[player] = street_put.get(player, 0) - amt
        elif t == 10 and player:
            amt = int(pl.get("value") or 0)
            act(
                player=player,
                type=ActionType.COLLECT,
                amount=amt,
                hand_desc=pl.get("handDescription"),
                cards=_norm_cards(pl.get("combination")),
                at=at,
                order=idx,
                raw=raw,
            )
            hand.collected[player] = hand.collected.get(player, 0) + amt
            if pl.get("cards"):
                hand.known_cards.setdefault(player, _norm_cards(pl["cards"]))
        elif t == 12 and player:
            raw_cards = pl.get("cards") or []
            cards = _norm_cards(raw_cards)  # what was revealed in THIS event
            act(player=player, type=ActionType.SHOW, cards=cards, at=at, order=idx, raw=raw)
            if cards:
                # merge positionally: "shows Q♠" then "shows A♠" -> both cards
                slots = show_slots.setdefault(player, [])
                while len(slots) < len(raw_cards):
                    slots.append(None)
                for ci, c in enumerate(raw_cards):
                    if c is not None:
                        slots[ci] = c
                consolidated = _norm_cards(slots)
                hand.shown_cards[player] = consolidated
                prev = hand.known_cards.get(player)
                if not prev or len(consolidated) >= len(prev):
                    hand.known_cards[player] = consolidated  # partial shows never downgrade full hole cards
        elif t == 19:
            hand.rake = int(pl.get("value") or 0)
        elif t in _NOTE_TYPES:
            hand.notes.append(f"{_NOTE_TYPES[t]}: {raw}")
            if t == 14 and pl.get("approved"):
                hand.run_it_twice = True
        elif t == 15:
            hand.ended_at = at
            street = Street.SHOWDOWN  # later shows are post-hand, like the CSV log
        elif t == 17:
            pass
        else:
            hand.unparsed.append(raw)

    if hand.ended_at is None:
        hand.ended_at = last_at
    return hand
