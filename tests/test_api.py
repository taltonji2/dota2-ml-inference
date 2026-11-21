import pytest
from fastapi.testclient import TestClient

from app.main import app
from dota import db
from dota.ingest import store_match

from conftest import HERO_NAMES, make_match


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "api.sqlite"
    c = db.connect(path)
    db.init_db(c)
    store_match(c, make_match(), avg_rank_tier=64, hero_names=HERO_NAMES)
    c.close()
    monkeypatch.setenv("DOTA_DB", str(path))
    return TestClient(app)


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Dota 2 Momentum" in r.text


def test_match_endpoints(client):
    assert client.get("/matches").json()[0]["match_id"] == 1001
    m = client.get("/matches/1001").json()
    assert m["has_time_series"] and len(m["players"]) == 10
    assert client.get("/matches/999").status_code == 404


def test_momentum_and_turning_points(client):
    mo = client.get("/matches/1001/players/0/momentum").json()
    assert mo["opponent_slot"] == 128 and mo["series"]
    tp = client.get("/matches/1001/turning-points").json()
    assert tp["turning_points"]


def test_predict_returns_503_until_model_exists(client):
    assert client.post("/predict/bracket", json={"match_id": 1001, "player_slot": 0}).status_code == 503


def test_missing_db_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTA_DB", str(tmp_path / "nope.sqlite"))
    assert TestClient(app).get("/matches").status_code == 503


def test_concurrent_requests_do_not_share_thread_state(client):
    from concurrent.futures import ThreadPoolExecutor

    urls = ["/matches/1001/players/0/momentum", "/matches/1001/turning-points"] * 5
    with ThreadPoolExecutor(4) as ex:
        codes = list(ex.map(lambda u: client.get(u).status_code, urls))
    assert codes == [200] * len(urls)
