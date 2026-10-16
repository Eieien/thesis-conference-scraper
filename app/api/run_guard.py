"""Starting background runs safely: idempotent, and never two runs that would collide.

Every endpoint that starts a run goes through `start_run`:

- **Idempotency-Key header.** The same key always means the same run: asking again (a retry
  after a timeout, a double-click) returns the run the key already started, with status 200 and
  `Idempotent-Replayed: true`, instead of starting another. Reusing a key for a different request
  is an error (422).
- **The same run already going.** Without a key, a request identical to a run still in progress
  (same kind, source and parameters) returns that run instead of starting a duplicate.
- **Runs that collide.** Some runs must not overlap: enrich loads its conferences up front and
  overwrote the IEEE website lookup's work when both ran at once (352 conferences, 2026-10-11).
  Starting one while a conflicting run is going is refused with 409, naming the running one.
- **Runs left behind.** A server that stops mid-run leaves its runs "running" forever; startup
  marks them failed (`close_abandoned`) so they neither mislead nor block new runs.
"""

from datetime import UTC, datetime

from fastapi import Header, HTTPException, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import ScrapeRun

# Kinds that write the same rows and must not run at the same time. Symmetric (see below).
_PAIRS = {
    "enrich": {"enrich", "ieee_details", "recover", "validate", "attendance"},
    "ieee_details": {"ieee_details"},
    "recover": {"recover"},
    "validate": {"validate"},
    "history": {"history"},
    "attendance": {"attendance"},
}
ALL_KINDS = {"collect", "enrich", "ieee_details", "recover", "validate", "history", "attendance"}


def conflicts(kind: str) -> set[str]:
    """The kinds a run of `kind` must not overlap with."""
    if kind == "update":
        return ALL_KINDS | {"update"}  # the chain writes everything
    out = set(_PAIRS.get(kind, set())) | {"update"}
    out |= {k for k, v in _PAIRS.items() if kind in v}
    return out


def start_run(
    db: Session,
    response: Response,
    *,
    kind: str,
    source: str | None = None,
    params: dict,
    key: str | None,
) -> tuple[ScrapeRun, bool]:
    """The run to report and whether it is new (start its work only then)."""
    if key:
        existing = db.scalar(select(ScrapeRun).where(ScrapeRun.idempotency_key == key))
        if existing is not None:
            if (existing.kind, existing.source, existing.params) != (kind, source, params):
                raise HTTPException(
                    422, f"Idempotency-Key {key!r} was already used for a different request"
                )
            return _replay(response, existing)

    running = db.scalars(select(ScrapeRun).where(ScrapeRun.status == "running")).all()
    for run in running:
        if (run.kind, run.source, run.params) == (kind, source, params):
            return _replay(response, run)
    clash = conflicts(kind)
    for run in running:
        if kind == "collect" and run.kind == "collect":
            blocked = run.source == source  # different sites collect in parallel; one site, once
        else:
            blocked = run.kind in clash
        if blocked:
            what = f"{run.kind} {run.source}" if run.source else run.kind
            raise HTTPException(
                409,
                f"Run {run.id} ({what}) is still going and would collide with this one. "
                f"Wait for it to finish (GET /runs/{run.id}).",
            )

    run = ScrapeRun(kind=kind, source=source, params=params, idempotency_key=key or None)
    db.add(run)
    db.commit()
    response.status_code = 202
    return run, True


def _replay(response: Response, run: ScrapeRun) -> tuple[ScrapeRun, bool]:
    response.status_code = 200
    response.headers["Idempotent-Replayed"] = "true"
    return run, False


def close_abandoned(db: Session) -> int:
    """Mark runs a previous server process left 'running' as failed. Returns how many."""
    result = db.execute(
        update(ScrapeRun)
        .where(ScrapeRun.status == "running")
        .values(
            status="failed",
            error="The server stopped before this run finished; start it again.",
            finished_at=datetime.now(UTC),
        )
    )
    db.commit()
    return result.rowcount or 0


IDEMPOTENCY_KEY = Header(
    None,
    alias="Idempotency-Key",
    max_length=128,
    description="Any unique string. Sending the same key again returns the same run.",
)
