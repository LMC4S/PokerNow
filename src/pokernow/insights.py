"""Advanced deterministic session insights.

Everything here is computable without judgement calls, so it can sit in the
tracker and refresh live:

- **Hero card quality** — were the hole cards you were dealt statistically
  better than random? (Needs your hole cards, i.e. a logged-in fetch.)
- **Hit rate & set frequency** — how often you connected with the flop vs the
  per-hand theoretical probability.
- **Hand-group performance** — net results for the three groups worth tracking
  at small samples (strong hands, small/mid pairs, two broadway cards), plus a
  pure in-position / out-of-position split.
- **Run-good meter** — for every player (from showdown-revealed cards): card
  luck decomposed street by street (equity swing of each reveal × the pot at
  that moment), plus "setup" counts (big pots lost holding a real hand).
- **Money flow** — who is paying whom, attributed per hand by pot share.

Equity numbers come from exact runout enumeration postflop and a fixed-seed
Monte Carlo preflop, so results are reproducible run to run.
"""

from __future__ import annotations

import random
from itertools import combinations
from math import comb, erf, sqrt
from typing import Any

from .models import ActionType, Hand, Session, Street

VOLUNTARY = {ActionType.CALL, ActionType.BET, ActionType.RAISE}

# ---------------------------------------------------------------------------
# Card handling. Cards are ints 0..51: rank = card >> 2 (0=deuce .. 12=ace),
# suit = card & 3.
# ---------------------------------------------------------------------------

RANKS = "23456789TJQKA"
_RANK_IDX = {r: i for i, r in enumerate(RANKS)}
_SUIT_IDX = {"s": 0, "h": 1, "d": 2, "c": 3}


def card_int(card: str) -> int | None:
    if not card or len(card) != 2:
        return None
    r = _RANK_IDX.get(card[0].upper())
    s = _SUIT_IDX.get(card[1].lower())
    if r is None or s is None:
        return None
    return r * 4 + s


def cards_int(cards: list[str] | None) -> list[int] | None:
    """Both hole cards as ints, or None when the hand isn't fully known."""
    out = [c for c in (card_int(c) for c in (cards or [])) if c is not None]
    return out if len(out) == 2 else None


def hand_class(cards: list[int]) -> str:
    """'AKs' / 'T9o' / 'QQ' for two int-cards."""
    a, b = sorted(cards, key=lambda c: -(c >> 2))
    r1, r2 = RANKS[a >> 2], RANKS[b >> 2]
    if r1 == r2:
        return r1 + r2
    return r1 + r2 + ("s" if (a & 3) == (b & 3) else "o")


def class_combos(cls: str) -> int:
    return 6 if len(cls) == 2 else (4 if cls[2] == "s" else 12)


def eval7(cards: list[int]) -> tuple:
    """Best 5-card rank tuple from 5-7 int-cards. Higher tuple = better."""
    rank_cnt = [0] * 13
    suit_cnt = [0] * 4
    suit_ranks = [0, 0, 0, 0]
    rank_mask = 0
    for c in cards:
        r = c >> 2
        s = c & 3
        rank_cnt[r] += 1
        suit_cnt[s] += 1
        suit_ranks[s] |= 1 << r
        rank_mask |= 1 << r

    def straight_high(mask: int) -> int:
        for hi in range(12, 3, -1):
            need = 0b11111 << (hi - 4)
            if mask & need == need:
                return hi
        if mask & 0b1111 == 0b1111 and mask >> 12 & 1:  # wheel
            return 3
        return -1

    for s in range(4):
        if suit_cnt[s] >= 5:
            sh = straight_high(suit_ranks[s])
            if sh >= 0:
                return (8, sh)
            top = []
            for r in range(12, -1, -1):
                if suit_ranks[s] >> r & 1:
                    top.append(r)
                    if len(top) == 5:
                        break
            return (5, *top)

    quads = trips = -1
    pairs: list[int] = []
    for r in range(12, -1, -1):
        c = rank_cnt[r]
        if c == 4:
            quads = r
        elif c == 3:
            if trips < 0:
                trips = r
            else:
                pairs.append(r)
        elif c == 2:
            pairs.append(r)
    if quads >= 0:
        kick = max(r for r in range(13) if rank_cnt[r] and r != quads)
        return (7, quads, kick)
    if trips >= 0 and pairs:
        return (6, trips, pairs[0])
    sh = straight_high(rank_mask)
    if sh >= 0:
        return (4, sh)
    singles = [r for r in range(12, -1, -1) if rank_cnt[r] == 1]
    if trips >= 0:
        return (3, trips, singles[0], singles[1])
    if len(pairs) >= 2:
        kick = max(r for r in range(12, -1, -1) if rank_cnt[r] and r not in pairs[:2])
        return (2, pairs[0], pairs[1], kick)
    if len(pairs) == 1:
        return (1, pairs[0], singles[0], singles[1], singles[2])
    return (0, *singles[:5])


