"""Starting runs: idempotency keys, duplicate runs and runs that collide (app/api/run_guard.py)."""

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.run_guard import close_abandoned, start_run
from app.db import Base
from app.models import ScrapeRun


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session


def start(db, kind, params=None, source=None, key=None):
    response = Response()
    run, new = start_run(db, response, kind=kind, source=source, params=params or {}, key=key)
    return run, new, response


def test_the_same_key_returns_the_same_run(db):
    first, new, r1 = start(db, "enrich", {"continent": "Asia"}, key="abc")
    assert new and r1.status_code == 202
    first.status = "succeeded"  # even after it finished
    db.commit()
    again, new, r2 = start(db, "enrich", {"continent": "Asia"}, key="abc")
    assert again.id == first.id and not new
    assert r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true"


def test_a_key_reused_for_another_request_is_refused(db):
    start(db, "enrich", {"continent": "Asia"}, key="abc")
    with pytest.raises(HTTPException) as e:
        start(db, "enrich", {"continent": "Europe"}, key="abc")
    assert e.value.status_code == 422


def test_a_double_click_returns_the_running_run(db):
    first, _, _ = start(db, "history", {"continent": "Asia"})
    again, new, response = start(db, "history", {"continent": "Asia"})
    assert again.id == first.id and not new and response.status_code == 200


def test_runs_that_collide_are_refused(db):
    lookup, _, _ = start(db, "ieee_details", {"batch_size": 60}, source="ieee")
    with pytest.raises(HTTPException) as e:
        start(db, "enrich", {"continent": "Europe"})  # the 352-conference bug of 2026-10-11
    assert e.value.status_code == 409 and f"Run {lookup.id}" in e.value.detail
    # Unrelated work may still run alongside.
    _, new, _ = start(db, "history", {"continent": "Europe"})
    assert new


def test_sources_collect_in_parallel_but_one_site_once(db):
    start(db, "collect", {"max_pages": None}, source="wikicfp")
    _, new, _ = start(db, "collect", {"max_pages": None}, source="easychair")
    assert new
    with pytest.raises(HTTPException):
        start(db, "collect", {"max_pages": 3}, source="wikicfp")
    with pytest.raises(HTTPException):
        start(db, "update", {})  # the chain collects too


def test_runs_left_running_by_a_dead_server_are_closed(db):
    run, _, _ = start(db, "enrich", {})
    assert close_abandoned(db) == 1
    db.refresh(run)
    assert run.status == "failed" and "stopped" in run.error
    _, new, _ = start(db, "enrich", {})  # no longer blocks
    assert new
    assert db.query(ScrapeRun).count() == 2
