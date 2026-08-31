"""Live tracker and export tests (HTTP stubbed via the FakeClient from test_fetch)."""
import json
import os
from pathlib import Path

from pokernow import fetch as F
from pokernow.export import write_exports
from pokernow.live import LiveTracker
from pokernow.parser import load_archive, parse_file

from test_fetch import FakeClient, _hand

FIXTURE = Path(__file__).parent / "fixtures" / "sample_log.csv"


def test_live_tracker_polls_incrementally(tmp_path, monkeypatch):
    hands = [_hand("h1", 1)]
    c = FakeClient([[]], hands)
    monkeypatch.setattr(F, "PokerNowClient", lambda *a, **k: c)
    updates = []
    t = LiveTracker("https://www.pokernow.com/games/g1test", data_dir=str(tmp_path), interval=999,
                    on_update=lambda tr, res: updates.append((res.new_hands, res.new_log_lines)))
    assert t.poll_now() is True  # first poll always produces a session
    assert t.state.version == 1 and t.state.hands == 1 and t.session is not None
    assert (tmp_path / "g1test" / "summary.md").exists() and (tmp_path / "g1test" / "hands.csv").exists()

    # nothing new -> no version bump, no files rewritten
    m = os.path.getmtime(tmp_path / "g1test" / "summary.md")
    assert t.poll_now() is False and t.state.version == 1

    # a new finished hand and an in-progress hand appear; only the finished one is persisted
    hands.append(_hand("h2", 2))
    hands.append({"id": "h3", "number": "3", "gameType": "th", "players": [], "events": [{"at": 1, "payload": {"type": 3, "seat": 1, "value": 5}}]})
    assert t.poll_now() is True
    assert t.state.version == 2 and t.state.hands == 2
    saved = json.loads((tmp_path / "g1test" / "poker-now-hands-game-g1test.json").read_text())
    assert [h["number"] for h in saved["hands"]] == ["1", "2"]
    assert updates[-1][0] == 1

    # once hand 3 finishes it is picked up, and nothing is duplicated
    hands[-1] = _hand("h3", 3)
    assert t.poll_now() is True
    saved = json.loads((tmp_path / "g1test" / "poker-now-hands-game-g1test.json").read_text())
    assert [h["number"] for h in saved["hands"]] == ["1", "2", "3"]
    assert len(load_archive(F.GameArchive("g1test", str(tmp_path))).hands) == 3

    # errors are surfaced, not fatal
    def boom(*a, **k):
        raise F.FetchError("simulated 429")
    monkeypatch.setattr(c, "_get", boom)
    assert t.poll_now() is False
    assert "429" in t.state.last_error and t.state.consecutive_errors == 1


def test_write_exports_from_log(tmp_path):
    session = parse_file(str(FIXTURE))
    out = write_exports(str(tmp_path), session, None, "fixture")
    assert set(out) == {"stats", "players", "hands", "summary"}
    stats = json.loads(Path(out["stats"]).read_text())
    assert stats["hands"] == 6 and stats["game_id"] == "fixture"
    players = Path(out["players"]).read_text().splitlines()
    assert players[0].startswith("name,player,aka,hands,net") and len(players) == 5
    hands = Path(out["hands"]).read_text().splitlines()
    assert hands[0].startswith("hand,id,started_at") and "net:Alice" in hands[0] and len(hands) == 7
    md = Path(out["summary"]).read_text()
    assert "## Players" in md and "| Alice |" in md and "Biggest pots" in md
