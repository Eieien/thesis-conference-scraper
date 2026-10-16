from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

# Explicit constraint names so Alembic batch migrations on SQLite stay predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


settings = get_settings()
settings.ensure_dirs()

_is_sqlite = settings.database_url.startswith("sqlite")
if _is_sqlite:
    # timeout: wait up to a minute for another writer (parallel collects) instead of failing
    # with "database is locked" after the default 5 s.
    _connect_args = {"check_same_thread": False, "timeout": 60}
elif settings.database_url.startswith("postgresql"):
    # prepare_threshold=None: Supabase's pooler can't keep server-side prepared statements.
    _connect_args = {"connect_timeout": 20, "prepare_threshold": None}
else:
    _connect_args = {}
engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=not _is_sqlite,  # the pooler drops idle connections
    execution_options=(
        {"schema_translate_map": {None: settings.database_schema}}
        if settings.database_schema
        else {}
    ),
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db
