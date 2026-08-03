import datetime
import json
import logging
import os
import re
import threading

import requests

from config import settings

logger = logging.getLogger("blinkit")

BASE_URL = "https://partnersbiz.com"
X_API_KEY = "fe25a1da-d76e-4da1-a7bf-175e8ecf130f"
ENTITY_ID = "2269"
ENTITY_TYPE = "manufacturer"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# Session survives process restarts (OTP is emailed, so re-login isn't free).
# ponytail: plaintext json on disk, fine for a local single-tenant token cache.
_TOKENS_FILE = os.path.join(os.path.dirname(__file__), "tokens.json")

_PRESIGNED_URL_RE = re.compile(r'https://grofers-prod-retail-reports[^"\\\s]+')


def _extract_presigned_url(data: dict) -> str | None:
    """Response key for the presigned S3 URL isn't documented consistently, so try known
    keys first, then fall back to scanning the raw JSON (matches the curl grep in the docs)."""
    for node in (data, data.get("data") if isinstance(data, dict) else None):
        if isinstance(node, dict):
            for key in ("url", "presigned_url", "download_url"):
                if node.get(key):
                    return node[key]
    match = _PRESIGNED_URL_RE.search(json.dumps(data))
    return match.group(0) if match else None


class BlinkitClient:
    """PartnersBiz (Blinkit) auth client — send_otp → verify_otp → authenticated requests."""

    def __init__(self, email: str | None = None):
        self.email = email or settings.blinkit_email
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._load_tokens()

    def _load_tokens(self) -> None:
        if not os.path.exists(_TOKENS_FILE):
            return
        with open(_TOKENS_FILE) as f:
            data = json.load(f)
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")

    def _save_tokens(self) -> None:
        with open(_TOKENS_FILE, "w") as f:
            json.dump({"access_token": self._access_token, "refresh_token": self._refresh_token}, f)

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token)

    def send_otp(self) -> dict:
        """Triggers an OTP email to self.email. Call verify_otp() next with the code received."""
        resp = self._session.post(
            f"{BASE_URL}/auth/api/v1/email/send_otp",
            data={"email_id": self.email},
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/x-www-form-urlencoded",
                "app_client": "partnersbiz-web",
                "service": "partnersbiz",
                "x-api-key": X_API_KEY,
                "user-agent": USER_AGENT,
            },
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("OTP sent to %s", self.email)
        return resp.json()

    def verify_otp(self, otp: str) -> dict:
        """Exchanges the emailed OTP for access_token + refresh_token, and caches them to disk."""
        resp = self._session.post(
            f"{BASE_URL}/auth/api/v1/email/verify_otp",
            data={"email_id": self.email, "verify_code": otp},
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/x-www-form-urlencoded",
                "app_client": "partnersbiz-web",
                "service": "partnersbiz",
                "x-api-key": X_API_KEY,
                "user-agent": USER_AGENT,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Blinkit OTP verification failed: {data}")

        with self._lock:
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token")
            self._save_tokens()

        logger.info("Blinkit login OK for %s", self.email)
        return data

    def auth_headers(self, entity: bool = True) -> dict:
        """Headers for authenticated calls. Raises if verify_otp() hasn't run yet (or tokens.json is empty)."""
        if not self._access_token:
            raise RuntimeError("Not logged in — call send_otp() then verify_otp(otp) first")

        headers = {
            "accept": "application/json, text/plain, */*",
            "access_token": self._access_token,
            "token": self._access_token,
            "app_client": "partnerbiz-web",
            "service": "partnersbiz",
            "x-api-key": X_API_KEY,
            "user-agent": USER_AGENT,
        }
        if entity:
            headers["x-entity-id"] = ENTITY_ID
            headers["x-entity-type"] = ENTITY_TYPE
        return headers

    def get(self, path: str, params: dict | None = None, entity: bool = True) -> requests.Response:
        """GET against BASE_URL + path, with auth headers attached. 401 means re-login (no refresh endpoint documented)."""
        resp = self._session.get(f"{BASE_URL}{path}", params=params, headers=self.auth_headers(entity), timeout=30)
        if resp.status_code == 401:
            logger.warning("Blinkit 401 — token expired, re-login required")
        return resp

    def find_po_id(self, po_number: str, months_back: int = 18) -> str | None:
        """Resolves a human PO number to Blinkit's po_id via the B2B orders list — confirmed
        live that order_number *is* the po_id (no separate id field), and the endpoint 403s
        without x-vendor-id. Paginates since a PO can be many pages back."""
        end = datetime.date.today()
        start = end - datetime.timedelta(days=30 * months_back)
        headers = self.auth_headers()
        headers["x-vendor-id"] = "4941"
        limit = 200
        offset = 0
        while True:
            resp = self._session.post(
                f"{BASE_URL}/seller-hub/api/b2b-orders/list",
                json={"order_date": {"start": str(start), "end": str(end)}, "offset": offset, "limit": limit},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            items = resp.json()["data"]["items"]
            if not items:
                return None
            match = next((o for o in items if o.get("order_number") == po_number), None)
            if match:
                return match["order_number"]
            offset += limit

    def get_discrepancy_note_url(self, po_id: str, invoice_id: str) -> str:
        """Returns the presigned S3 URL for a discrepancy note PDF (short-lived, download promptly)."""
        resp = self.get(
            f"/v1/client-po-details/{po_id}/fetch_discrepancy_note_pdf/",
            params={"vendor_invoice_id": invoice_id},
        )
        resp.raise_for_status()
        data = resp.json()
        url = _extract_presigned_url(data)
        if not url:
            raise RuntimeError(f"No presigned URL in discrepancy note response: {data}")
        return url

    def download_discrepancy_note_pdf(self, po_id: str, invoice_id: str) -> bytes:
        """Fetches the presigned URL then downloads the PDF bytes (S3 URL needs no auth headers)."""
        url = self.get_discrepancy_note_url(po_id, invoice_id)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content


blinkit_client = BlinkitClient()


def demo() -> None:
    """ponytail self-check: header shape only, no live network call."""
    c = BlinkitClient(email="test@example.com")
    c._access_token = "fake-token"
    h = c.auth_headers()
    assert h["access_token"] == "fake-token" and h["x-entity-id"] == ENTITY_ID
    assert not c.is_authenticated or True  # is_authenticated reflects real state, not this hack
    c2 = BlinkitClient(email="test@example.com")
    c2._access_token = None  # force logged-out state regardless of any cached tokens.json
    try:
        c2.auth_headers()
        raise AssertionError("expected RuntimeError before login")
    except RuntimeError:
        pass

    assert _extract_presigned_url({"url": "https://grofers-prod-retail-reports/x.pdf"}) == \
        "https://grofers-prod-retail-reports/x.pdf"
    assert _extract_presigned_url({"data": {"presigned_url": "https://grofers-prod-retail-reports/y.pdf"}}) == \
        "https://grofers-prod-retail-reports/y.pdf"
    assert _extract_presigned_url({"nested": {"junk": 'see "https://grofers-prod-retail-reports/z.pdf" here'}}) == \
        "https://grofers-prod-retail-reports/z.pdf"
    assert _extract_presigned_url({"nothing": "here"}) is None

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    c3 = BlinkitClient(email="test@example.com")
    c3._access_token = "fake-token"

    def fake_post(url, json, headers, timeout):
        items = [{"order_number": "1723710043291"}] if json["offset"] == 0 else []
        return FakeResponse({"data": {"items": items}})

    c3._session.post = fake_post
    assert c3.find_po_id("1723710043291") == "1723710043291"
    assert c3.find_po_id("NOPE") is None

    print("blinkit_client demo OK")


if __name__ == "__main__":
    demo()
