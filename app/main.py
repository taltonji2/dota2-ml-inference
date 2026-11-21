"""FastAPI app: small read-only endpoints plus a static dashboard.

The API never calls OpenDota during a request. Data arrives through the
ingest CLI, so requests are fast and the app works offline.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dota import db
from dota.brackets import medal_name
from dota.momentum import player_momentum, turning_points

STATIC = Path(__file__).parent / "static"
MODEL_PATH = Path(os.environ.get("DOTA_MODEL", "ml/bracket_model.joblib"))

app = FastAPI(title="Dota 2 momentum and bracket inference", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def get_conn() -> Iterator[sqlite3.Connection]:
    path = os.environ.get("DOTA_DB", db.DEFAULT_DB)
    if not Path(path).exists():
        raise HTTPException(503, f"database not found at {path}; run the ingest first")
    conn = db.connect(path, read_only=os.environ.get("DOTA_DB_READONLY") == "1")
    try:
        yield conn
    finally:
        conn.close()


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_loaded": MODEL_PATH.exists()}


@app.get("/matches")
def list_matches(
    parsed_only: bool = True,
    limit: int = Query(50, le=500),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    where = "WHERE parsed_at IS NOT NULL" if parsed_only else ""
    rows = conn.execute(
        f"SELECT match_id, start_time, duration_s, bracket, radiant_win FROM matches {where} "
        "ORDER BY start_time DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


@app.get("/matches/{match_id}")
def get_match(match_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    m = conn.execute("SELECT * FROM matches WHERE match_id=?", (match_id,)).fetchone()
    if m is None:
        raise HTTPException(404, "match not ingested")
    players = [
        dict(r, medal=medal_name(r["rank_tier"]))
        for r in conn.execute(
            "SELECT player_slot, account_id, hero_id, is_radiant, lane, lane_role, rank_tier "
            "FROM match_players WHERE match_id=? ORDER BY player_slot",
            (match_id,),
        )
    ]
    return {**dict(m), "has_time_series": m["parsed_at"] is not None, "players": players}


@app.get("/matches/{match_id}/players/{slot}/momentum")
def get_momentum(
    match_id: int,
    slot: int,
    window: int = Query(3, ge=1, le=10),
    threshold: float = Query(0.15, gt=0, le=1),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    result = player_momentum(conn, match_id, slot, window=window, swing_threshold=threshold)
    if result is None:
        raise HTTPException(404, "no time series for this match and player")
    return result


@app.get("/matches/{match_id}/turning-points")
def get_turning_points(match_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    result = turning_points(conn, match_id)
    if not result["advantage"]:
        raise HTTPException(404, "no time series for this match")
    return result


class BracketRequest(BaseModel):
    match_id: int | None = None
    player_slot: int | None = None
    features: dict[str, float] | None = None


@app.post("/predict/bracket")
def predict_bracket(req: BracketRequest) -> dict:
    # The bracket model lands in the next PR, once the data spike confirms coverage.
    if not MODEL_PATH.exists():
        raise HTTPException(503, "bracket model not trained yet")
    raise HTTPException(501, "prediction not implemented yet")
