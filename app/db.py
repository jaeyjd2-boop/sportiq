from sqlalchemy import create_engine, event
from app.config import settings

engine = create_engine(settings.normalized_database_url, pool_pre_ping=True, pool_size=5, max_overflow=10)

if engine.dialect.name == "sqlite":
    # Local/test runs only (production is PostgreSQL): the collector's SQL uses NOW().
    @event.listens_for(engine, "connect")
    def _sqlite_now(dbapi_conn, _record):
        from datetime import datetime, timezone
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat(sep=" "))
