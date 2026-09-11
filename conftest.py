import pytest
from fastapi.testclient import TestClient
from app.api import create_app
from app.config import Settings
from app.database import Database
from app.repository import Repository
from app.providers import MockProvider
from app.schemas import Principal
from app.security import mint_demo_token


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, environment="test", database_url=f"sqlite:///{tmp_path}/test.db",
        provider="mock", redis_url="", orchestrator="native", jwt_secret="test-only-" + "x" * 48,
        jwt_algorithm="HS256", jwt_public_key="", auto_migrate=True, rate_limit_per_minute=1000)


@pytest.fixture
def principal():
    return Principal(tenant_id="acme", user_id="demo", roles=frozenset({"user", "editor", "admin", "operator"}))


@pytest.fixture
def db(settings):
    value = Database(settings); value.migrate()
    yield value
    value.close()


@pytest.fixture
def repo(db):
    return Repository(db)


@pytest.fixture
def provider(settings):
    return MockProvider(settings)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), raise_server_exceptions=False) as value:
        yield value


@pytest.fixture
def auth(settings):
    def make(user="demo", tenant="acme", roles=None):
        value = mint_demo_token(settings, tenant, user, roles if roles is not None else ["user", "editor", "admin", "operator"])
        return {"Authorization": "Bearer " + value}
    return make
