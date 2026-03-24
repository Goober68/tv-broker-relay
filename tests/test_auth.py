"""
Tests for the auth system:
  - Registration (validation, duplicate email)
  - Login (success, wrong password, inactive account)
  - JWT access token (valid, expired, wrong type)
  - Refresh token rotation (success, replay detection, expired)
  - Logout (single session, all sessions)
  - Password change
  - /auth/me
  - get_current_tenant dependency
"""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from jose import jwt

from app.config import get_settings


# ── Registration ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_success(client):
    resp = await client.post("/auth/register", json={
        "email": "alice@example.com",
        "password": "secure123",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "alice@example.com"
    assert data["is_admin"] is False
    assert data["email_verified"] is False
    assert "password" not in data
    assert "password_hash" not in data


@pytest.mark.asyncio
async def test_register_email_normalised(client):
    resp = await client.post("/auth/register", json={
        "email": "  ALICE@EXAMPLE.COM  ",
        "password": "secure123",
    })
    assert resp.status_code == 201
    assert resp.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    payload = {"email": "bob@example.com", "password": "secure123"}
    await client.post("/auth/register", json=payload)
    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 409
    assert "already registered" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_register_invalid_email(client):
    resp = await client.post("/auth/register", json={
        "email": "not-an-email",
        "password": "secure123",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_password_too_short(client):
    resp = await client.post("/auth/register", json={
        "email": "carol@example.com",
        "password": "abc",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_password_no_digit(client):
    resp = await client.post("/auth/register", json={
        "email": "carol@example.com",
        "password": "nodigitshere",
    })
    assert resp.status_code == 422


# ── Login ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_success(client, registered_tenant):
    resp = await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0
    # Refresh token should be in HttpOnly cookie
    assert "refresh_token" in resp.cookies


@pytest.mark.asyncio
async def test_login_wrong_password(client, registered_tenant):
    resp = await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": "wrongpassword1",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_email(client):
    resp = await client.post("/auth/login", json={
        "email": "ghost@example.com",
        "password": "password123",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_inactive_account(client, db_session, registered_tenant):
    from app.services.auth import get_tenant_by_email
    tenant = await get_tenant_by_email(db_session, registered_tenant["email"])
    tenant.is_active = False
    await db_session.commit()

    resp = await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    assert resp.status_code == 401


# ── Access Token ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_access_token_is_valid_jwt(client, registered_tenant):
    resp = await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    token = resp.json()["access_token"]
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["sub"] == str(registered_tenant["id"])
    assert payload["type"] == "access"
    assert payload["is_admin"] is False


@pytest.mark.asyncio
async def test_expired_token_rejected(client, registered_tenant):
    settings = get_settings()
    # Manually craft an expired token
    payload = {
        "sub": str(registered_tenant["id"]),
        "is_admin": False,
        "type": "access",
        "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        "iat": datetime.now(timezone.utc) - timedelta(minutes=20),
    }
    expired_token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_tampered_token_rejected(client):
    resp = await client.get("/auth/me", headers={"Authorization": "Bearer not.a.real.jwt"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_missing_token_rejected(client):
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


# ── /auth/me ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_me(client, auth_headers, registered_tenant):
    resp = await client.get("/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == registered_tenant["email"]
    assert resp.json()["id"] == registered_tenant["id"]


# ── Refresh Token Rotation ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_issues_new_access_token(client, registered_tenant):
    login = await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    # Cookie is automatically sent by httpx in follow-up requests
    resp = await client.post("/auth/refresh")
    assert resp.status_code == 200
    assert "access_token" in resp.json()
    # New refresh cookie should be set
    assert "refresh_token" in resp.cookies


@pytest.mark.asyncio
async def test_refresh_rotates_cookie(client, registered_tenant):
    await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    old_cookie = client.cookies.get("refresh_token")
    await client.post("/auth/refresh")
    new_cookie = client.cookies.get("refresh_token")
    assert old_cookie != new_cookie


@pytest.mark.asyncio
async def test_refresh_without_cookie_rejected(client):
    resp = await client.post("/auth/refresh")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_replay_attack_revokes_all_tokens(client, registered_tenant):
    """
    Presenting a refresh token that's already been rotated (revoked) should
    trigger family invalidation — all tokens for this tenant are revoked.
    """
    await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    old_cookie = client.cookies.get("refresh_token")

    # Rotate once (valid)
    await client.post("/auth/refresh")

    # Present the old (now revoked) token — replay attack
    client.cookies.set("refresh_token", old_cookie)
    resp = await client.post("/auth/refresh")
    assert resp.status_code == 401

    # All tokens should now be invalid — even a legitimate new one
    resp2 = await client.post("/auth/refresh")
    assert resp2.status_code == 401


# ── Logout ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_logout_clears_cookie(client, registered_tenant):
    await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    resp = await client.post("/auth/logout")
    assert resp.status_code == 204
    # Cookie should be cleared
    assert client.cookies.get("refresh_token") in (None, "")


@pytest.mark.asyncio
async def test_logout_invalidates_refresh_token(client, registered_tenant):
    await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    cookie = client.cookies.get("refresh_token")
    await client.post("/auth/logout")

    # Try to use the revoked token
    client.cookies.set("refresh_token", cookie)
    resp = await client.post("/auth/refresh")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_all_requires_auth(client):
    resp = await client.post("/auth/logout-all")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_all_revokes_all_sessions(client, registered_tenant):
    # Create two sessions
    for _ in range(2):
        await client.post("/auth/login", json={
            "email": registered_tenant["email"],
            "password": registered_tenant["password"],
        })

    login = await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/auth/logout-all", headers=headers)
    assert resp.status_code == 204

    # Refresh should now fail for all sessions
    resp2 = await client.post("/auth/refresh")
    assert resp2.status_code == 401


# ── Password Change ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_password_success(client, auth_headers, registered_tenant):
    resp = await client.put("/auth/me/password", headers=auth_headers, json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_change_password_wrong_current(client, auth_headers, registered_tenant):
    resp = await client.put("/auth/me/password", headers=auth_headers, json={
        "email": registered_tenant["email"],
        "password": "wrongpassword9",
    })
    assert resp.status_code == 400


# ── Token Type Confusion ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_token_cannot_be_used_as_access_token(client, registered_tenant):
    """
    The refresh token is opaque and stored in a cookie — it's not a JWT and
    cannot be used in an Authorization header.
    """
    await client.post("/auth/login", json={
        "email": registered_tenant["email"],
        "password": registered_tenant["password"],
    })
    raw_refresh = client.cookies.get("refresh_token")
    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {raw_refresh}"})
    assert resp.status_code == 401


# ── Auto-created Free Subscription ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_creates_free_subscription(client):
    """Billing subscription must exist immediately after registration."""
    resp = await client.post("/auth/register", json={
        "email": "newsub@example.com",
        "password": "password123",
    })
    assert resp.status_code == 201
    tenant_id = resp.json()["id"]

    # Log in and check /billing/subscription — must not 500
    login = await client.post("/auth/login", json={
        "email": "newsub@example.com", "password": "password123",
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    sub_resp = await client.get("/billing/subscription", headers=headers)
    assert sub_resp.status_code == 200
    data = sub_resp.json()
    assert data["plan"]["name"] == "free"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_get_me_includes_plan_name(client, auth_headers):
    resp = await client.get("/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "plan_name" in data
    assert data["plan_name"] == "free"


@pytest.mark.asyncio
async def test_billing_subscription_available_immediately_after_register(client):
    """
    Regression: /billing/subscription must not fail for a brand-new tenant
    who has never fired a webhook.
    """
    await client.post("/auth/register", json={
        "email": "immediate@example.com", "password": "password123",
    })
    login = await client.post("/auth/login", json={
        "email": "immediate@example.com", "password": "password123",
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Should not raise 500 or 404
    resp = await client.get("/billing/subscription", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["orders_this_period"] == 0
    assert resp.json()["orders_remaining"] == 50  # free plan limit


@pytest.mark.asyncio
async def test_get_me_backfills_subscription_for_existing_tenant(client, db_session):
    """
    Tenants who registered before auto-create was added should get a
    subscription created on the next /auth/me call.
    """
    from app.models.tenant import Tenant as TenantModel
    from app.services.auth import hash_password
    from app.services.plans import get_subscription

    # Manually create a tenant with no subscription (simulates pre-migration state)
    orphan = TenantModel(
        email="orphan@example.com",
        password_hash=hash_password("password123"),
    )
    db_session.add(orphan)
    await db_session.commit()

    # Verify no subscription yet
    sub = await get_subscription(db_session, orphan.id)
    assert sub is None

    # Log in and call /auth/me
    login = await client.post("/auth/login", json={
        "email": "orphan@example.com", "password": "password123",
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me_resp = await client.get("/auth/me", headers=headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["plan_name"] == "free"

    # Subscription should now exist
    await db_session.refresh(orphan)
    sub = await get_subscription(db_session, orphan.id)
    assert sub is not None
    assert sub.status == "active"
