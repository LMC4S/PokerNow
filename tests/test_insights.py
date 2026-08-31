"""Tests for the deterministic insights module (evaluator, equity, session stats)."""

import itertools
import json
import random
from collections import Counter

from pokernow.insights import (
    NULL_MEAN,
    PRE_EQ,
    card_int,
    cards_int,
    class_combos,
    compute_insights,
    equities,
    eval7,
    hand_class,
    hand_group,
)
from pokernow.parser import parse_text


# ---------------------------------------------------------------------------
# evaluator
# ---------------------------------------------------------------------------

def _ref_eval5(cards):
    ranks = sorted((c >> 2 for c in cards), reverse=True)
    suits = [c & 3 for c in cards]
    flush = len(set(suits)) == 1
    rs = sorted(set(ranks), reverse=True)
    sh = -1
    if len(rs) == 5:
        if rs[0] - rs[4] == 4:
            sh = rs[0]
        elif rs == [12, 3, 2, 1, 0]:
            sh = 3
    cnt = Counter(ranks)
    groups = sorted(cnt.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    if sh >= 0 and flush:
        return (8, sh)
    if groups[0][1] == 4:
        return (7, groups[0][0], groups[1][0])
    if groups[0][1] == 3 and groups[1][1] == 2:
        return (6, groups[0][0], groups[1][0])
    if flush:
        return (5, *ranks)
    if sh >= 0:
        return (4, sh)
    if groups[0][1] == 3:
        k = [r for r in ranks if r != groups[0][0]]
        return (3, groups[0][0], *k)
    if groups[0][1] == 2 and groups[1][1] == 2:
        k = [r for r in ranks if cnt[r] == 1]
        return (2, groups[0][0], groups[1][0], *k)
    if groups[0][1] == 2:
        k = [r for r in ranks if r != groups[0][0]]
        return (1, groups[0][0], *k)
    return (0, *ranks)


def _ref_eval7(cards):
    return max(_ref_eval5(c) for c in itertools.combinations(cards, 5))


def cs(*names):
    return [card_int(n) for n in names]


def test_eval7_matches_reference_on_random_hands():
    rng = random.Random(7)
    for _ in range(2000):
        cards = rng.sample(range(52), 7)
        assert eval7(cards) == _ref_eval7(cards), cards


def test_eval7_known_hands():
    assert eval7(cs("As", "Ks", "Qs", "Js", "Ts", "2d", "3c"))[0] == 8  # royal-ish SF
    assert eval7(cs("Ah", "2s", "3s", "4s", "5s", "9d", "9c"))[0:2] == (4, 3)  # wheel straight
    assert eval7(cs("Ah", "Ad", "Ac", "Kh", "Kd", "2s", "3s")) == (6, 12, 11)  # aces full
    # two trips make a full house of the higher trips
    assert eval7(cs("Ah", "Ad", "Ac", "2h", "2d", "2c", "9s")) == (6, 12, 0)
    # flush beats straight
    assert eval7(cs("2h", "4h", "6h", "8h", "Th", "9s", "Jc"))[0] == 5


def test_hand_class_and_groups():
    assert hand_class(cs("As", "Ks")) == "AKs"
    assert hand_class(cs("Kd", "As")) == "AKo"
    assert hand_class(cs("7c", "7d")) == "77"
    assert hand_group("AA") == "strong"
    assert hand_group("AQs") == "strong"
    assert hand_group("AQo") == "broadway"
    assert hand_group("77") == "pairs_small"
    assert hand_group("QTo") == "broadway"
    assert hand_group("A9s") is None


def test_pre_eq_table_complete_and_centred():
    assert len(PRE_EQ) == 169
    assert sum(class_combos(c) for c in PRE_EQ) == 1326
    assert abs(NULL_MEAN - 0.5) < 0.005
    assert PRE_EQ["AA"] > 0.8 and PRE_EQ["72o"] < 0.4


def test_equities_river_and_turn():
    # river: made flush vs top pair -> 100 / 0
    eq = equities([cs("Ah", "Kh"), cs("As", "Ad")], cs("2h", "7h", "9h", "3c", "3d"))
    assert eq == [1.0, 0.0]
    # turn: set vs flush draw; probabilities sum to 1 and favourite is the set
    eq = equities([cs("7s", "7d"), cs("Ah", "Kh")], cs("7h", "2h", "9c", "3s"))
    assert abs(sum(eq) - 1.0) < 1e-9
    assert eq[0] > 0.7


# ---------------------------------------------------------------------------
# session-level insights on a tiny synthetic game
# ---------------------------------------------------------------------------

A = {"id": "aaa111", "seat": 1, "name": "Alice"}
B = {"id": "bbb222", "seat": 2, "name": "Bob"}


def _ev(at, **payload):
    return {"at": at, "payload": payload}


def _session_json():
    t = 1710100000000
    hands = [
        {
            # Bob open-raises, Alice calls with AA; all-in on the flop; Alice's
            # kings-up loses to a rivered straight. Showdown with both known.
            "id": "h1", "handVersion": 2, "number": "1", "gameType": "th", "cents": False,
            "smallBlind": 5, "bigBlind": 10, "ante": None, "straddleSeat": None, "dealerSeat": 1,
            "startedAt": t, "bombPot": False, "doubleBoard": None,
            "players": [dict(A, stack=500, hand=["Ah", "Ad"]), dict(B, stack=500)],
            "events": [
                _ev(t + 1, type=3, seat=1, value=5),
                _ev(t + 2, type=2, seat=2, value=10),
                _ev(t + 3, type=8, seat=1, value=30),
                _ev(t + 4, type=7, seat=2, value=30),
                _ev(t + 5, type=9, turn=1, run=1, cards=["Kh", "Qd", "2c"]),
                _ev(t + 6, type=1, seat=2, value=470, allIn=True),
                _ev(t + 7, type=7, seat=1, value=470, allIn=True),
                _ev(t + 8, type=9, turn=2, run=1, cards=["3d"]),
                _ev(t + 9, type=9, turn=3, run=1, cards=["Ts"]),
                _ev(t + 10, type=15),
                _ev(t + 11, type=12, seat=2, cards=["Ac", "Jc"]),
                _ev(t + 12, type=10, pot=1000, seat=2, value=1000, cards=["Ac", "Jc"],
                    combination=["Ac", "Kh", "Qd", "Jc", "Ts"], handDescription="Straight, A High",
                    position=2, runNumber="1", hiLo="h"),
            ],
        },
        {
            # Alice folds 72o preflop to a raise; Bob takes the blinds.
            "id": "h2", "handVersion": 2, "number": "2", "gameType": "th", "cents": False,
            "smallBlind": 5, "bigBlind": 10, "ante": None, "straddleSeat": None, "dealerSeat": 2,
            "startedAt": t + 60000, "bombPot": False, "doubleBoard": None,
            "players": [dict(A, stack=30, hand=["7h", "2d"]), dict(B, stack=970)],
            "events": [
                _ev(t + 60001, type=3, seat=2, value=5),
                _ev(t + 60002, type=2, seat=1, value=10),
                _ev(t + 60003, type=8, seat=2, value=30),
                _ev(t + 60004, type=11, seat=1),
                _ev(t + 60005, type=16, seat=2, value=20),
                _ev(t + 60006, type=10, pot=25, seat=2, value=25),
            ],
        },
    ]
    return json.dumps({"playerId": "aaa111", "gameId": "g1", "hands": hands})


def test_compute_insights_end_to_end():
    session = parse_text(_session_json(), source_name="test.json")
    ins = compute_insights(session, big_blind=10)

    assert ins["hero"] and ins["hero"].startswith("Alice")
    assert ins["hero_dealt"] == 2
    assert ins["hero_cards_known"] == 2

    q = ins["quality"]
    assert q["n"] == 2
    # AA + 72o were dealt
    assert dict(q["top_classes"]) == {"AA": 1, "72o": 1}
    assert next(c for c in q["categories"] if c["name"] == "AA")["observed"] == 1
    # AA was played, 72o folded
    assert q["played_mean_eq"] == PRE_EQ["AA"]
    assert q["folded_mean_eq"] == PRE_EQ["72o"]

    # luck: hand 1 is a measured showdown; Alice was a big favourite on the
    # flop and lost the pot, so her luck is deeply negative and Bob's mirrors it.
    alice = next(v for k, v in ins["luck"].items() if k.startswith("Alice"))
    bob = next(v for k, v in ins["luck"].items() if k.startswith("Bob"))
    assert alice["measured"] == bob["measured"] == 1
    assert alice["luck"] < -700
    assert alice["luck"] + bob["luck"] == 0  # luck is zero-sum when all survivors are known
    assert alice["allin_n"] == 1
    assert alice["adjusted_net"] == alice["net"] - alice["luck"]
    # Alice lost 50 bb holding an overpair when the money went in — the classic
    # "hit by a train" spot the setup counter exists for.
    assert alice["setup_n"] == 1
    assert alice["setup_chips"] == -500
    assert bob["setup_n"] == 0

    # whole-hand ledger by starting hand: AA played (strong) — paid 500 in
    # total, received 0, net -500; 72o dealt & folded (other)
    g = ins["groups"]
    assert g["strong"]["dealt"] == 1 and g["strong"]["played"] == 1
    assert g["strong"]["paid"] == 500 and g["strong"]["received"] == 0
    assert g["strong"]["net"] == -500 and g["strong"]["bb_per_hand"] == -50.0
    assert g["other"]["dealt"] == 1 and g["other"]["played"] == 0

    # same ledger by flop strength: AA is an overpair on Kh Qd 2c -> top_pair
    fs = ins["flop_strength"]
    assert fs["top_pair"] == {"hands": 1, "paid": 500, "received": 0, "net": -500,
                              "net_bb": -50.0, "bb_per_hand": -50.0}
    assert fs["two_pair_plus"]["hands"] == 0

    # table-wide position: hand 1's flop was seen by both; dealer Alice acts last
    pt = ins["position_table"]
    assert pt["ip"]["hands"] == 1 and pt["ip"]["net"] == -500
    assert pt["oop"]["hands"] == 1 and pt["oop"]["net"] == 500
    pp_alice = next(v for k, v in ins["position_players"].items() if k.startswith("Alice"))
    assert pp_alice["ip"] == {"hands": 1, "net": -500, "net_bb": -50.0, "bb_per_hand": -50.0}
    assert pp_alice["oop"]["hands"] == 0

    # money flow: Bob won everything Alice lost across both hands
    bob_key = next(k for k in ins["flow"] if k.startswith("Bob"))
    alice_key = next(k for k in ins["flow"][bob_key] if k.startswith("Alice"))
    assert ins["flow"][bob_key][alice_key] == -alice["net"]  # every chip Alice lost went to Bob


def test_card_luck_uses_pot_at_reveal_not_final_pot():
    """A miracle river after calling a tiny bet is only a little luck: the big
    river bet won afterwards is payoff, not luck. Alice rivers a straight
    (gutshot, ~4/44 on the turn) in a 60-chip pot, then wins a 500 river bet."""
    t = 1710100000000
    hand = {
        "id": "h3", "handVersion": 2, "number": "1", "gameType": "th", "cents": False,
        "smallBlind": 5, "bigBlind": 10, "ante": None, "straddleSeat": None, "dealerSeat": 1,
        "startedAt": t, "bombPot": False, "doubleBoard": None,
        "players": [dict(A, stack=1000, hand=["5h", "6h"]), dict(B, stack=1000)],
        "events": [
            _ev(t + 1, type=3, seat=1, value=5),
            _ev(t + 2, type=2, seat=2, value=10),
            _ev(t + 3, type=7, seat=1, value=10),
            _ev(t + 4, type=0, seat=2),
            _ev(t + 5, type=9, turn=1, run=1, cards=["Kh", "8s", "2d"]),
            _ev(t + 6, type=0, seat=2), _ev(t + 7, type=0, seat=1),
            _ev(t + 8, type=9, turn=2, run=1, cards=["4s"]),
            _ev(t + 9, type=1, seat=2, value=20),          # tiny turn bet
            _ev(t + 10, type=7, seat=1, value=20),         # Alice calls with the gutshot
            _ev(t + 11, type=9, turn=3, run=1, cards=["7c"]),  # rivered straight
            _ev(t + 12, type=1, seat=1, value=500),
            _ev(t + 13, type=7, seat=2, value=500),
            _ev(t + 14, type=15),
            _ev(t + 15, type=12, seat=2, cards=["Kd", "Qd"]),
            _ev(t + 16, type=10, pot=1060, seat=1, value=1060, cards=["5h", "6h"],
                combination=["4s", "5h", "6h", "7c", "8s"], handDescription="Straight, 8 High",
                position=1, runNumber="1", hiLo="h"),
        ],
    }
    session = parse_text(json.dumps({"playerId": "aaa111", "gameId": "g2", "hands": [hand]}),
                         source_name="t.json")
    ins = compute_insights(session, big_blind=10)
    alice = next(v for k, v in ins["luck"].items() if k.startswith("Alice"))
    # she won a 1060 pot, but the river fell on a 60-chip pot: the river's luck
    # is ~(1 − eq_turn) × 60 ≈ +55, plus small flop/turn terms — nowhere near
    # the 530 that final-pot-based accounting would credit her with
    assert alice["net"] == 530
    assert 0 < alice["luck"] < 110
    bob = next(v for k, v in ins["luck"].items() if k.startswith("Bob"))
    assert alice["luck"] + bob["luck"] == 0


def test_insights_without_hero_cards():
    raw = json.loads(_session_json())
    raw["playerId"] = None
    for h in raw["hands"]:
        for p in h["players"]:
            p.pop("hand", None)
    session = parse_text(json.dumps(raw), source_name="anon.json")
    session.hero = None
    ins = compute_insights(session, big_blind=10)
    assert ins["quality"] is None
    # only Bob's hand was revealed at showdown, so no luck can be measured —
    # but the showdown is surfaced as unmeasured for both survivors, and the
    # money flow still works from the pot accounting alone
    alice = next(v for k, v in ins["luck"].items() if k.startswith("Alice"))
    assert alice["measured"] == 0
    assert alice["unmeasured_n"] == 1
    assert alice["unmeasured_net"] == -500
    assert ins["flow"]
