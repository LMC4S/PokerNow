"""Data model for parsed PokerNow.club hand histories."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


class Street(str, Enum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"


class ActionType(str, Enum):
    SMALL_BLIND = "small_blind"
    BIG_BLIND = "big_blind"
    MISSED_SMALL_BLIND = "missed_small_blind"
    MISSED_BIG_BLIND = "missed_big_blind"
    STRADDLE = "straddle"
    ANTE = "ante"
    BOMB_POT = "bomb_pot"
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"
    RAISE = "raise"
    UNCALLED_RETURN = "uncalled_return"
    COLLECT = "collect"
    SHOW = "show"
    MUCK = "muck"


@dataclass
class Player:
    """A player identity. PokerNow names look like ``"Alice @ abc123"``."""

    name: str
    id: str

    @property
    def key(self) -> str:
        return f"{self.name} @ {self.id}"


@dataclass
class Action:
    street: Street
    player: str  # player key
    type: ActionType
    amount: int | None = None  # chip amount for this action (delta put in on this action)
    to_amount: int | None = None  # for raises: the total "raise to" amount
    all_in: bool = False
    cards: list[str] = field(default_factory=list)  # for SHOW
    hand_desc: str | None = None  # e.g. "Two Pair, A's & 7's"
    at: datetime | None = None
    order: int | None = None
    raw: str = ""


@dataclass
class SeatInfo:
    seat: int
    player: str  # player key
    stack: int


@dataclass
class Hand:
    number: int
    id: str
    game_type: str
    dealer: str | None
    started_at: datetime | None
    ended_at: datetime | None = None
    seats: list[SeatInfo] = field(default_factory=list)
    hero_cards: list[str] = field(default_factory=list)
    board: list[str] = field(default_factory=list)
    board_runs: list[list[str]] = field(default_factory=list)  # for run-it-twice
    actions: list[Action] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    small_blind: int | None = None
    big_blind: int | None = None
    bomb_pot: bool = False
    double_board: bool = False
    run_it_twice: bool = False
    undealt_cards: list[list[str]] = field(default_factory=list)  # rabbit hunt, one list per board
    notes: list[str] = field(default_factory=list)  # recognised informational lines
    known_cards: dict[str, list[str]] = field(default_factory=dict)  # every hole card revealed (hero + shown)
    hero: str | None = None
    hero_net_reported: int | None = None  # from JSON export's playerNet, for cross-checking
    rake: int = 0
    # derived
    contributions: dict[str, int] = field(default_factory=dict)  # chips put in (net of uncalled returns)
    collected: dict[str, int] = field(default_factory=dict)  # chips collected from pot
    shown_cards: dict[str, list[str]] = field(default_factory=dict)

    @property
    def players(self) -> list[str]:
        return [s.player for s in self.seats]

    @property
    def pot(self) -> int:
        return sum(self.contributions.values())

    def net(self, player: str) -> int:
        return self.collected.get(player, 0) - self.contributions.get(player, 0)

    @property
    def winners(self) -> list[str]:
        return [p for p, v in self.collected.items() if v > 0]

    @property
    def chip_mismatch(self) -> int:
        """Difference between chips collected and chips in the pot (0 when accounting balances)."""
        return sum(self.collected.values()) + self.rake - self.pot

    def actions_on(self, street: Street) -> list[Action]:
        return [a for a in self.actions if a.street == street]

    @property
    def survivors(self) -> list[str]:
        folded = {a.player for a in self.actions if a.type is ActionType.FOLD}
        return [p for p in self.players if p not in folded]

    @property
    def went_to_showdown(self) -> bool:
        """Two or more players still in at the end (cards shown or the board ran out)."""
        return len(self.survivors) >= 2 and (bool(self.shown_cards) or len(self.board) == 5)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["ended_at"] = self.ended_at.isoformat() if self.ended_at else None
        for a in d["actions"]:
            a["at"] = a["at"].isoformat() if a["at"] else None
            a["street"] = a["street"].value if isinstance(a["street"], Street) else a["street"]
            a["type"] = a["type"].value if isinstance(a["type"], ActionType) else a["type"]
        d["pot"] = self.pot
        d["winners"] = self.winners
        d["net"] = {p: self.net(p) for p in self.players}
        d["chip_mismatch"] = self.chip_mismatch
        d["went_to_showdown"] = self.went_to_showdown
        return d


@dataclass
class TableEvent:
    """Non-hand events: joins, quits, stack changes, admin messages."""

    at: datetime | None
    order: int | None
    kind: str
    player: str | None
    amount: int | None
    raw: str


@dataclass
class Session:
    hands: list[Hand] = field(default_factory=list)
    events: list[TableEvent] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    source_name: str | None = None
    source_format: str = "log"  # "log" (CSV) or "json" (hand export)
    aliases: dict[str, str] = field(default_factory=dict)  # old player key -> canonical key
    hero: str | None = None

    @property
    def players(self) -> list[str]:
        seen: dict[str, None] = {}
        for h in self.hands:
            for p in h.players:
                seen.setdefault(p, None)
        return list(seen)

    @property
    def started_at(self) -> datetime | None:
        return self.hands[0].started_at if self.hands else None

    @property
    def ended_at(self) -> datetime | None:
        last = self.hands[-1] if self.hands else None
        return (last.ended_at or last.started_at) if last else None
