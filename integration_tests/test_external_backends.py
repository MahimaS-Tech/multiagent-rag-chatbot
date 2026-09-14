"""Opt-in real infrastructure tests. Use ONLY a disposable PostgreSQL database ending in _test."""
import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from app.database import Database
from app.repository import Repository
from app.schemas import DocumentInput, Principal
from app.text import hash_embedding
from app.models import Job

PG_URL = os.environ.get("TEST_POSTGRES_URL", "")
REDIS_URL = os.environ.get("TEST_REDIS_URL", "")


@pytest.fixture
def pg(settings):
    if not PG_URL:
        pytest.skip("TEST_POSTGRES_URL not configured; PostgreSQL not tested")
    pytest.importorskip("psycopg")
    url = make_url(PG_URL)
    if not (url.database or "").endswith("_test"):
        pytest.fail("Integration tests require a disposable database with a name ending in _test")
    schema = "rag_test_" + uuid4().hex
    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        conn.execute(CreateSchema(schema))
    settings.database_url, settings.auto_migrate = PG_URL, False
    db = Database(settings); db.engine.dispose()
    db.engine = create_engine(PG_URL, connect_args={"options": f"-csearch_path={schema},public -cstatement_timeout=10000"},
                              execution_options={"schema_translate_map": {None:schema}})
    db.sessions = sessionmaker(db.engine, expire_on_commit=False)
    try:
        db.migrate()
        yield db, Repository(db)
    finally:
        db.close()
        with admin.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.mark.integration
def test_postgres_real_vector_fts_and_tenant_acl(pg):
    db, repo = pg
    principal = Principal(tenant_id="acme", user_id="demo", roles=frozenset({"editor"}))
    body = "Customers can request a refund within 30 days of purchase."
    repo.submit_document(principal, DocumentInput(title="Refund", text=body, visibility="tenant"), repo.settings.embedding_space)
    job = repo.claim_job(); assert job
    assert repo.finish_job(job, [body], [hash_embedding(body)])
    result = repo.search(principal, "refund", hash_embedding("refund"), repo.settings.embedding_space, 10)
    assert result["vector"] and result["lexical"]
    other = Principal(tenant_id="other", user_id="demo")
    assert repo.search(other, "refund", hash_embedding("refund"), repo.settings.embedding_space, 10) == {"vector":[], "lexical":[]}


@pytest.mark.integration
def test_postgres_skip_locked_worker_claims(pg):
    db, repo = pg
    principal = Principal(tenant_id="acme", user_id="demo", roles=frozenset({"editor"}))
    for number in range(2):
        repo.submit_document(principal, DocumentInput(title=str(number), text="Example refund policy document."), repo.settings.embedding_space)
    with db.sessions.begin() as session:
        locked = session.scalar(select(Job).where(Job.state=="queued").order_by(Job.available_at, Job.id).limit(1).with_for_update())
        claimed = repo.claim_job()
        assert claimed is not None and claimed["id"] != locked.id


@pytest.mark.integration
async def test_real_redis_shared_limit_and_cache():
    if not REDIS_URL:
        pytest.skip("TEST_REDIS_URL not configured; Redis not tested")
    pytest.importorskip("redis")
    from app.controls import RedisControls
    from app.errors import AppError
    one, two = RedisControls(REDIS_URL, 2), RedisControls(REDIS_URL, 2)
    key = "test-" + uuid4().hex
    try:
        await one.rate(key); await two.rate(key)
        with pytest.raises(AppError, match="rate_limited"):
            await one.rate(key)
        await one.set(key, [{"id":"example", "score":.5}], 5)
        assert await two.get(key) == [{"id":"example", "score":.5}]
    finally:
        await one.close(); await two.close()


@pytest.mark.optional
def test_optional_langgraph_end_to_end(settings, auth):
    pytest.importorskip("langgraph")
    from fastapi.testclient import TestClient
    from app.api import create_app
    from tests.helpers import ingest, ask
    settings.orchestrator = "langgraph"
    with TestClient(create_app(settings)) as client:
        ingest(client, auth())
        response = ask(client, auth())
        assert response.status_code == 200 and response.json()["status"] == "answered"
        assert response.json()["agent_steps"] == ["planner", "retriever", "answerer", "verifier"]
