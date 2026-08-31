"""Player and session statistics computed from parsed hands."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .models import ActionType, Hand, Session, Street

VOLUNTARY = {ActionType.CALL, ActionType.BET, ActionType.RAISE}
AGGRESSIVE = {ActionType.BET, ActionType.RAISE}
DECISIONS = {ActionType.FOLD, ActionType.CHECK, ActionType.CALL, ActionType.BET, ActionType.RAISE}
FORCED_POSTS = {
    ActionType.SMALL_BLIND,
    ActionType.BIG_BLIND,
    ActionType.MISSED_SMALL_BLIND,
    ActionType.MISSED_BIG_BLIND,
    ActionType.STRADDLE,
    ActionType.ANTE,
    ActionType.BOMB_POT,
}


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


@dataclass
class PlayerStats:
    player: str
    name: str
    aka: list[str] = field(default_factory=list)  # earlier display names of the same player ID
    hands: int = 0
    hands_won: int = 0
    net: int = 0
    vpip_hands: int = 0
    pfr_hands: int = 0
    three_bet_opps: int = 0
    three_bet_hands: int = 0
    saw_flop: int = 0
    wtsd: int = 0  # went to showdown (given saw flop)
    wsd: int = 0  # won at showdown
    bets_raises: int = 0
    calls: int = 0
    folds: int = 0
    checks: int = 0
    all_ins: int = 0
    biggest_pot_won: int = 0
    total_won: int = 0  # gross collected
    total_invested: int = 0
    cbet_opps: int = 0
    cbets: int = 0
    fold_to_cbet_opps: int = 0
    fold_to_cbets: int = 0
    hands_with_showdown_cards: int = 0
    first_stack: int | None = None
    last_stack: int | None = None
    hand_numbers: list[int] = field(default_factory=list)
    net_by_hand: list[int] = field(default_factory=list)

    # --- derived ---
    @property
    def vpip(self) -> float | None:
        return _pct(self.vpip_hands, self.hands)

    @property
    def pfr(self) -> float | None:
        return _pct(self.pfr_hands, self.hands)

    @property
    def three_bet(self) -> float | None:
        return _pct(self.three_bet_hands, self.three_bet_opps)

    @property
    def wtsd_pct(self) -> float | None:
        return _pct(self.wtsd, self.saw_flop)

    @property
    def wsd_pct(self) -> float | None:
        return _pct(self.wsd, self.wtsd)

    @property
    def af(self) -> float | None:
        """Aggression factor = (bets + raises) / calls."""
        if self.calls == 0:
            return float("inf") if self.bets_raises else None
        return round(self.bets_raises / self.calls, 2)

    @property
    def cbet_pct(self) -> float | None:
        return _pct(self.cbets, self.cbet_opps)

    @property
    def fold_to_cbet_pct(self) -> float | None:
        return _pct(self.fold_to_cbets, self.fold_to_cbet_opps)

    @property
    def win_rate_bb100(self) -> float | None:
        return None  # filled by SessionStats when big blind known

    def to_dict(self, big_blind: int | None = None) -> dict[str, Any]:
        d = asdict(self)
        af = self.af
        d.update(
            vpip=self.vpip,
            pfr=self.pfr,
            three_bet=self.three_bet,
            wtsd_pct=self.wtsd_pct,
            wsd_pct=self.wsd_pct,
            af=None if af is None else (999.0 if af == float("inf") else af),
            cbet_pct=self.cbet_pct,
            fold_to_cbet_pct=self.fold_to_cbet_pct,
            win_pct=_pct(self.hands_won, self.hands),
            net_bb=round(self.net / big_blind, 1) if big_blind else None,
            bb_per_100=round(100 * self.net / big_blind / self.hands, 1) if big_blind and self.hands else None,
        )
        return d


@dataclass
class SessionStats:
    hands: int
    players: list[PlayerStats]
    started_at: str | None
    ended_at: str | None
    duration_minutes: float | None
    total_pot: int
    avg_pot: float | None
    biggest_pot: int
    biggest_pot_hand: int | None
    small_blind: int | None
    big_blind: int | None
    game_types: list[str]
    showdown_pct: float | None
    flop_pct: float | None
    avg_players_per_hand: float | None
    unparsed_lines: int
    hands_per_hour: float | None = None
    raised_pot_pct: float | None = None  # hands with >=1 preflop raise
    threebet_pot_pct: float | None = None  # hands with >=2 preflop raises
    limp_pot_pct: float | None = None  # hands with no preflop raise

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["players"] = [p.to_dict(self.big_blind) for p in self.players]
        return d


def _short_name(key: str) -> str:
    return key.rsplit(" @ ", 1)[0]


def _most_common(values: list[int]) -> int | None:
    if not values:
        return None
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def compute_hand_stats(hand: Hand, stats: dict[str, PlayerStats]) -> None:
    """Update ``stats`` in place with one hand's worth of data."""
    players = hand.players
    if not players:
        # Fall back to whoever acted if we have no stack line.
        seen: dict[str, None] = {}
        for a in hand.actions:
            seen.setdefault(a.player, None)
        players = list(seen)

    for p in players:
        ps = stats.get(p)
        if ps is None:
            ps = stats[p] = PlayerStats(player=p, name=_short_name(p))
        ps.hands += 1
        ps.hand_numbers.append(hand.number)
        net = hand.net(p)
        ps.net += net
        ps.net_by_hand.append(net)
        ps.total_invested += max(hand.contributions.get(p, 0), 0)
        won = hand.collected.get(p, 0)
        ps.total_won += won
        if won > 0:
            ps.hands_won += 1
            ps.biggest_pot_won = max(ps.biggest_pot_won, hand.pot)
        for s in hand.seats:
            if s.player == p:
                if ps.first_stack is None:
                    ps.first_stack = s.stack
                ps.last_stack = s.stack + net

    # ---- preflop analysis ----
    preflop = hand.actions_on(Street.PREFLOP)
    vpip_players: set[str] = set()
    pfr_players: set[str] = set()
    raises_so_far = 0
    three_bet_opps: set[str] = set()
    three_bettors: set[str] = set()
    last_aggressor: str | None = None
    folded: set[str] = set()

    for a in preflop:
        if a.type in FORCED_POSTS:
            continue
        if a.type is ActionType.FOLD:
            folded.add(a.player)
        if a.type in VOLUNTARY:
            vpip_players.add(a.player)
        if a.type is ActionType.RAISE or a.type is ActionType.BET:
            if raises_so_far == 1:
                three_bet_opps.add(a.player)
                three_bettors.add(a.player)
            raises_so_far += 1
            pfr_players.add(a.player)
            last_aggressor = a.player
        elif raises_so_far == 1 and a.type in {ActionType.CALL, ActionType.FOLD}:
            # Facing a single raise: had a 3-bet opportunity but didn't take it.
            three_bet_opps.add(a.player)

    for p in vpip_players:
        if p in stats:
            stats[p].vpip_hands += 1
    for p in pfr_players:
        if p in stats:
            stats[p].pfr_hands += 1
    for p in three_bet_opps:
        if p in stats:
            stats[p].three_bet_opps += 1
    for p in three_bettors:
        if p in stats:
            stats[p].three_bet_hands += 1

    # ---- action counts / aggression ----
    for a in hand.actions:
        ps = stats.get(a.player)
        if ps is None:
            continue
        if a.type in AGGRESSIVE:
            ps.bets_raises += 1
        elif a.type is ActionType.CALL:
            ps.calls += 1
        elif a.type is ActionType.FOLD:
            ps.folds += 1
        elif a.type is ActionType.CHECK:
            ps.checks += 1
        if a.all_in:
            ps.all_ins += 1

    # ---- flop / showdown ----
    preflop_folded = {a.player for a in preflop if a.type is ActionType.FOLD}
    if hand.board:
        flop_players = [p for p in players if p not in preflop_folded]
        for p in flop_players:
            if p in stats:
                stats[p].saw_flop += 1

        # Continuation bet: the last preflop aggressor has a c-bet opportunity
        # if, when they first act on the flop, nobody has bet yet.
        flop_actions = hand.actions_on(Street.FLOP)
        if last_aggressor and last_aggressor in flop_players and len(flop_players) > 1:
            first_idx = next((i for i, a in enumerate(flop_actions) if a.player == last_aggressor), None)
            if first_idx is not None:
                bet_before = any(a.type in AGGRESSIVE for a in flop_actions[:first_idx])
                if not bet_before and last_aggressor in stats:
                    stats[last_aggressor].cbet_opps += 1
                    cbet_action = flop_actions[first_idx]
                    if cbet_action.type in AGGRESSIVE:
                        stats[last_aggressor].cbets += 1
                        responders: dict[str, ActionType] = {}
                        for a in flop_actions[first_idx + 1 :]:
                            if a.type not in DECISIONS:
                                continue
                            if a.player != last_aggressor and a.player not in responders:
                                responders[a.player] = a.type
                        for p, t in responders.items():
                            if p in stats:
                                stats[p].fold_to_cbet_opps += 1
                                if t is ActionType.FOLD:
                                    stats[p].fold_to_cbets += 1

    # Showdown: players who showed cards or were in a hand where 2+ players
    # reached the end without folding and the river/board completed.
    if hand.went_to_showdown:
        for p in hand.survivors:
            if p in stats:
                stats[p].wtsd += 1
                if hand.collected.get(p, 0) > 0:
                    stats[p].wsd += 1
    for p in hand.shown_cards:
        if p in stats:
            stats[p].hands_with_showdown_cards += 1


