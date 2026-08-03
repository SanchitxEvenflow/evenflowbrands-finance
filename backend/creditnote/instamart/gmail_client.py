import base64
import json
import logging
import threading
import time

import requests

from config import settings

logger = logging.getLogger("instamart_gmail")

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

SUBJECT_MARKER = "GRN & Purchase Return"
ATTACHMENT_PREFIX = "Discrepancy_Note"


def _find_attachment_part(part: dict) -> dict | None:
    filename = part.get("filename")
    if filename and filename.startswith(ATTACHMENT_PREFIX):
        return part
    for sub in part.get("parts", []):
        found = _find_attachment_part(sub)
        if found:
            return found
    return None


class SwiggyGmailClient:
    """Reads Discrepancy Note PDFs out of the Swiggy Instamart procurement mailbox via the
    Gmail API. Auth is a one-time browser consent (SWIGGY_REFRESH_TOKEN in .env) — this
    client only ever does silent refresh-token exchanges after that."""

    def __init__(self):
        self._session = requests.Session()
        self._access_token: str | None = None
        self._expires_at: float = 0
        # FastAPI runs sync routes in a thread pool, so concurrent process-po calls hit this
        # client from multiple threads at once — same guard blinkit_client's token refresh and
        # the root Zoho token manager use, to stop N concurrent expiries from firing N redundant
        # refreshes instead of one.
        self._lock = threading.Lock()

    def _get_token(self) -> str:
        with self._lock:
            if self._access_token and time.time() < self._expires_at:
                return self._access_token
            creds = json.loads(settings.swiggy)["installed"]
            resp = self._session.post(TOKEN_URL, data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": settings.swiggy_refresh_token,
                "grant_type": "refresh_token",
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            self._expires_at = time.time() + data.get("expires_in", 3600) - 60
            return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def find_discrepancy_note_pdf(self, po_number: str) -> tuple[bytes, str]:
        """Searches for a "GRN & Purchase Return" mail mentioning po_number (the two terms
        aren't guaranteed to appear together as one phrase, so they're searched as separate
        quoted terms), takes the newest match (messages.list returns newest first), and
        downloads its Discrepancy_Note* PDF attachment."""
        resp = self._session.get(
            f"{GMAIL_API_BASE}/messages",
            params={"q": f'"{SUBJECT_MARKER}" "{po_number}"', "maxResults": 10},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
        if not messages:
            raise ValueError(f"No mail found for PO {po_number!r}")

        msg_id = messages[0]["id"]
        resp = self._session.get(
            f"{GMAIL_API_BASE}/messages/{msg_id}",
            params={"format": "full"},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()["payload"]

        attachment_part = _find_attachment_part(payload)
        if not attachment_part:
            raise ValueError(f"Mail for PO {po_number!r} has no {ATTACHMENT_PREFIX}* attachment")
        filename = attachment_part["filename"]

        resp = self._session.get(
            f"{GMAIL_API_BASE}/messages/{msg_id}/attachments/{attachment_part['body']['attachmentId']}",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        pdf_bytes = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return pdf_bytes, filename


swiggy_gmail = SwiggyGmailClient()


def demo() -> None:
    """ponytail self-check: token refresh + attachment lookup, mocked session, no live network call."""
    import config as config_module

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    orig_swiggy = config_module.settings.swiggy
    orig_refresh = config_module.settings.swiggy_refresh_token
    config_module.settings.swiggy = json.dumps({"installed": {"client_id": "cid", "client_secret": "secret"}})
    config_module.settings.swiggy_refresh_token = "refresh-tok"

    try:
        client = SwiggyGmailClient()
        calls = {"token": 0, "get": []}

        def fake_post(url, data, timeout):
            calls["token"] += 1
            assert data["refresh_token"] == "refresh-tok"
            return FakeResponse({"access_token": "access-tok", "expires_in": 3600})

        client._session.post = fake_post

        nested_payload = {
            "parts": [
                {"filename": "", "parts": [
                    {"filename": "logo.png"},
                    {"filename": "Discrepancy_Note_FC5-DN772809.pdf", "body": {"attachmentId": "att-1"}},
                ]},
            ],
        }

        def fake_get(url, headers, timeout, params=None):
            calls["get"].append(url)
            assert headers["Authorization"] == "Bearer access-tok"
            if url.endswith("/messages"):
                assert params["q"] == '"GRN & Purchase Return" "FC5PO415849"'
                return FakeResponse({"messages": [{"id": "msg-1"}]})
            if url.endswith("/messages/msg-1"):
                return FakeResponse({"payload": nested_payload})
            if url.endswith("/attachments/att-1"):
                raw = b"%PDF-fake-bytes"
                return FakeResponse({"data": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
            raise AssertionError(f"unexpected GET {url}")

        client._session.get = fake_get

        pdf_bytes, filename = client.find_discrepancy_note_pdf("FC5PO415849")
        assert calls["token"] == 1  # one refresh, then cached for the 3 GETs
        assert filename == "Discrepancy_Note_FC5-DN772809.pdf"
        assert pdf_bytes == b"%PDF-fake-bytes"

        # no messages found → raises rather than silently returning nothing
        client._session.get = lambda url, headers, timeout, params=None: FakeResponse({"messages": []})
        try:
            client.find_discrepancy_note_pdf("NOPE")
            raise AssertionError("expected ValueError for no matching mail")
        except ValueError:
            pass

        # concurrency: N threads racing an expired token must trigger exactly one refresh,
        # not N — this is what _lock in _get_token exists to guarantee.
        race_client = SwiggyGmailClient()
        race_calls = {"token": 0}
        release = threading.Event()

        def slow_post(url, data, timeout):
            race_calls["token"] += 1
            release.wait(timeout=2)  # hold the lock so other threads pile up behind it
            return FakeResponse({"access_token": "race-tok", "expires_in": 3600})

        race_client._session.post = slow_post
        threads = [threading.Thread(target=race_client._get_token) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        release.set()
        for t in threads:
            t.join(timeout=2)
        assert race_calls["token"] == 1, f"expected 1 refresh, got {race_calls['token']}"
    finally:
        config_module.settings.swiggy = orig_swiggy
        config_module.settings.swiggy_refresh_token = orig_refresh

    print("instamart gmail_client demo OK")


if __name__ == "__main__":
    demo()