def equities(holes: list[list[int]], board: list[int], preflop_iters: int = 2000) -> list[float]:
    """Equity (win + tie share) per hand. Exact enumeration once a flop is out;
    fixed-seed Monte Carlo preflop so results are reproducible."""
    dead = set(board)
    for h in holes:
        dead.update(h)
    deck = [c for c in range(52) if c not in dead]
    need = 5 - len(board)
    eq = [0.0] * len(holes)
    if need <= 0:
        runs: Any = [()]
    elif need <= 2:
        runs = combinations(deck, need)
    else:
        rng = random.Random(0xC0FFEE)
        runs = (tuple(rng.sample(deck, need)) for _ in range(preflop_iters))
    total = 0
    for run in runs:
        total += 1
        full = board + list(run)
        scores = [eval7(h + full) for h in holes]
        best = max(scores)
        winners = [i for i, s in enumerate(scores) if s == best]
        for i in winners:
            eq[i] += 1.0 / len(winners)
    return [e / total for e in eq]


# ---------------------------------------------------------------------------
# Preflop equity vs one random hand, per starting-hand class. Generated with
# this module's eval7, 20,000 fixed-seed Monte Carlo boards per class
# (combo-weighted mean 0.50006). Regenerate with scripts in the repo history
# if the evaluator ever changes.
# ---------------------------------------------------------------------------

