import json
import logging
import os
import threading
import time

import requests

from config import settings

logger = logging.getLogger("zoho")

ZOHO_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
ZOHO_API_BASE = "https://www.zohoapis.in/books/v3"

# 5 workers keeps concurrent Zoho calls at ~10 (resolve + pdf per worker),
# which stays within the typical Books API rate limit (~60 req/min on free plans).
MAX_WORKERS = 5

# On 429, retry up to this many times with exponential backoff: 2s, 4s, 8s
MAX_RETRIES = 4
BACKOFF_BASE = 2.0  # seconds
# If Zoho's Retry-After exceeds this, it's an hourly quota — fail fast instead of hanging.
MAX_WAIT_SECS = 30

# Access tokens are cached to disk so short-lived one-off processes (scripts, CLI runs)
# reuse the same token instead of each burning its own refresh call — repeated refreshes
# in a short window trip Zoho's "too many requests continuously" block (confirmed live).
_TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), "zoho_token_cache.json")
_EXPIRY_BUFFER_SECS = 300  # refresh 5 min before actual expiry, not at the wire


class ZohoTokenManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0
        if not self._load_cached_token():
            self.force_refresh()

    def _load_cached_token(self) -> bool:
        if not os.path.exists(_TOKEN_CACHE_FILE):
            return False
        with open(_TOKEN_CACHE_FILE) as f:
            data = json.load(f)
        if data.get("expires_at", 0) - _EXPIRY_BUFFER_SECS <= time.time():
            return False
        self._access_token = data["access_token"]
        self._expires_at = data["expires_at"]
        logger.info("Reusing cached OAuth token (valid for another %.0fs)", self._expires_at - time.time())
        return True

    def _save_cached_token(self) -> None:
        with open(_TOKEN_CACHE_FILE, "w") as f:
            json.dump({"access_token": self._access_token, "expires_at": self._expires_at}, f)

    def get_token(self) -> str:
        # ponytail: no cross-process lock on the refresh-check race, fine for this
        # single-tenant local use — add one if this ever runs as multiple processes.
        if time.time() >= self._expires_at - _EXPIRY_BUFFER_SECS:
            self.force_refresh()
        return self._access_token

    def force_refresh(self) -> None:
        with self._lock:
            logger.info("Refreshing OAuth token → %s", ZOHO_TOKEN_URL)
            resp = requests.post(
                ZOHO_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": settings.refresh_token,
                    "client_id": settings.client_id,
                    "client_secret": settings.client_secret,
                },
                timeout=15,
            )
            if not resp.ok:
                logger.error("Token refresh HTTP %s: %s", resp.status_code, resp.text)
                resp.raise_for_status()
            data = resp.json()
            if "access_token" not in data:
                logger.error("Token refresh failed: %s", data)
                raise RuntimeError(f"Token refresh failed: {data}")
            self._access_token = data["access_token"]
            self._expires_at = time.time() + data.get("expires_in", 3600)
            self._save_cached_token()
            logger.info("Token refreshed OK (expires in ~%ds)", data.get("expires_in", 3600))


class ZohoClient:
    def __init__(self, token_manager: ZohoTokenManager):
        self._tm = token_manager
        self._session = requests.Session()

    def _get(self, url: str, params: dict, accept: str = "application/json") -> requests.Response:
        logger.debug("GET %s  params=%s", url, params)

        for attempt in range(MAX_RETRIES):
            headers = {
                "Authorization": f"Zoho-oauthtoken {self._tm.get_token()}",
                "Accept": accept,
            }
            resp = self._session.get(url, params=params, headers=headers, timeout=30)

            if resp.status_code == 401:
                logger.warning("401 — refreshing token and retrying")
                self._tm.force_refresh()
                continue  # retry immediately after token refresh

            if resp.status_code == 429:
                # Honour Retry-After if Zoho sends it, otherwise use backoff
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if attempt < MAX_RETRIES - 1:
                    logger.warning(
                        "429 rate limited — waiting %.0fs before retry %d/%d  [%s]",
                        retry_after, attempt + 1, MAX_RETRIES - 1, url,
                    )
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error("429 — exhausted %d retries for %s", MAX_RETRIES, url)

            return resp

        return resp  # last response after all retries

    def resolve_id(self, credit_note_number: str) -> str:
        logger.info("  [resolve] %s", credit_note_number)
        resp = self._get(
            f"{ZOHO_API_BASE}/creditnotes",
            params={"organization_id": settings.org_id, "creditnote_number": credit_note_number},
        )
        resp.raise_for_status()
        notes = resp.json().get("creditnotes", [])
        if not notes:
            logger.warning("  [resolve] NOT FOUND: %s", credit_note_number)
            raise ValueError(f"Credit note not found: {credit_note_number!r}")
        internal_id = notes[0]["creditnote_id"]
        logger.info("  [resolve] %s → %s", credit_note_number, internal_id)
        return internal_id

    def download_pdf(self, creditnote_id: str) -> bytes:
        logger.info("  [pdf]     downloading id=%s", creditnote_id)
        resp = self._get(
            f"{ZOHO_API_BASE}/creditnotes/{creditnote_id}",
            params={"organization_id": settings.org_id},
            accept="application/pdf",
        )
        resp.raise_for_status()
        size_kb = len(resp.content) / 1024
        logger.info("  [pdf]     id=%s  %.1f KB", creditnote_id, size_kb)
        return resp.content


token_manager = ZohoTokenManager()
zoho = ZohoClient(token_manager)


def _fetch_one(number: str) -> tuple[str, bytes | None, str | None]:
    """Returns (number, pdf_bytes, error_message)."""
    try:
        creditnote_id = zoho.resolve_id(number)
        pdf_bytes = zoho.download_pdf(creditnote_id)
        return number, pdf_bytes, None
    except Exception as exc:
        logger.error("  [FAILED]  %s — %s", number, exc)
        return number, None, str(exc)
