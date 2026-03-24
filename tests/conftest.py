import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

import os
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("OANDA_API_KEY", "test")
os.environ.setdefault("OANDA_ACCOUNT_ID", "test-account")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing-only")
# Valid Fernet key for tests
os.environ["CREDENTIAL_ENCRYPTION_KEY"] = "pMGOSKKNHJl0C7lm6kCFLUHb5fUBpakRSaGBvvS3vEQ="

from app.main import app
from app.models.db import Base, get_db

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        from app.models import order, position, tenant, api_key, broker_account  # noqa
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()


# ── Auth helpers ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def registered_tenant(client):
    resp = await client.post("/auth/register", json={
        "email": "test@example.com",
        "password": "password123",
    })
    assert resp.status_code == 201
    return {"email": "test@example.com", "password": "password123", "id": resp.json()["id"]}


@pytest_asyncio.fixture
async def auth_headers(client, registered_tenant):
    resp = await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api_key(client, auth_headers, registered_tenant):
    resp = await client.post(
        "/api-keys", json={"name": "Test Key"}, headers=auth_headers,
    )
    assert resp.status_code == 201
    return resp.json()["raw_key"], registered_tenant["id"]


@pytest_asyncio.fixture
async def oanda_broker_account(client, auth_headers):
    """Create an Oanda broker account for the test tenant."""
    resp = await client.post("/broker-accounts", json={
        "broker": "oanda",
        "account_alias": "primary",
        "display_name": "Test Oanda",
        "credentials": {
            "api_key": "test-oanda-key",
            "account_id": "101-001-test",
            "base_url": "https://api-fxpractice.oanda.com/v3",
        },
    }, headers=auth_headers)
    assert resp.status_code == 201
    return resp.json()
