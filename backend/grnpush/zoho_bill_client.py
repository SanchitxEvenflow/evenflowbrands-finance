import logging
import time

import requests
from rapidfuzz import process as fuzz_process, fuzz

from config import settings
from zoho_client import MAX_RETRIES, BACKOFF_BASE, MAX_WAIT_SECS, ZOHO_API_BASE, token_manager

logger = logging.getLogger("zoho_bill")

_ORG_STATE_CODE = "29"  # Karnataka
_FUZZY_THRESHOLD = 80


def _word_overlap(a: str, b: str) -> int:
    a_words = {w for w in a.lower().split() if len(w) > 3}
    b_words = {w for w in b.lower().split() if len(w) > 3}
    return len(a_words & b_words)


class ZohoBillClient:
    def __init__(self):
        self._session = requests.Session()
        self._vendor_cache: dict[str, str] = {}   # name_lower → contact_id
        self._vendor_gst: dict[str, str] = {}     # name_lower → gst_no
        self._gst_vendor: dict[str, str] = {}     # gst_no → contact_id
        self._contact_gst: dict[str, str] = {}    # contact_id → gst_no
        self._item_cache: dict[str, dict | None] = {}

    def _get(self, url: str, params: dict) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Zoho-oauthtoken {token_manager.get_token()}"}
            resp = self._session.get(url, params=params, headers=headers, timeout=30)

            if resp.status_code == 401:
                logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting", retry_after)
                    return resp
                if attempt < MAX_RETRIES - 1:
                    logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            return resp

        return resp

    def _post(self, url: str, params: dict, payload: dict) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            headers = {
                "Authorization": f"Zoho-oauthtoken {token_manager.get_token()}",
                "Content-Type": "application/json",
            }
            resp = self._session.post(url, params=params, json=payload, headers=headers, timeout=30)

            if resp.status_code == 401:
                logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting", retry_after)
                    return resp
                if attempt < MAX_RETRIES - 1:
                    logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            return resp

        return resp

    def _post_multipart(self, url: str, params: dict, files: dict) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Zoho-oauthtoken {token_manager.get_token()}"}
            resp = self._session.post(url, params=params, files=files, headers=headers, timeout=30)

            if resp.status_code == 401:
                logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting", retry_after)
                    return resp
                if attempt < MAX_RETRIES - 1:
                    logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            return resp

        return resp

    def _load_all_vendors(self) -> None:
        logger.info("Loading all Zoho vendors into cache...")
        page = 1
        while True:
            resp = self._get(
                f"{ZOHO_API_BASE}/contacts",
                params={"organization_id": settings.org_id, "contact_type": "vendor",
                        "page": page, "per_page": 200},
            )
            data = resp.json() if resp.ok else {}
            for c in data.get("contacts", []):
                name_lower = c["contact_name"].lower()
                contact_id = c["contact_id"]
                gst_no = (c.get("gst_no") or "").strip().upper()
                self._vendor_cache[name_lower] = contact_id
                self._vendor_gst[name_lower] = gst_no
                if gst_no:
                    self._gst_vendor[gst_no] = contact_id
                    self._contact_gst[contact_id] = gst_no
            if not data.get("page_context", {}).get("has_more_page"):
                break
            page += 1
        logger.info("Loaded %d vendors into cache", len(self._vendor_cache))

    def find_vendor_id(
        self, vendor_code: str, vendor_name: str = "", vendor_gst: str = ""
    ) -> tuple[str | None, str, float]:
        """Returns (contact_id, method, score). method is one of gst/exact_name/substring/word_overlap/fuzzy/unmatched."""
        if not self._vendor_cache:
            self._load_all_vendors()

        # 1. GST match (primary — deterministic, unique)
        if vendor_gst:
            gst_key = vendor_gst.strip().upper()
            vendor_id = self._gst_vendor.get(gst_key)
            if vendor_id:
                logger.info("Vendor %r matched by GST %r → %s", vendor_name or vendor_code, gst_key, vendor_id)
                return vendor_id, "gst", 100.0

        key = vendor_name.lower()
        vendor_id = None
        method = "unmatched"
        score = 0.0

        # 2. exact name match
        vendor_id = self._vendor_cache.get(key)
        if vendor_id:
            method, score = "exact_name", 100.0

        # 3. substring match
        if not vendor_id and vendor_name:
            vendor_id = next(
                (cid for name, cid in self._vendor_cache.items() if key in name or name in key),
                None,
            )
            if vendor_id:
                method, score = "substring", 90.0

        # 4. word-overlap match (handles "PVT.LTD" vs "PRIVATE LIMITED")
        if not vendor_id and vendor_name:
            best_name = max(
                self._vendor_cache.keys(),
                key=lambda name: _word_overlap(key, name),
                default=None,
            )
            if best_name and _word_overlap(key, best_name) >= 2:
                vendor_id = self._vendor_cache[best_name]
                method, score = "word_overlap", 85.0
                logger.info("Vendor %r matched by word-overlap → %r", vendor_name, best_name)

        # 5. fuzzy match (rapidfuzz — catches typos / abbreviations)
        if not vendor_id and vendor_name:
            result = fuzz_process.extractOne(
                key,
                self._vendor_cache.keys(),
                scorer=fuzz.WRatio,
                score_cutoff=_FUZZY_THRESHOLD,
            )
            if result:
                best_name, fuzzy_score, _ = result
                vendor_id = self._vendor_cache[best_name]
                method, score = "fuzzy", fuzzy_score
                logger.info("Vendor %r matched by fuzzy (score=%d) → %r", vendor_name, fuzzy_score, best_name)

        if vendor_id:
            logger.info("Vendor %r → %s (method=%s)", vendor_name or vendor_code, vendor_id, method)
        else:
            logger.warning("Vendor not found in Zoho: code=%r name=%r gst=%r", vendor_code, vendor_name, vendor_gst)
        return vendor_id, method, score

    def is_interstate_vendor(self, vendor_id: str) -> bool:
        if not self._vendor_cache:
            self._load_all_vendors()
        gst = self._contact_gst.get(vendor_id, "")
        if not gst:
            return True  # no GST on file → assume interstate (safer default)
        return not gst.startswith(_ORG_STATE_CODE)

    def find_item_metadata(self, sku: str) -> dict | None:
        if sku in self._item_cache:
            return self._item_cache[sku]

        resp = self._get(
            f"{ZOHO_API_BASE}/items",
            params={"organization_id": settings.org_id, "search_text": sku},
        )
        items = resp.json().get("items", []) if resp.ok else []
        match = next((i for i in items if i.get("sku") == sku), items[0] if items else None)

        if match:
            tax_prefs = match.get("item_tax_preferences") or []
            intra_tax = next((p["tax_id"] for p in tax_prefs if p.get("tax_specification") == "intra"), None)
            inter_tax = next((p["tax_id"] for p in tax_prefs if p.get("tax_specification") == "inter"), None)
            meta = {
                "item_id": match.get("item_id"),
                "account_id": match.get("purchase_account_id") or match.get("account_id"),
                "intra_tax_id": intra_tax,
                "inter_tax_id": inter_tax,
            }
            logger.info("Item SKU %r → item_id=%s intra_tax=%s inter_tax=%s",
                        sku, meta["item_id"], intra_tax, inter_tax)
        else:
            meta = None
            logger.warning("Item SKU %r not found in Zoho", sku)

        self._item_cache[sku] = meta
        return meta

    def list_bills(self, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """Return all bills from Zoho with bill_id, bill_number, and cf_grn parsed out."""
        results = []
        page = 1
        while True:
            params: dict = {"organization_id": settings.org_id, "page": page, "per_page": 200}
            if date_from:
                params["date_start"] = date_from
            if date_to:
                params["date_end"] = date_to
            resp = self._get(f"{ZOHO_API_BASE}/bills", params=params)
            data = resp.json() if resp.ok else {}
            for bill in data.get("bills", []):
                cf_grn = ""
                for cf in bill.get("custom_fields") or []:
                    if cf.get("api_name") == "cf_grn":
                        cf_grn = cf.get("value") or ""
                        break
                results.append({
                    "bill_id": bill.get("bill_id", ""),
                    "bill_number": bill.get("bill_number", ""),
                    "cf_grn": cf_grn,
                    "date": bill.get("date", ""),
                })
            if not data.get("page_context", {}).get("has_more_page"):
                break
            page += 1
        logger.info("Fetched %d bill(s) from Zoho", len(results))
        return results

    def find_bill(self, bill_number: str, vendor_id: str | None = None) -> dict | None:
        """Return {'bill_id': ..., 'date': ...} if the bill exists in Zoho, else None.

        Optionally filters by vendor_id to avoid matching a same-numbered bill from a different vendor.
        """
        resp = self._get(
            f"{ZOHO_API_BASE}/bills",
            params={"organization_id": settings.org_id, "bill_number": bill_number},
        )
        bills = resp.json().get("bills", []) if resp.ok else []
        match = next(
            (b for b in bills
             if b.get("bill_number") == bill_number
             and (vendor_id is None or b.get("vendor_id") == vendor_id)),
            None,
        )
        if match:
            return {"bill_id": match["bill_id"], "date": match.get("date", "")}
        return None

    def bill_exists(self, bill_number: str) -> bool:
        return self.find_bill(bill_number) is not None

    def create_draft_bill(self, payload: dict) -> dict:
        grn_code = payload.get("bill_number", "?")
        logger.info("Creating draft bill for GRN %s", grn_code)
        resp = self._post(
            f"{ZOHO_API_BASE}/bills",
            params={"organization_id": settings.org_id},
            payload=payload,
        )
        data = resp.json()
        if not resp.ok or data.get("code", 0) != 0:
            raise RuntimeError(f"Zoho bill creation failed [{resp.status_code}]: {data}")
        bill_id = data["bill"]["bill_id"]
        logger.info("Draft bill created: %s → bill_id=%s", grn_code, bill_id)
        return data["bill"]

    def upload_bill_attachment(self, bill_id: str, filename: str, content: bytes, content_type: str) -> None:
        """Attach a file (e.g. vendor's invoice PDF) to an existing Bill."""
        resp = self._post_multipart(
            f"{ZOHO_API_BASE}/bills/{bill_id}/attachment",
            params={"organization_id": settings.org_id},
            files={"attachment": (filename, content, content_type)},
        )
        if not resp.ok:
            raise RuntimeError(f"Zoho attachment upload failed [{resp.status_code}]: {resp.text}")


zoho_bill = ZohoBillClient()
