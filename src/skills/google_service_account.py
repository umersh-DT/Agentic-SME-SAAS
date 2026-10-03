import asyncio
import json
import logging
import os
from typing import Dict, Optional, Sequence

logger = logging.getLogger("google_service_account")

DEFAULT_KEY_PATH = "config/google-service-account.json"


class GoogleNotConfigured(Exception):
    pass


def service_account_key_path() -> str:
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or DEFAULT_KEY_PATH


def service_account_email() -> Optional[str]:
    """The robot account's email address that owners share their calendar / Search Console with."""
    try:
        with open(service_account_key_path(), encoding="utf-8") as f:
            return json.load(f).get("client_email")
    except (OSError, ValueError):
        return None


def _fetch_token_sync(scopes: Sequence[str]) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    path = service_account_key_path()
    if not os.path.exists(path):
        raise GoogleNotConfigured(f"Google service account key not found at {path}.")
    credentials = service_account.Credentials.from_service_account_file(path, scopes=list(scopes))
    credentials.refresh(Request())
    return credentials.token


async def google_auth_headers(scopes: Sequence[str]) -> Dict[str, str]:
    """Bearer header for Google APIs, signed with the service account key."""
    token = await asyncio.to_thread(_fetch_token_sync, tuple(scopes))
    return {"Authorization": f"Bearer {token}"}
