"""Tests for the JSON hand-export parser, using a synthetic handVersion-2 file."""
import json
from pathlib import Path

import pytest

from pokernow.models import ActionType
from pokernow.parser import combine_sessions, parse_file, parse_text
from pokernow.stats import compute_session_stats

FIXTURE = Path(__file__).parent / "fixtures" / "sample_hands.json"

A = {"id": "aaa111", "seat": 1, "name": "Alice"}
B = {"id": "bbb222", "seat": 2, "name": "Bob"}
C = {"id": "ccc333", "seat": 3, "name": "Carol Smith"}


def ev(at, **payload):
    return {"at": at, "payload": payload}


def make_fixture():
    t = 1710100000000
    hands = [
        {  # hand 1: limp, min-bet encoded as CALL(7) with nothing in front, raise, fold; rake 0
            "id": "j1", "handVersion": 2, "number": "1", "gameType": "th", "cents": False,
            "smallBlind": 5, "bigBlind": 10, "ante": None, "straddleSeat": None, "dealerSeat": 1,
            "startedAt": t, "bombPot": False, "sevenDeuceBounty": None, "doubleBoard": None,
            "players": [dict(A, stack=1000, hand=["Ah", "Kd"]), dict(B, stack=1000), dict(C, stack=1000)],
            "events": [
                ev(t + 1, type=3, seat=2, value=5), ev(t + 2, type=2, seat=3, value=10),
                ev(t + 3, type=7, seat=1, value=10),          # Alice limps
                ev(t + 4, type=7, seat=2, value=10),          # Bob completes
                ev(t + 5, type=0, seat=3),                    # Carol checks
                ev(t + 6, type=9, turn=1, run=1, cards=["As", "7d", "2c"], handsLabels={}),
                ev(t + 7, type=0, seat=2), ev(t + 8, type=0, seat=3),
                ev(t + 9, type=7, seat=1, value=10),          # PokerNow quirk: min-bet as "call"
                ev(t + 10, type=8, seat=2, value=40),         # Bob raises to 40
                ev(t + 11, type=11, seat=3),                  # Carol folds
                ev(t + 12, type=7, seat=1, value=40),         # Alice calls (total 40)
                ev(t + 13, type=9, turn=2, run=1, cards=["Tc"], handsLabels={}),
                ev(t + 14, type=0, seat=2), ev(t + 15, type=1, seat=1, value=50),   # Alice bets 50 (type 1 BET)
                ev(t + 16, type=11, seat=2),
                ev(t + 17, type=16, seat=1, value=50),        # uncalled
                ev(t + 18, type=10, seat=1, value=110, pot=110, position=1),
                ev(t + 19, type=15),
                ev(t + 20, type=12, seat=1, cards=[None, "Kd"]),  # post-hand partial show, one card at a time
                ev(t + 21, type=12, seat=1, cards=["Ah", None]),  # then the other card
            ],
            "playerNet": 60,
        },
        {  # hand 2: bomb pot + double board, rake 3
            "id": "j2", "handVersion": 2, "number": "2", "gameType": "th", "cents": False,
            "smallBlind": 5, "bigBlind": 10, "ante": None, "straddleSeat": None, "dealerSeat": 2,
            "startedAt": t + 60000, "bombPot": True, "sevenDeuceBounty": None, "doubleBoard": True,
            "players": [dict(A, stack=1080, hand=["9h", "9d"]), dict(B, stack=950), dict(C, stack=970)],
            "events": [
                ev(t + 60001, type=1, seat=1, value=20, bombPot=True),
                ev(t + 60002, type=7, seat=2, value=20, bombPot=True),
                ev(t + 60003, type=7, seat=3, value=20, bombPot=True),
                ev(t + 60004, type=9, turn=1, run=1, cards=["9s", "4c", "2d"], handsLabels={}),
                ev(t + 60005, type=9, turn=1, run=2, cards=["Kh", "Qh", "Jh"], handsLabels={}),
                ev(t + 60006, type=0, seat=2), ev(t + 60007, type=0, seat=3), ev(t + 60008, type=0, seat=1),
                ev(t + 60009, type=9, turn=2, run=1, cards=["5s"], handsLabels={}),
                ev(t + 60010, type=9, turn=2, run=2, cards=["3c"], handsLabels={}),
                ev(t + 60011, type=0, seat=2), ev(t + 60012, type=0, seat=3), ev(t + 60013, type=0, seat=1),
                ev(t + 60014, type=9, turn=3, run=1, cards=["6d"], handsLabels={}),
                ev(t + 60015, type=9, turn=3, run=2, cards=["Th"], handsLabels={}),
                ev(t + 60016, type=0, seat=2), ev(t + 60017, type=0, seat=3), ev(t + 60018, type=0, seat=1),
                ev(t + 60019, type=15),
                ev(t + 60020, type=12, seat=1, cards=["9h", "9d"]), ev(t + 60021, type=12, seat=2, cards=["Ah", "2h"]),
                ev(t + 60022, type=12, seat=3, cards=["7c", "8c"]),
                ev(t + 60023, type=10, seat=1, value=28, pot=28, position=1, runNumber="1", cards=["9h", "9d"], combination=["9h", "9d", "9s", "6d", "5s"], handDescription="Three of a Kind, 9's", hiLo="h"),
                ev(t + 60024, type=10, seat=2, value=29, pot=29, position=1, runNumber="2", cards=["Ah", "2h"], combination=["Ah", "Kh", "Qh", "Jh", "Th"], handDescription="Royal Flush", hiLo="h"),
                ev(t + 60025, type=19, value=3),
            ],
            "playerNet": 8,
        },
    ]
    return {"generatedAt": "2024-03-10T21:00:00.000Z", "playerId": "aaa111", "gameId": "gameXYZ123", "hands": hands}


