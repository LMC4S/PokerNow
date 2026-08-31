from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pokernow.app import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "sample_log.csv"


@pytest.fixture()
def client():
    return TestClient(create_app())


@pytest.fixture()
def sid(client):
    with FIXTURE.open("rb") as f:
        r = client.post("/api/sessions", files={"file": ("sample_log.csv", f, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "PokerNow Hand History" in r.text


def test_upload_and_summary(client, sid):
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["hands"] == 6
    assert body["hero"] == "Alice @ aaa111" and body["source_format"] == "log"
    names = [p["name"] for p in body["summary"]["players"]]
    assert names[0] == "Alice"  # sorted by net desc

    r = client.get("/api/sessions")
    assert r.json()[0]["id"] == sid


def test_upload_is_idempotent(client, sid):
    with FIXTURE.open("rb") as f:
        r = client.post("/api/sessions", files={"file": ("again.csv", f, "text/csv")})
    assert r.json()["id"] == sid
    assert len(client.get("/api/sessions").json()) == 1


def test_hands_filters(client, sid):
    r = client.get(f"/api/sessions/{sid}/hands")
    assert r.json()["total"] == 6
    assert r.json()["hands"][5]["bomb_pot"] is True
    r = client.get(f"/api/sessions/{sid}/hands", params={"min_pot": 1000})
    assert [h["number"] for h in r.json()["hands"]] == [3, 5]
    r = client.get(f"/api/sessions/{sid}/hands", params={"showdown": "true"})
    assert [h["number"] for h in r.json()["hands"]] == [3, 4, 5]
    r = client.get(f"/api/sessions/{sid}/hands", params={"player": "Dave", "limit": 2, "offset": 1})
    assert r.json()["total"] == 6 and [h["number"] for h in r.json()["hands"]] == [2, 3]


def test_involvement_filter(client, sid):
    # Alice folded preflop in hand 1 -> "flop" excludes it; "won" narrows to her wins
    r = client.get(f"/api/sessions/{sid}/hands", params={"player": "Alice", "involvement": "flop"})
    assert 1 not in [h["number"] for h in r.json()["hands"]]
    r = client.get(f"/api/sessions/{sid}/hands", params={"player": "Alice", "involvement": "won"})
    assert [h["number"] for h in r.json()["hands"]] == [3, 5, 6]
    r = client.get(f"/api/sessions/{sid}/hands", params={"player": "Alice", "involvement": "vpip"})
    assert 1 not in [h["number"] for h in r.json()["hands"]]


def test_hand_detail(client, sid):
    r = client.get(f"/api/sessions/{sid}/hands/3")
    assert r.status_code == 200
    h = r.json()
    assert h["pot"] == 1020
    assert h["board"] == ["Qh", "9c", "4d", "Ts", "Jc"]
    assert h["actions"][0]["type"] == "small_blind"
    assert h["net"]["Alice @ aaa111"] == 530
    assert client.get(f"/api/sessions/{sid}/hands/999").status_code == 404


def test_player_endpoints(client, sid):
    r = client.get(f"/api/sessions/{sid}/players")
    assert len(r.json()) == 4
    r = client.get(f"/api/sessions/{sid}/players/Lady Carol")
    assert r.status_code == 200
    assert r.json()["hands_count"] == 6
    # lookup by a former name also works
    r = client.get(f"/api/sessions/{sid}/players/Carol Smith")
    assert r.status_code == 200 and r.json()["name"] == "Lady Carol"
    assert client.get(f"/api/sessions/{sid}/players/Nobody").status_code == 404


def test_events_and_unparsed(client, sid):
    assert len(client.get(f"/api/sessions/{sid}/events").json()) == 11
    u = client.get(f"/api/sessions/{sid}/unparsed").json()
    assert u["hands"] == {"4": ["Some future log line we do not understand yet"]}


def test_fetch_rejects_bad_url(client):
    r = client.post("/api/fetch", json={"url": "not a game"})
    assert r.status_code == 400


def test_archives_empty(client, tmp_path):
    from pokernow.app import create_app as mk
    c = TestClient(mk(data_dir=str(tmp_path)))
    assert c.get("/api/archives").json() == []
    assert c.post("/api/archives/nothing/load").status_code == 404


def test_bad_upload(client):
    r = client.post("/api/sessions", files={"file": ("x.csv", b"hello,world\n1,2\n", "text/csv")})
    assert r.status_code == 400


def test_delete(client, sid):
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_credentials_roundtrip(client, monkeypatch):
    for var in ("POKERNOW_NPT", "POKERNOW_APT", "POKERNOW_COOKIES"):
        monkeypatch.delenv(var, raising=False)
    assert client.get("/api/credentials").json() == {"set": False, "source": None}
    # a bare token value
    assert client.post("/api/credentials", json={"cookie": "abc123token"}).json() == {"set": True, "source": "ui"}
    assert client.get("/api/credentials").json() == {"set": True, "source": "ui"}
    # a pasted cookie header
    assert client.post("/api/credentials", json={"cookie": "npt=tok1; apt=tok2"}).status_code == 200
    # garbage is rejected and does not clobber the stored cookie
    assert client.post("/api/credentials", json={"cookie": "hello world"}).status_code == 400
    assert client.post("/api/credentials", json={"cookie": "  "}).status_code == 400
    assert client.get("/api/credentials").json()["set"] is True
    assert client.delete("/api/credentials").json() == {"set": False, "source": None}


def test_credentials_env_fallback(client, monkeypatch):
    monkeypatch.setenv("POKERNOW_NPT", "envtoken")
    assert client.get("/api/credentials").json() == {"set": True, "source": "env"}
    monkeypatch.delenv("POKERNOW_NPT")