PRE_EQ = {
    "AA": 0.845, "AKs": 0.6665, "AKo": 0.6515, "AQs": 0.661, "AQo": 0.6411, "AJs": 0.6545, "AJo": 0.6364, "ATs": 0.6438,
    "ATo": 0.6251, "A9s": 0.6324, "A9o": 0.6091, "A8s": 0.6188, "A8o": 0.6017, "A7s": 0.6096, "A7o": 0.5872, "A6s": 0.5974,
    "A6o": 0.5776, "A5s": 0.6022, "A5o": 0.5778, "A4s": 0.5935, "A4o": 0.5698, "A3s": 0.5821, "A3o": 0.5612, "A2s": 0.5794,
    "A2o": 0.5485, "KK": 0.8218, "KQs": 0.6322, "KQo": 0.6216, "KJs": 0.6196, "KJo": 0.6077, "KTs": 0.6226, "KTo": 0.5949,
    "K9s": 0.5965, "K9o": 0.5799, "K8s": 0.5847, "K8o": 0.5647, "K7s": 0.5737, "K7o": 0.5505, "K6s": 0.5636, "K6o": 0.5358,
    "K5s": 0.5589, "K5o": 0.5309, "K4s": 0.5522, "K4o": 0.5209, "K3s": 0.5506, "K3o": 0.5178, "K2s": 0.5421, "K2o": 0.5,
    "QQ": 0.7974, "QJs": 0.6038, "QJo": 0.5777, "QTs": 0.5949, "QTo": 0.5751, "Q9s": 0.5716, "Q9o": 0.5536, "Q8s": 0.5602,
    "Q8o": 0.5341, "Q7s": 0.542, "Q7o": 0.5179, "Q6s": 0.5361, "Q6o": 0.512, "Q5s": 0.5329, "Q5o": 0.5012, "Q4s": 0.5224,
    "Q4o": 0.4911, "Q3s": 0.5081, "Q3o": 0.481, "Q2s": 0.5043, "Q2o": 0.4757, "JJ": 0.7774, "JTs": 0.5774, "JTo": 0.5527,
    "J9s": 0.5565, "J9o": 0.5292, "J8s": 0.5401, "J8o": 0.5161, "J7s": 0.5241, "J7o": 0.4957, "J6s": 0.5056, "J6o": 0.4746,
    "J5s": 0.504, "J5o": 0.4722, "J4s": 0.4919, "J4o": 0.4595, "J3s": 0.4818, "J3o": 0.4541, "J2s": 0.4762, "J2o": 0.4528,
    "TT": 0.7514, "T9s": 0.5391, "T9o": 0.5108, "T8s": 0.5256, "T8o": 0.4978, "T7s": 0.5073, "T7o": 0.4771, "T6s": 0.4903,
    "T6o": 0.4678, "T5s": 0.4761, "T5o": 0.446, "T4s": 0.4638, "T4o": 0.434, "T3s": 0.4563, "T3o": 0.4252, "T2s": 0.4504,
    "T2o": 0.4112, "99": 0.7146, "98s": 0.5139, "98o": 0.477, "97s": 0.4853, "97o": 0.4657, "96s": 0.4717, "96o": 0.4485,
    "95s": 0.453, "95o": 0.4255, "94s": 0.4387, "94o": 0.4097, "93s": 0.4294, "93o": 0.4028, "92s": 0.4234, "92o": 0.3911,
    "88": 0.6904, "87s": 0.4818, "87o": 0.4435, "86s": 0.4652, "86o": 0.4324, "85s": 0.446, "85o": 0.4182, "84s": 0.4262,
    "84o": 0.4037, "83s": 0.4093, "83o": 0.378, "82s": 0.3992, "82o": 0.3675, "77": 0.6633, "76s": 0.4554, "76o": 0.422,
    "75s": 0.4375, "75o": 0.4027, "74s": 0.4179, "74o": 0.3858, "73s": 0.4077, "73o": 0.3701, "72s": 0.3797, "72o": 0.3463,
    "66": 0.6296, "65s": 0.4296, "65o": 0.3997, "64s": 0.4085, "64o": 0.3765, "63s": 0.3988, "63o": 0.3594, "62s": 0.376,
    "62o": 0.334, "55": 0.6094, "54s": 0.4129, "54o": 0.3845, "53s": 0.397, "53o": 0.3641, "52s": 0.3716, "52o": 0.3438,
    "44": 0.5694, "43s": 0.3861, "43o": 0.3517, "42s": 0.3632, "42o": 0.3263, "33": 0.5373, "32s": 0.3547, "32o": 0.3208,
    "22": 0.5072,
}

_TOTAL_COMBOS = 1326
NULL_MEAN = sum(v * class_combos(k) for k, v in PRE_EQ.items()) / _TOTAL_COMBOS
NULL_SD = sqrt(sum(class_combos(k) * (v - NULL_MEAN) ** 2 for k, v in PRE_EQ.items()) / _TOTAL_COMBOS)

# theoretical flop probabilities
P_PAIR_UNPAIRED = 1 - comb(44, 3) / comb(50, 3)  # unpaired hand pairs (or better) on the flop
P_SET_FLOP = 1 - comb(48, 3) / comb(50, 3)  # pocket pair flops a set (or better)

# category tests for dealt-card quality: name -> (membership fn, probability)
_CATEGORIES: list[tuple[str, Any, float]] = [
    ("AA", lambda c: c == "AA", 6 / 1326),
    ("QQ+", lambda c: c in ("AA", "KK", "QQ"), 18 / 1326),
    ("TT+ / AK / AQ", lambda c: c in ("AA", "KK", "QQ", "JJ", "TT", "AKs", "AKo", "AQs", "AQo"), 62 / 1326),
    ("Pocket pair", lambda c: len(c) == 2, 78 / 1326),
    ("Ace-high (any Ax)", lambda c: c[0] == "A", 198 / 1326),
    ("Suited", lambda c: len(c) == 3 and c[2] == "s", 312 / 1326),
]

STRONG_GROUP = {"AA", "KK", "QQ", "JJ", "TT", "AKs", "AKo", "AQs"}