def compute_session_stats(session: Session) -> SessionStats:
    stats: dict[str, PlayerStats] = {}
    aka: dict[str, set[str]] = {}
    for old_key, new_key in session.aliases.items():
        old_name = old_key.rsplit(" @ ", 1)[0]
        aka.setdefault(new_key, set()).add(old_name)
    total_pot = 0
    biggest_pot = 0
    biggest_pot_hand: int | None = None
    sbs: list[int] = []
    bbs: list[int] = []
    games: dict[str, None] = {}
    showdowns = 0
    flops = 0
    player_counts: list[int] = []
    limped = raised = threebet = 0

    for h in session.hands:
        compute_hand_stats(h, stats)
        pot = h.pot
        total_pot += pot
        if pot > biggest_pot:
            biggest_pot, biggest_pot_hand = pot, h.number
        if h.small_blind:
            sbs.append(h.small_blind)
        if h.big_blind:
            bbs.append(h.big_blind)
        if h.game_type:
            games.setdefault(h.game_type, None)
        if h.board:
            flops += 1
        if h.went_to_showdown:
            showdowns += 1
        if h.players:
            player_counts.append(len(h.players))
        pf_raises = sum(1 for a in h.actions_on(Street.PREFLOP) if a.type in AGGRESSIVE)
        if pf_raises == 0:
            limped += 1
        elif pf_raises == 1:
            raised += 1
        else:
            threebet += 1

    n = len(session.hands)
    started = session.started_at
    ended = session.ended_at
    duration = None
    if started and ended:
        duration = round((ended - started).total_seconds() / 60.0, 1)

    for key, ps in stats.items():
        ps.aka = sorted(n for n in aka.get(key, ()) if n != ps.name)
    players = sorted(stats.values(), key=lambda p: -p.net)
    return SessionStats(
        hands=n,
        players=players,
        started_at=started.isoformat() if started else None,
        ended_at=ended.isoformat() if ended else None,
        duration_minutes=duration,
        total_pot=total_pot,
        avg_pot=round(total_pot / n, 1) if n else None,
        biggest_pot=biggest_pot,
        biggest_pot_hand=biggest_pot_hand,
        small_blind=_most_common(sbs),
        big_blind=_most_common(bbs),
        game_types=list(games),
        showdown_pct=_pct(showdowns, n),
        flop_pct=_pct(flops, n),
        avg_players_per_hand=round(sum(player_counts) / len(player_counts), 2) if player_counts else None,
        unparsed_lines=len(session.unparsed) + sum(len(h.unparsed) for h in session.hands),
        hands_per_hour=round(n / (duration / 60.0)) if duration else None,
        raised_pot_pct=_pct(raised, n),
        threebet_pot_pct=_pct(threebet, n),
        limp_pot_pct=_pct(limped, n),
    )
