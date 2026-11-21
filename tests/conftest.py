import pytest

from dota import db
from dota.demo import HERO_NAMES, make_match  # noqa: F401  (re-exported for tests)
from dota.ingest import store_match


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.sqlite")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def loaded(conn):
    store_match(conn, make_match(), avg_rank_tier=64, hero_names=HERO_NAMES)
    return conn
