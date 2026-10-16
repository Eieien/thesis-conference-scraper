"""Write the data as a MariaDB/MySQL .sql.gz file, for phpMyAdmin's Import tab.

    uv run python -m scripts.export_mysql            # -> data/exports/conference_data.sql.gz

The file drops and recreates the tables, so importing it again replaces the old copy.
Tables: conferences, fees, raw_listings, alembic_version and an empty scrape_runs (the run
log stays local). Left out: fetched_pages (it only points at page files in data/cache here).

The EDAS account holder's details are removed: EDAS pages were read while logged in, so their
text carries the account's email, EDAS person ID, IP address and a session token.
"""

import gzip
import json
import re
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import Float, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateIndex, CreateTable

# Also works as `python scripts/export_mysql.py` (e.g. the editor's Run button), from any folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import models  # noqa: E402, F401  (registers the tables)
from app.config import DATA_DIR, get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.pipeline.privacy import AccountScrubber  # noqa: E402

TABLES = ["conferences", "fees", "raw_listings", "scrape_runs", "alembic_version"]
# Created empty: the run log stays local, but /runs should answer [] instead of failing.
EMPTY = {"scrape_runs"}
BATCH_BYTES = 500_000  # one INSERT stays well under MariaDB's max_allowed_packet


@compiles(Float, "mysql")
def _double(_type, _compiler, **_kw):
    # MySQL's FLOAT keeps ~7 digits: IDR 12,500,000 or 37,499.88 PHP would lose cents.
    return "DOUBLE"


@compiles(Text, "mysql")
def _mediumtext(_type, _compiler, **_kw):
    # TEXT stops at 64 KB; long CFP texts would be refused in strict mode.
    return "MEDIUMTEXT"


def _account_scrubber(db: sqlite3.Connection):
    """Removes the EDAS account's email, person ID, IP and session token (app/pipeline/privacy)."""
    scrubber = AccountScrubber(
        extra for (extra,) in db.execute("select extra from raw_listings where source = 'edas'")
    )
    return scrubber, scrubber.ids, scrubber.emails


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return repr(value)
    s = str(value)
    s = (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\0", "\\0")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x1a", "\\Z")
    )
    return f"'{s}'"


_DATETIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?$"
)


def _clean(value, scrub):
    if isinstance(value, str):
        if m := _DATETIME.match(value):
            return f"{m[1]} {m[2]}"  # DATETIME: no fraction, no offset (all UTC)
        return scrub(value)
    return value


def export(out: Path) -> dict[str, int]:
    db = sqlite3.connect(get_settings().database_url.removeprefix("sqlite:///"))
    scrub, ids, emails = _account_scrubber(db)
    dialect = mysql.dialect()
    counts: dict[str, int] = {}
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8", newline="\n") as f:
        f.write("-- Conference scraper data for MariaDB/MySQL. Import in phpMyAdmin.\n")
        f.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS = 0;\n\n")
        for name in reversed(TABLES):
            f.write(f"DROP TABLE IF EXISTS `{name}`;\n")
        for name in TABLES:
            if name == "alembic_version":
                f.write(
                    "\nCREATE TABLE `alembic_version` (version_num VARCHAR(32) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                    " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n"
                )
                cols = ["version_num"]
            else:
                table = Base.metadata.tables[name]
                ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
                if name == "raw_listings":
                    # MariaDB compares text case-insensitively; EasyChair has both
                    # "icai2026" and "ICAI2026", which would break the unique key.
                    col = "source_id VARCHAR(128) NOT NULL"
                    assert col in ddl
                    ddl = ddl.replace(col, "source_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL")
                f.write(f"\n{ddl} ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n")
                for index in table.indexes:
                    f.write(f"{str(CreateIndex(index).compile(dialect=dialect)).strip()};\n")
                cols = [c.name for c in table.columns]

            quoted = ", ".join(f"`{c}`" for c in cols)
            head = f"INSERT INTO `{name}` ({quoted}) VALUES\n"
            rows, size, n = [], 0, 0
            cur = db.execute(f"select {', '.join(chr(34) + c + chr(34) for c in cols)} from {name}")
            if name in EMPTY:
                cur = []
            for row in cur:
                values = "(" + ", ".join(_literal(_clean(v, scrub)) for v in row) + ")"
                rows.append(values)
                size += len(values)
                n += 1
                if size > BATCH_BYTES:
                    f.write(head + ",\n".join(rows) + ";\n")
                    rows, size = [], 0
            if rows:
                f.write(head + ",\n".join(rows) + ";\n")
            counts[name] = n
        f.write("\nSET FOREIGN_KEY_CHECKS = 1;\n")

    # Nothing of the account may be left in the file.
    with gzip.open(out, "rt", encoding="utf-8") as f:
        text = f.read()
    leaks = [s for s in [*ids, *emails] if s in text]
    if leaks:
        out.unlink()
        raise SystemExit(f"account details still in the export ({len(leaks)}); file removed")
    return counts


def main() -> None:
    default = DATA_DIR / "exports" / "conference_data.sql.gz"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    counts = export(out)
    print(json.dumps(counts))
    print(f"Saved {out.resolve()} ({out.stat().st_size / 1_048_576:.1f} MB)")


if __name__ == "__main__":
    main()