def hand_group(cls: str) -> str | None:
    """The three groups worth tracking at session-size samples; None = untracked."""
    if cls in STRONG_GROUP:
        return "strong"
    if len(cls) == 2:
        return "pairs_small"  # 22-99 (TT+ went to strong)
    if _RANK_IDX[cls[0]] >= _RANK_IDX["T"] and _RANK_IDX[cls[1]] >= _RANK_IDX["T"]:
        return "broadway"  # both cards T or higher (AQo, KQ, QJ, JTs, ...)
    return None


# ---------------------------------------------------------------------------
# small stats helpers
# ---------------------------------------------------------------------------

def _normal_p(z: float) -> float:
    """Two-sided p-value under the normal approximation."""
    return round(2 * (1 - 0.5 * (1 + erf(abs(z) / sqrt(2)))), 4)


def _binom_two_sided(k: int, n: int, p: float) -> float:
    """Exact two-sided binomial p-value (sum of outcomes no more likely than k)."""
    if n == 0:
        return 1.0
    pk = comb(n, k) * p**k * (1 - p) ** (n - k)
    tot = 0.0
    for i in range(n + 1):
        pi = comb(n, i) * p**i * (1 - p) ** (n - i)
        if pi <= pk * 1.0000001:
            tot += pi
    return round(min(tot, 1.0), 4)


# ---------------------------------------------------------------------------
# per-hand pieces
# ---------------------------------------------------------------------------

def _voluntary_preflop(hand: Hand, player: str) -> bool:
    return any(
        a.player == player and a.type in VOLUNTARY and a.street is Street.PREFLOP
        for a in hand.actions
    )


def _folded_preflop(hand: Hand, player: str) -> bool:
    return any(
        a.player == player and a.type is ActionType.FOLD and a.street is Street.PREFLOP
        for a in hand.actions
    )


def _postflop_order(hand: Hand) -> list[str]:
    """Players in postflop acting order (first-to-act first)."""
    seats = sorted(hand.seats, key=lambda s: s.seat)
    keys = [s.player for s in seats]
    if hand.dealer in keys:
        di = keys.index(hand.dealer)
        keys = keys[di + 1 :] + keys[: di + 1]
    return keys


_MONEY_IN = VOLUNTARY | {
    ActionType.SMALL_BLIND,
    ActionType.BIG_BLIND,
    ActionType.MISSED_SMALL_BLIND,
    ActionType.MISSED_BIG_BLIND,
    ActionType.STRADDLE,
    ActionType.ANTE,
    ActionType.BOMB_POT,
}

_STREET_BOARD_LEN = {Street.PREFLOP: 0, Street.FLOP: 3, Street.TURN: 4, Street.RIVER: 5}


def _commit_street(hand: Hand, player: str) -> Street:
    """The street where the player put in the most chips (ties go later)."""
    puts = {st: 0 for st in _STREET_BOARD_LEN}
    for a in hand.actions:
        if a.player == player and a.street in puts and a.type in _MONEY_IN and a.amount:
            puts[a.street] += a.amount
    best = Street.PREFLOP
    for st in (Street.FLOP, Street.TURN, Street.RIVER):
        if puts[st] >= puts[best]:
            best = st
    return best


def _made_hand(holes: list[int], prefix: list[int]) -> bool:
    """Did the player hold a legitimate hand on this board — top pair or
    better (preflop: JJ+ or AK)? Used to tell 'run over by a setup' apart
    from putting money in with nothing."""
    r1, r2 = holes[0] >> 2, holes[1] >> 2
    pocket = r1 == r2
    if not prefix:
        return (pocket and r1 >= _RANK_IDX["J"]) or {r1, r2} == {_RANK_IDX["A"], _RANK_IDX["K"]}
    board_ranks = {c >> 2 for c in prefix}
    top = max(board_ranks)
    if pocket and r1 > top:
        return True  # overpair
    if top in (r1, r2):
        return True  # top pair
    score = eval7(holes + prefix)
    if score[0] >= 4:
        return True  # straight or better
    return score[0] >= 2 and (pocket or bool({r1, r2} & board_ranks))  # two pair / trips using a hole card


