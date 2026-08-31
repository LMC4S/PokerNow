"""Unit tests for the fetch client (no network: HTTP is stubbed)."""
import json
from pathlib import Path

import pytest

from pokernow import fetch as F
from pokernow.parser import load_archive


def test_parse_game_ref():
    assert F.parse_game_ref("https://www.pokernow.com/games/pglAbCdEfGhIjKlMnOpQrStUv") == ("https://www.pokernow.com", "pglAbCdEfGhIjKlMnOpQrStUv")
    assert F.parse_game_ref("https://www.pokernow.club/hand-replayer/game/abcDEF123456/hand/x")[1] == "abcDEF123456"
    assert F.parse_game_ref("abcDEF123456") == (F.DEFAULT_HOST, "abcDEF123456")
    with pytest.raises(F.FetchError):
        F.parse_game_ref("https://example.com/nothing")


def test_credentials_cookie(monkeypatch):
    monkeypatch.delenv("POKERNOW_NPT", raising=False)
    assert F.Credentials.from_env().cookie_header is None
    assert F.Credentials(npt="x", apt="y").cookie_header == "npt=x; apt=y"


class FakeClient(F.PokerNowClient):
    """Serves canned responses; records the paths requested."""

    def __init__(self, pages, hands):
        super().__init__(min_interval=0)
        self.pages = pages  # list of log pages returned in order
        self.hands_data = hands
        self.calls = []

    def _get(self, path, **kw):
        self.calls.append(path)
        if path.endswith("/players_sessions"):
            return {"buyInTotal": 1, "playersInfos": {}}
        if "/log?" in path:
            return {"logs": self.pages.pop(0) if self.pages else [], "infos": {}}
        if path.endswith(f"/api/hand-replayer/game/g1test"):
            return {"hands": [{"id": h["id"], "number": h["number"]} for h in self.hands_data]}
        if "/api/hand-replayer/hand/" in path:
            hid = path.rsplit("/", 1)[1]
            return next({k: v for k, v in h.items() if k != "id"} for h in self.hands_data if h["id"] == hid)
        raise AssertionError(path)


def _hand(hid, num):
    t = 1710100000000 + num * 1000
    return {"id": hid, "handVersion": 2, "number": str(num), "gameType": "th", "smallBlind": 5, "bigBlind": 10,
            "dealerSeat": 1, "startedAt": t, "bombPot": False, "doubleBoard": None,
            "players": [{"id": "a", "seat": 1, "name": "Al", "stack": 100}, {"id": "b", "seat": 2, "name": "Bo", "stack": 100}],
            "events": [{"at": t, "payload": {"type": 3, "seat": 1, "value": 5}}, {"at": t + 1, "payload": {"type": 2, "seat": 2, "value": 10}},
                       {"at": t + 2, "payload": {"type": 11, "seat": 1}}, {"at": t + 3, "payload": {"type": 16, "seat": 2, "value": 5}},
                       {"at": t + 4, "payload": {"type": 10, "seat": 2, "value": 10, "pot": 10, "position": 1}}, {"at": t + 5, "payload": {"type": 15}}]}


def test_log_paging_and_csv():
    pages = [
        [{"at": "2024-01-01T00:00:03.000Z", "created_at": "170410000030000", "msg": "-- ending hand #1 --"},
         {"at": "2024-01-01T00:00:02.000Z", "created_at": "170410000020000", "msg": '"Bo @ b" collected 10 from pot'}],
        [{"at": "2024-01-01T00:00:02.000Z", "created_at": "170410000020000", "msg": '"Bo @ b" collected 10 from pot'},
         {"at": "2024-01-01T00:00:01.000Z", "created_at": "170410000010000", "msg": '-- starting hand #1 (id: x)  No Limit Texas Hold\'em (dealer: "Al @ a") --'}],
        [{"at": "2024-01-01T00:00:01.000Z", "created_at": "170410000010000", "msg": "dup"}],
    ]
    c = FakeClient(pages, [])
    rows = c.fetch_log("g1test")
    assert [r["order"] for r in sorted(rows, key=lambda r: r["order"])] == [170410000010000, 170410000020000, 170410000030000]
    # pager passes the oldest created_at of the previous page as before_at
    assert "before_at=170410000020000" in c.calls[1]
    res = F.FetchResult(game_id="g1test", host="h", log_rows=rows)
    csv_text = res.log_csv()
    assert csv_text.splitlines()[0] == '"entry","at","order"'
    assert csv_text.splitlines()[1].startswith('"-- ending hand #1 --"')  # newest first, like PokerNow


def test_fetch_hands_and_archive_refresh(tmp_path, monkeypatch):
    hands = [_hand("h1", 1), _hand("h2", 2)]
    c = FakeClient([[]], hands)
    arch = F.GameArchive("g1test", str(tmp_path))
    monkeypatch.setattr(F, "PokerNowClient", lambda *a, **k: c)
    res = arch.refresh("https://www.pokernow.com/games/g1test", what=("ledger", "hands", "log"))
    assert res.hands_json and [h["number"] for h in res.hands_json["hands"]] == ["1", "2"]
    assert res.ledger is not None
    assert any(w.startswith("No login cookie") for w in res.warnings)
    assert Path(arch.hands_path).exists() and Path(arch.ledger_path).exists() and Path(arch.meta_path).exists()
    n_hand_calls = sum(1 for p in c.calls if "/api/hand-replayer/hand/" in p)
    assert n_hand_calls == 2

    # second refresh: a new hand appears; only it is fetched
    hands.append(_hand("h3", 3))
    c.calls.clear()
    res2 = arch.refresh("g1test", what=("hands",))
    assert [h["number"] for h in res2.hands_json["hands"]] == ["1", "2", "3"]
    assert sum(1 for p in c.calls if "/api/hand-replayer/hand/" in p) == 1

    sess = load_archive(arch)
    assert len(sess.hands) == 3 and sess.source_format == "json"
    assert all(h.chip_mismatch == 0 for h in sess.hands)
    meta = json.loads(Path(arch.meta_path).read_text())
    assert meta["hands"] == 3


def test_backfill_refetches_cached_hands(tmp_path, monkeypatch):
    hands = [_hand("h1", 1), _hand("h2", 2)]
    c = FakeClient([[]], hands)
    monkeypatch.setattr(F, "PokerNowClient", lambda *a, **k: c)
    arch = F.GameArchive("g1test", str(tmp_path))
    arch.refresh("g1test", what=("hands",))
    c.calls.clear()
    # normal refresh: nothing refetched
    arch.refresh("g1test", what=("hands",))
    assert sum(1 for p in c.calls if "/api/hand-replayer/hand/" in p) == 0
    # backfill: every hand refetched (e.g. now with login cookie -> hole cards)
    hands[0] = dict(hands[0])
    hands[0]["players"] = [dict(hands[0]["players"][0], hand=["Ah", "Kh"]), hands[0]["players"][1]]
    c.calls.clear()
    arch.refresh("g1test", what=("hands",), force_hands=True)
    assert sum(1 for p in c.calls if "/api/hand-replayer/hand/" in p) == 2
    saved = json.loads(Path(arch.hands_path).read_text())
    assert saved["hands"][0]["players"][0]["hand"] == ["Ah", "Kh"]
