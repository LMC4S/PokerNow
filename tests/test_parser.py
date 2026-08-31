from pathlib import Path

import pytest

from pokernow.models import ActionType, Street
from pokernow.parser import parse_cards, parse_file, parse_session
from pokernow.stats import compute_session_stats

FIXTURE = Path(__file__).parent / "fixtures" / "sample_log.csv"


@pytest.fixture(scope="module")
def session():
    return parse_file(str(FIXTURE))


@pytest.fixture(scope="module")
def stats(session):
    return compute_session_stats(session)


def by_name(stats, name):
    return next(p for p in stats.players if p.name == name)


def test_parse_cards():
    assert parse_cards("Ah, 10d, K♠") == ["Ah", "Td", "Ks"]
    assert parse_cards("Qs, Qd.") == ["Qs", "Qd"]
    assert parse_cards("") == []


def test_hands_are_chronological_even_though_file_is_newest_first(session):
    assert [h.number for h in session.hands] == [1, 2, 3, 4, 5, 6]
    assert session.hands[0].started_at < session.hands[-1].started_at


def test_events_and_unparsed(session):
    kinds = [e.kind for e in session.events]
    assert kinds.count("join") == 5  # incl. Lady Carol rejoining
    assert "approve" in kinds and "quit" in kinds and "stack_update" in kinds and "id_change" in kinds and "info" in kinds
    assert session.unparsed == []
    assert session.hands[3].unparsed == ["Some future log line we do not understand yet"]


def test_id_change_merges_player(session):
    assert session.aliases["Dave @ ddd444"] == "Dave @ ddd999"
    assert all("Dave @ ddd444" not in h.players for h in session.hands)
    assert "Dave @ ddd999" in session.hands[0].players and "Dave @ ddd999" in session.hands[5].players


def test_rename_same_id_merges_player(session):
    # Carol Smith rejoined as "Lady Carol" with the same ID; latest name wins everywhere.
    assert session.aliases["Carol Smith @ ccc333"] == "Lady Carol @ ccc333"
    for h in session.hands:
        assert "Carol Smith @ ccc333" not in h.players
    assert "Lady Carol @ ccc333" in session.hands[0].players  # hand 1, before the rename
    assert "Lady Carol @ ccc333" in session.hands[5].players
    quits = [e for e in session.events if e.kind == "quit"]
    assert all(e.player != "Carol Smith @ ccc333" for e in quits)


def test_post_hand_show_and_rabbit_hunt_attach_to_previous_hand(session):
    h = session.hands[0]
    assert h.shown_cards == {"Bob @ bbb222": ["7d", "2c"]}
    assert h.undealt_cards == [["5s", "5d", "Kc", "Ah", "2d"]]
    assert session.hero == "Alice @ aaa111"


def test_bomb_pot_hand(session):
    h = session.hands[5]
    assert h.bomb_pot is True
    posts = [a for a in h.actions if a.type is ActionType.BOMB_POT]
    assert len(posts) == 4 and all(a.amount == 40 for a in posts)
    assert h.pot == 480 and h.net("Alice @ aaa111") == 280 and h.net("Dave @ ddd999") == -200
    # raise to 300 over a 100 bet: increment is 300 (Alice had 0 in on the turn)
    r = next(a for a in h.actions if a.type is ActionType.RAISE)
    assert r.to_amount == 300 and r.amount == 300


def test_hand1_uncalled_bet_and_pot(session):
    h = session.hands[0]
    assert h.small_blind == 5 and h.big_blind == 10
    assert h.dealer == "Alice @ aaa111"
    assert h.hero_cards == ["7h", "2c"]
    assert h.pot == 20
    assert h.net("Bob @ bbb222") == 10
    assert h.net("Lady Carol @ ccc333") == -10
    assert h.net("Alice @ aaa111") == 0


def test_hand2_three_bet_pot(session):
    h = session.hands[1]
    assert h.pot == 195
    assert h.board == ["Kh", "7d", "2s"]
    raises = [a for a in h.actions if a.type is ActionType.RAISE]
    assert [(a.player.split(" @ ")[0], a.to_amount, a.amount) for a in raises] == [("Alice", 30, 30), ("Bob", 90, 90)]
    call = next(a for a in h.actions if a.type is ActionType.CALL)
    assert call.amount == 60 and call.to_amount == 90  # "calls 90" = total on street; 30 already in
    assert h.net("Bob @ bbb222") == 105
    assert h.net("Alice @ aaa111") == -90