def _hand_luck(hand: Hand) -> dict[str, dict[str, Any]] | None:
    """Per-player card luck for a showdown hand, decomposed street by street.

    For each card reveal (flop, turn, river) the luck contribution is
    ``(equity after the card − equity before it) × pot at that moment`` — only
    the money already in the middle rides on the card. Hitting a miracle river
    after calling a tiny bet therefore counts as a little luck; getting the
    same river with stacks already in counts as a lot. Chips won or lost in
    betting *after* a card falls are decisions, not luck, and are never
    attributed here.

    Measured between the showdown survivors whose hole cards are known.
    Returns None when the hand can't be measured (fewer than 2 known
    survivors, no full board, or multi-run boards)."""
    if not hand.went_to_showdown or len(hand.board) < 5:
        return None
    if len(hand.board_runs) > 1 or hand.double_board:
        return None
    survivors = hand.survivors
    known: dict[str, list[int]] = {}
    seen: set[int] = set()
    for p in survivors:
        ints = cards_int(hand.known_cards.get(p))
        if ints and not (set(ints) & seen):
            known[p] = ints
            seen.update(ints)
    if len(known) < 2:
        return None
    board = [c for c in (card_int(c) for c in hand.board) if c is not None]
    if len(board) < 5:
        return None
    if any(c in board for cs in known.values() for c in cs):
        return None  # corrupted data: a hole card also appears on the board
    # pot as it stood when each street's cards were revealed
    put_by_street = {Street.PREFLOP: 0, Street.FLOP: 0, Street.TURN: 0, Street.RIVER: 0}
    for a in hand.actions:
        if a.street not in put_by_street:
            continue
        if a.type in _MONEY_IN and a.amount:
            put_by_street[a.street] += a.amount
        elif a.type is ActionType.UNCALLED_RETURN and a.amount:
            put_by_street[a.street] -= a.amount
    pot_at_flop = put_by_street[Street.PREFLOP]
    pot_at_turn = pot_at_flop + put_by_street[Street.FLOP]
    pot_at_river = pot_at_turn + put_by_street[Street.TURN]
    if pot_at_flop <= 0:
        return None
    players = list(known)
    holes = [known[p] for p in players]
    eq_pre = equities(holes, [])
    eq_flop = equities(holes, board[:3])
    eq_turn = equities(holes, board[:4])
    eq_river = equities(holes, board[:5])  # resolves to the actual result
    out: dict[str, dict[str, Any]] = {}
    for i, p in enumerate(players):
        luck = (
            (eq_flop[i] - eq_pre[i]) * pot_at_flop
            + (eq_turn[i] - eq_flop[i]) * pot_at_turn
            + (eq_river[i] - eq_turn[i]) * pot_at_river
        )
        commit = _commit_street(hand, p)
        out[p] = {
            "luck": luck,
            "won": eq_river[i] > 0.5,
            # a real hand (top pair+, preflop JJ+/AK) on the street where
            # their money went in — the raw material of a "setup" loss
            "commit_hand": _made_hand(known[p], board[: _STREET_BOARD_LEN[commit]]),
        }
    return out


_LUCK_CACHE: dict[str, dict[str, dict[str, Any]] | None] = {}


def _hand_luck_cached(hand: Hand) -> dict[str, dict[str, Any]] | None:
    key = f"{hand.id}|{''.join(hand.board)}|{sorted(hand.known_cards.items())!r}"
    if key not in _LUCK_CACHE:
        if len(_LUCK_CACHE) > 20000:
            _LUCK_CACHE.clear()
        _LUCK_CACHE[key] = _hand_luck(hand)
    return _LUCK_CACHE[key]


# ---------------------------------------------------------------------------
# the main entry point
# ---------------------------------------------------------------------------