@pytest.fixture(scope="module")
def session():
    FIXTURE.write_text(json.dumps(make_fixture(), ensure_ascii=False, indent=1))
    return parse_file(str(FIXTURE))


def test_sniffs_json(session):
    assert session.source_format == "json"
    assert session.hero == "Alice @ aaa111"
    assert [h.number for h in session.hands] == [1, 2]


def test_hand1_actions_and_net(session):
    h = session.hands[0]
    assert h.game_type == "No Limit Texas Hold'em"
    assert h.dealer == "Alice @ aaa111" and h.small_blind == 5 and h.big_blind == 10
    assert h.hero_cards == ["Ah", "Kd"]
    types = [a.type for a in h.actions]
    assert types[:3] == [ActionType.SMALL_BLIND, ActionType.BIG_BLIND, ActionType.CALL]
    flop = [a for a in h.actions if a.street.value == "flop"]
    assert [a.type for a in flop][:5] == [ActionType.CHECK, ActionType.CHECK, ActionType.BET, ActionType.RAISE, ActionType.FOLD]
    assert flop[2].amount == 10 and flop[3].to_amount == 40 and flop[3].amount == 40
    call = flop[5]
    assert call.type is ActionType.CALL and call.amount == 30 and call.to_amount == 40
    turn_bet = next(a for a in h.actions if a.street.value == "turn" and a.type is ActionType.BET)
    assert turn_bet.amount == 50
    assert h.pot == 110 and h.chip_mismatch == 0
    assert h.net("Alice @ aaa111") == 60 == h.hero_net_reported
    assert h.shown_cards == {"Alice @ aaa111": ["Ah", "Kd"]}  # two partial shows consolidated positionally
    assert h.known_cards["Alice @ aaa111"] == ["Ah", "Kd"]
    shows = [a for a in h.actions if a.type is ActionType.SHOW]
    assert [a.cards for a in shows] == [["Kd"], ["Ah"]]  # each step still shows what was revealed then
    show = [a for a in h.actions if a.type is ActionType.SHOW][0]
    assert show.street.value == "showdown"  # post-hand


def test_hand2_bomb_pot_double_board_rake(session):
    h = session.hands[1]
    assert h.bomb_pot and h.double_board and not h.run_it_twice
    assert [a.type for a in h.actions[:3]] == [ActionType.BOMB_POT] * 3
    assert h.board == ["9s", "4c", "2d", "5s", "6d"]
    assert h.board_runs == [["9s", "4c", "2d", "5s", "6d"], ["Kh", "Qh", "Jh", "3c", "Th"]]
    assert h.rake == 3
    assert h.pot == 60 and sum(h.collected.values()) == 57 and h.chip_mismatch == 0
    assert h.net("Alice @ aaa111") == 8 == h.hero_net_reported
    assert h.known_cards["Carol Smith @ ccc333"] == ["7c", "8c"]
    c = [a for a in h.actions if a.type is ActionType.COLLECT]
    assert c[1].hand_desc == "Royal Flush"


def test_stats_from_json(session):
    st = compute_session_stats(session)
    alice = next(p for p in st.players if p.name == "Alice")
    assert alice.hands == 2 and alice.net == 68
    assert alice.vpip_hands == 1  # bomb pot post is forced, not VPIP
    assert sum(p.net for p in st.players) == -3  # rake leaves the table


def test_combine_with_log():
    js = parse_file(str(FIXTURE))
    log_text = (
        "entry,at,order\n"
        '"The player ""Bob @ bbb222"" joined the game with a stack of 1000.",2024-03-10T19:59:00.000Z,1\n'
        '"-- starting hand #1 (id: j1)  No Limit Texas Hold\'em (dealer: ""Alice @ aaa111"") --",2024-03-10T20:00:00.000Z,2\n'
        '"Your hand is Ah, Kd",2024-03-10T20:00:00.000Z,3\n'
        '"-- ending hand #1 --",2024-03-10T20:00:20.000Z,4\n'
        '"Undealt cards: As, 7d, 2c, Tc [3h]",2024-03-10T20:00:21.000Z,5\n'
    )
    lg = parse_text(log_text)
    merged = combine_sessions(js, lg)
    assert merged.source_format == "json"
    assert [e.kind for e in merged.events] == ["join"]
    assert merged.hands[0].undealt_cards == [["As", "7d", "2c", "Tc", "3h"]]
    assert merged.hands[0].hero_cards == ["Ah", "Kd"]
