"""
Tradovate OAuth callback handler.

Flow:
  1. Frontend redirects user to Tradovate OAuth authorize URL
  2. User logs in on trader.tradovate.com and authorizes
  3. Tradovate redirects to this callback with ?code=...&state=...
  4. We exchange the code for an access token
  5. Fetch account list using the token
  6. Redirect browser to frontend with accounts + encrypted token
"""
import json
import base64
import logging
import urllib.parse
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.services.credentials import encrypt_credentials, decrypt_credentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oauth", tags=["oauth"])

TRADOVATE_AUTH_URL = "https://trader.tradovate.com/oauth"
TRADOVATE_TOKEN_URLS = {
    "live": "https://live.tradovateapi.com/auth/oauthtoken",
    "demo": "https://demo.tradovateapi.com/auth/oauthtoken",
}
TRADOVATE_API_URLS = {
    "live": "https://live.tradovateapi.com/v1",
    "demo": "https://demo.tradovateapi.com/v1",
}


@router.get("/tradovate/callback")
async def tradovate_oauth_callback(
    code: str = Query(...),
    state: str = Query(""),
):
    """
    Handle Tradovate OAuth redirect. Exchange code for token,
    fetch accounts, redirect to frontend with data.
    """
    settings = get_settings()

    # Decrypt state to recover env and tenant context
    env = "live"
    try:
        if state:
            state_data = decrypt_credentials(urllib.parse.unquote(state))
            env = state_data.get("env", "live")
    except Exception:
        logger.warning("Could not decrypt OAuth state — defaulting to live")

    token_url = TRADOVATE_TOKEN_URLS[env]
    api_base = TRADOVATE_API_URLS[env]

    # Exchange authorization code for access token
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.tradovate_oauth_client_id,
                    "client_secret": settings.tradovate_oauth_client_secret,
                    "redirect_uri": settings.tradovate_oauth_redirect_uri,
                },
            )
            resp.raise_for_status()
            token_data = resp.json()
            logger.info(f"Tradovate OAuth token response keys: {list(token_data.keys())}")

            if "errorText" in token_data:
                logger.error(f"Tradovate token exchange error: {token_data['errorText']}")
                return RedirectResponse(
                    f"/broker-accounts?oauth_error={urllib.parse.quote(token_data['errorText'])}"
                )

            access_token = token_data.get("accessToken") or token_data.get("access_token")
            if not access_token:
                logger.error(f"No access token in response: {list(token_data.keys())}")
                return RedirectResponse(
                    f"/broker-accounts?oauth_error={urllib.parse.quote('No access token returned')}"
                )

            # Fetch account list
            acct_resp = await client.get(
                f"{api_base}/account/list",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            acct_resp.raise_for_status()
            accounts = acct_resp.json()
            logger.info(f"Tradovate OAuth account/list returned {len(accounts)} accounts: {accounts}")

    except httpx.HTTPStatusError as e:
        msg = f"Tradovate API error: {e.response.status_code}"
        logger.error(f"OAuth token exchange failed: {e.response.text}")
        return RedirectResponse(f"/broker-accounts?oauth_error={urllib.parse.quote(msg)}")
    except Exception as e:
        logger.exception("OAuth callback error")
        return RedirectResponse(
            f"/broker-accounts?oauth_error={urllib.parse.quote(str(e))}"
        )

    # Encrypt credentials for safe transport in URL
    encrypted_token = encrypt_credentials({
        "access_token": access_token,
        "base_url": api_base,
        "auth_method": "oauth",
    })

    # Base64-encode account list (no secrets)
    accounts_json = json.dumps([
        {"name": a["name"], "id": a["id"], "nickname": a.get("nickname")}
        for a in accounts
    ])
    accounts_b64 = base64.urlsafe_b64encode(accounts_json.encode()).decode()

    redirect_url = (
        f"/broker-accounts"
        f"?oauth=tradovate"
        f"&env={env}"
        f"&accounts={accounts_b64}"
        f"&token={urllib.parse.quote(encrypted_token)}"
    )

    return RedirectResponse(redirect_url)