def test_hand3_allin_straddle_showdown(session):
    h = session.hands[2]
    assert h.pot == 1020
    straddle = next(a for a in h.actions if a.type is ActionType.STRADDLE)
    assert straddle.player == "Bob @ bbb222" and straddle.amount == 20
    allin = next(a for a in h.actions if a.all_in)
    assert allin.player == "Dave @ ddd999" and allin.to_amount == 490 and allin.amount == 410
    assert h.contributions["Dave @ ddd999"] == 490
    assert h.contributions["Alice @ aaa111"] == 490
    assert h.board == ["Qh", "9c", "4d", "Ts", "Jc"]  # unicode suits and "10" normalised
    assert h.shown_cards == {"Alice @ aaa111": ["Qs", "Qd"], "Dave @ ddd999": ["Ac", "Ad"]}
    collect = next(a for a in h.actions if a.type is ActionType.COLLECT)
    assert collect.hand_desc == "Three of a Kind, Q's"
    assert collect.cards == ["Qs", "Qd", "Qh", "Jc", "Ts"]
    assert h.net("Alice @ aaa111") == 530 and h.net("Dave @ ddd999") == -490


def test_hand4_split_pot_and_streets(session):
    h = session.hands[3]
    assert h.pot == 100
    assert h.winners == ["Bob @ bbb222", "Lady Carol @ ccc333", "Dave @ ddd999"]
    assert h.net("Bob @ bbb222") == 4 and h.net("Lady Carol @ ccc333") == 3 and h.net("Alice @ aaa111") == -10
    streets = [a.street for a in h.actions]
    assert Street.FLOP in streets and Street.TURN in streets and Street.RIVER in streets
    flop_fold = next(a for a in h.actions if a.type is ActionType.FOLD)
    assert flop_fold.street is Street.FLOP


def test_hand5_run_it_twice(session):
    h = session.hands[4]
    assert h.pot == 1941
    assert h.board == ["2h", "5c", "9d", "Kd", "3s"]
    assert h.board_runs == [["2h", "5c", "9d", "Kd", "3s"], ["As", "8c", "8d", "Ac", "7s"]]
    assert h.run_it_twice is True and h.double_board is False
    assert h.net("Alice @ aaa111") == 2 and h.net("Lady Carol @ ccc333") == 3


def test_chips_conserved_every_hand(session):
    for h in session.hands:
        assert h.chip_mismatch == 0, f"hand {h.number}"
        assert sum(h.net(p) for p in h.players) == 0, f"hand {h.number}"


def test_session_stats(stats):
    assert stats.hands == 6
    assert stats.small_blind == 5 and stats.big_blind == 10
    assert stats.biggest_pot == 1941 and stats.biggest_pot_hand == 5
    assert stats.unparsed_lines == 1
    assert stats.flop_pct == 83.3
    assert stats.showdown_pct == 50.0
    assert sum(p.net for p in stats.players) == 0
    assert len(stats.players) == 4  # Dave's two IDs merged


def test_player_stats(stats):
    alice = by_name(stats, "Alice")
    assert alice.hands == 6
    assert alice.net == 712
    assert alice.vpip_hands == 4  # hands 2,3,4,5 (bomb-pot post in 6 is forced)
    assert alice.pfr_hands == 3  # hands 2,3,5
    assert alice.three_bet == 100.0  # one opportunity (hand 3), taken
    assert alice.bets_raises == 5 and alice.calls == 4  # AF 1.25
    assert alice.af == 1.25
    assert alice.fold_to_cbet_pct == 100.0
    assert alice.first_stack == 1000 and alice.last_stack == 1717

    bob = by_name(stats, "Bob")
    assert bob.three_bet == 50.0  # 3-bet hand 2, folded to raise hand 5
    assert bob.cbet_pct == 100.0
    assert bob.af == 3.0

    carol = by_name(stats, "Lady Carol")
    assert carol.hands == 6  # 5 as Carol Smith + 1 as Lady Carol, same ID
    assert carol.aka == ["Carol Smith"]

    dave = by_name(stats, "Dave")
    assert dave.hands == 6  # both IDs
    assert dave.all_ins == 1
    assert dave.three_bet is None  # never faced a single raise with a chance to 3-bet
    assert dave.wsd_pct == 50.0  # lost hand 3, chopped hand 4

    d = alice.to_dict(10)
    assert d["net_bb"] == 71.2 and d["bb_per_100"] == 1186.7


def test_timestamp_only_export_is_sorted():
    text = (
        "entry,at\n"
        '"-- ending hand #1 --",2024-01-01T00:00:20.000Z\n'
        '"""Bob @ b"" collected 10 from pot",2024-01-01T00:00:15.000Z\n'
        '"""Bob @ b"" posts a big blind of 10",2024-01-01T00:00:10.000Z\n'
        '"-- starting hand #1 (id: x)  (No Limit Texas Hold\'em) (dealer: ""Al @ a"") --",2024-01-01T00:00:00.000Z\n'
    )
    s = parse_session(text)
    assert len(s.hands) == 1
    assert s.hands[0].collected == {"Bob @ b": 10}
    assert s.hands[0].big_blind == 10


