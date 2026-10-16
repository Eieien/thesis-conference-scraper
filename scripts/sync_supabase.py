"""Copy the public site's data to Supabase, or to the local database for development.

    uv run python -m scripts.sync_supabase              # push to SUPABASE_DB_URL (.env)
    uv run python -m scripts.sync_supabase --init       # first time: create tables and policies
    uv run python -m scripts.sync_supabase --local      # the local database (`npm run db` in web/)
    uv run python -m scripts.sync_supabase --json PATH  # write a snapshot file instead

After a push to Supabase the site is told to show the new data at once (SITE_URL and
REVALIDATE_SECRET in .env); without them it shows it within five minutes.

All tables are replaced in one transaction, so visitors see the old data until the new data is
complete. What is published, and what is left out, is decided in app/pipeline/publish.py.
"""

import argparse
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import BASE_DIR, get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.pipeline.publish import public_data  # noqa: E402

SCHEMA = BASE_DIR / "supabase" / "schema.sql"

CONFERENCE_JSON = {"standard_fee", "student_fee", "attendance", "history", "source_links"}
FEE_JSON = {"kind"}


def _json_default(value):
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value))


def _upsert(conn, table: str, rows: list[dict], json_columns: set[str]) -> tuple[int, int]:
    """Make `table` hold exactly `rows`: insert the new, update only the changed, delete the gone.
    Idempotent: a second run with the same data writes nothing. Returns (written, deleted)."""
    ids = [r["id"] for r in rows]
    deleted = conn.execute(
        text(f"delete from public.{table} where not (id = any(:ids))"), {"ids": ids}
    ).rowcount
    if not rows:
        return 0, deleted
    columns = list(rows[0])
    values = ", ".join(f"cast(:{c} as jsonb)" if c in json_columns else f":{c}" for c in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "id")
    stmt = text(
        f"insert into public.{table} ({', '.join(columns)}) values ({values}) "
        f"on conflict (id) do update set {updates} "
        # Unchanged rows are left alone: no write, no new row version, nothing for readers to see.
        f"where (public.{table}.*) is distinct from (excluded.*)"
    )
    # None stays SQL NULL: json.dumps(None) would store a JSON null, which `is null` misses.
    params = [
        {
            c: json.dumps(r[c], default=_json_default)
            if c in json_columns and r[c] is not None
            else r[c]
            for c in columns
        }
        for r in rows
    ]
    written = conn.execute(stmt, params).rowcount
    return max(written, 0), deleted


def push(data: dict, url: str, *, init: bool) -> dict[str, int]:
    """Bring Supabase in line with `data`, in one transaction. Returns what changed."""
    # Supabase gives postgresql://...; SQLAlchemy needs the driver named.
    url = url.replace("postgresql://", "postgresql+psycopg://", 1).replace(
        "postgres://", "postgresql+psycopg://", 1
    )
    # prepare_threshold=None: no server-side prepared statements. Supabase's pooler (pgbouncer)
    # can hand each statement to another backend, where a prepared name is missing or taken.
    engine = create_engine(url, connect_args={"connect_timeout": 20, "prepare_threshold": None})
    with engine.begin() as conn:  # one transaction: all or nothing
        if init:
            conn.exec_driver_sql(SCHEMA.read_text(encoding="utf-8"))
        # Fees first for deletes (their conference may be going), conferences first for writes.
        fee_ids = [f["id"] for f in data["fees"]]
        fees_deleted = conn.execute(
            text("delete from public.fees where not (id = any(:ids))"), {"ids": fee_ids}
        ).rowcount
        conf_written, conf_deleted = _upsert(
            conn, "conferences", data["conferences"], CONFERENCE_JSON
        )
        fees_written, _ = _upsert(conn, "fees", data["fees"], FEE_JSON)
        conn.execute(
            text(
                "insert into public.site_meta (id, data) values (1, cast(:d as jsonb)) "
                "on conflict (id) do update set data = excluded.data"
            ),
            {"d": json.dumps(data["meta"])},
        )
    return {
        "conferences written": conf_written,
        "conferences deleted": conf_deleted,
        "fees written": fees_written,
        "fees deleted": fees_deleted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--init", action="store_true", help="create tables and policies first")
    parser.add_argument("--json", type=Path, help="write a JSON snapshot instead of pushing")
    parser.add_argument(
        "--local", action="store_true", help="push to the local database (LOCAL_DB_URL)"
    )
    args = parser.parse_args()

    settings = get_settings()
    with SessionLocal() as db:
        data = public_data(db, settings, settings.public_continents)
    meta = data["meta"]
    summary = f"{meta['conferences']} conferences ({meta['conferences_with_fees']} with fees), "
    summary += f"{len(data['fees'])} fees, {', '.join(meta['continents'])}"

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(data, default=_json_default), encoding="utf-8")
        print(f"Wrote {summary} to {args.json.resolve()}")
        return
    if args.local:
        changed = push(data, settings.local_db_url, init=args.init)
        print(f"Synced {summary} to the local database; changed: {json.dumps(changed)}")
        return
    if not settings.supabase_db_url:
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    changed = push(data, settings.supabase_db_url.get_secret_value(), init=args.init)
    print(f"Synced {summary} to Supabase; changed: {json.dumps(changed)}")
    print(revalidate(settings))


def revalidate(settings) -> str:
    """Ask the deployed site to drop its cached answers, so the new data shows at once."""
    if not settings.site_url or not settings.revalidate_secret:
        return "The site shows the new data within five minutes (SITE_URL not set)."
    request = urllib.request.Request(
        settings.site_url.rstrip("/") + "/api/revalidate",
        method="POST",
        headers={"Authorization": f"Bearer {settings.revalidate_secret.get_secret_value()}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as r:
            return f"The site shows the new data now (revalidate answered {r.status})."
    except Exception as e:  # the data is published either way; only the refresh is late
        return f"Couldn't tell the site to refresh ({e}); it shows the new data within 5 min."


if __name__ == "__main__":
    main()
