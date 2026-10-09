"""Zoho Books netting (net adjustments): offset a linked customer's invoices against
the same party's vendor bills. Endpoints mirror what the Books web UI calls (netting.har)."""
import json
import logging
import time

import requests

from config import settings
from zoho_client import BACKOFF_BASE, MAX_RETRIES, MAX_WAIT_SECS, ZOHO_API_BASE, token_manager

logger = logging.getLogger("zoho_netting")


class ZohoNettingClient:
    def __init__(self):
        self._session = requests.Session()

    def _request(self, method: str, path: str, params: dict | None = None, data: dict | None = None) -> dict:
        params = {**(params or {}), "organization_id": settings.org_id}
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Zoho-oauthtoken {token_manager.get_token()}"}
            resp = self._session.request(method, f"{ZOHO_API_BASE}{path}", params=params, data=data, headers=headers, timeout=30)
            if resp.status_code == 401:
                logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS or attempt == MAX_RETRIES - 1:
                    break
                logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue
            break
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"message": resp.text}
        if not resp.ok or body.get("code", 0) != 0:
            raise RuntimeError(f"Zoho {method} {path} failed [{resp.status_code}]: {body.get('message')}")
        return body

    # ── read ──────────────────────────────────────────────────────────────
    def list_nettings(self, page: int = 1, per_page: int = 100) -> dict:
        return self._request("GET", "/netadjustments", {
            "page": page, "per_page": per_page, "sort_column": "created_time", "sort_order": "D",
        })

    def get_netting(self, net_adjustment_id: str) -> dict:
        return self._request("GET", f"/netadjustments/{net_adjustment_id}")["net_adjustment"]

    def get_journals(self, net_adjustment_id: str) -> dict:
        return self._request("GET", f"/transactions/{net_adjustment_id}/journals", {
            "entity_type": "net_adjustment", "report_basis": "accrual", "per_page": 25, "page": 1,
        })

    def get_editpage(self, customer_id: str) -> dict:
        """Linked vendor, AR/AP outstanding, nettable amount, numbering groups, AR/AP accounts."""
        return self._request("GET", "/netadjustments/editpage/fromcontacts", {"customer_id": customer_id})

    def unpaid_invoices(self, customer_id: str, currency_id: str, date_start: str | None = None,
                        date_end: str | None = None, date_less_equals: str | None = None) -> list[dict]:
        params = {"customer_id": customer_id, "currency_id": currency_id, "status": "unpaid"}
        params.update({k: v for k, v in {"date_start": date_start, "date_end": date_end,
                                         "date_less_equals": date_less_equals}.items() if v})
        return self._paged("/invoices/frompayments", params, "invoices")

    def unpaid_bills(self, vendor_id: str, currency_id: str, date_start: str | None = None,
                     date_end: str | None = None, date_less_equals: str | None = None) -> list[dict]:
        params = {"vendor_id": vendor_id, "currency_id": currency_id, "status": "unpaid"}
        params.update({k: v for k, v in {"date_start": date_start, "date_end": date_end,
                                         "date_less_equals": date_less_equals}.items() if v})
        return self._paged("/bills/frompayments", params, "bills")

    def _paged(self, path: str, params: dict, key: str) -> list[dict]:
        out, page = [], 1
        while True:
            body = self._request("GET", path, {**params, "page": page, "per_page": 200})
            out.extend(body.get(key, []))
            if not body.get("page_context", {}).get("has_more_page"):
                return out
            page += 1

    # ── write ─────────────────────────────────────────────────────────────
    def create_netting(self, payload: dict) -> dict:
        # Books UI sends form-encoded JSONString, same as the public API accepts.
        body = self._request("POST", "/netadjustments", data={"JSONString": json.dumps(payload)})
        logger.info("Netting created: %s", body["net_adjustment"].get("net_adjustment_number"))
        return body["net_adjustment"]

    def delete_netting(self, net_adjustment_id: str) -> str:
        # ponytail: DELETE not in netting.har — assumed from Books REST convention, verify on first use.
        return self._request("DELETE", f"/netadjustments/{net_adjustment_id}").get("message", "")


zoho_netting = ZohoNettingClient()
