import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine, text

log = logging.getLogger(__name__)

MIGRATION_LOCK_ID = 7_340_001


def make_engine(database_url: str, pool_size: int = 5) -> Engine:
    return create_engine(database_url, pool_size=pool_size, pool_pre_ping=True, future=True)


def run_migrations(engine: Engine, migrations_dir: Path) -> list[str]:
    """Apply pending *.sql files in lexical order; safe to call from several processes."""
    applied: list[str] = []
    files = sorted(migrations_dir.glob("*.sql"))
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS sdd_schema_migrations ("
                " version VARCHAR(255) PRIMARY KEY,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        done = {row[0] for row in conn.execute(text("SELECT version FROM sdd_schema_migrations"))}
        for path in files:
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
            conn.execute(
                text("INSERT INTO sdd_schema_migrations (version) VALUES (:v)"), {"v": path.name}
            )
            applied.append(path.name)
    return applied
