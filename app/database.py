import asyncio
from pathlib import Path
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.models import Base


class Database:
    def __init__(self, settings):
        self.settings = settings
        options = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            filename = settings.database_url.removeprefix("sqlite:///")
            if filename != ":memory:":
                Path(filename).parent.mkdir(parents=True, exist_ok=True)
            else:
                options["poolclass"] = StaticPool
            options["connect_args"] = {"check_same_thread": False, "timeout": 20}
        else:
            options.update(pool_size=settings.db_concurrency, max_overflow=0, pool_timeout=5,
                           connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000 -c lock_timeout=5000"})
        self.engine = create_engine(settings.database_url, **options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.slots = asyncio.Semaphore(settings.db_concurrency)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def pragmas(connection, record):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

    async def call(self, function, *args, **kwargs):
        async with self.slots:
            task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not free a database slot while its synchronous transaction still runs.
                try:
                    await task
                finally:
                    raise

    def ping(self):
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def close(self):
        self.engine.dispose()

    def migrate(self):
        """Versioned initial migration. Run once per release, not from production replicas."""
        with self.engine.begin() as connection:
            postgres = self.engine.dialect.name == "postgresql"
            if postgres:
                connection.execute(text("SELECT pg_advisory_xact_lock(910112026)"))
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"))
            version = connection.scalar(text("SELECT max(version) FROM schema_migrations")) or 0
            if version > 1:
                raise RuntimeError("The database schema is newer than this application")
            if version == 0:
                Base.metadata.create_all(connection)
                if postgres:
                    connection.execute(text("ALTER TABLE chunks ADD COLUMN search_vector tsvector "
                        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"))
                    connection.execute(text("CREATE INDEX ix_chunks_fts ON chunks USING gin(search_vector)"))
                    connection.execute(text("CREATE INDEX ix_chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"))
                connection.execute(text("INSERT INTO schema_migrations(version) VALUES (1)"))

    def check_schema(self):
        with self.engine.connect() as connection:
            if connection.scalar(text("SELECT max(version) FROM schema_migrations")) != 1:
                raise RuntimeError("Run python -m app.cli migrate before starting the service")