def compute_insights(session: Session, big_blind: int | None = None) -> dict[str, Any]:
    hands = session.hands
    hero = session.hero
    bb = big_blind or 0

    def in_bb(chips: float) -> float | None:
        return round(chips / bb, 1) if bb else None

    # ---------------- hero card quality ----------------
    hero_cards_by_hand: list[tuple[Hand, list[int]]] = []
    hero_dealt = 0
    if hero:
        for h in hands:
            if hero not in h.players:
                continue
            hero_dealt += 1
            ints = cards_int(h.known_cards.get(hero) or (h.hero_cards if h.hero == hero else None))
            if ints:
                hero_cards_by_hand.append((h, ints))

    quality: dict[str, Any] | None = None
    flop_hit: dict[str, Any] | None = None
    sets: dict[str, Any] | None = None
    groups: dict[str, Any] | None = None
    flop_str: dict[str, Any] | None = None
    position: dict[str, Any] | None = None

    if hero and hero_cards_by_hand:
        classes = [hand_class(c) for _, c in hero_cards_by_hand]
        eqs = [PRE_EQ[c] for c in classes]
        n = len(eqs)
        mean = sum(eqs) / n
        z = (mean - NULL_MEAN) / (NULL_SD / sqrt(n))
        played = [PRE_EQ[hand_class(c)] for h, c in hero_cards_by_hand if _voluntary_preflop(h, hero)]
        folded = [PRE_EQ[hand_class(c)] for h, c in hero_cards_by_hand if not _voluntary_preflop(h, hero)]
        counts: dict[str, int] = {}
        for c in classes:
            counts[c] = counts.get(c, 0) + 1
        quality = {
            "n": n,
            "mean_eq": round(mean, 4),
            "null_mean": round(NULL_MEAN, 4),
            "null_sd": round(NULL_SD, 4),
            "z": round(z, 2),
            "p": _normal_p(z),
            "categories": [
                {
                    "name": name,
                    "observed": sum(1 for c in classes if fn(c)),
                    "expected": round(n * p, 1),
                    "p": _binom_two_sided(sum(1 for c in classes if fn(c)), n, p),
                }
                for name, fn, p in _CATEGORIES
            ],
            "top_classes": sorted(counts.items(), key=lambda kv: -kv[1])[:6],
            "played_mean_eq": round(sum(played) / len(played), 4) if played else None,
            "folded_mean_eq": round(sum(folded) / len(folded), 4) if folded else None,
        }

        # flop hit rate (per-hand theoretical probability -> z-test)
        hit_k = 0
        mu = var = 0.0
        n_flop = 0
        pp_dealt = pp_flop = pp_flop_set = pp_river_set = 0
        pp_set_net = 0
        pp_river_mu = 0.0
        for h, c in hero_cards_by_hand:
            pocket = (c[0] >> 2) == (c[1] >> 2)
            if pocket:
                pp_dealt += 1
            if len(h.board) < 3 or _folded_preflop(h, hero):
                continue
            flop_ranks = {card_int(b) >> 2 for b in h.board[:3] if card_int(b) is not None}
            board_ranks = {card_int(b) >> 2 for b in h.board if card_int(b) is not None}
            n_flop += 1
            if pocket:
                pp_flop += 1
                hit = (c[0] >> 2) in flop_ranks
                if hit:
                    pp_flop_set += 1
                    pp_set_net += h.net(hero)
                if (c[0] >> 2) in board_ranks:
                    pp_river_set += 1
                # expectation over the board cards actually dealt (3, 4 or 5)
                dealt = min(len(h.board), 5)
                pp_river_mu += 1 - comb(48, dealt) / comb(50, dealt)
                p0 = P_SET_FLOP
            else:
                hit = bool({c[0] >> 2, c[1] >> 2} & flop_ranks)
                p0 = P_PAIR_UNPAIRED
            hit_k += 1 if hit else 0
            mu += p0
            var += p0 * (1 - p0)
        if n_flop:
            zf = (hit_k - mu) / sqrt(var) if var > 0 else 0.0
            flop_hit = {
                "n": n_flop,
                "hit": hit_k,
                "expected": round(mu, 1),
                "rate": round(hit_k / n_flop, 3),
                "expected_rate": round(mu / n_flop, 3),
                "z": round(zf, 2),
                "p": _normal_p(zf),
            }
        if pp_flop:
            sets = {
                "dealt": pp_dealt,
                "saw_flop": pp_flop,
                "flop_set": pp_flop_set,
                "flop_expected": round(pp_flop * P_SET_FLOP, 1),
                "flop_p": _binom_two_sided(pp_flop_set, pp_flop, P_SET_FLOP),
                "flop_set_net": pp_set_net,
                "river_set": pp_river_set,
                "river_expected": round(pp_river_mu, 1),
            }

        # hand groups as a whole-hand ledger: for each category you chose to
        # play, everything paid in vs everything the pots paid back
        g_acc: dict[str, dict[str, Any]] = {
            k: {"dealt": 0, "played": 0, "paid": 0, "received": 0, "net": 0}
            for k in ("strong", "pairs_small", "broadway", "other")
        }
        for h, c in hero_cards_by_hand:
            if h.bomb_pot:
                continue
            g = hand_group(hand_class(c)) or "other"
            g_acc[g]["dealt"] += 1
            if _voluntary_preflop(h, hero):
                g_acc[g]["played"] += 1
                g_acc[g]["paid"] += max(h.contributions.get(hero, 0), 0)
                g_acc[g]["received"] += h.collected.get(hero, 0)
                g_acc[g]["net"] += h.net(hero)
        for g in g_acc.values():
            g["played_pct"] = round(100 * g["played"] / g["dealt"], 1) if g["dealt"] else None
            g["net_bb"] = in_bb(g["net"])
            g["bb_per_hand"] = round(g["net"] / bb / g["played"], 2) if bb and g["played"] else None
        groups = g_acc

        # the same whole-hand ledger, bucketed by the made hand on the flop
        def flop_strength(holes: list[int], flop: list[int]) -> str:
            r1, r2 = holes[0] >> 2, holes[1] >> 2
            pocket = r1 == r2
            board_ranks = {c >> 2 for c in flop}
            top = max(board_ranks)
            score = eval7(holes + flop)
            if score[0] >= 4 or (score[0] >= 2 and (pocket or bool({r1, r2} & board_ranks))):
                return "two_pair_plus"  # incl. sets, trips, straights, flushes
            if (pocket and r1 > top) or top in (r1, r2):
                return "top_pair"
            if pocket or bool({r1, r2} & board_ranks):
                return "weak_pair"  # middle/bottom pair, underpair
            return "no_pair"

        fs_acc: dict[str, dict[str, Any]] = {
            k: {"hands": 0, "paid": 0, "received": 0, "net": 0}
            for k in ("two_pair_plus", "top_pair", "weak_pair", "no_pair")
        }
        for h, c in hero_cards_by_hand:
            if h.bomb_pot or len(h.board) < 3 or _folded_preflop(h, hero):
                continue
            flop = [x for x in (card_int(b) for b in h.board[:3]) if x is not None]
            if len(flop) < 3 or any(x in flop for x in c):
                continue
            slot = fs_acc[flop_strength(c, flop)]
            slot["hands"] += 1
            slot["paid"] += max(h.contributions.get(hero, 0), 0)
            slot["received"] += h.collected.get(hero, 0)
            slot["net"] += h.net(hero)
        for s in fs_acc.values():
            s["net_bb"] = in_bb(s["net"])
            s["bb_per_hand"] = round(s["net"] / bb / s["hands"], 2) if bb and s["hands"] else None
        flop_str = fs_acc

        # pure IP / OOP split over saw-flop hands
        pos_acc = {k: {"hands": 0, "net": 0} for k in ("ip", "oop")}
        for h, _c in hero_cards_by_hand:
            if h.bomb_pot or len(h.board) < 3 or _folded_preflop(h, hero):
                continue
            active = [p for p in _postflop_order(h) if not _folded_preflop(h, p)]
            if hero not in active or len(active) < 2:
                continue
            key = "ip" if active[-1] == hero else "oop"
            pos_acc[key]["hands"] += 1
            pos_acc[key]["net"] += h.net(hero)
        for g in pos_acc.values():
            g["net_bb"] = in_bb(g["net"])
            g["bb_per_hand"] = round(g["net"] / bb / g["hands"], 2) if bb and g["hands"] else None
        position = pos_acc

    # ---------------- table-wide position ledger ----------------
    # position needs no hole cards, so this covers every flop-seen hand for
    # every player: the single last-to-act player is IP, everyone else OOP
    pos_table: dict[str, dict[str, Any]] = {
        "ip": {"hands": 0, "net": 0},
        "oop": {"hands": 0, "net": 0},
    }
    pos_players: dict[str, dict[str, dict[str, Any]]] = {}
    for h in hands:
        if h.bomb_pot or len(h.board) < 3:
            continue
        active = [p for p in _postflop_order(h) if not _folded_preflop(h, p)]
        if len(active) < 2:
            continue
        for p in active:
            key = "ip" if p == active[-1] else "oop"
            slot = pos_table[key]
            slot["hands"] += 1
            slot["net"] += h.net(p)
            pslot = pos_players.setdefault(p, {"ip": {"hands": 0, "net": 0}, "oop": {"hands": 0, "net": 0}})[key]
            pslot["hands"] += 1
            pslot["net"] += h.net(p)
    for slot in list(pos_table.values()) + [s for d in pos_players.values() for s in d.values()]:
        slot["net_bb"] = in_bb(slot["net"])
        slot["bb_per_hand"] = round(slot["net"] / bb / slot["hands"], 2) if bb and slot["hands"] else None

    # ---------------- run-good meter, all players ----------------
    luck_acc: dict[str, dict[str, Any]] = {}

    def lacc(p: str) -> dict[str, Any]:
        return luck_acc.setdefault(
            p,
            {
                "measured": 0,
                "luck": 0.0,
                "unmeasured_n": 0,
                "unmeasured_net": 0,
                "setup_n": 0,
                "setup_chips": 0,
                "allin_n": 0,
                "allin_luck": 0.0,
                "net": 0,
            },
        )

    nets: dict[str, int] = {}
    for h in hands:
        for p in h.players:
            nets[p] = nets.get(p, 0) + h.net(p)
        luck = _hand_luck_cached(h)
        if h.went_to_showdown:
            # survivors whose hands could not be scored: their result is
            # invisible to the luck number — surface how much money that is
            for p in h.survivors:
                if not luck or p not in luck:
                    a = lacc(p)
                    a["unmeasured_n"] += 1
                    a["unmeasured_net"] += h.net(p)
        if not luck:
            continue
        any_allin = any(a.all_in for a in h.actions)
        for p, r in luck.items():
            a = lacc(p)
            a["measured"] += 1
            a["luck"] += r["luck"]
            if any_allin and any(x.player == p and x.all_in for x in h.actions):
                a["allin_n"] += 1
                a["allin_luck"] += r["luck"]
            # a "setup": lost a big pot (>= 40 bb) holding a real hand when the
            # money went in — the hit-by-a-train feeling, whether the chips
            # count as bad luck (was ahead) or not (was behind a monster)
            if not r["won"] and r["commit_hand"] and bb and h.net(p) <= -40 * bb:
                a["setup_n"] += 1
                a["setup_chips"] += h.net(p)

    for p, a in luck_acc.items():
        a["net"] = nets.get(p, 0)
        a["luck"] = round(a["luck"])
        a["allin_luck"] = round(a["allin_luck"])
        a["luck_bb"] = in_bb(a["luck"])
        a["adjusted_net"] = a["net"] - a["luck"]

    # ---------------- money flow ----------------
    flow: dict[str, dict[str, float]] = {}
    for h in hands:
        win_nets = {p: h.net(p) for p in h.players if h.net(p) > 0}
        total_won = sum(win_nets.values())
        if total_won <= 0:
            continue
        for loser in h.players:
            lost = -h.net(loser)
            if lost <= 0:
                continue
            for winner, wn in win_nets.items():
                flow.setdefault(winner, {}).setdefault(loser, 0.0)
                flow[winner][loser] += lost * wn / total_won
    flow_out = {
        w: {l: round(v) for l, v in d.items() if round(v) != 0} for w, d in flow.items()
    }

    return {
        "hero": hero,
        "big_blind": bb or None,
        "hands": len(hands),
        "hero_dealt": hero_dealt,
        "hero_cards_known": len(hero_cards_by_hand),
        "quality": quality,
        "flop_hit": flop_hit,
        "sets": sets,
        "groups": groups,
        "flop_strength": flop_str,
        "position": position,
        "position_table": pos_table,
        "position_players": pos_players,
        "luck": luck_acc,
        "flow": flow_out,
    }