def test_dead_button_and_old_start_format():
    text = (
        "entry,at,order\n"
        '"-- starting hand #7  (No Limit Texas Hold\'em) (dead button) --",2024-01-01T00:00:00.000Z,1\n'
        '"Player stacks: #1 ""Al @ a"" (100) | #3 ""Bo @ b"" (200)",2024-01-01T00:00:01.000Z,2\n'
        '"""Al @ a"" posts a small blind of 1",2024-01-01T00:00:02.000Z,3\n'
        '"""Bo @ b"" posts a big blind of 2",2024-01-01T00:00:03.000Z,4\n'
        '"""Al @ a"" folds",2024-01-01T00:00:04.000Z,5\n'
        '"Uncalled bet of 1 returned to ""Bo @ b""",2024-01-01T00:00:05.000Z,6\n'
        '"""Bo @ b"" collected 2 from pot",2024-01-01T00:00:06.000Z,7\n'
        '"-- ending hand #7 --",2024-01-01T00:00:07.000Z,8\n'
    )
    s = parse_session(text)
    h = s.hands[0]
    assert h.number == 7 and h.dealer is None and h.id == ""
    assert [x.seat for x in h.seats] == [1, 3]
    assert h.pot == 2 and h.net("Bo @ b") == 1 and h.net("Al @ a") == -1


def test_missed_small_blind_is_dead_money():
    """A missed SB goes to the pot but is not part of the live wager; the missed
    BB is live. 'raises to N' is the live total (real hand: PokerNow #48)."""
    text = (
        "entry,at,order\n"
        '"-- starting hand #1 (id: x)  No Limit Texas Hold\'em (dealer: ""A @ a1"") --",2024-01-01T00:00:00.000Z,1\n'
        '"Player stacks: #1 ""A @ a1"" (1000) | #2 ""B @ b2"" (1000) | #3 ""C @ c3"" (1000)",2024-01-01T00:00:01.000Z,2\n'
        '"""A @ a1"" posts a small blind of 10",2024-01-01T00:00:02.000Z,3\n'
        '"""B @ b2"" posts a big blind of 20",2024-01-01T00:00:03.000Z,4\n'
        '"""C @ c3"" posts a missing small blind of 10",2024-01-01T00:00:04.000Z,5\n'
        '"""C @ c3"" posts a missed big blind of 20",2024-01-01T00:00:05.000Z,6\n'
        '"""C @ c3"" raises to 100",2024-01-01T00:00:06.000Z,7\n'
        '"""A @ a1"" folds",2024-01-01T00:00:07.000Z,8\n'
        '"""B @ b2"" folds",2024-01-01T00:00:08.000Z,9\n'
        '"Uncalled bet of 80 returned to ""C @ c3""",2024-01-01T00:00:09.000Z,10\n'
        '"""C @ c3"" collected 60 from pot",2024-01-01T00:00:10.000Z,11\n'
        '"-- ending hand #1 --",2024-01-01T00:00:11.000Z,12\n'
    )
    s = parse_session(text)
    h = s.hands[0]
    r = next(a for a in h.actions if a.type is ActionType.RAISE)
    assert r.amount == 80 and r.to_amount == 100  # 20 live (missed BB) already in; dead 10 not counted
    assert h.contributions["C @ c3"] == 10 + 20 + 80 - 80
    assert h.pot == 60 and h.chip_mismatch == 0
    assert h.net("C @ c3") == 30


def test_two_partial_shows_consolidate():
    text = (
        "entry,at,order\n"
        '"-- starting hand #1 (id: x)  No Limit Texas Hold\'em (dealer: ""Al @ a"") --",2024-01-01T00:00:00.000Z,1\n'
        '"Player stacks: #1 ""Al @ a"" (100) | #2 ""Bo @ b"" (100)",2024-01-01T00:00:01.000Z,2\n'
        '"""Al @ a"" posts a small blind of 1",2024-01-01T00:00:02.000Z,3\n'
        '"""Bo @ b"" posts a big blind of 2",2024-01-01T00:00:03.000Z,4\n'
        '"""Al @ a"" folds",2024-01-01T00:00:04.000Z,5\n'
        '"Uncalled bet of 1 returned to ""Bo @ b""",2024-01-01T00:00:05.000Z,6\n'
        '"""Bo @ b"" collected 2 from pot",2024-01-01T00:00:06.000Z,7\n'
        '"-- ending hand #1 --",2024-01-01T00:00:07.000Z,8\n'
        '"""Bo @ b"" shows a Q♠.",2024-01-01T00:00:08.000Z,9\n'
        '"""Bo @ b"" shows a A♠.",2024-01-01T00:00:09.000Z,10\n'
    )
    s = parse_session(text)
    h = s.hands[0]
    assert h.shown_cards == {"Bo @ b": ["Qs", "As"]}
    assert h.known_cards["Bo @ b"] == ["Qs", "As"]
