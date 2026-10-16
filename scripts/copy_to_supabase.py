"""Copy the whole scraper database (every table, every row) into Supabase, in a private schema.

    uv run python -m scripts.copy_to_supabase            # to SUPABASE_DB_URL (.env)
    uv run python -m scripts.copy_to_supabase --local    # to the local PGlite (web/: npm run db)

The tables land in the schema `scraper`, next to (not instead of) the public site's tables that
scripts/sync_supabase.py writes to `public`. `scraper` is private: Supabase's API only serves
the schemas listed in its API settings (`public` by default), and on top of that the website's
roles (anon, authenticated) get no access to it and every table has row level security with no
policy. Only the database password (SUPABASE_DB_URL) can read it.

Each run replaces the copy completely, in one transaction: drop, create, copy. The EDAS account
holder's email, person ID, IP and session token are removed from every value on the way, as the
MariaDB export does (app/pipeline/privacy.py), and the run fails if any are left. Page bodies of
the fetch cache live in data/cache/ on disk, not in the database, so they are not copied.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, select, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.db import engine as source
from app.models import RawListing  # noqa: E402,F401  (registers every table on Base)
from app.pipeline.privacy import AccountScrubber, scrub_deep  # noqa: E402

SCHEMA = "scraper"
BATCH = 500
# Parents before children, so foreign keys are satisfied as rows arrive.
ORDER = ["scrape_runs", "fetched_pages", "conferences", "raw_listings", "fees"]


def _postgres(url: str):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1).replace(
        "postgres://", "postgresql+psycopg://", 1
    )
    # prepare_threshold=None: Supabase's pooler can't keep server-side prepared statements.
    return create_engine(url, connect_args={"connect_timeout": 20, "prepare_threshold": None})


def copy(url: str) -> dict[str, int]:
    tables = [Base.metadata.tables[name] for name in ORDER]
    assert set(ORDER) == set(Base.metadata.tables), "a new table: add it to ORDER"
    with source.connect() as src:
        # The account's ids and emails can sit in any of the EDAS texts, so read them all.
        scrub = AccountScrubber(
            s
            for (e,) in src.execute(select(RawListing.extra).where(RawListing.source == "edas"))
            for s in (e or {}).values()
            if isinstance(s, str)
        )
        version = src.execute(text("select version_num from alembic_version")).scalar()

    target = _postgres(url).execution_options(schema_translate_map={None: SCHEMA})
    counts: dict[str, int] = {}
    with target.begin() as dst, source.connect() as src:
        dst.execute(text(f"create schema if not exists {SCHEMA}"))
        Base.metadata.drop_all(dst, tables=tables)
        Base.metadata.create_all(dst, tables=tables)
        for table in tables:
            n = 0
            rows = src.execution_options(stream_results=True).execute(select(table))
            while batch := rows.fetchmany(BATCH):
                clean = [
                    {k: scrub_deep(v, scrub) for k, v in row._mapping.items()} for row in batch
                ]
                dst.execute(table.insert(), clean)
                n += len(clean)
            counts[table.name] = n
            # Row ids go on from where the copy ends, like in SQLite.
            if "id" in table.c and n:
                dst.execute(
                    text(
                        f"select setval(pg_get_serial_sequence('{SCHEMA}.{table.name}', 'id'), "
                        f"(select max(id) from {SCHEMA}.{table.name}))"
                    )
                )

        # Which migration the copy matches, as in SQLite.
        dst.execute(text(f"drop table if exists {SCHEMA}.alembic_version"))
        dst.execute(
            text(f"create table {SCHEMA}.alembic_version (version_num varchar(32) primary key)")
        )
        dst.execute(text(f"insert into {SCHEMA}.alembic_version values (:v)"), {"v": version})

        # Private: no access for the website's roles, and RLS with no policy as a second lock.
        for name in [*ORDER, "alembic_version"]:
            dst.execute(text(f"alter table {SCHEMA}.{name} enable row level security"))
        dst.execute(
            text(
                f"""do $$ begin
                  if exists (select 1 from pg_roles where rolname = 'anon') then
                    revoke all on all tables in schema {SCHEMA} from anon, authenticated;
                    revoke all on schema {SCHEMA} from anon, authenticated;
                  end if;
                end $$"""
            )
        )
        dst.execute(text(f"revoke all on schema {SCHEMA} from public"))

        # Nothing of the account may be left (checked inside the transaction: a leak undoes it all).
        leaks = 0
        for needle in [*scrub.ids, *scrub.emails]:
            for name in [*ORDER, "alembic_version"]:
                # A whole row as text: every column at once, JSON included.
                leaks += dst.execute(
                    text(f"select count(*) from {SCHEMA}.{name} t where t::text like :n"),
                    {"n": f"%{needle}%"},
                ).scalar()
        if leaks:
            raise SystemExit(f"EDAS account details left in {leaks} rows; nothing was copied")

        # Every row arrived.
        for table in tables:
            there = dst.execute(select(func.count()).select_from(table)).scalar()
            if there != counts[table.name]:
                raise SystemExit(f"{table.name}: {there} rows copied of {counts[table.name]}")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local", action="store_true", help="the local PGlite (LOCAL_DB_URL)")
    args = parser.parse_args()
    settings = get_settings()
    if args.local:
        url, where = settings.local_db_url, "the local database"
    elif settings.supabase_db_url:
        url, where = settings.supabase_db_url.get_secret_value(), "Supabase"
    else:
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    counts = copy(url)
    total = sum(counts.values())
    detail = ", ".join(f"{n:,} {t}" for t, n in counts.items())
    print(f"Copied {total:,} rows to {where}, schema '{SCHEMA}': {detail}")


if __name__ == "__main__":
    main()
